"""Deterministic new-entry brakes, independent of model opinions and limit prices."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from math import isfinite
from typing import Any

from .data_provider import fetch_realtime_quotes
from .runtime_state import load_runtime_state, save_market_risk


DEFAULT_MARKET_RISK = {
    "enabled": True,
    "index_drop_limits": {"000001": 0.02, "399006": 0.03},
    "stock_drop_limit": 0.05,
    "max_quote_age_seconds": 180,
}


def risk_settings(config: dict) -> dict:
    return {**DEFAULT_MARKET_RISK, **(config.get("market_risk") or {})}


def quote_fresh(quote: dict, now: datetime, max_age: int = 180) -> bool:
    try:
        raw = str(quote.get("timestamp") or "")
        stamp = datetime.strptime(raw, "%Y%m%d%H%M%S") if raw.isdigit() else datetime.fromisoformat(raw)
        if stamp.date() != now.date() or stamp > now + timedelta(seconds=30):
            return False
        if time(11, 30) < now.time() < time(13) and stamp.time() >= time(11, 30):
            return True
        # The official final snapshot stays valid for that evening's next-day plan.
        if now.time() >= time(15, 1) and stamp.time() >= time(15, 0):
            return True
        return (now - stamp).total_seconds() <= max_age
    except (TypeError, ValueError):
        return False


def evaluate_market_risk(quotes: dict, *, now: datetime, config: dict, prior: dict | None = None) -> dict:
    settings = risk_settings(config)
    day = now.strftime("%Y-%m-%d")
    result: dict[str, Any] = {
        "date": day, "checked_at": now.isoformat(timespec="seconds"),
        "status": "normal", "block_new_buys": False, "reasons": [], "indices": {},
        "latched": False,
    }
    if not settings["enabled"]:
        result["status"] = "disabled"
        return result
    if prior and prior.get("date") == day and prior.get("latched"):
        result.update(status="blocked", block_new_buys=True, latched=True)
        result["reasons"] = list(prior.get("reasons") or [])
    missing = []
    for code, limit in settings["index_drop_limits"].items():
        code = str(code)
        quote = quotes.get(code) or {}
        try:
            change = float(quote["change_ratio"])
            price = float(quote["price"])
            valid = isfinite(change) and isfinite(price) and price > 0
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid or not quote_fresh(quote, now, int(settings["max_quote_age_seconds"])):
            missing.append(code)
            continue
        result["indices"][code] = dict(quote)
        if change <= -abs(float(limit)):
            reason = f"{quote.get('name') or code}跌{abs(change):.2%}，达到暂停新增线{abs(float(limit)):.1%}"
            if reason not in result["reasons"] and not result["latched"]:
                result["reasons"].append(reason)
            result.update(status="blocked", block_new_buys=True, latched=True)
    if missing and not result["latched"]:
        result.update(status="unavailable", block_new_buys=True)
        result["reasons"] = ["指数行情缺失或过期：" + "、".join(missing)]
    return result


def refresh_market_risk(config: dict, *, now: datetime | None = None, quotes: dict | None = None) -> dict:
    current = now or datetime.now()
    if quotes is None:
        try:
            quotes = fetch_realtime_quotes(risk_settings(config)["index_drop_limits"])
        except Exception:
            quotes = {}
    prior = load_runtime_state().get("market_risk") or {}
    result = evaluate_market_risk(quotes, now=current, config=config, prior=prior)
    return save_market_risk(result)


def current_market_risk(config: dict, *, now: datetime | None = None) -> dict:
    current = now or datetime.now()
    prior = load_runtime_state().get("market_risk") or {}
    return evaluate_market_risk(prior.get("indices") or {}, now=current, config=config, prior=prior)


def stock_entry_risk(quote: dict, config: dict) -> str | None:
    """A deep fall or invalid quote cannot become an entry merely by hitting a price."""
    try:
        change = float(quote["change_ratio"])
        if not isfinite(change):
            raise ValueError("invalid change")
    except (KeyError, TypeError, ValueError):
        return "个股当日涨跌幅缺失，无法校验急跌风险"
    limit = abs(float(risk_settings(config)["stock_drop_limit"]))
    if change <= -limit:
        return f"个股当日跌幅{abs(change):.2%}达到{limit:.1%}，暂停新增并复核支撑"
    return None
