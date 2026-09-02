from __future__ import annotations

import asyncio
from datetime import datetime, time as dt_time, timedelta
from typing import Optional

import pandas as pd

from .data_provider import daily_close_confirmed, fetch_realtime_quotes, safe_fetch_kline
from .decision_engine import build_decision_plan, format_decision_plan
from .kdj import calculate_kdj
from .logger import app_logger
from .notifier import notify, notify_price_target, notify_reverse_t
from .performance_store import backfill_snapshots, get_performance
from .runtime_state import mark_task_channel, task_channel_complete, task_complete
from .shadow_tracker import record_and_evaluate
from .stage_research import load_stage_report, refresh_stage_report
from .state import state
from .strategy import check_kdj_signal
from .trade_ledger import replay_position
from .trading_calendar import is_session_date, next_session

# 可选：LLM生成交易建议
try:
    from .llm_advisor import generate_trading_advice, health_check
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False

# A股交易时段（盘中才拉取行情；午间休市也暂停）
TRADING_SESSIONS = [
    (dt_time(9, 30), dt_time(11, 30)),
    (dt_time(13, 0), dt_time(15, 0)),
]

# 收盘前多抓一轮，确保最后一根K线（15:00）数据入库
CLOSE_GRACE_SECONDS = 90

# 收盘总结防重复：记录已发送总结的日期
_close_summary_sent_date: Optional[str] = None

# 次日操作指引防重复：记录已发送指引的日期
_next_day_plan_sent_date: Optional[str] = None


def is_trading_time(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if not is_session_date(now, state.config):
        return False
    t = now.time()
    for start, end in TRADING_SESSIONS:
        if start <= t <= end:
            return True
        # 收盘后的宽限期（仅下午场）
        if end == dt_time(15, 0):
            close_grace = (now.replace(hour=15, minute=0, second=0, microsecond=0).timestamp()
                           + CLOSE_GRACE_SECONDS)
            if t >= end and now.timestamp() <= close_grace:
                return True
    return False


def _daily_estimate_from_intraday(daily_data: pd.DataFrame, intraday_data: pd.DataFrame) -> Optional[pd.DataFrame]:
    """用当日分钟线折算一根盘中日线，返回用于计算日线KDJ的数据。"""
    if daily_data.empty or intraday_data.empty or "datetime" not in intraday_data.columns:
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    today_intraday = intraday_data[intraday_data["datetime"].astype(str).str.startswith(today)].copy()
    if today_intraday.empty:
        return None

    daily = daily_data.copy()
    if "date" not in daily.columns:
        return None
    daily["date"] = daily["date"].astype(str).str[:10]
    daily = daily[daily["date"] != today].copy()

    today_bar = {
        "date": today,
        "open": float(today_intraday.iloc[0]["open"]),
        "high": float(today_intraday["high"].max()),
        "low": float(today_intraday["low"].min()),
        "close": float(today_intraday.iloc[-1]["close"]),
    }
    if "volume" in today_intraday.columns:
        today_bar["volume"] = float(today_intraday["volume"].sum())

    return pd.concat([daily, pd.DataFrame([today_bar])], ignore_index=True)


def _latest_view(symbol: dict, timeframe: str, latest: dict, estimated: bool = False,
                 thresholds: Optional[dict] = None) -> dict:
    view = {
        "symbol": symbol["code"],
        "name": symbol.get("name") or symbol["code"],
        "timeframe": timeframe,
        "close": round(float(latest["close"]), 4),
        "k": round(float(latest["k"]), 2),
        "d": round(float(latest["d"]), 2),
        "j": round(float(latest["j"]), 2),
        "timestamp": str(latest.get("datetime") or latest.get("date") or ""),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "estimated": estimated,
    }
    if thresholds:
        view["best_thresholds"] = thresholds
    return view


def _best_thresholds(symbol_code: str, kdj_config: dict) -> dict:
    """读取单只股票的最优 KDJ 阈值；没有寻优结果时回退全局阈值。"""
    try:
        from .optimizer import get_best
        best = get_best(symbol_code)
    except Exception as exc:
        app_logger.warning("load best thresholds failed: symbol=%s error=%s", symbol_code, exc)
        best = None

    if not best:
        return {
            "buy": float(kdj_config.get("lower", 20)),
            "sell": float(kdj_config.get("upper", 80)),
            "auto": False,
        }
    return {
        "buy": float(best.get("buy", kdj_config.get("lower", 20))),
        "sell": float(best.get("sell", kdj_config.get("upper", 80))),
        "auto": True,
        "optimized_at": best.get("optimized_at"),
        "total_return": best.get("total_return"),
        "max_drawdown": best.get("max_drawdown"),
        "round_trips": best.get("round_trips"),
    }


def run_once(*, skip_alerts: bool = False) -> None:
    config = state.config
    kdj_config = config.get("kdj", {})
    cooldown_seconds = int(config.get("alert", {}).get("cooldown_seconds", 600))

    # 到价提醒使用独立实时快照；获取失败或时间戳不新鲜时严格不发送。
    if not skip_alerts:
        _maybe_send_price_target_alerts(config, cooldown_seconds)

    for symbol in list(state.symbols):
        thresholds = _best_thresholds(symbol["code"], kdj_config)
        daily_raw = None
        intraday_for_estimate = None
        for timeframe in config.get("timeframes", []):
            data = safe_fetch_kline(symbol["code"], timeframe)
            if data is None or data.empty:
                continue
            if timeframe == "1d":
                daily_raw = data.copy()
            elif timeframe == "10m":
                intraday_for_estimate = data.copy()

            kdj_data = calculate_kdj(
                data,
                n=int(kdj_config.get("n", 9)),
                m1=int(kdj_config.get("m1", 3)),
                m2=int(kdj_config.get("m2", 3)),
            )
            latest = kdj_data.iloc[-1].to_dict()
            display_data = kdj_data
            if timeframe != "1d":
                today = datetime.now().strftime("%Y-%m-%d")
                time_column = "datetime" if "datetime" in kdj_data.columns else "date"
                current_day = kdj_data[kdj_data[time_column].astype(str).str.startswith(today)].copy()
                if not current_day.empty:
                    display_data = current_day
            series = []
            for row in display_data.tail(120).to_dict("records"):
                series.append(
                    {
                        "timestamp": str(row.get("datetime") or row.get("date") or ""),
                        "open": round(float(row["open"]), 4),
                        "high": round(float(row["high"]), 4),
                        "low": round(float(row["low"]), 4),
                        "close": round(float(row["close"]), 4),
                        "k": round(float(row["k"]), 2),
                        "d": round(float(row["d"]), 2),
                        "j": round(float(row["j"]), 2),
                    }
                )
            state.update_series(symbol["code"], timeframe, series)
            latest_view = _latest_view(symbol, timeframe, latest, thresholds=thresholds)
            state.update_latest(symbol["code"], timeframe, latest_view)
            app_logger.info(
                "latest kdj: %s %s close=%s k=%.2f d=%.2f j=%.2f",
                symbol["code"],
                timeframe,
                latest_view["close"],
                latest_view["k"],
                latest_view["d"],
                latest_view["j"],
            )

            # 原始周期（1d/10m 等）只用于页面展示和生成盘中折算日线；
            # 微信/邮件提醒统一只发送下方的 1d_est，避免发送无交易意义的 10m 信号。
            continue

        estimated_daily = None
        if daily_raw is not None:
            positions = ((config.get("trade_plan") or {}).get("positions") or {})
            position = _position_for_code(positions, symbol["code"])
            if position.get("strategy_budget"):
                try:
                    daily_with_kdj = calculate_kdj(
                        daily_raw,
                        n=int(kdj_config.get("n", 9)),
                        m1=int(kdj_config.get("m1", 3)),
                        m2=int(kdj_config.get("m2", 3)),
                    )
                    snapshot_bars = [
                        {
                            "timestamp": str(row.get("date") or row.get("datetime") or ""),
                            "close": float(row["close"]),
                        }
                        for row in daily_with_kdj.tail(120).to_dict("records")
                    ]
                    performance_summary = backfill_snapshots(
                        symbol["code"],
                        snapshot_bars,
                        position,
                        exclude_dates=(() if daily_close_confirmed() else (datetime.now().strftime("%Y-%m-%d"),)),
                    )
                    if position.get("shadow_tracking_enabled"):
                        formal_latest = (state.latest.get(symbol["code"]) or {}).get("1d")
                        formal_series = (state.series.get(symbol["code"]) or {}).get("1d") or []
                        if formal_latest and formal_series:
                            plan = build_decision_plan(
                                symbol_code=symbol["code"],
                                symbol_name=str(symbol.get("name") or symbol["code"]),
                                latest_daily=formal_latest,
                                daily_series=formal_series,
                                position=position,
                                decision_date=datetime.now().strftime("%Y-%m-%d"),
                                performance_state=performance_summary,
                            )
                            record_and_evaluate(
                                plan,
                                formal_series,
                                horizons=position.get("shadow_horizons") or (5, 10, 20, 30, 60),
                            )
                            stage_report = load_stage_report(symbol["code"])
                            if not stage_report or stage_report.get("source_signal_date") != plan.get("signal_date"):
                                refresh_stage_report(symbol["code"], position)
                except Exception as exc:
                    app_logger.warning("strategy performance/shadow snapshot failed: %s %s", symbol["code"], exc)
        if daily_raw is not None and intraday_for_estimate is not None:
            estimated_daily = _daily_estimate_from_intraday(daily_raw, intraday_for_estimate)
        if not skip_alerts:
            _maybe_send_reverse_t_alert(symbol, config, cooldown_seconds)
        if estimated_daily is not None and not estimated_daily.empty:
            estimated_kdj = calculate_kdj(
                estimated_daily,
                n=int(kdj_config.get("n", 9)),
                m1=int(kdj_config.get("m1", 3)),
                m2=int(kdj_config.get("m2", 3)),
            )
            latest_estimated = estimated_kdj.iloc[-1].to_dict()
            estimated_view = _latest_view(symbol, "1d_est", latest_estimated, estimated=True, thresholds=thresholds)
            estimated_view["source_timeframe"] = "10m"
            estimated_view["note"] = "盘中用10分钟线折算的今日临时日线，非收盘确认"
            state.update_latest(symbol["code"], "1d_est", estimated_view)
            app_logger.info(
                "estimated daily kdj from 10m: %s close=%s k=%.2f d=%.2f j=%.2f",
                symbol["code"],
                estimated_view["close"],
                estimated_view["k"],
                estimated_view["d"],
                estimated_view["j"],
            )

            # 非交易时间的初始填充只拉数据，不发告警
            if skip_alerts:
                continue

            signal_key = f"{symbol['code']}:1d_est"
            signal = check_kdj_signal(
                symbol,
                "1d_est",
                latest_estimated,
                upper=thresholds["sell"],
                lower=thresholds["buy"],
            )
            if not signal:
                state.clear_alert_zone(signal_key)
                continue

            if not state.should_alert(signal_key, signal.direction, cooldown_seconds):
                continue

            alert = {
                **signal.__dict__,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "email_sent": False,
                "estimated": True,
                "source_timeframe": "10m",
                "best_thresholds": thresholds,
                "note": (
                    f"盘中用10分钟线折算的日线KDJ，按该股票最优阈值触发："
                    f"K<{thresholds['buy']:g} 买入预警 / K>{thresholds['sell']:g} 卖出预警"
                ),
            }
            notify(config, alert)
            state.add_alert(alert)


def _quote_time(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    for pattern in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _quote_is_fresh(quote: dict, now: datetime, max_age_seconds: int) -> bool:
    timestamp = _quote_time(quote.get("timestamp"))
    if timestamp is None or timestamp.date() != now.date():
        return False
    age = now - timestamp
    return timedelta(seconds=-60) <= age <= timedelta(seconds=max_age_seconds)


def _configured_position_lots(config: dict, code: str, day: str) -> Optional[float]:
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    position = _position_for_code(positions, code)
    if not position:
        return 0.0
    try:
        return float(replay_position(position, as_of=day).get("total_lots", 0) or 0)
    except Exception as exc:
        app_logger.warning("price target position replay failed: symbol=%s error=%s", code, exc)
        return None


def _maybe_send_price_target_alerts(
    config: dict,
    cooldown_seconds: int,
    *,
    now: Optional[datetime] = None,
) -> None:
    rules = config.get("price_alerts") or {}
    enabled_rules = {
        str(code): rule for code, rule in rules.items()
        if isinstance(rule, dict) and bool(rule.get("enabled", True))
    }
    if not enabled_rules:
        return
    try:
        quotes = fetch_realtime_quotes(enabled_rules)
    except Exception as exc:
        app_logger.warning("real-time price targets unavailable; no alert sent: %s", exc)
        return

    current = now or datetime.now()
    for code, rule in enabled_rules.items():
        quote = quotes.get(code)
        max_age_seconds = int(rule.get("max_quote_age_seconds", 180) or 180)
        if not quote or not _quote_is_fresh(quote, current, max_age_seconds):
            app_logger.warning("stale/missing price target quote; no alert sent: symbol=%s", code)
            continue

        target = float(rule.get("target_price", 0) or 0)
        if target <= 0:
            continue
        tolerance_ratio = max(0.0, float(rule.get("tolerance_ratio", 0.005) or 0))
        reset_ratio = max(tolerance_ratio, float(rule.get("reset_ratio", 0.015) or 0))
        trigger_price = target * (1 + tolerance_ratio)
        reset_price = target * (1 + reset_ratio)
        latest_price = float(quote["price"])
        signal_key = f"{code}:price_target:buy:{target:.4f}"

        # Hysteresis: entering <= trigger sends once; only >= reset re-arms it.
        if latest_price >= reset_price:
            state.clear_alert_zone(signal_key)
            continue
        if latest_price > trigger_price:
            continue
        if bool(rule.get("only_when_flat", True)):
            held_lots = _configured_position_lots(config, code, current.strftime("%Y-%m-%d"))
            if held_lots is None or held_lots > 0:
                continue
        if not state.should_alert(signal_key, "entered", cooldown_seconds):
            continue

        lots = max(1, int(rule.get("lots", 1) or 1))
        fee_per_lot = float(rule.get("fee_per_lot", 5) or 5)
        alert = {
            "type": "price_target",
            "symbol": code,
            "name": str(rule.get("name") or quote.get("name") or code),
            "timeframe": "实时到价",
            "direction": "buy_target",
            "close": latest_price,
            "target_price": target,
            "trigger_price": round(trigger_price, 4),
            "lots": lots,
            "estimated_cash": round(latest_price * 100 * lots + fee_per_lot * lots, 2),
            "change_ratio": quote.get("change_ratio"),
            "timestamp": str(quote.get("timestamp") or ""),
            "created_at": current.strftime("%Y-%m-%d %H:%M:%S"),
            "source": quote.get("source"),
            "reason": str(rule.get("reason") or "目标价附近首次试仓"),
            "risk_note": str(rule.get("risk_note") or "到价不等于止跌，单次仅1手"),
            "email_sent": False,
        }
        notify_price_target(config, alert)
        state.add_alert(alert)


def _position_for_code(positions: dict, code: str) -> dict:
    position_key = next((key for key in positions if str(key) == str(code)), code)
    return positions.get(position_key, {}) or {}


def _has_position_for_code(positions: dict, code: str) -> bool:
    return any(str(key) == str(code) for key in positions)


def _maybe_send_reverse_t_alert(symbol: dict, config: dict, cooldown_seconds: int) -> None:
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    position = _position_for_code(positions, symbol["code"])
    if not (position.get("reverse_t") or {}).get("enabled"):
        return
    plan = _build_deterministic_plan(symbol, config, datetime.now().strftime("%Y-%m-%d"))
    if plan is None:
        return
    reverse_t = plan.get("reverse_t") or {}
    decision = reverse_t.get("decision") or {}
    action = str(decision.get("action") or "hold")
    signal_key = f"{symbol['code']}:reverse_t"
    executable = decision.get("status") == "executable" and action in {
        "sell_core_for_reverse_t", "buyback_core", "protective_buyback",
    }
    if not executable:
        state.clear_alert_zone(signal_key)
        return
    if not state.should_alert(signal_key, action, cooldown_seconds):
        return
    signal = reverse_t.get("signal") or {}
    latest = ((state.latest.get(symbol["code"]) or {}).get("10m")) or {}
    alert = {
        "symbol": symbol["code"],
        "name": symbol.get("name") or symbol["code"],
        "timeframe": "10m反T",
        "direction": "high" if action == "sell_core_for_reverse_t" else "low",
        "k": float(signal.get("k") if signal.get("k") is not None else latest.get("k", 0) or 0),
        "d": float(latest.get("d", 0) or 0),
        "j": float(latest.get("j", 0) or 0),
        "close": float(signal.get("close") if signal.get("close") is not None else latest.get("close", 0) or 0),
        "timestamp": str(signal.get("intraday_date") or latest.get("timestamp") or ""),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "email_sent": False,
        "reverse_t": {
            "decision": decision,
            "price_plan": reverse_t.get("price_plan"),
            "rule": reverse_t.get("rule"),
            "quota_lots": reverse_t.get("quota_lots"),
            "core_floor_lots": reverse_t.get("core_floor_lots"),
        },
    }
    notify_reverse_t(config, symbol, plan, alert)
    state.add_alert(alert)


def _refresh_formal_daily_for_plan(today_str: str) -> bool:
    """Refresh positioned symbols and require today's completed daily bar.

    A post-close next-day plan must never be generated from the previous
    session's bar.  Returning False leaves the persisted task incomplete so
    the monitor loop retries on its next pass.
    """
    config = state.config
    kdj_config = config.get("kdj", {})
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    all_ready = True

    for symbol in list(state.symbols):
        code = symbol["code"]
        if not _has_position_for_code(positions, code):
            continue

        data = safe_fetch_kline(code, "1d")
        if data is None or data.empty:
            app_logger.warning("next-day plan waiting: %s formal daily is unavailable", code)
            all_ready = False
            continue

        kdj_data = calculate_kdj(
            data,
            n=int(kdj_config.get("n", 9)),
            m1=int(kdj_config.get("m1", 3)),
            m2=int(kdj_config.get("m2", 3)),
        )
        latest = kdj_data.iloc[-1].to_dict()
        signal_date = str(latest.get("date") or latest.get("datetime") or "")[:10]
        if signal_date != today_str:
            app_logger.warning(
                "next-day plan waiting: %s formal daily date=%s expected=%s",
                code,
                signal_date or "missing",
                today_str,
            )
            all_ready = False
            continue

        thresholds = _best_thresholds(code, kdj_config)
        series = [
            {
                "timestamp": str(row.get("date") or row.get("datetime") or ""),
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
                "k": round(float(row["k"]), 2),
                "d": round(float(row["d"]), 2),
                "j": round(float(row["j"]), 2),
            }
            for row in kdj_data.tail(120).to_dict("records")
        ]
        state.update_series(code, "1d", series)
        latest_view = _latest_view(symbol, "1d", latest, thresholds=thresholds)
        latest_view["data_source"] = str(data.attrs.get("data_source") or "unknown_daily")
        state.update_latest(code, "1d", latest_view)

        position = _position_for_code(positions, code)
        if position.get("strategy_budget"):
            snapshot_bars = [
                {"timestamp": item["timestamp"], "close": item["close"]}
                for item in series
            ]
            backfill_snapshots(code, snapshot_bars, position)

    return all_ready


def _deliver_persisted(task_name: str, day: str, subject: str, content: str, config: dict) -> bool:
    """Deliver each configured channel at most once across process restarts."""
    from .notifier import send_email, send_pushplus

    channels = [str(channel) for channel in config.get("alert", {}).get("channels", [])]
    senders = {"email": send_email, "pushplus": send_pushplus}
    for channel in channels:
        if task_channel_complete(task_name, day, channel):
            continue
        sender = senders.get(channel)
        if sender is None:
            mark_task_channel(task_name, day, channel, False, detail="unsupported channel")
            continue
        ok = bool(sender(config, subject, content))
        mark_task_channel(task_name, day, channel, ok, detail=None if ok else "send failed")
    return task_complete(task_name, day, channels)


def _send_close_summary() -> None:
    """收盘前发送当日各股票 1d_est 盘中折算 KDJ 总结。"""
    global _close_summary_sent_date
    today_str = datetime.now().strftime("%Y-%m-%d")
    if _close_summary_sent_date == today_str:
        return

    config = state.config
    kdj_config = config.get("kdj", {})
    lines = ["收盘KDJ总结", f"日期：{today_str}", ""]

    has_data = False
    for symbol in state.symbols:
        code = symbol["code"]
        name = symbol.get("name") or code
        symbol_latest = state.latest.get(code, {})
        est_view = symbol_latest.get("1d_est")

        if not est_view:
            lines.append(f"{name}({code})：无盘中折算数据")
            continue

        has_data = True
        thresholds = _best_thresholds(code, kdj_config)
        buy = thresholds["buy"]
        sell = thresholds["sell"]
        k_val = est_view["k"]
        d_val = est_view["d"]
        j_val = est_view["j"]
        close_val = est_view["close"]

        if k_val >= sell:
            position = "⚠卖出区"
        elif k_val <= buy:
            position = "⭐买入区"
        else:
            position = "  中性"

        lines.append(
            f"{position} {name}({code}) "
            f"K={k_val:.2f} D={d_val:.2f} J={j_val:.2f} "
            f"收盘={close_val:.4f} "
            f"[阈值 K<{buy:g}买/K>{sell:g}卖]"
        )

    if not has_data:
        lines.append("（无有效盘中折算数据）")

    lines.append("")
    lines.append("该系统只做提醒，不自动下单。")

    content = "\n".join(lines)
    subject = f"KDJ收盘总结 {today_str}"

    channels = config.get("alert", {}).get("channels", [])
    if task_complete("close_summary", today_str, list(channels)):
        _close_summary_sent_date = today_str
        return
    completed = _deliver_persisted("close_summary", today_str, subject, content, config)
    if completed:
        _close_summary_sent_date = today_str
        app_logger.info("close summary sent for %s (%d symbols)", today_str, len(state.symbols))
    else:
        app_logger.warning("close summary remains pending for %s", today_str)


def _send_next_day_plan() -> None:
    """收盘后发送次日 T+1 操作指引。"""
    global _next_day_plan_sent_date
    today_str = datetime.now().strftime("%Y-%m-%d")
    if _next_day_plan_sent_date == today_str:
        return

    config = state.config
    trade_plan_config = config.get("trade_plan", {})
    use_llm = config.get("use_llm_advice", False)
    # 交易日收盘计划必须基于当天正式日线。数据源尚未更新时不发送，
    # 保持任务未完成，让监控循环继续重试，绝不回退到上一交易日。
    if daily_close_confirmed() and is_session_date(today_str, config):
        if not _refresh_formal_daily_for_plan(today_str):
            return

    applicable_date = next_session(today_str, config) or "下一交易日"
    lines = [
        "次日T+1操作指引",
        f"生成日期：{today_str}",
        f"正式日线：{today_str}",
        f"适用交易日：{applicable_date}",
        "",
    ]
    review_jobs: list[tuple[dict, dict]] = []

    configured_positions = (trade_plan_config.get("positions", {}) or {})
    has_data = False
    for symbol in state.symbols:
        if not _has_position_for_code(configured_positions, symbol["code"]):
            continue

        code = symbol["code"]
        name = symbol.get("name") or code

        deterministic_plan = _build_deterministic_plan(symbol, config, today_str)
        if deterministic_plan is None:
            lines.append(f"{name}({code})：正式日线或交易账本尚未就绪，不生成操作计划。")
            lines.append("")
            has_data = True
            continue

        if daily_close_confirmed() and is_session_date(today_str, config):
            if deterministic_plan.get("signal_date") != today_str:
                app_logger.error(
                    "next-day plan blocked: %s signal_date=%s expected=%s",
                    code,
                    deterministic_plan.get("signal_date"),
                    today_str,
                )
                return

        # 确定性计划永远是主计划；LLM只能在其后做只读复核。
        lines.append(format_decision_plan(deterministic_plan))
        review_jobs.append((symbol, deterministic_plan))
        has_data = True
        lines.append("")

    if not has_data:
        lines.append("（无已配置交易计划的有效日线数据）")

    lines.append("说明：确定性计划为唯一主计划，只做提醒、不自动下单；模型复核不能修改动作、手数、价位和T+1。")

    content = "\n".join(lines)
    subject = f"次日T+1操作指引 {today_str}"

    channels = config.get("alert", {}).get("channels", [])
    if task_complete("next_day_plan", today_str, list(channels)):
        _next_day_plan_sent_date = today_str
    else:
        completed = _deliver_persisted("next_day_plan", today_str, subject, content, config)
        if not completed:
            app_logger.warning("deterministic next day plan remains pending for %s", today_str)
            return
        _next_day_plan_sent_date = today_str
        app_logger.info("deterministic next day plan sent for %s (%d plans)", today_str, len(review_jobs))

    if not use_llm or not LLM_AVAILABLE or not review_jobs:
        return
    if task_complete("llm_plan_review", today_str, list(channels)):
        return

    review_lines = ["确定性计划模型复核", f"日期：{today_str}", "", "以下内容只做风险复核，不能改变已发送的主计划。", ""]
    review_count = 0
    for symbol, deterministic_plan in review_jobs:
        review = _generate_llm_advice(symbol, config, today_str, deterministic_plan)
        if not review:
            app_logger.warning("LLM review unavailable for %s; deterministic plan already sent", symbol["code"])
            continue
        provider_label = "Codex" if review["provider"] == "codex_cli" else "Axera备用"
        review_lines.extend([
            f"{symbol.get('name') or symbol['code']}({symbol['code']})",
            f"复核来源：{provider_label}",
            review["text"],
            "",
        ])
        review_count += 1
    if not review_count:
        return
    review_content = "\n".join(review_lines)
    review_subject = f"交易计划模型复核 {today_str}"
    if _deliver_persisted("llm_plan_review", today_str, review_subject, review_content, config):
        app_logger.info("LLM plan review sent for %s (%d reviews)", today_str, review_count)


def _build_deterministic_plan(symbol: dict, config: dict, today_str: str) -> Optional[dict]:
    code = symbol["code"]
    latest_daily = (state.latest.get(code) or {}).get("1d")
    daily_series = (state.series.get(code) or {}).get("1d", [])
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    position = _position_for_code(positions, code)
    if not latest_daily or not daily_series or not position:
        return None
    return build_decision_plan(
        symbol_code=code,
        symbol_name=str(symbol.get("name") or code),
        latest_daily=latest_daily,
        daily_series=daily_series,
        position=position,
        decision_date=today_str,
        performance_state=get_performance(code)["summary"],
        intraday_series=(state.series.get(code) or {}).get("10m", []),
        intraday_execution_enabled=is_trading_time(),
    )


def _generate_llm_advice(symbol: dict, config: dict, today_str: str,
                         deterministic_plan: Optional[dict] = None) -> Optional[dict]:
    """Generate a read-only review through the configured LLM provider chain."""
    if not LLM_AVAILABLE:
        return None

    code = symbol["code"]
    name = symbol.get("name") or code

    # 复核必须与确定性计划使用同一根正式日线，禁止混入1d_est。
    symbol_latest = state.latest.get(code, {})
    confirmed_day = symbol_latest.get("1d")
    latest_day = confirmed_day

    if not latest_day:
        app_logger.warning("LLM: no data available for %s", code)
        return None

    daily_data = {
        "close": latest_day.get("close"),
        "open": latest_day.get("open"),
        "high": latest_day.get("high"),
        "low": latest_day.get("low"),
        "k": latest_day.get("k"),
        "d": latest_day.get("d"),
        "j": latest_day.get("j"),
    }

    # 获取持仓配置
    trade_plan_config = config.get("trade_plan", {})
    positions = trade_plan_config.get("positions", {}) or {}
    position = _position_for_code(positions, code)
    position_context = {**position, "ledger": replay_position(position, as_of=today_str)}
    deterministic_plan = deterministic_plan or _build_deterministic_plan(symbol, config, today_str)
    if deterministic_plan is None:
        return None

    # 获取战略上下文（从记忆文件读取）
    strategy_context = _load_strategy_context(code)

    # 获取成交历史
    trade_history = position.get("trade_history", [])

    try:
        advice = generate_trading_advice(
            symbol_name=name,
            symbol_code=code,
            daily_data=daily_data,
            position=position_context,
            strategy_context=strategy_context,
            trade_history=trade_history,
            deterministic_plan=deterministic_plan,
            advisor_config=config.get("llm") or {},
        )
        if advice:
            return advice
    except Exception as exc:
        app_logger.error("LLM advice generation failed for %s: %s", code, exc)

    return None


def _llm_health_check() -> None:
    """每日检查Codex主通道，并在需要时验证Axera备用通道。"""
    if not LLM_AVAILABLE:
        app_logger.warning("LLM health check skipped: LLM not available")
        mark_task_channel("llm_health", datetime.now().strftime("%Y-%m-%d"), "check", True, detail="not available")
        return

    config = state.config
    result = health_check(config.get("llm") or {})
    if result["ok"]:
        app_logger.info(
            "LLM health check OK, provider=%s fallback=%s latency=%dms",
            result["provider"], result["fallback_used"], result["latency_ms"],
        )
        detail = f"provider={result['provider']}"
        if result["fallback_used"]:
            from .notifier import send_pushplus
            content = f"""LLM主通道降级，备用通道可用
时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Codex错误：{result.get('primary_error') or '未知'}
当前可用：Axera备用通道
延迟：{result['latency_ms']}ms

确定性交易计划不受影响；模型复核将自动使用Axera，并在消息中标明来源。"""
            send_pushplus(config, "LLM已切换Axera备用通道", content)
            detail += f" primary_error={result.get('primary_error')}"
        mark_task_channel("llm_health", datetime.now().strftime("%Y-%m-%d"), "check", True, detail=detail)
    else:
        app_logger.error("LLM health check FAILED for all providers: %s", result["error"])
        from .notifier import send_pushplus
        content = f"""LLM主备通道健康检查均失败
时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
错误：{result['error']}
延迟：{result['latency_ms']}ms

确定性交易计划仍会独立生成和发送；本次不会发送未经模型成功校验的复核内容。
请检查Codex登录/代理以及Axera服务。"""
        send_pushplus(config, "LLM主备通道均不可用", content)
        mark_task_channel(
            "llm_health", datetime.now().strftime("%Y-%m-%d"), "check", True,
            detail=f"all failed: {result['error']}",
        )


def _load_strategy_context(code: str) -> str:
    """Load strategy context from memory files."""
    import os
    memory_dir = "/data/kongdejing/.claude/projects/-data-kongdejing-workspace-kdj-quantitative-metrics/memory"

    # 交易记忆已收敛为单一权威摘要，不再加载旧阶段计划或纠错快照。
    strategy_files = ["canonical_trading_context.md"]

    contexts = []
    for filename in strategy_files:
        filepath = os.path.join(memory_dir, filename)
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
                # 提取核心内容（去掉frontmatter）
                if "---" in content:
                    parts = content.split("---")
                    if len(parts) >= 3:
                        content = parts[2].strip()
                contexts.append(content[:4000])  # 单一权威摘要，保留完整策略和最终持仓

    if contexts:
        return "\n\n".join(contexts)

    # 默认战略描述
    return (
        "用户采用中航核心仓分阶段扩仓，并在盘中冲高、10分钟K从80以上拐头时使用现有可卖老仓做反T："
        "不使用MA均线；总持仓20%为反T额度，单次最多2手，卖出后必须先按盈利位补回。"
    )


async def monitor_loop() -> None:
    interval = int(state.config.get("poll_interval_seconds", 60))
    was_trading = None
    # 收盘后/周末重启时内存状态为空，先补一轮数据，保证页面立即可用
    if not is_trading_time() and not state.latest:
        app_logger.info("initial monitor tick on startup (market closed, filling state)")
        await asyncio.to_thread(run_once, skip_alerts=True)
    while True:
        trading = is_trading_time()
        if trading != was_trading:
            app_logger.info("monitor %s", "resumed (trading hours)" if trading else "paused (market closed)")
            was_trading = trading
        if not trading:
            now = datetime.now()
            # 收盘后15:10发送次日指引（非交易时段也执行）
            if is_session_date(now, state.config) and now.hour == 15 and now.minute >= 15:
                await asyncio.to_thread(_send_next_day_plan)
            if is_session_date(now, state.config) and now.hour >= 9 and not task_channel_complete(
                "llm_health", now.strftime("%Y-%m-%d"), "check"
            ):
                await asyncio.to_thread(_llm_health_check)
            await asyncio.sleep(interval)
            continue
        app_logger.info("start monitor tick")
        await asyncio.to_thread(run_once)

        # 每日09:00健康检查：测试LLM API可用性
        now = datetime.now()
        if now.hour >= 9 and not task_channel_complete("llm_health", now.strftime("%Y-%m-%d"), "check"):
            await asyncio.to_thread(_llm_health_check)

        # 收盘前10分钟（14:50-15:00）发送当日KDJ总结，给用户操作窗口
        if now.hour == 14 and now.minute >= 50:
            await asyncio.to_thread(_send_close_summary)

        # 15:15起尝试发送；若当天正式日线未就绪则不发送并持续重试。
        if now.hour == 15 and now.minute >= 15 and is_session_date(now, state.config):
            await asyncio.to_thread(_send_next_day_plan)

        await asyncio.sleep(interval)
