"""Independent quote polling and honest display freshness (not trading signals)."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from math import isfinite

from .data_provider import fetch_realtime_quotes
from .logger import app_logger
from .trading_calendar import is_session_date


def quote_time(value: object) -> datetime | None:
    try:
        raw = str(value or "")
        return datetime.strptime(raw, "%Y%m%d%H%M%S") if raw.isdigit() else datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def market_session(now: datetime, config: dict) -> dict:
    phase, label = "closed", "休市"
    if is_session_date(now, config):
        clock = now.time()
        if time(9, 30) <= clock <= time(11, 30) or time(13) <= clock <= time(15):
            phase, label = "trading", "交易中"
        elif time(11, 30) < clock < time(13):
            phase, label = "lunch", "午间休市"
        elif clock > time(15):
            phase, label = "closed", "已收盘"
        else:
            phase, label = "preopen", "开盘前"
    return {"phase": phase, "label": label, "server_time": now.isoformat(timespec="seconds")}


def quote_view(quote: dict | None, now: datetime, config: dict) -> dict:
    result = dict(quote or {})
    result.update(status="missing", status_label="等待实时报价", stale=True)
    stamp = quote_time(result.get("timestamp"))
    try:
        price = float(result["price"])
        valid = isfinite(price) and price > 0
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid or stamp is None or stamp > now + timedelta(seconds=30):
        result.pop("price", None)
        return result
    result["quote_time"] = stamp.strftime("%Y-%m-%d %H:%M:%S")
    session = market_session(now, config)["phase"]
    age = (now - stamp).total_seconds()
    limit = max(30, int(config.get("quote_max_age_seconds", 90)))
    if stamp.date() != now.date():
        result.update(status="historical", status_label="历史报价，非今日现价")
    elif session == "lunch" and stamp.time() >= time(11, 30):
        result.update(status="session_close", status_label="午间收盘", stale=False)
    elif session == "closed" and stamp.time() >= time(15):
        result.update(status="session_close", status_label="收盘行情", stale=False)
    elif session == "lunch" or (session == "closed" and now.time() > time(15)):
        result.update(status="stale", status_label="等待收尾行情")
    elif age > limit:
        result.update(status="stale", status_label="行情滞后")
    else:
        result.update(status="fresh", status_label="实时行情" if session == "trading" else "盘前行情", stale=False)
    return result


def poll_quotes_once() -> None:
    # Lazy imports keep AppState's display helpers independent of the runner.
    from .state import state
    from .market_risk import refresh_market_risk, risk_settings

    codes = list(dict.fromkeys(
        [str(s["code"]) for s in list(state.symbols)]
        + list(risk_settings(state.config)["index_drop_limits"])
    ))
    try:
        quotes = fetch_realtime_quotes(codes)
        missing = [code for code in codes if not quotes.get(code)]
        state.update_quotes(quotes, error="部分实时报价缺失" if missing else None)
        refresh_market_risk(state.config, quotes=quotes)
    except Exception as exc:
        state.update_quotes({}, error="实时报价拉取失败")
        app_logger.warning("independent quote refresh failed: %s", exc)


async def quote_loop() -> None:
    from .state import state

    previous_codes = None
    while True:
        now = datetime.now()
        codes = tuple(str(s["code"]) for s in list(state.symbols))
        # Lunch still polls: the last trade must not freeze at 11:28. Outside
        # market hours fetch once on startup/watchlist changes, not all night.
        market_window = is_session_date(now, state.config) and time(9, 25) <= now.time() <= time(15, 15)
        if market_window or codes != previous_codes:
            await asyncio.to_thread(poll_quotes_once)
            previous_codes = codes
        interval = max(5, int(state.config.get("quote_poll_interval_seconds", 15))) if market_window else 60
        await asyncio.sleep(interval)
