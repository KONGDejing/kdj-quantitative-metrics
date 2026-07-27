from __future__ import annotations

import asyncio
from datetime import datetime, time as dt_time

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


def is_trading_time(now: datetime | None = None) -> bool:
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


def run_once() -> None:
    config = state.config
    kdj_config = config.get("kdj", {})
    cooldown_seconds = int(config.get("alert", {}).get("cooldown_seconds", 600))

    for symbol in list(state.symbols):
        for timeframe in config.get("timeframes", []):
            data = safe_fetch_kline(symbol["code"], timeframe)
            if data is None or data.empty:
                continue

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
            latest_view = {
                "symbol": symbol["code"],
                "name": symbol.get("name") or symbol["code"],
                "timeframe": timeframe,
                "close": round(float(latest["close"]), 4),
                "k": round(float(latest["k"]), 2),
                "d": round(float(latest["d"]), 2),
                "j": round(float(latest["j"]), 2),
                "timestamp": str(latest.get("datetime") or latest.get("date") or ""),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
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

            signal_key = f"{symbol['code']}:{timeframe}"
            signal = check_kdj_signal(
                symbol,
                timeframe,
                latest,
                upper=float(kdj_config.get("upper", 80)),
                lower=float(kdj_config.get("lower", 20)),
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
            }
            notify(config, alert)
            state.add_alert(alert)


async def monitor_loop() -> None:
    interval = int(state.config.get("poll_interval_seconds", 60))
    was_trading = None
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
        await asyncio.sleep(interval)
