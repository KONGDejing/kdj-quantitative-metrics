from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from statistics import pstdev
from threading import Lock
from typing import Any, Iterable, Optional

from .backtest import FEE_RATE, SLIPPAGE
from .config import BASE_DIR


RUNTIME_DIR = BASE_DIR / "runtime"
SHADOW_PATH = RUNTIME_DIR / "shadow_decisions.json"
DEFAULT_HORIZONS = (5, 10, 20, 30, 60)
TRACKER_VERSION = 1
_lock = Lock()


def _day(value: Any) -> str:
    return str(value or "")[:10]


def classify_price_regime(closes: Iterable[float]) -> dict[str, Any]:
    """Classify the stock's price state using only values available at that close."""
    values = [float(value) for value in closes if value is not None and math.isfinite(float(value))]
    if len(values) < 60:
        return {
            "label": "insufficient",
            "close": round(values[-1], 4) if values else None,
            "ma20": None,
            "ma60": None,
            "return20": None,
            "volatility20": None,
        }
    close = values[-1]
    ma20 = sum(values[-20:]) / 20
    ma60 = sum(values[-60:]) / 60
    return20 = close / values[-21] - 1.0
    daily_returns = [values[index] / values[index - 1] - 1.0 for index in range(len(values) - 20, len(values))]
    volatility20 = pstdev(daily_returns) * math.sqrt(244) if len(daily_returns) > 1 else 0.0
    if volatility20 >= 0.45:
        label = "high_volatility"
    elif close > ma20 > ma60 and return20 > 0.03:
        label = "uptrend"
    elif close < ma20 < ma60 and return20 < -0.03:
        label = "downtrend"
    else:
        label = "range"
    return {
        "label": label,
        "close": round(close, 4),
        "ma20": round(ma20, 4),
        "ma60": round(ma60, 4),
        "return20": round(return20, 6),
        "volatility20": round(volatility20, 6),
    }


def classify_regime(bars: list[dict[str, Any]], signal_index: Optional[int] = None) -> dict[str, Any]:
    if signal_index is None:
        signal_index = len(bars) - 1
    if signal_index < 0:
        return classify_price_regime([])
    return classify_price_regime(float(item["close"]) for item in bars[:signal_index + 1])


def _load_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": TRACKER_VERSION, "records": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": TRACKER_VERSION, "records": []}
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return {"version": TRACKER_VERSION, "records": []}
    return data


def _save_unlocked(store: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _bars(daily_series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = []
    for raw in daily_series:
        day = _day(raw.get("timestamp") or raw.get("date") or raw.get("datetime"))
        try:
            bar = {
                "date": day,
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if day:
            prepared.append(bar)
    return sorted(prepared, key=lambda item: item["date"])


def _net_long_return(entry: float, exit_close: float) -> float:
    entry_price = entry * (1.0 + SLIPPAGE)
    exit_price = exit_close * (1.0 - SLIPPAGE)
    return exit_price / entry_price * (1.0 - FEE_RATE) ** 2 - 1.0


def _execution(record: dict[str, Any], bars: list[dict[str, Any]], signal_index: int) -> dict[str, Any]:
    existing = record.get("execution")
    if existing and existing.get("status") != "pending":
        return existing
    if signal_index + 1 >= len(bars):
        return {"status": "pending", "reason": "等待下一交易日开盘"}

    action = str((record.get("plan") or {}).get("decision", {}).get("action") or "review")
    next_bar = bars[signal_index + 1]
    if action == "buy_core":
        price_plan = (record.get("plan") or {}).get("price_plan") or {}
        lower = float(price_plan.get("lower", 0) or 0)
        upper = float(price_plan.get("upper", 0) or 0)
        chase = float(price_plan.get("do_not_chase_above", upper) or upper)
        invalidation = float(price_plan.get("invalidate_below", 0) or 0)
        next_open = float(next_bar["open"])
        if lower <= next_open <= upper and next_open <= chase and next_open >= invalidation:
            return {
                "status": "filled",
                "date": next_bar["date"],
                "raw_open": round(next_open, 4),
                "entry_price": round(next_open * (1.0 + SLIPPAGE), 4),
                "method": "next_open_inside_limit_zone",
            }
        if next_open > upper or next_open > chase:
            return {"status": "not_filled", "date": next_bar["date"], "reason": "次日开盘高于允许区间"}
        return {
            "status": "ambiguous",
            "date": next_bar["date"],
            "reason": "次日低开，只有日线OHLC无法可靠判断限价单与失效条件的先后顺序",
        }
    if action == "sell_tactical":
        return {
            "status": "filled",
            "date": next_bar["date"],
            "raw_open": round(float(next_bar["open"]), 4),
            "exit_price": round(float(next_bar["open"]) * (1.0 - SLIPPAGE), 4),
            "method": "next_open",
        }
    if action == "hold":
        return {"status": "counterfactual", "date": next_bar["date"], "raw_open": round(float(next_bar["open"]), 4)}
    return {"status": "unscored", "reason": f"动作{action}不做收益评分"}


def _evaluate_record(record: dict[str, Any], bars: list[dict[str, Any]]) -> bool:
    signal_date = str(record.get("signal_date") or "")
    signal_index = next((index for index, item in enumerate(bars) if item["date"] == signal_date), None)
    if signal_index is None:
        return False
    changed = False
    execution = _execution(record, bars, signal_index)
    if execution != record.get("execution"):
        record["execution"] = execution
        changed = True

    action = str((record.get("plan") or {}).get("decision", {}).get("action") or "review")
    results = record.setdefault("horizons", {})
    for horizon in record.get("evaluation_horizons") or DEFAULT_HORIZONS:
        key = str(int(horizon))
        target_index = signal_index + int(horizon)
        if target_index >= len(bars):
            continue
        if (results.get(key) or {}).get("status") == "evaluated":
            continue
        due = bars[target_index]
        evaluation: dict[str, Any] = {"status": "unscored", "due_date": due["date"]}
        next_index = signal_index + 1
        if next_index >= len(bars):
            continue
        future_bars = bars[next_index:target_index + 1]
        reference_open = float(bars[next_index]["open"])
        market_return = _net_long_return(reference_open, float(due["close"]))

        if action == "hold":
            evaluation.update({
                "status": "evaluated",
                "underlying_return": round(market_return, 6),
                "decision_score": round(-market_return, 6),
                "favorable": market_return <= 0,
                "meaning": "未加仓相对次日开盘买入的机会成本；仅评价增量动作，不评价已有核心仓",
            })
        elif action == "buy_core" and execution.get("status") == "filled":
            raw_entry = float(execution["raw_open"])
            entry_price = float(execution["entry_price"])
            net_multiplier = (1.0 - FEE_RATE) ** 2
            return_pct = float(due["close"]) * (1.0 - SLIPPAGE) / entry_price * net_multiplier - 1.0
            adverse = min(item["low"] for item in future_bars) * (1.0 - SLIPPAGE) / entry_price * net_multiplier - 1.0
            favorable = max(item["high"] for item in future_bars) * (1.0 - SLIPPAGE) / entry_price * net_multiplier - 1.0
            evaluation.update({
                "status": "evaluated",
                "underlying_return": round(return_pct, 6),
                "decision_score": round(return_pct, 6),
                "favorable": return_pct > 0,
                "max_adverse_excursion": round(adverse, 6),
                "max_favorable_excursion": round(favorable, 6),
                "raw_entry_open": round(raw_entry, 4),
            })
        elif action == "sell_tactical" and execution.get("status") == "filled":
            evaluation.update({
                "status": "evaluated",
                "underlying_return": round(market_return, 6),
                "decision_score": round(-market_return, 6),
                "favorable": market_return <= 0,
                "meaning": "卖出后相对继续持有的回避收益",
            })
        else:
            evaluation["reason"] = execution.get("reason") or "该动作没有可评分成交"
        results[key] = evaluation
        changed = True
    return changed


def _summary(records: list[dict[str, Any]], symbol: Optional[str] = None) -> dict[str, Any]:
    selected = [item for item in records if symbol is None or str(item.get("symbol")) == str(symbol)]
    by_horizon: dict[str, Any] = {}
    horizons = sorted({int(h) for record in selected for h in record.get("evaluation_horizons") or []})
    for horizon in horizons:
        evaluated = [
            record["horizons"][str(horizon)]
            for record in selected
            if (record.get("horizons") or {}).get(str(horizon), {}).get("status") == "evaluated"
        ]
        favorable = [item for item in evaluated if item.get("favorable") is True]
        scores = [float(item["decision_score"]) for item in evaluated if item.get("decision_score") is not None]
        returns = [float(item["underlying_return"]) for item in evaluated if item.get("underlying_return") is not None]
        by_horizon[str(horizon)] = {
            "evaluated": len(evaluated),
            "favorable": len(favorable),
            "favorable_rate": round(len(favorable) / len(evaluated), 6) if evaluated else None,
            "avg_decision_score": round(sum(scores) / len(scores), 6) if scores else None,
            "avg_underlying_return": round(sum(returns) / len(returns), 6) if returns else None,
        }
    fully_pending = sum(1 for record in selected if not any(
        item.get("status") == "evaluated" for item in (record.get("horizons") or {}).values()
    ))
    by_action: dict[str, int] = {}
    for record in selected:
        action = str((record.get("plan") or {}).get("decision", {}).get("action") or "unknown")
        by_action[action] = by_action.get(action, 0) + 1
    return {
        "records": len(selected),
        "pending_records": fully_pending,
        "by_action": by_action,
        "by_horizon": by_horizon,
        "latest": selected[-1] if selected else None,
        "sample_warning": "影子记录少于30次，不据此修改实盘规则" if len(selected) < 30 else None,
    }


def record_and_evaluate(
    plan: dict[str, Any],
    daily_series: list[dict[str, Any]],
    *,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    path: Path = SHADOW_PATH,
    recorded_at: Optional[str] = None,
) -> dict[str, Any]:
    bars = _bars(daily_series)
    symbol = str((plan.get("symbol") or {}).get("code") or "")
    signal_date = str(plan.get("signal_date") or "")
    version = str(plan.get("version", 0) or 0)
    decision_id = f"{symbol}:{signal_date}:v{version}"
    now = recorded_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    normalized_horizons = sorted({int(value) for value in horizons if int(value) > 0})
    signal_index = next((index for index, item in enumerate(bars) if item["date"] == signal_date), None)

    with _lock:
        store = _load_unlocked(path)
        records = store.setdefault("records", [])
        record = next((item for item in records if item.get("id") == decision_id), None)
        changed = False
        if record is None:
            record = {
                "id": decision_id,
                "symbol": symbol,
                "signal_date": signal_date,
                "decision_date": str(plan.get("decision_date") or ""),
                "recorded_at": now,
                "backfilled": _day(now) > signal_date,
                "tracker_version": TRACKER_VERSION,
                "evaluation_horizons": normalized_horizons,
                "regime": classify_regime(bars, signal_index) if signal_index is not None else classify_price_regime([]),
                "plan": plan,
                "execution": {"status": "pending"},
                "horizons": {},
            }
            records.append(record)
            records.sort(key=lambda item: (str(item.get("signal_date")), str(item.get("id"))))
            changed = True
        for item in records:
            if str(item.get("symbol")) == symbol:
                changed = _evaluate_record(item, bars) or changed
        store["updated_at"] = now
        store["summary"] = _summary(records)
        if changed:
            _save_unlocked(store, path)
        return {"summary": _summary(records, symbol), "records": [item for item in records if str(item.get("symbol")) == symbol]}


def get_shadow_decisions(symbol: Optional[str] = None, *, path: Path = SHADOW_PATH) -> dict[str, Any]:
    with _lock:
        store = _load_unlocked(path)
        records = list(store.get("records") or [])
        selected = records if symbol is None else [item for item in records if str(item.get("symbol")) == str(symbol)]
        return {"version": store.get("version", TRACKER_VERSION), "summary": _summary(records, symbol), "records": selected}
