from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from .config import BASE_DIR
from .performance_store import get_performance
from .shadow_tracker import get_shadow_decisions
from .trade_ledger import LOT_SIZE, replay_position
from .walk_forward import load_report


RUNTIME_DIR = BASE_DIR / "runtime"


def report_path(symbol: str) -> Path:
    return RUNTIME_DIR / f"stage_capital_{symbol}.json"


def _non_overlapping(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    last_exit = ""
    for event in sorted(events, key=lambda item: str(item.get("entry_date") or "")):
        entry = str(event.get("entry_date") or "")
        exit_day = str(event.get("exit_date") or "")
        if not entry or not exit_day or (last_exit and entry <= last_exit):
            continue
        selected.append(event)
        last_exit = exit_day
    return selected


def _stage_curve(
    events: list[dict[str, Any]],
    lots: int,
    *,
    budget: float,
    max_deployed_ratio: float,
) -> dict[str, Any]:
    equity = budget
    peak = budget
    max_drawdown = 0.0
    executed = 0
    skipped_capital = 0
    wins = 0
    curve = [{"date": events[0]["entry_date"] if events else None, "equity": round(equity, 2)}]
    returns: list[float] = []
    adverse_values: list[float] = []
    first_date = str(events[0]["entry_date"]) if events else None
    last_date = str(events[-1]["exit_date"]) if events else None

    for event in events:
        entry_price = float(event["entry_price"])
        notional = entry_price * LOT_SIZE * lots
        if notional > equity * max_deployed_ratio:
            skipped_capital += 1
            continue
        event_return = float(event["return_pct"])
        adverse = float(event["max_adverse_excursion"])
        trough_equity = equity + notional * adverse
        drawdown = trough_equity / peak - 1.0 if peak else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        pnl = notional * event_return
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0 if peak else 0.0)
        executed += 1
        wins += int(pnl > 0)
        returns.append(pnl / budget)
        adverse_values.append(notional * adverse / budget)
        curve.append({
            "date": event["exit_date"],
            "equity": round(equity, 2),
            "event_pnl": round(pnl, 2),
            "signal_date": event["signal_date"],
            "price_regime": event.get("price_regime"),
        })

    years = 0.0
    if first_date and last_date:
        years = max(0.0, (date.fromisoformat(last_date) - date.fromisoformat(first_date)).days / 365.25)
    total_return = equity / budget - 1.0 if budget else 0.0
    annualized = (equity / budget) ** (1.0 / years) - 1.0 if years > 0 and equity > 0 else None
    return {
        "lots": lots,
        "executed_events": executed,
        "skipped_for_capital": skipped_capital,
        "win_rate": round(wins / executed, 6) if executed else None,
        "final_equity": round(equity, 2),
        "total_return": round(total_return, 6),
        "annualized_return": round(annualized, 6) if annualized is not None else None,
        "max_drawdown": round(max_drawdown, 6),
        "avg_event_sleeve_return": round(sum(returns) / len(returns), 6) if returns else None,
        "worst_event_sleeve_mae": round(min(adverse_values), 6) if adverse_values else None,
        "curve": curve,
    }


def build_stage_report(symbol: str, position: dict[str, Any]) -> dict[str, Any]:
    research = load_report(str(symbol))
    if research is None:
        raise RuntimeError("缺少walk-forward报告")
    raw_events = list(research.get("current_rule_oos_events") or [])
    events = _non_overlapping(raw_events)
    budget = float(position.get("strategy_budget", 0) or 0)
    if budget <= 0:
        raise RuntimeError("策略资金预算未配置")
    max_deployed_ratio = float(position.get("max_deployed_ratio", 0.85) or 0.85)
    configured_stages = position.get("expansion_stages") or [15, 20, 30, 40]
    ledger = replay_position(position, as_of=date.today().isoformat(), strict=True)
    current_lots = int(ledger.get("core_lots", 0) or 0)
    stages = sorted({current_lots, *(int(value) for value in configured_stages if int(value) > 0)})
    stage_results = [_stage_curve(events, lots, budget=budget, max_deployed_ratio=max_deployed_ratio) for lots in stages]

    next_stage = next((value for value in stages if value > current_lots), current_lots)
    performance = get_performance(str(symbol))["summary"]
    latest_snapshot = performance.get("latest") or {}
    current_price = float(latest_snapshot.get("close") or ledger.get("breakeven_cost") or 0)
    shadow = get_shadow_decisions(str(symbol))["summary"]
    shadow_latest = shadow.get("latest") or {}
    shadow_30 = (shadow.get("by_horizon") or {}).get("30") or {}
    current_oos = research.get("current_rule_oos") or {}
    fundamental_status = str((position.get("fundamental_gate") or {}).get("status") or "unknown").lower()
    next_stage_cost = next_stage * LOT_SIZE * current_price
    current_drawdown = abs(float(performance.get("current_drawdown") or 0))
    gates = [
        {"name": "ledger", "passed": bool((ledger.get("validation") or {}).get("ok")), "detail": "交易账本必须一致"},
        {"name": "capital", "passed": next_stage_cost <= budget * max_deployed_ratio, "detail": f"按最新正式收盘估算，{next_stage}手占资金池{next_stage_cost / budget:.2%}"},
        {"name": "drawdown", "passed": current_drawdown < float(position.get("drawdown_pause", 0.10) or 0.10), "detail": f"当前回撤{current_drawdown:.2%}"},
        {"name": "oos_samples", "passed": int(current_oos.get("events", 0) or 0) >= 30, "detail": f"当前规则样本外{int(current_oos.get('events', 0) or 0)}次"},
        {"name": "oos_tail", "passed": float(current_oos.get("p10_return") or -1) > -0.15, "detail": f"样本外10%分位{float(current_oos.get('p10_return') or -1):.2%}"},
        {"name": "fold_stability", "passed": float(research.get("fold_profit_ratio") or 0) >= 0.60, "detail": f"盈利测试窗口比例{float(research.get('fold_profit_ratio') or 0):.2%}"},
        {"name": "shadow_samples", "passed": int(shadow_30.get("evaluated", 0) or 0) >= 10, "detail": f"30日到期影子样本{int(shadow_30.get('evaluated', 0) or 0)}/10"},
        {"name": "shadow_quality", "passed": float(shadow_30.get("favorable_rate") or 0) >= 0.60, "detail": f"30日影子正向率{float(shadow_30.get('favorable_rate') or 0):.2%}"},
        {"name": "fundamental", "passed": fundamental_status == "pass", "detail": f"基本面闸门={fundamental_status}；阶段晋级要求pass"},
    ]
    advance_allowed = next_stage > current_lots and all(bool(item["passed"]) for item in gates)
    return {
        "version": 1,
        "symbol": str(symbol),
        "source_signal_date": shadow_latest.get("signal_date"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": {
            "type": "non_overlapping_oos_event_sleeve_curve",
            "budget": budget,
            "max_deployed_ratio": max_deployed_ratio,
            "source_events": len(raw_events),
            "non_overlapping_events": len(events),
            "note": "固定使用当前实盘确认规则；每个独立事件持有30交易日，空档资金计现金，不优化手数，不等同于持续持有核心仓的收益预测",
        },
        "current_stage_lots": current_lots,
        "next_stage_lots": next_stage,
        "stages": stage_results,
        "advancement": {
            "allowed": advance_allowed,
            "action": "advance_stage" if advance_allowed else "hold_current_stage",
            "failed_gates": [item["name"] for item in gates if not item["passed"]],
            "gates": gates,
            "auto_apply": False,
        },
    }


def save_report(report: dict[str, Any], *, path: Optional[Path] = None) -> Path:
    target = path or report_path(str(report["symbol"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def refresh_stage_report(symbol: str, position: dict[str, Any]) -> dict[str, Any]:
    report = build_stage_report(str(symbol), position)
    save_report(report)
    return report


def load_stage_report(symbol: str, *, path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    target = path or report_path(str(symbol))
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
