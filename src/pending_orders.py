from __future__ import annotations

from typing import Any


def active_pending_orders(
    position: dict[str, Any], as_of_date: str, *, for_next_session: bool = False
) -> list[dict[str, Any]]:
    """Only same-session open orders are live; A-share day orders do not roll over."""
    active = []
    for item in position.get("pending_orders") or []:
        if str(item.get("status") or "open").lower() != "open":
            continue
        placed_day = str(item.get("placed_at") or "")[:10]
        if placed_day and placed_day < as_of_date:
            continue
        if for_next_session and (not placed_day or placed_day <= as_of_date):
            continue
        active.append(dict(item))
    return active
