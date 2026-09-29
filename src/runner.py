from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dt_time, timedelta
from math import isfinite
from typing import Optional

import pandas as pd

from .candidate_digest import build_candidate_digest, format_candidate_digest
from .data_provider import daily_close_confirmed, fetch_realtime_quotes, safe_fetch_kline
from .decision_engine import build_decision_plan, format_decision_plan
from .kdj import calculate_kdj
from .kdj_policy import kdj_alerts_enabled
from .logger import app_logger
from .market_risk import current_market_risk, refresh_market_risk, risk_settings, stock_entry_risk
from .notifier import notify, notify_observation_exit, notify_price_target, notify_reverse_t
from .observation_discipline import (
    evaluate_entry_support,
    evaluate_stabilization,
    format_observation_discipline,
    observation_discipline,
)
from .pending_orders import active_pending_orders
from .performance_store import backfill_snapshots, get_performance
from .runtime_state import claim_observation_entry, load_runtime_state, mark_task_channel, task_channel_complete, task_complete
from .shadow_tracker import record_and_evaluate
from .stage_research import load_stage_report, refresh_stage_report
from .state import state
from .trade_fees import estimate_trade_fee
from .strategy import check_kdj_signal
from .trade_ledger import replay_position
from .trading_calendar import is_session_date, next_session

# 可选：LLM生成交易建议
try:
    from .llm_advisor import generate_candidate_price_advice, generate_trading_advice, health_check
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
MINUTE_BAR_CONFIRMATION_GRACE_SECONDS = 15

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


def _daily_estimate_from_intraday(daily_data: pd.DataFrame, intraday_data: pd.DataFrame, *, day: Optional[str] = None) -> Optional[pd.DataFrame]:
    """用当日分钟线折算一根盘中日线，返回用于计算日线KDJ的数据。"""
    if daily_data.empty or intraday_data.empty or "datetime" not in intraday_data.columns:
        return None

    today = day or datetime.now().strftime("%Y-%m-%d")
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
                 thresholds: Optional[dict] = None, complete: Optional[bool] = None) -> dict:
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
    if complete is not None:
        view["complete"] = complete
    return view


def _minute_bar_complete(timestamp: object, observed_at: datetime) -> bool:
    """Minute providers label a candle with its ending time, even while it forms."""
    bar_end = _quote_time(timestamp)
    # Allow the provider a few seconds to publish the final value at the boundary.
    return bool(bar_end and observed_at >= bar_end + timedelta(seconds=MINUTE_BAR_CONFIRMATION_GRACE_SECONDS))


def _session_close_boundary(now: datetime) -> Optional[datetime]:
    """Only finalize after a session; never enable trading alerts in the break."""
    if not is_session_date(now, state.config):
        return None
    for hour, minute, until in ((11, 30, dt_time(13)), (15, 0, dt_time(23, 59, 59))):
        end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= end + timedelta(seconds=MINUTE_BAR_CONFIRMATION_GRACE_SECONDS) and now.time() < until:
            return end
    return None


def _refresh_session_close(now: Optional[datetime] = None) -> bool:
    """Retry unfinished final candles during lunch/after close, without alerts.

    The independent final quote must agree with the minute close. A successful
    but lagging provider response is not enough to mark the candle finalized.
    """
    now = now or datetime.now()
    boundary = _session_close_boundary(now)
    if boundary is None or "10m" not in state.config.get("timeframes", []):
        return True
    token = boundary.strftime("%Y-%m-%d %H:%M:%S")
    pending = [
        symbol for symbol in list(state.symbols)
        if ((state.latest.get(symbol["code"]) or {}).get("10m") or {}).get("finalized_session") != token
    ]
    if not pending:
        return True
    try:
        quotes = fetch_realtime_quotes([str(s["code"]) for s in pending])
    except Exception as exc:
        app_logger.warning("session-close quotes unavailable: %s", exc)
        return False
    config = state.config.get("kdj", {})
    all_ready = True
    # Minute-only reconciliation avoids repeatedly fetching slow daily sources.
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = list(pool.map(lambda s: safe_fetch_kline(s["code"], "10m"), pending))
    for symbol, data in zip(pending, frames):
        code = symbol["code"]
        if data is None or data.empty:
            all_ready = False
            continue
        last = data.iloc[-1]
        quote = quotes.get(str(code)) or {}
        stamp = _quote_time(quote.get("timestamp"))
        bar_end = _quote_time(last.get("datetime"))
        try:
            quote_price = float(quote.get("price") or 0)
            candle_price = float(last["close"])
        except (TypeError, ValueError):
            all_ready = False
            continue
        if (
            bar_end != boundary or stamp is None or stamp < boundary
            or stamp > now + timedelta(seconds=30)
            or stamp.date() != boundary.date() or not isfinite(quote_price) or quote_price <= 0
            or not isfinite(candle_price) or candle_price <= 0
            or abs(candle_price - quote_price) > 0.011
        ):
            app_logger.warning("session-close bar not final yet: symbol=%s expected=%s", code, token)
            all_ready = False
            continue
        kdj = calculate_kdj(data, n=int(config.get("n", 9)), m1=int(config.get("m1", 3)), m2=int(config.get("m2", 3)))
        current_day = kdj[kdj["datetime"].astype(str).str.startswith(boundary.strftime("%Y-%m-%d"))]
        series = []
        for row in current_day.tail(120).to_dict("records"):
            point = {field: round(float(row[field]), 2 if field in {"k", "d", "j"} else 4)
                     for field in ("open", "high", "low", "close", "k", "d", "j")}
            point.update(timestamp=str(row["datetime"]), complete=_minute_bar_complete(row["datetime"], now))
            series.append(point)
        thresholds = _best_thresholds(code, config)
        view = _latest_view(symbol, "10m", kdj.iloc[-1].to_dict(), thresholds=thresholds, complete=True)
        view["finalized_session"] = token
        previous_close = float(quote.get("previous_close") or 0)
        if previous_close > 0:
            view.update(previous_close=previous_close, change_ratio=round(float(view["close"]) / previous_close - 1, 6))
        daily_rows = (state.series.get(code) or {}).get("1d") or []
        if daily_rows:
            daily = pd.DataFrame(daily_rows).rename(columns={"timestamp": "date"})
            estimated = _daily_estimate_from_intraday(daily, data, day=boundary.strftime("%Y-%m-%d"))
            if estimated is not None:
                estimated_kdj = calculate_kdj(estimated, n=int(config.get("n", 9)), m1=int(config.get("m1", 3)), m2=int(config.get("m2", 3)))
                estimated_view = _latest_view(symbol, "1d_est", estimated_kdj.iloc[-1].to_dict(), estimated=True, thresholds=thresholds)
                estimated_view.update(source_timeframe="10m", note="分钟线折算日线，非正式收盘确认")
                state.update_latest(code, "1d_est", estimated_view)
        state.update_series(code, "10m", series)
        state.update_latest(code, "10m", view)
        app_logger.info("session-close candle finalized: symbol=%s end=%s close=%s", code, token, view["close"])
    return all_ready


def _best_thresholds(symbol_code: str, kdj_config: dict) -> dict:
    """读取单只股票的最优 KDJ 阈值；没有寻优结果时回退全局阈值。"""
    try:
        from .optimizer import get_best
        best = get_best(symbol_code)
    except Exception as exc:
        app_logger.warning("load best thresholds failed: symbol=%s error=%s", symbol_code, exc)
        best = None

    if not best or not bool(best.get("qualified", False)):
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
    symbols_snapshot = list(state.symbols)

    # Tencent's previous_close is the exchange comparison base for today's
    # percentage change (including the adjusted base on an XD day).  Fetch it
    # once in a batch and combine it with the latest 10-minute close below.
    try:
        realtime_quotes = fetch_realtime_quotes(list(dict.fromkeys(
            [str(item["code"]) for item in symbols_snapshot] + list(risk_settings(config)["index_drop_limits"])
        )))
    except Exception as exc:
        app_logger.warning("realtime comparison base unavailable; UI will use formal daily close: %s", exc)
        realtime_quotes = {}
    market_context = refresh_market_risk(config, quotes=realtime_quotes)

    # 观察仓买卖信号使用独立实时快照；行情不新鲜时严格不发送。
    if not skip_alerts:
        _maybe_send_new_entry_risk_alert(config, market_context)
        _maybe_send_price_target_alerts(config, cooldown_seconds, market_context=market_context)
        _maybe_send_observation_exit_alerts(config, cooldown_seconds)

    for symbol in symbols_snapshot:
        allow_kdj_alerts = kdj_alerts_enabled(config, symbol["code"])
        thresholds = _best_thresholds(symbol["code"], kdj_config)
        daily_raw = None
        intraday_for_estimate = None
        for timeframe in config.get("timeframes", []):
            fetch_started_at = datetime.now()
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
                point = {
                        "timestamp": str(row.get("datetime") or row.get("date") or ""),
                        "open": round(float(row["open"]), 4),
                        "high": round(float(row["high"]), 4),
                        "low": round(float(row["low"]), 4),
                        "close": round(float(row["close"]), 4),
                        "k": round(float(row["k"]), 2),
                        "d": round(float(row["d"]), 2),
                        "j": round(float(row["j"]), 2),
                    }
                if timeframe == "10m":
                    point["complete"] = _minute_bar_complete(point["timestamp"], fetch_started_at)
                series.append(point)
            state.update_series(symbol["code"], timeframe, series)
            latest_view = _latest_view(
                symbol, timeframe, latest, thresholds=thresholds,
                complete=series[-1]["complete"] if timeframe == "10m" else None,
            )
            if timeframe == "10m":
                quote = realtime_quotes.get(str(symbol["code"])) or {}
                try:
                    previous_close = float(quote.get("previous_close") or 0)
                except (TypeError, ValueError):
                    previous_close = 0
                if previous_close > 0:
                    latest_view["previous_close"] = round(previous_close, 4)
                    latest_view["change_ratio"] = round(float(latest_view["close"]) / previous_close - 1, 6)
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
                                market_risk=market_context,
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

            # Some symbols have explicitly failed KDJ suitability review.  Keep
            # their indicator on the UI, but never turn it into a trading alert.
            if not allow_kdj_alerts:
                state.clear_alert_zone(f"{symbol['code']}:1d_est")
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
            if signal.direction == "low" and market_context.get("block_new_buys", True):
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


def _observation_portfolio_status(config: dict, day: str) -> tuple[float, set[str]]:
    """Read actual observation holdings and today's recorded first buys."""
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    invested = 0.0
    new_symbols: set[str] = set()
    for code, position in positions.items():
        if str(position.get("strategy_mode") or "") != "long_term":
            continue
        try:
            summary = replay_position(position, as_of=day, strict=True)
        except Exception as exc:
            app_logger.warning("observation position replay failed: symbol=%s error=%s", code, exc)
            return float("inf"), {"ledger_invalid"}
        lots = float(summary.get("total_lots", 0) or 0)
        average_cost = float(summary.get("average_entry_cost", 0) or 0)
        invested += lots * average_cost * 100
        for order in active_pending_orders(position, day):
            if order.get("side") == "buy":
                order_price = float(order.get("limit_price", 0) or 0)
                order_lots = int(order.get("lots", 0) or 0)
                invested += order_price * order_lots * 100 + estimate_trade_fee(order_price, order_lots)
                new_symbols.add(str(code))
        if not any(
            str(trade.get("side") or "").lower() == "buy"
            and str(trade.get("reported_at") or "")[:10] == day
            for trade in (position.get("trade_history") or [])
        ):
            continue
        new_symbols.add(str(code))
    return round(invested, 2), new_symbols


def _maybe_send_new_entry_risk_alert(config: dict, risk: dict) -> None:
    if not risk.get("latched"):
        return
    day = risk["date"]
    lines = [f"新增买入暂停｜{day}", "；".join(risk.get("reasons") or []),
             "今日暂停观察仓新买与中航扩仓；明日重新核验。原反T盈利回补与卖出纪律保持。"]
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    for code, position in positions.items():
        pending = int(replay_position(position, as_of=day).get("pending_core_buyback_lots", 0) or 0)
        buys = [o for o in active_pending_orders(position, day) if o.get("side") == "buy"]
        if position.get("strategy_mode") == "expand_base" and sum(int(o.get("lots", 0)) for o in buys) <= pending:
            continue
        for order in buys:
            lines.append(f"{code}：{float(order.get('limit_price', 0)):.2f}元买{int(order.get('lots', 0))}手，建议撤销未成交新增买单。")
    lines.append("请在券商端处理未成交新增买单；系统不能自动撤单，也不会把撤单建议记成已撤销。")
    _deliver_persisted("new_entry_market_risk", day, f"市场急跌：暂停新增买入 {day}", "\n".join(lines), config)


def _has_open_buy_order(config: dict, code: str, day: str) -> bool:
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    position = _position_for_code(positions, code)
    return any(
        str(item.get("side") or "").lower() == "buy"
        and str(item.get("status") or "open").lower() == "open"
        for item in active_pending_orders(position, day)
    )


def _maybe_send_price_target_alerts(
    config: dict,
    cooldown_seconds: int,
    *,
    now: Optional[datetime] = None,
    market_context: Optional[dict] = None,
) -> None:
    """Send only a stabilized, executable entry signal for flat candidates.

    Reaching the configured target is an internal observation condition.  It
    never sends a passive WeChat message by itself.
    """
    rules = config.get("price_alerts") or {}
    enabled_rules = {
        str(code): rule for code, rule in rules.items()
        if isinstance(rule, dict) and bool(rule.get("enabled", True))
    }
    if not enabled_rules:
        return
    try:
        quotes = fetch_realtime_quotes(list(enabled_rules) + list(risk_settings(config)["index_drop_limits"]))
    except Exception as exc:
        app_logger.warning("real-time price targets unavailable; no alert sent: %s", exc)
        return

    current = now or datetime.now()
    risk = market_context if market_context is not None else refresh_market_risk(config, now=current, quotes=quotes)
    if risk.get("block_new_buys", True):
        return
    discipline = observation_discipline(config)
    if not bool(discipline.get("enabled", True)):
        return
    current_day = current.strftime("%Y-%m-%d")
    observation_cost, bought_today = _observation_portfolio_status(config, current_day)
    # A sent instruction can already have filled at the broker before UI reporting.
    claim = (load_runtime_state().get("observation_entry_claims") or {}).get(current_day)
    if claim:
        return
    max_new_symbols = max(1, int(discipline.get("max_new_symbols_per_day", 1) or 1))
    for code, rule in enabled_rules.items():
        if len(bought_today) >= max_new_symbols:
            app_logger.info("observation entry suppressed by daily new-symbol limit: symbol=%s", code)
            continue
        quote = quotes.get(code)
        max_age_seconds = int(rule.get("max_quote_age_seconds", 180) or 180)
        if not quote or not _quote_is_fresh(quote, current, max_age_seconds):
            app_logger.warning("stale/missing price target quote; no alert sent: symbol=%s", code)
            continue
        if stock_entry_risk(quote, config):
            continue

        target = float(rule.get("target_price", 0) or 0)
        if target <= 0:
            continue
        tolerance_ratio = max(0.0, float(rule.get("tolerance_ratio", 0.005) or 0))
        reset_ratio = max(tolerance_ratio, float(rule.get("reset_ratio", 0.015) or 0))
        trigger_price = target * (1 + tolerance_ratio)
        reset_price = target * (1 + reset_ratio)
        latest_price = float(quote["price"])
        signal_key = f"{code}:observation_entry:{target:.4f}"
        minimum_price = float(rule.get("minimum_price", 0) or 0)

        # Hysteresis: leaving the observation area re-arms a future stabilized signal.
        if latest_price >= reset_price:
            state.clear_alert_zone(signal_key)
            continue
        # A bounded setup is invalid below its configured lower edge; a deeper
        # fall must be reviewed instead of being treated as an even better buy.
        if minimum_price > 0 and latest_price < minimum_price:
            state.clear_alert_zone(signal_key)
            continue
        if latest_price > trigger_price:
            continue
        if bool(rule.get("only_when_flat", True)):
            held_lots = _configured_position_lots(config, code, current_day)
            if held_lots is None or held_lots > 0:
                continue
        if _has_open_buy_order(config, code, current_day):
            app_logger.info("observation entry suppressed by existing buy order: symbol=%s", code)
            continue

        # 目标价只负责进入后台观察；下列条件不全部通过就不发消息。
        earliest_text = str(discipline.get("earliest_entry_time") or "14:45")
        try:
            earliest_time = datetime.strptime(earliest_text, "%H:%M").time()
        except ValueError:
            earliest_time = dt_time(14, 45)
        if current.time() < earliest_time:
            continue
        lots = 1
        fee_per_lot = float(rule.get("fee_per_lot", 5) or 5)
        estimated_cash = trigger_price * 100 * lots + estimate_trade_fee(
            trigger_price, lots, fee_per_lot=fee_per_lot
        )
        capital_limit = float(discipline.get("total_capital_limit", 20_000) or 20_000)
        if observation_cost + estimated_cash > capital_limit + 1e-9:
            app_logger.info(
                "observation entry suppressed by capital limit: symbol=%s current=%.2f projected=%.2f limit=%.2f",
                code,
                observation_cost,
                observation_cost + estimated_cash,
                capital_limit,
            )
            continue

        try:
            intraday = safe_fetch_kline(code, "5m")
        except Exception as exc:
            app_logger.warning("observation stabilization data unavailable: symbol=%s error=%s", code, exc)
            continue
        if intraday is None or intraday.empty:
            app_logger.warning("observation stabilization data empty: symbol=%s", code)
            continue
        stabilization = evaluate_stabilization(
            intraday.to_dict("records"),
            now=current,
            current_price=latest_price,
            discipline=discipline,
        )
        if not stabilization.get("ready"):
            app_logger.info(
                "observation target reached but entry not ready: symbol=%s reason=%s",
                code,
                stabilization.get("reason"),
            )
            continue
        formal = safe_fetch_kline(code, "1d")
        support = evaluate_entry_support(
            formal.to_dict("records") if formal is not None else [], quote, now=current,
        )
        if not support.get("ready"):
            app_logger.info("observation support invalid: symbol=%s reason=%s", code, support.get("reason"))
            continue
        if not state.should_alert(signal_key, "buy_ready", cooldown_seconds):
            continue
        if not claim_observation_entry(current_day, code, estimated_cash):
            return

        alert = {
            "type": "observation_buy_ready",
            "symbol": code,
            "name": str(rule.get("name") or quote.get("name") or code),
            "timeframe": "实时到价",
            "direction": "buy_target",
            "close": latest_price,
            "target_price": target,
            "trigger_price": round(trigger_price, 4),
            "max_buy_price": round(trigger_price, 2),
            "lots": lots,
            "estimated_cash": round(estimated_cash, 2),
            "observation_cost": observation_cost,
            "change_ratio": quote.get("change_ratio"),
            "timestamp": str(quote.get("timestamp") or ""),
            "created_at": current.strftime("%Y-%m-%d %H:%M:%S"),
            "source": quote.get("source"),
            "reason": str(rule.get("reason") or "目标价附近回稳后首次试仓"),
            "risk_note": str(rule.get("risk_note") or "回稳不代表不再下跌，单次仅1手"),
            "observation_discipline": discipline,
            "stabilization": stabilization,
            "market_risk": risk,
            "support_check": support,
            "email_sent": False,
        }
        notify_price_target(config, alert)
        state.add_alert(alert)
        return


def _maybe_send_observation_exit_alerts(
    config: dict,
    cooldown_seconds: int,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Send executable exits for observation lots and the Zhonghang final target."""
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    held: dict[str, tuple[dict, dict, bool]] = {}
    current = now or datetime.now()
    day = current.strftime("%Y-%m-%d")
    for raw_code, position in positions.items():
        strategy_mode = str(position.get("strategy_mode") or "")
        final_exit = strategy_mode == "expand_base" and float(position.get("final_exit_target", 0) or 0) > 0
        if strategy_mode != "long_term" and not final_exit:
            continue
        code = str(raw_code)
        try:
            summary = replay_position(position, as_of=day)
        except Exception as exc:
            app_logger.warning("observation exit replay failed: symbol=%s error=%s", code, exc)
            continue
        if float(summary.get("total_lots", 0) or 0) > 0:
            held[code] = (position, summary, final_exit)
    if not held:
        return
    try:
        quotes = fetch_realtime_quotes(held)
    except Exception as exc:
        app_logger.warning("observation exit quotes unavailable; no alert sent: %s", exc)
        return

    symbol_names = {
        str(item.get("code")): str(item.get("name") or item.get("code"))
        for item in (config.get("symbols") or [])
    }
    reset_ratio = float(observation_discipline(config).get("exit_reset_ratio", 0.02) or 0.02)
    for code, (position, summary, final_exit) in held.items():
        quote = quotes.get(code)
        max_age_seconds = int(position.get("max_quote_age_seconds", 180) or 180)
        if not quote or not _quote_is_fresh(quote, current, max_age_seconds):
            app_logger.warning("stale/missing observation exit quote; no alert sent: symbol=%s", code)
            continue
        price = float(quote["price"])
        stop_loss = 0.0 if final_exit else float(position.get("stop_loss", 0) or 0)
        target_sell = float(
            position.get("final_exit_target", 0) if final_exit else position.get("target_sell", 0)
            or 0
        )
        stop_key = f"{code}:observation_exit:stop:{stop_loss:.4f}"
        target_key = (
            f"{code}:final_exit:target:{target_sell:.4f}:{day}"
            if final_exit else f"{code}:observation_exit:target:{target_sell:.4f}"
        )
        if stop_loss <= 0 or price > stop_loss * (1 + reset_ratio):
            state.clear_alert_zone(stop_key)
        if target_sell <= 0 or price < target_sell * (1 - reset_ratio):
            state.clear_alert_zone(target_key)

        action = ""
        threshold = 0.0
        signal_key = ""
        if stop_loss > 0 and price <= stop_loss:
            action, threshold, signal_key = "sell_stop", stop_loss, stop_key
        elif target_sell > 0 and price >= target_sell:
            action, threshold, signal_key = "sell_take_profit", target_sell, target_key
        if not action:
            continue
        sellable = int(float(summary.get("sellable_lots_today", 0) or 0))
        if sellable <= 0:
            app_logger.info("observation exit blocked by T+1: symbol=%s action=%s", code, action)
            continue
        if not state.should_alert(signal_key, action, cooldown_seconds):
            continue
        lots = sellable if final_exit else min(1, sellable)
        alert = {
            "type": "core_final_exit" if final_exit else "observation_exit",
            "symbol": code,
            "name": symbol_names.get(code) or str(quote.get("name") or code),
            "direction": action,
            "close": price,
            "threshold_price": threshold,
            "lots": lots,
            "sellable_lots": sellable,
            "timestamp": str(quote.get("timestamp") or ""),
            "created_at": current.strftime("%Y-%m-%d %H:%M:%S"),
            "source": quote.get("source"),
            "email_sent": False,
            "position_exit": final_exit,
        }
        notify_observation_exit(config, alert)
        state.add_alert(alert)


def _position_for_code(positions: dict, code: str) -> dict:
    position_key = next((key for key in positions if str(key) == str(code)), code)
    return positions.get(position_key, {}) or {}


def _has_position_for_code(positions: dict, code: str) -> bool:
    return any(str(key) == str(code) for key in positions)


def _symbols_for_next_day_plan(config: dict, day: str) -> list[dict[str, str]]:
    """Include every real holding even when it is absent from the UI watchlist."""
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    result = [
        {"code": str(symbol["code"]), "name": str(symbol.get("name") or symbol["code"])}
        for symbol in state.symbols
        if _has_position_for_code(positions, str(symbol["code"]))
    ]
    included = {symbol["code"] for symbol in result}
    price_alerts = config.get("price_alerts") or {}

    for raw_code, position in positions.items():
        code = str(raw_code)
        if code in included:
            continue
        try:
            ledger = replay_position(position, as_of=day)
        except Exception as exc:
            app_logger.warning("next-day plan position replay failed: symbol=%s error=%s", code, exc)
            continue
        if (
            int(ledger.get("total_lots", 0) or 0) <= 0
            and int(ledger.get("pending_core_buyback_lots", 0) or 0) <= 0
        ):
            continue
        alert_rule = _position_for_code(price_alerts, code)
        result.append({"code": code, "name": str(alert_rule.get("name") or code)})
        included.add(code)

    return result


def _maybe_send_reverse_t_alert(
    symbol: dict,
    config: dict,
    cooldown_seconds: int,
    *,
    now: Optional[datetime] = None,
) -> None:
    current = now or datetime.now()
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    position = _position_for_code(positions, symbol["code"])
    if not (position.get("reverse_t") or {}).get("enabled"):
        return
    plan = _build_deterministic_plan(symbol, config, current.strftime("%Y-%m-%d"))
    if plan is None:
        return
    reverse_t = plan.get("reverse_t") or {}
    decision = reverse_t.get("decision") or {}
    action = str(decision.get("action") or "hold")
    signal_key = f"{symbol['code']}:reverse_t"
    executable = decision.get("status") == "executable" and action in {
        "sell_core_for_reverse_t", "buyback_core", "protective_buyback",
        "manage_existing_buyback",
    }
    if not executable:
        state.clear_alert_zone(signal_key)
        return
    signal = reverse_t.get("signal") or {}
    latest = ((state.latest.get(symbol["code"]) or {}).get("10m")) or {}
    signal_time = _quote_time(signal.get("intraday_timestamp") or latest.get("timestamp"))
    if (
        signal_time is None
        or signal_time.date() != current.date()
        # The provider may label the currently forming 10-minute candle with
        # its future end time; this is a provisional, real-time signal.
        or not timedelta(minutes=-10) <= current - signal_time <= timedelta(minutes=15)
    ):
        app_logger.warning(
            "reverse-T executable result rejected because 10m bar is stale: symbol=%s timestamp=%s",
            symbol["code"],
            signal.get("intraday_timestamp"),
        )
        return
    if not state.should_alert(signal_key, action, cooldown_seconds):
        return
    alert = {
        "symbol": symbol["code"],
        "name": symbol.get("name") or symbol["code"],
        "timeframe": "10m反T",
        "direction": "high" if action == "sell_core_for_reverse_t" else "low",
        "k": float(signal.get("k") if signal.get("k") is not None else latest.get("k", 0) or 0),
        "d": float(signal.get("d") if signal.get("d") is not None else latest.get("d", 0) or 0),
        "j": float(signal.get("j") if signal.get("j") is not None else latest.get("j", 0) or 0),
        "close": float(signal.get("close") if signal.get("close") is not None else latest.get("close", 0) or 0),
        "timestamp": str(signal.get("intraday_timestamp") or latest.get("timestamp") or ""),
        "forming": bool(signal.get("intraday_forming", latest.get("complete") is False)),
        "created_at": current.strftime("%Y-%m-%d %H:%M:%S"),
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

    for symbol in _symbols_for_next_day_plan(config, today_str):
        code = symbol["code"]

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


def _build_daily_portfolio_pnl(config: dict, day: str) -> Optional[dict]:
    """Report held stocks and today's exits, never untouched flat candidates."""
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    rows: list[dict] = []
    excluded_flat_realized_pnl = 0.0
    previous_calendar_day = (datetime.fromisoformat(day) - timedelta(days=1)).strftime("%Y-%m-%d")
    names = {
        str(symbol.get("code")): str(symbol.get("name") or symbol.get("code"))
        for symbol in [*(config.get("symbols") or []), *state.symbols]
    }
    price_alerts = config.get("price_alerts") or {}

    for raw_code, position in positions.items():
        code = str(raw_code)
        try:
            prior_calendar_ledger = replay_position(position, as_of=previous_calendar_day)
            current_ledger = replay_position(position, as_of=day, strict=True)
        except Exception as exc:
            app_logger.warning("daily portfolio pnl ledger failed: symbol=%s error=%s", code, exc)
            return None
        prior_lots = float(prior_calendar_ledger.get("total_lots", 0) or 0)
        current_lots = float(current_ledger.get("total_lots", 0) or 0)
        traded_today = any(
            str(trade.get("reported_at") or "")[:10] == day
            and str(trade.get("side") or "").lower() in {"buy", "sell"}
            for trade in (position.get("trade_history") or [])
        )
        if prior_lots <= 0 and current_lots <= 0 and not traded_today:
            # Keep historical completed-trade profit in the whole-ledger
            # total even though this flat stock has no row today.
            excluded_flat_realized_pnl += float(current_ledger.get("realized_pnl", 0) or 0)
            continue

        daily_series = (state.series.get(code) or {}).get("1d", [])
        bars_by_day: dict[str, dict] = {}
        for item in daily_series:
            bar_day = str(item.get("timestamp") or item.get("date") or "")[:10]
            if bar_day and bar_day <= day:
                bars_by_day[bar_day] = item
        if day not in bars_by_day:
            data = safe_fetch_kline(code, "1d")
            if data is not None:
                for item in data.to_dict("records"):
                    bar_day = str(item.get("date") or item.get("datetime") or "")[:10]
                    if bar_day and bar_day <= day:
                        bars_by_day[bar_day] = item
        ordered_days = sorted(bars_by_day)
        if not ordered_days or ordered_days[-1] != day or (len(ordered_days) < 2 and prior_lots > 0):
            app_logger.warning(
                "daily portfolio pnl waiting: %s formal daily date=%s expected=%s",
                code,
                ordered_days[-1] if ordered_days else "missing",
                day,
            )
            return None

        previous_day = ordered_days[-2] if len(ordered_days) >= 2 else previous_calendar_day
        current_close = float(bars_by_day[day]["close"])
        previous_close = float(bars_by_day[previous_day]["close"]) if len(ordered_days) >= 2 else 0.0
        try:
            previous_ledger = replay_position(position, as_of=previous_day, strict=True)
        except Exception as exc:
            app_logger.warning("daily portfolio pnl ledger failed: symbol=%s error=%s", code, exc)
            return None

        previous_equity = (
            float(previous_ledger.get("total_lots", 0) or 0) * 100 * previous_close
            - float(previous_ledger.get("net_cash_invested", 0) or 0)
        )
        current_equity = (
            float(current_ledger.get("total_lots", 0) or 0) * 100 * current_close
            - float(current_ledger.get("net_cash_invested", 0) or 0)
        )
        rows.append({
            "symbol": code,
            "name": names.get(code) or str(_position_for_code(price_alerts, code).get("name") or code),
            "date": day,
            "previous_date": previous_day,
            "previous_close": round(previous_close, 4),
            "close": round(current_close, 4),
            "lots": int(current_lots),
            "exited_today": current_lots <= 0 and prior_lots > 0,
            "today_realized_pnl": round(
                float(current_ledger.get("realized_pnl", 0) or 0)
                - float(previous_ledger.get("realized_pnl", 0) or 0), 2
            ),
            "daily_pnl": round(current_equity - previous_equity, 2),
            # Under the diluted/breakeven-cost convention, this is current
            # holding P&L while shares remain, and realized P&L after exit.
            "cumulative_pnl": round(current_equity, 2),
            "breakeven_cost": current_ledger.get("breakeven_cost"),
        })

    if not rows:
        return None
    return {
        "date": day,
        "rows": rows,
        "daily_pnl": round(sum(float(row["daily_pnl"]) for row in rows), 2),
        "cumulative_pnl": round(sum(float(row["cumulative_pnl"]) for row in rows), 2),
        "ledger_cumulative_pnl": round(
            sum(float(row["cumulative_pnl"]) for row in rows) + excluded_flat_realized_pnl, 2
        ),
    }


def _format_daily_portfolio_pnl(report: dict) -> str:
    def money(value: float) -> str:
        return f"{float(value):+.2f}元"

    lines = [
        "每日收盘持仓盈亏",
        f"日期：{report['date']}",
        "口径：今日盈亏按上一交易日收盘到今日正式收盘计算，包含今日成交和已录入手续费；持仓盈亏按摊薄保本成本计算。",
        "",
    ]
    for row in report["rows"]:
        line = f"{row['name']}({row['symbol']})：今日{money(row['daily_pnl'])}；"
        if row.get("exited_today"):
            line += (
                f"今日已清仓，卖出已实现{money(row['today_realized_pnl'])}；"
                f"该股账本累计已实现{money(row['cumulative_pnl'])}；收盘{row['close']:.2f}元；持仓0手。"
            )
        else:
            cost = row.get("breakeven_cost")
            cost_text = f"{float(cost):.3f}元" if cost is not None else "-"
            line += (
                f"持仓盈亏{money(row['cumulative_pnl'])}；"
                f"摊薄保本成本{cost_text}；收盘{row['close']:.2f}元；持仓{row['lots']}手。"
            )
        lines.append(line)
    lines.extend([
        "",
        f"今日合计：{money(report['daily_pnl'])}",
        f"所列股票账本累计合计：{money(report['cumulative_pnl'])}",
        f"账本累计合计：{money(report['ledger_cumulative_pnl'])}",
        "说明：账本累计合计还包含未逐只列出的历史清仓股票已实现盈亏；未成交挂单不计入。",
    ])
    return "\n".join(lines)


def _send_daily_portfolio_pnl() -> None:
    """Send one consolidated, confirmed-close portfolio P&L message per day."""
    day = datetime.now().strftime("%Y-%m-%d")
    config = state.config
    channels = list(config.get("alert", {}).get("channels", []))
    if task_complete("daily_portfolio_pnl", day, channels):
        return
    report = _build_daily_portfolio_pnl(config, day)
    if report is None:
        return
    subject = f"每日收盘持仓盈亏 {day}｜今日{report['daily_pnl']:+.2f}元"
    if _deliver_persisted(
        "daily_portfolio_pnl", day, subject, _format_daily_portfolio_pnl(report), config
    ):
        app_logger.info("daily portfolio pnl sent for %s: %.2f", day, report["daily_pnl"])


def _send_candidate_price_digest(*, now: Optional[datetime] = None) -> None:
    """Ask the LLM to recalculate tomorrow's candidate prices from fresh data."""
    from .notifier import send_pushplus

    current = now or datetime.now()
    config = state.config
    day = current.strftime("%Y-%m-%d")
    task_name = "candidate_price_digest"
    if (
        not is_session_date(current, config)
        or current.time() < dt_time(15, 15)
        or not daily_close_confirmed(current)
        or task_channel_complete(task_name, day, "pushplus")
        or not config.get("use_llm_advice", False)
        or not LLM_AVAILABLE
    ):
        return
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    held_codes = {
        str(code)
        for code, position in positions.items()
        if int(replay_position(position, as_of=day).get("total_lots", 0) or 0) > 0
    }
    report = build_candidate_digest(
        day,
        held_codes=held_codes,
        market_risk=refresh_market_risk(config, now=current),
        analyzer=lambda analysis_day, inputs: generate_candidate_price_advice(
            analysis_day, inputs, config.get("llm") or {}
        ),
    )
    if report is None:
        return
    subject = f"候选股票次日参考买价（等待确认）{day}"
    sent = send_pushplus(config, subject, format_candidate_digest(report))
    mark_task_channel(task_name, day, "pushplus", bool(sent), detail=None if sent else "send failed")
    if sent:
        app_logger.info("candidate price digest sent for %s (%d symbols)", day, len(report["rows"]))


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
        if not kdj_alerts_enabled(config, code):
            continue
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

    has_data = False
    for symbol in _symbols_for_next_day_plan(config, today_str):

        code = symbol["code"]
        name = symbol.get("name") or code

        deterministic_plan = _build_deterministic_plan(
            symbol, config, today_str, for_next_session=True
        )
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

    lines.append(format_observation_discipline(config))
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


def _build_deterministic_plan(
    symbol: dict, config: dict, today_str: str, *, for_next_session: bool = False
) -> Optional[dict]:
    code = symbol["code"]
    latest_daily = (state.latest.get(code) or {}).get("1d")
    daily_series = (state.series.get(code) or {}).get("1d", [])
    positions = ((config.get("trade_plan") or {}).get("positions") or {})
    position = _position_for_code(positions, code)
    if not latest_daily or not daily_series or not position:
        return None
    plan = build_decision_plan(
        symbol_code=code,
        symbol_name=str(symbol.get("name") or code),
        latest_daily=latest_daily,
        daily_series=daily_series,
        position=position,
        decision_date=today_str,
        performance_state=get_performance(code)["summary"],
        intraday_series=(state.series.get(code) or {}).get("10m", []),
        intraday_execution_enabled=is_trading_time(),
        for_next_session=for_next_session,
        market_risk=current_market_risk(config),
    )
    plan["observation_discipline"] = observation_discipline(config)
    return plan


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
        "不使用MA均线；总持仓20%为反T额度，单次最多1手，卖出后必须先按盈利位补回。"
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
            if _session_close_boundary(now) is not None:
                try:
                    await asyncio.to_thread(_refresh_session_close, now)
                except Exception:
                    app_logger.exception("session-close reconciliation failed; will retry")
            # 收盘后15:10发送次日指引（非交易时段也执行）
            if is_session_date(now, state.config) and now.hour == 15 and now.minute >= 15:
                await asyncio.to_thread(_send_next_day_plan)
                await asyncio.to_thread(_send_daily_portfolio_pnl)
            if is_session_date(now, state.config) and now.time() >= dt_time(15, 15):
                await asyncio.to_thread(_send_candidate_price_digest, now=now)
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
            await asyncio.to_thread(_send_daily_portfolio_pnl)
            await asyncio.to_thread(_send_candidate_price_digest, now=now)

        await asyncio.sleep(interval)
