from __future__ import annotations

import asyncio
from datetime import datetime, time as dt_time
from typing import Optional

import pandas as pd

from .data_provider import safe_fetch_kline
from .kdj import calculate_kdj
from .logger import app_logger
from .notifier import notify
from .state import state
from .strategy import check_kdj_signal

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
    if now.weekday() >= 5:  # 周末
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
        if daily_raw is not None and intraday_for_estimate is not None:
            estimated_daily = _daily_estimate_from_intraday(daily_raw, intraday_for_estimate)
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


def _trend_mode(daily_series: list[dict], k_val: float, d_val: float) -> dict:
    if len(daily_series) < 11:
        return {"mode": "震荡", "score": 0, "reasons": ["历史数据不足，按震荡模式保守执行"]}

    closes = [float(item["close"]) for item in daily_series]
    close = closes[-1]
    ret_5 = close / closes[-6] - 1.0
    ret_10 = close / closes[-11] - 1.0
    ma5_now = sum(closes[-5:]) / 5
    ma5_prev = sum(closes[-6:-1]) / 5

    reasons = []
    if ret_5 <= -0.06:
        reasons.append(f"5日跌幅{ret_5 * 100:.1f}%")
    if ret_10 <= -0.10:
        reasons.append(f"10日跌幅{ret_10 * 100:.1f}%")
    if close < ma5_now and ma5_now < ma5_prev:
        reasons.append("收盘低于5日线且5日线下行")
    if closes[-1] < closes[-2] < closes[-3] < closes[-4]:
        reasons.append("连续3天下跌")
    if k_val < d_val:
        reasons.append("K<D，动能偏弱")

    if len(reasons) >= 2:
        return {"mode": "防守", "score": len(reasons), "reasons": reasons}

    recent_low = min(closes[-8:-2])
    repair_reasons = []
    if close > recent_low and closes[-1] >= closes[-2]:
        repair_reasons.append("低点后不再创新低")
    if k_val >= d_val and k_val < 70:
        repair_reasons.append("K>=D，低位修复")
    if ret_5 > -0.03:
        repair_reasons.append(f"5日跌幅收敛到{ret_5 * 100:.1f}%")
    if len(repair_reasons) >= 2 and close < ma5_now * 1.03:
        return {"mode": "修复", "score": len(reasons), "reasons": repair_reasons}

    return {"mode": "震荡", "score": len(reasons), "reasons": reasons or ["未触发防守过滤"]}

def _today_trade_reports(position: dict, today_str: str) -> list[dict]:
    """Return today's manually reported trades for a position."""
    reports = position.get("trade_history") or []
    if not reports and position.get("last_report"):
        reports = [position["last_report"]]
    return [
        item for item in reports
        if str(item.get("reported_at", "")).startswith(today_str)
    ]


def _trade_side_text(side: str) -> str:
    return "买入" if side == "buy" else "卖出"


def _weighted_trade_price(reports: list[dict]) -> Optional[float]:
    total_lots = 0
    total_amount = 0.0
    for report in reports:
        price = report.get("price")
        if price is None:
            continue
        lots = int(report.get("lots", 0) or 0)
        if lots <= 0:
            continue
        total_lots += lots
        total_amount += float(price) * lots
    if total_lots <= 0:
        return None
    return total_amount / total_lots


def _pending_sell_reports(position: dict) -> list[dict]:
    """Match buybacks against previous sells LIFO and return sells still waiting to be bought back."""
    pending: list[dict] = []
    for report in position.get("trade_history") or []:
        side = report.get("side")
        lots = int(report.get("lots", 0) or 0)
        if lots <= 0:
            continue
        if side == "sell":
            pending.append({**report, "lots": lots})
            continue
        if side != "buy":
            continue
        remaining = lots
        while remaining > 0 and pending:
            last = pending[-1]
            last_lots = int(last.get("lots", 0) or 0)
            if last_lots <= remaining:
                remaining -= last_lots
                pending.pop()
            else:
                last["lots"] = last_lots - remaining
                remaining = 0
    return pending


def _position_for_code(positions: dict, code: str) -> dict:
    position_key = next((key for key in positions if str(key) == code), code)
    return positions.get(position_key, {}) or {}


def _has_position_for_code(positions: dict, code: str) -> bool:
    return any(str(key) == code for key in positions)

def _close_trade_plan(symbol: dict, kdj_config: dict, trade_plan_config: Optional[dict] = None,
                      today_str: Optional[str] = None) -> list[str]:
    code = symbol["code"]
    name = symbol.get("name") or code
    symbol_latest = state.latest.get(code, {})
    daily_series = state.series.get(code, {}).get("1d", [])
    latest_day = symbol_latest.get("1d") or symbol_latest.get("1d_est")
    latest_bar = daily_series[-1] if daily_series else {}
    thresholds = _best_thresholds(code, kdj_config)

    lines = [f"{name}({code})"]
    if not latest_day:
        lines.append("  今日无有效日线数据，今天不做。")
        return lines

    close_val = float(latest_day["close"])
    k_val = float(latest_day["k"])
    d_val = float(latest_day["d"])
    j_val = float(latest_day["j"])
    buy = float(thresholds["buy"])
    sell = float(thresholds["sell"])

    prev_close = close_val
    if len(daily_series) >= 2:
        prev_close = float(daily_series[-2]["close"])
    day_change_pct = (close_val / prev_close - 1.0) * 100 if prev_close else 0.0
    day_amp_pct = 0.0
    if latest_bar and prev_close:
        day_amp_pct = (float(latest_bar["high"]) - float(latest_bar["low"])) / prev_close * 100

    # 基于当日收盘价给出第二天可直接挂单的参考价
    buy_1p5 = round(close_val * 0.985, 2)
    buy_2p0 = round(close_val * 0.98, 2)
    buy_2p5 = round(close_val * 0.975, 2)
    sell_1p5 = round(close_val * 1.015, 2)
    sell_2p0 = round(close_val * 1.02, 2)
    sell_3p0 = round(close_val * 1.03, 2)

    position = _position_for_code(((trade_plan_config or {}).get("positions", {}) or {}), code)
    base_lots = int(position.get("base_lots", 0) or 0)
    base_remaining = int(position.get("base_lots_remaining", base_lots) or 0)
    t_lots_held = int(position.get("t_lots_held", 0) or 0)
    cost = position.get("cost")
    target_sell = position.get("target_sell")
    t_lots = int(position.get("t_lots", 1))
    max_t_lots = int(position.get("max_t_lots", max(2, t_lots * 2)))
    available_sell_lots = base_remaining + t_lots_held
    can_buy_lots = max(0, max_t_lots - t_lots_held)
    today_str = today_str or datetime.now().strftime("%Y-%m-%d")
    today_reports = _today_trade_reports(position, today_str)
    today_buys = [item for item in today_reports if item.get("side") == "buy"]
    today_sells = [item for item in today_reports if item.get("side") == "sell"]
    today_buy_lots = sum(int(item.get("lots", 0) or 0) for item in today_buys)
    today_sell_lots = sum(int(item.get("lots", 0) or 0) for item in today_sells)
    pending_reports = _pending_sell_reports(position)
    pending_total_lots = min(
        sum(int(item.get("lots", 0) or 0) for item in pending_reports),
        max(0, base_lots - base_remaining - t_lots_held),
    )
    net_sold_lots = max(0, today_sell_lots - today_buy_lots)
    pending_buyback_lots = min(
        max(net_sold_lots, pending_total_lots),
        max(0, base_lots - base_remaining - t_lots_held),
    )
    avg_sell_price = _weighted_trade_price(today_sells if net_sold_lots else pending_reports)
    buyback_1p5 = round(avg_sell_price * 0.985, 2) if avg_sell_price else None
    buyback_2p0 = round(avg_sell_price * 0.98, 2) if avg_sell_price else None
    buyback_lots = min(t_lots, pending_buyback_lots)
    trend = _trend_mode(daily_series, k_val, d_val)
    mode = trend["mode"]
    reason_text = "、".join(trend["reasons"][:3])

    if base_lots and cost:
        pnl_pct = (close_val / float(cost) - 1.0) * 100
        position_text = f"持仓：底仓{base_remaining}/{base_lots}手，T仓{t_lots_held}手，成本{float(cost):.2f}，浮盈{pnl_pct:+.2f}%"
        if target_sell:
            target_gap = (float(target_sell) / close_val - 1.0) * 100
            position_text += f"；目标{float(target_sell):.2f}，还差{target_gap:.2f}%"
        lines.append(position_text)

    if today_reports:
        lines.append("今日成交：")
        for report in today_reports:
            price_text = f"，成交价{float(report['price']):.2f}" if report.get("price") is not None else ""
            fee_text = f"，手续费{float(report['fee']):.2f}" if report.get("fee") is not None else ""
            note_text = f"（{report['note']}）" if report.get("note") else ""
            lines.append(
                f"- 已{_trade_side_text(str(report.get('side')))}{int(report.get('lots', 0) or 0)}手"
                f"{price_text}{fee_text}{note_text}"
            )
        if today_buy_lots:
            lines.append(f"今日买入说明：今天买入{today_buy_lots}手记为T仓，下一交易日不能卖。")
        if pending_buyback_lots:
            if avg_sell_price:
                lines.append(
                    f"成交后状态：当前底仓{base_remaining}/{base_lots}手，T仓{t_lots_held}手，"
                    f"仍有{pending_buyback_lots}手待补回；参考买回价={avg_sell_price:.2f}×0.985={buyback_1p5:.2f}。"
                )
            else:
                lines.append(
                    f"成交后状态：当前底仓{base_remaining}/{base_lots}手，T仓{t_lots_held}手，"
                    f"仍有{pending_buyback_lots}手待补回；缺少卖出价，需按实际成交价计算买回价。"
                )
        else:
            lines.append(f"成交后状态：当前底仓{base_remaining}/{base_lots}手，T仓{t_lots_held}手，无待补回仓位。")

    lines.append(f"收盘：{close_val:.2f}（较前收 {day_change_pct:+.2f}%），K={k_val:.2f}，振幅{day_amp_pct:.2f}%")
    lines.append(f"趋势模式：{mode}（{reason_text}）")
    if pending_buyback_lots:
        lines.append("最终执行版：")
        buyback_text = (
            f"{buyback_1p5:.2f}附近" if buyback_1p5 is not None
            else "按实际卖出价回落1.5%的位置"
        )
        deeper_buyback_text = (
            f"{buyback_2p0:.2f}附近" if buyback_2p0 is not None
            else "按实际卖出价回落2.0%的位置"
        )
        lines.extend([
            f"1）核心任务：优先补回今日卖出的{buyback_lots}手，恢复底仓；这不是额外加仓。",
            f"2）补回：回落到{buyback_text}且止跌，买回{buyback_lots}手；若急跌到{deeper_buyback_text}，先确认不破位再补。",
            "3）高开/冲高：不追补；只有重新冲高且仍有可卖老仓时，才按盘中计划少量兑现。",
            "4）不做：不回落不买回；低开低走、重新跌破近期低点、或K重新下穿D时，即使到价也先不补。",
        ])
        lines.append("5）T+1：明天买回的仓位，后天才能卖；明天可卖的是今天以前持有的老仓。")
        if target_sell:
            lines.append(f"底仓原则：目标仍看{float(target_sell):.2f}附近，本次买回只是恢复今日卖出的底仓。")
        return lines

    lines.append("最终执行版：")

    if mode == "防守":
        lines.extend([
            "1）低开/下跌：不挂低吸买单，不做反T，避免越跌越补。",
            f"2）高开/冲高：涨1.5%到{sell_1p5:.2f}卖{min(t_lots, available_sell_lots)}手老仓；涨2.0%到{sell_2p0:.2f}最多再卖{min(t_lots, max(0, available_sell_lots - t_lots))}手；涨3.0%到{sell_3p0:.2f}不追，偏兑现。" if available_sell_lots else "2）高开/冲高：当前无可卖老仓，不卖。",
            f"3）卖出后买回：只在从卖出价回落2.0%后买回{t_lots}手；不回落不买回。",
            "4）不做：低开低走、继续破位、没有可卖老仓、或全天反弹无量。",
        ])
    elif mode == "修复":
        buy_lots = min(t_lots, can_buy_lots)
        lines.extend([
            f"1）低开/下跌：跌1.5%到{buy_1p5:.2f}先观察；跌2.0%到{buy_2p0:.2f}且止跌，最多买{buy_lots}手；不连续加仓。" if buy_lots else "1）低开/下跌：T仓已满，不再买。",
            f"2）高开/冲高：涨1.5%到{sell_1p5:.2f}卖{min(t_lots, available_sell_lots)}手老仓；涨2.0%到{sell_2p0:.2f}不追，偏兑现。" if available_sell_lots else "2）高开/冲高：当前无可卖老仓，不卖。",
            f"3）卖出后买回：从卖出价回落1.5%买回{t_lots}手；不回落不买回。",
            "4）不做：重新跌破近期低点、振幅不足2%、或K重新下穿D。",
        ])
    else:
        first_buy_lots = min(t_lots, can_buy_lots)
        second_buy_lots = min(t_lots, max(0, can_buy_lots - first_buy_lots))
        first_sell_lots = min(t_lots, available_sell_lots)
        second_sell_lots = min(t_lots, max(0, available_sell_lots - first_sell_lots))
        lines.extend([
            f"1）低开/下跌：跌1.5%到{buy_1p5:.2f}先不急；跌2.0%到{buy_2p0:.2f}买{first_buy_lots}手；继续跌2.5%到{buy_2p5:.2f}再买{second_buy_lots}手；全天最多买{can_buy_lots}手。" if can_buy_lots else "1）低开/下跌：T仓已满，不再买。",
            f"2）高开/冲高：涨1.5%到{sell_1p5:.2f}卖{first_sell_lots}手老仓；涨2.0%到{sell_2p0:.2f}最多再卖{second_sell_lots}手；涨3.0%到{sell_3p0:.2f}不追，偏兑现。" if available_sell_lots else "2）高开/冲高：当前无可卖老仓，不卖。",
            f"3）卖出后买回：从卖出价回落1.5%买回{t_lots}手；回落2.0%买回剩余T仓；不回落就不买回。",
            "4）不做：振幅不足2%；低开后继续破位不止跌；没有可卖老仓；价格卡在中间不上不下。",
        ])

    lines.append("5）T+1：明天卖的是今天以前持有的老仓；明天新买的仓位，后天才能卖。")
    if target_sell:
        lines.append(f"底仓原则：{base_lots}手底仓继续等{float(target_sell):.2f}附近，日内只动T仓。")
    return lines


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

    from .notifier import send_email, send_pushplus
    channels = config.get("alert", {}).get("channels", [])
    if "email" in channels:
        send_email(config, subject, content)
    if "pushplus" in channels:
        send_pushplus(config, subject, content)

    _close_summary_sent_date = today_str
    app_logger.info("close summary sent for %s (%d symbols)", today_str, len(state.symbols))


def _send_next_day_plan() -> None:
    """收盘后发送次日 T+1 操作指引。"""
    global _next_day_plan_sent_date
    today_str = datetime.now().strftime("%Y-%m-%d")
    if _next_day_plan_sent_date == today_str:
        return

    config = state.config
    kdj_config = config.get("kdj", {})
    trade_plan_config = config.get("trade_plan", {})
    lines = ["次日T+1操作指引", f"日期：{today_str}", ""]

    configured_positions = (trade_plan_config.get("positions", {}) or {})
    has_data = False
    for symbol in state.symbols:
        if not _has_position_for_code(configured_positions, symbol["code"]):
            continue
        plan_lines = _close_trade_plan(symbol, kdj_config, trade_plan_config, today_str=today_str)
        if len(plan_lines) > 1:
            has_data = True
        lines.extend(plan_lines)
        lines.append("")

    if not has_data:
        lines.append("（无已配置交易计划的有效日线数据）")

    lines.append("说明：以上为收盘后第二天的挂单参考价，只做提醒，不自动下单。")

    content = "\n".join(lines)
    subject = f"次日T+1操作指引 {today_str}"

    from .notifier import send_email, send_pushplus
    channels = config.get("alert", {}).get("channels", [])
    if "email" in channels:
        send_email(config, subject, content)
    if "pushplus" in channels:
        send_pushplus(config, subject, content)

    _next_day_plan_sent_date = today_str
    app_logger.info("next day plan sent for %s (%d symbols)", today_str, len(state.symbols))


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
            if now.weekday() < 5 and now.hour >= 15:
                await asyncio.to_thread(_send_next_day_plan)
            await asyncio.sleep(interval)
            continue
        app_logger.info("start monitor tick")
        await asyncio.to_thread(run_once)

        # 收盘前10分钟（14:50-15:00）发送当日KDJ总结，给用户操作窗口
        now = datetime.now()
        if now.hour == 14 and now.minute >= 50:
            await asyncio.to_thread(_send_close_summary)

        # 收盘后发送次日 T+1 操作指引
        if now.hour >= 15 and now.weekday() < 5:
            await asyncio.to_thread(_send_next_day_plan)

        await asyncio.sleep(interval)
