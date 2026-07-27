from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertSignal:
    symbol: str
    name: str
    timeframe: str
    direction: str
    k: float
    d: float
    j: float
    close: float
    timestamp: str


def check_kdj_signal(symbol: dict, timeframe: str, latest: dict, upper: float, lower: float) -> AlertSignal | None:
    k = float(latest["k"])
    if k >= upper:
        direction = "high"
    elif k <= lower:
        direction = "low"
    else:
        return None

    return AlertSignal(
        symbol=symbol["code"],
        name=symbol.get("name") or symbol["code"],
        timeframe=timeframe,
        direction=direction,
        k=k,
        d=float(latest["d"]),
        j=float(latest["j"]),
        close=float(latest["close"]),
        timestamp=str(latest.get("datetime") or latest.get("date") or ""),
    )
