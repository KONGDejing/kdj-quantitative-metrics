from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional


# Official SSE/SZSE 2026 closure schedule. Weekend dates need not be repeated.
# Source: https://www.sse.com.cn/disclosure/dealinstruc/closed/
OFFICIAL_CLOSED_DATES: dict[int, set[str]] = {
    2026: {
        "2026-01-01", "2026-01-02",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
        "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    },
}


def _date(value: Optional[date | datetime | str] = None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def calendar_status(value: Optional[date | datetime | str] = None, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    day = _date(value)
    calendar_cfg = (config or {}).get("market_calendar") or {}
    open_overrides = {str(item)[:10] for item in calendar_cfg.get("open_dates", [])}
    closed_overrides = {str(item)[:10] for item in calendar_cfg.get("closed_dates", [])}
    text = day.isoformat()
    covered = day.year in OFFICIAL_CLOSED_DATES
    if text in open_overrides:
        session = True
        reason = "config_open_override"
        verified = True
    elif text in closed_overrides:
        session = False
        reason = "config_closed_override"
        verified = True
    elif day.weekday() >= 5:
        session = False
        reason = "weekend"
        verified = True
    elif covered:
        session = text not in OFFICIAL_CLOSED_DATES[day.year]
        reason = "official_session" if session else "official_holiday"
        verified = True
    else:
        fail_closed = bool(calendar_cfg.get("fail_closed_unknown_year", True))
        session = not fail_closed
        reason = "unknown_year_blocked" if fail_closed else "unknown_year_weekday_fallback"
        verified = False
    return {
        "date": text,
        "is_session": session,
        "verified": verified,
        "reason": reason,
        "coverage_years": sorted(OFFICIAL_CLOSED_DATES),
        "source": "SSE annual closure schedule",
    }


def is_session_date(value: Optional[date | datetime | str] = None, config: Optional[dict[str, Any]] = None) -> bool:
    return bool(calendar_status(value, config)["is_session"])


def next_session(value: Optional[date | datetime | str] = None, config: Optional[dict[str, Any]] = None) -> Optional[str]:
    current = _date(value)
    for offset in range(1, 370):
        candidate = current + timedelta(days=offset)
        status = calendar_status(candidate, config)
        if not status["verified"] and bool(((config or {}).get("market_calendar") or {}).get("fail_closed_unknown_year", True)):
            return None
        if status["is_session"]:
            return candidate.isoformat()
    return None
