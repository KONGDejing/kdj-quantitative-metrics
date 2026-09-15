from __future__ import annotations

from typing import Any


def kdj_alerts_enabled(config: dict[str, Any], symbol: str) -> bool:
    """Return whether KDJ may generate alerts/optimization for a symbol.

    KDJ values can still be calculated for charts when alerts are disabled.  This
    separation lets exceptional symbols use price/fundamental plans without an
    unsuitable KDJ backtest turning into a trading prompt.
    """
    policy = config.get("kdj_alerts") or {}
    if not bool(policy.get("enabled", True)):
        return False
    excluded = {str(code) for code in (policy.get("excluded_symbols") or [])}
    return str(symbol) not in excluded
