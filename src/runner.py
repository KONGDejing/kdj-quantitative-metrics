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


def run_once() -> None:
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


def _send_close_summary() -> None:
    """收盘后发送当日各股票 1d_est 盘中折算 KDJ 总结。"""
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

        # 判断当前 K 值相对最优阈值的位置
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


async def monitor_loop() -> None:
    interval = int(state.config.get("poll_interval_seconds", 60))
    was_trading = None
    # 收盘后/周末重启时内存状态为空，先补一轮数据，保证页面立即可用
    if not is_trading_time() and not state.latest:
        app_logger.info("initial monitor tick on startup (market closed, filling state)")
        await asyncio.to_thread(run_once)
    while True:
        trading = is_trading_time()
        if trading != was_trading:
            app_logger.info("monitor %s", "resumed (trading hours)" if trading else "paused (market closed)")
            was_trading = trading
        if not trading:
            await asyncio.sleep(interval)
            continue
        app_logger.info("start monitor tick")
        await asyncio.to_thread(run_once)

        # 收盘前10分钟（14:50-15:00）发送当日KDJ总结，给用户操作窗口
        now = datetime.now()
        if now.hour == 14 and now.minute >= 50:
            await asyncio.to_thread(_send_close_summary)

        await asyncio.sleep(interval)
