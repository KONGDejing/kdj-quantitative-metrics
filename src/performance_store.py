from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Optional

from .config import BASE_DIR
from .trade_ledger import LOT_SIZE, replay_position


RUNTIME_DIR = BASE_DIR / "runtime"
PERFORMANCE_PATH = RUNTIME_DIR / "strategy_performance.json"
_lock = Lock()
_PATH_FIELDS = {"high_water_equity", "drawdown_from_high_water"}


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "strategies": {}}


def _load_unlocked(path: Path = PERFORMANCE_PATH) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("strategies"), dict):
            return _empty_store()
        return data
    except (OSError, json.JSONDecodeError):
        return _empty_store()


def _save_unlocked(data: dict[str, Any], path: Path = PERFORMANCE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _same_snapshot(left: Optional[dict[str, Any]], right: dict[str, Any]) -> bool:
    if left is None:
        return False
    return (
        {key: value for key, value in left.items() if key not in _PATH_FIELDS}
        == {key: value for key, value in right.items() if key not in _PATH_FIELDS}
    )


def _snapshot(symbol: str, day: str, close: float, position: dict[str, Any]) -> dict[str, Any]:
    ledger = replay_position(position, as_of=day, strict=True)
    budget = float(position.get("strategy_budget", 0) or 0)
    market_value = float(ledger["total_lots"]) * LOT_SIZE * float(close)
    net_invested = float(ledger.get("net_cash_invested", 0) or 0)
    cash = budget - net_invested if budget else None
    equity = cash + market_value if cash is not None else None
    book_value = (
        float(ledger["total_lots"]) * LOT_SIZE * float(ledger["average_entry_cost"])
        if ledger.get("average_entry_cost") is not None else 0.0
    )
    return {
        "date": str(day)[:10],
        "symbol": str(symbol),
        "close": round(float(close), 4),
        "core_lots": ledger["core_lots"],
        "t_lots": ledger["t_lots"],
        "market_value": round(market_value, 2),
        "net_cash_invested": round(net_invested, 2),
        "cash": round(cash, 2) if cash is not None else None,
        "equity": round(equity, 2) if equity is not None else None,
        "sleeve_return": round(equity / budget - 1.0, 6) if equity is not None and budget else None,
        "deployed_position_return": round(market_value / net_invested - 1.0, 6) if net_invested > 0 else None,
        "inventory_return": round(market_value / book_value - 1.0, 6) if book_value else None,
        "realized_pnl": ledger["realized_pnl"],
    }


def _recompute_path_metrics(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    high_water: Optional[float] = None
    max_drawdown = 0.0
    max_drawdown_date: Optional[str] = None
    for item in snapshots:
        equity = item.get("equity")
        if equity is None:
            item["high_water_equity"] = None
            item["drawdown_from_high_water"] = None
            continue
        equity = float(equity)
        high_water = equity if high_water is None else max(high_water, equity)
        drawdown = equity / high_water - 1.0 if high_water else 0.0
        item["high_water_equity"] = round(high_water, 2)
        item["drawdown_from_high_water"] = round(drawdown, 6)
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            max_drawdown_date = item["date"]
    latest = snapshots[-1] if snapshots else None
    return {
        "first_date": snapshots[0]["date"] if snapshots else None,
        "last_date": latest["date"] if latest else None,
        "snapshot_count": len(snapshots),
        "latest": latest,
        "high_water_equity": round(high_water, 2) if high_water is not None else None,
        "current_drawdown": latest.get("drawdown_from_high_water") if latest else None,
        "max_drawdown": round(max_drawdown, 6),
        "max_drawdown_date": max_drawdown_date,
    }


def upsert_snapshot(
    symbol: str,
    day: str,
    close: float,
    position: dict[str, Any],
    *,
    path: Path = PERFORMANCE_PATH,
) -> dict[str, Any]:
    snapshot = _snapshot(symbol, day, close, position)
    with _lock:
        store = _load_unlocked(path)
        strategy = store["strategies"].setdefault(str(symbol), {"snapshots": []})
        by_date = {str(item.get("date")): item for item in strategy.get("snapshots") or []}
        if _same_snapshot(by_date.get(snapshot["date"]), snapshot):
            return strategy.get("summary") or _recompute_path_metrics(list(by_date.values()))
        by_date[snapshot["date"]] = snapshot
        snapshots = [by_date[key] for key in sorted(by_date)]
        strategy["snapshots"] = snapshots[-3000:]
        strategy["summary"] = _recompute_path_metrics(strategy["snapshots"])
        _save_unlocked(store, path)
        return strategy["summary"]


def backfill_snapshots(
    symbol: str,
    daily_series: list[dict[str, Any]],
    position: dict[str, Any],
    *,
    path: Path = PERFORMANCE_PATH,
    exclude_dates: Iterable[str] = (),
) -> dict[str, Any]:
    opening_day = str((position.get("opening") or {}).get("as_of") or "")[:10]
    relevant = [
        item for item in daily_series
        if str(item.get("timestamp") or item.get("date") or "")[:10] >= opening_day
    ]
    if not relevant:
        return get_performance(symbol, path=path)
    with _lock:
        store = _load_unlocked(path)
        strategy = store["strategies"].setdefault(str(symbol), {"snapshots": []})
        by_date = {str(item.get("date")): item for item in strategy.get("snapshots") or []}
        excluded = {str(value)[:10] for value in exclude_dates}
        changed = False
        for excluded_day in excluded:
            if by_date.pop(excluded_day, None) is not None:
                changed = True
        for bar in relevant:
            day = str(bar.get("timestamp") or bar.get("date") or "")[:10]
            snapshot = _snapshot(symbol, day, float(bar["close"]), position)
            if not _same_snapshot(by_date.get(day), snapshot):
                by_date[day] = snapshot
                changed = True
        if not changed:
            return strategy.get("summary") or _recompute_path_metrics(list(by_date.values()))
        strategy["snapshots"] = [by_date[key] for key in sorted(by_date)][-3000:]
        strategy["summary"] = _recompute_path_metrics(strategy["snapshots"])
        _save_unlocked(store, path)
        return strategy["summary"]


def get_performance(symbol: str, *, path: Path = PERFORMANCE_PATH) -> dict[str, Any]:
    with _lock:
        store = _load_unlocked(path)
        strategy = (store.get("strategies") or {}).get(str(symbol))
        if not strategy:
            return {"summary": _recompute_path_metrics([]), "snapshots": []}
        snapshots = list(strategy.get("snapshots") or [])
        summary = strategy.get("summary") or _recompute_path_metrics(snapshots)
        return {"summary": summary, "snapshots": snapshots}
