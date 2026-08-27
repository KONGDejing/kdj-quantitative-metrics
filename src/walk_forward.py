from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .backtest import FEE_RATE, SLIPPAGE
from .config import BASE_DIR
from .kdj import calculate_kdj
from .shadow_tracker import classify_price_regime


RUNTIME_DIR = BASE_DIR / "runtime"
BUY_K_GRID = [10.0, 12.5, 15.0]
LOOKBACK_GRID = [1, 3, 5]
CONFIRMATION_GRID = [1, 2, 3]
REBOUND_K_GRID = [20.0, 25.0, 30.0]


def report_path(symbol: str) -> Path:
    return RUNTIME_DIR / f"walk_forward_{symbol}.json"


def prepare_daily(df: pd.DataFrame) -> pd.DataFrame:
    data = calculate_kdj(df.copy()).reset_index(drop=True)
    data["date"] = pd.to_datetime(data["date"]).dt.strftime("%Y-%m-%d")
    closes = data["close"].astype(float).tolist()
    data["price_regime"] = [
        classify_price_regime(closes[:index + 1])["label"]
        for index in range(len(closes))
    ]
    return data


def _confirmations(data: pd.DataFrame, index: int) -> tuple[int, list[str]]:
    if index < 1:
        return 0, []
    current = data.iloc[index]
    previous = data.iloc[index - 1]
    passed: list[str] = []
    if float(current["k"]) > float(previous["k"]):
        passed.append("k_turn_up")
    if float(current["k"]) >= float(current["d"]) and float(previous["k"]) < float(previous["d"]):
        passed.append("k_cross_d")
    if float(current["close"]) >= float(previous["close"]):
        passed.append("close_not_lower")
    prior = data.iloc[max(0, index - 3):index]
    if not prior.empty and float(current["low"]) >= float(prior["low"].min()):
        passed.append("no_new_recent_low")
    return len(passed), passed


def find_events(
    data: pd.DataFrame,
    params: dict[str, Any],
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    horizon: int = 30,
    cooldown: int = 10,
    fee_rate: float = FEE_RATE,
    slippage: float = SLIPPAGE,
) -> list[dict[str, Any]]:
    """Find independent confirmed entry events and evaluate next-open to future-close returns."""
    buy_k = float(params["buy_k"])
    lookback = int(params["oversold_lookback"])
    confirmation_min = int(params["confirmation_min"])
    rebound_k_max = float(params["rebound_k_max"])
    events: list[dict[str, Any]] = []
    next_allowed_index = 1
    last_signal_index = len(data) - horizon - 1
    dates = data["date"].astype(str).to_numpy()
    opens = data["open"].astype(float).to_numpy()
    closes = data["close"].astype(float).to_numpy()
    highs = data["high"].astype(float).to_numpy()
    lows = data["low"].astype(float).to_numpy()
    k_values = data["k"].astype(float).to_numpy()
    d_values = data["d"].astype(float).to_numpy()
    regimes = data["price_regime"].astype(str).to_numpy() if "price_regime" in data else None

    for index in range(1, max(1, last_signal_index + 1)):
        signal_date = str(dates[index])
        exit_index = index + horizon
        exit_date = str(dates[exit_index])
        if start_date and signal_date < start_date:
            continue
        if end_date and (signal_date > end_date or exit_date > end_date):
            continue
        if index < next_allowed_index:
            continue
        recent_k = k_values[max(0, index - lookback + 1):index + 1]
        if not len(recent_k) or float(recent_k.min()) >= buy_k:
            continue
        if float(k_values[index]) > rebound_k_max:
            continue
        confirmation_names: list[str] = []
        if k_values[index] > k_values[index - 1]:
            confirmation_names.append("k_turn_up")
        if k_values[index] >= d_values[index] and k_values[index - 1] < d_values[index - 1]:
            confirmation_names.append("k_cross_d")
        if closes[index] >= closes[index - 1]:
            confirmation_names.append("close_not_lower")
        prior_lows = lows[max(0, index - 3):index]
        if len(prior_lows) and lows[index] >= float(prior_lows.min()):
            confirmation_names.append("no_new_recent_low")
        confirmation_count = len(confirmation_names)
        if confirmation_count < confirmation_min:
            continue

        entry_index = index + 1
        entry_price = float(opens[entry_index]) * (1.0 + slippage)
        exit_price = float(closes[exit_index]) * (1.0 - slippage)
        net_multiplier = (1.0 - fee_rate) ** 2
        return_pct = exit_price / entry_price * net_multiplier - 1.0
        adverse = float(lows[entry_index:exit_index + 1].min()) * (1.0 - slippage) / entry_price * net_multiplier - 1.0
        favorable = float(highs[entry_index:exit_index + 1].max()) * (1.0 - slippage) / entry_price * net_multiplier - 1.0
        events.append({
            "signal_date": signal_date,
            "entry_date": str(dates[entry_index]),
            "exit_date": exit_date,
            "signal_k": round(float(k_values[index]), 4),
            "confirmation_count": confirmation_count,
            "confirmations": confirmation_names,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "return_pct": round(return_pct, 6),
            "max_adverse_excursion": round(adverse, 6),
            "max_favorable_excursion": round(favorable, 6),
            "price_regime": str(regimes[index]) if regimes is not None else "unknown",
        })
        next_allowed_index = index + cooldown
    return events


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {
            "events": 0, "win_rate": None, "avg_return": None, "median_return": None,
            "p10_return": None, "worst_return": None, "avg_mae": None, "worst_mae": None,
            "avg_mfe": None, "score": None,
        }
    returns = pd.Series([float(item["return_pct"]) for item in events])
    adverse = pd.Series([float(item["max_adverse_excursion"]) for item in events])
    favorable = pd.Series([float(item["max_favorable_excursion"]) for item in events])
    avg_return = float(returns.mean())
    median_return = float(returns.median())
    p10_return = float(returns.quantile(0.10))
    score = avg_return + median_return + 0.5 * p10_return + 0.25 * float(adverse.mean())
    return {
        "events": len(events),
        "win_rate": round(float((returns > 0).mean()), 6),
        "avg_return": round(avg_return, 6),
        "median_return": round(median_return, 6),
        "p10_return": round(p10_return, 6),
        "worst_return": round(float(returns.min()), 6),
        "avg_mae": round(float(adverse.mean()), 6),
        "worst_mae": round(float(adverse.min()), 6),
        "avg_mfe": round(float(favorable.mean()), 6),
        "score": round(score, 6),
    }


def summarize_events_by_regime(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    labels = ["uptrend", "downtrend", "range", "high_volatility", "insufficient", "unknown"]
    grouped = {
        label: summarize_events([item for item in events if str(item.get("price_regime") or "unknown") == label])
        for label in labels
    }
    return {label: metrics for label, metrics in grouped.items() if metrics["events"] > 0}


def _candidate_params() -> list[dict[str, Any]]:
    return [
        {
            "buy_k": buy_k,
            "oversold_lookback": lookback,
            "confirmation_min": confirmations,
            "rebound_k_max": rebound_k,
        }
        for buy_k, lookback, confirmations, rebound_k in product(
            BUY_K_GRID, LOOKBACK_GRID, CONFIRMATION_GRID, REBOUND_K_GRID
        )
    ]


def _choose_train_params(
    data: pd.DataFrame,
    train_start: str,
    train_end: str,
    *,
    horizon: int,
    cooldown: int,
    min_train_events: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evaluated: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for params in _candidate_params():
        metrics = summarize_events(find_events(
            data, params, start_date=train_start, end_date=train_end,
            horizon=horizon, cooldown=cooldown,
        ))
        evaluated.append((params, metrics))
    qualified = [item for item in evaluated if int(item[1]["events"]) >= min_train_events]
    pool = qualified or evaluated
    best_params, best_metrics = max(
        pool,
        key=lambda item: (
            float(item[1]["score"] if item[1]["score"] is not None else -999),
            int(item[1]["events"]),
        ),
    )
    return best_params, {**best_metrics, "qualified": bool(qualified)}


def run_walk_forward(
    df: pd.DataFrame,
    *,
    symbol: str = "002179",
    horizon: int = 30,
    cooldown: int = 10,
    initial_train_years: int = 6,
    test_years: int = 2,
    min_train_events: int = 8,
) -> dict[str, Any]:
    data = prepare_daily(df)
    first_year = int(str(data.iloc[0]["date"])[:4])
    last_year = int(str(data.iloc[-1]["date"])[:4])
    train_end_year = first_year + initial_train_years - 1
    current_params = {
        "buy_k": 15.0,
        "oversold_lookback": 3,
        "confirmation_min": 2,
        "rebound_k_max": 25.0,
    }
    folds: list[dict[str, Any]] = []
    selected_oos_events: list[dict[str, Any]] = []
    current_oos_events: list[dict[str, Any]] = []

    while train_end_year < last_year:
        test_start_year = train_end_year + 1
        test_end_year = min(last_year, test_start_year + test_years - 1)
        train_start = f"{first_year}-01-01"
        train_end = f"{train_end_year}-12-31"
        test_start = f"{test_start_year}-01-01"
        test_end = f"{test_end_year}-12-31"
        selected, train_metrics = _choose_train_params(
            data, train_start, train_end, horizon=horizon, cooldown=cooldown,
            min_train_events=min_train_events,
        )
        test_events = find_events(
            data, selected, start_date=test_start, end_date=test_end,
            horizon=horizon, cooldown=cooldown,
        )
        current_events = find_events(
            data, current_params, start_date=test_start, end_date=test_end,
            horizon=horizon, cooldown=cooldown,
        )
        selected_oos_events.extend(test_events)
        current_oos_events.extend(current_events)
        folds.append({
            "train": f"{train_start}~{train_end}",
            "test": f"{test_start}~{test_end}",
            "selected_params": selected,
            "train_metrics": train_metrics,
            "test_metrics": summarize_events(test_events),
            "current_rule_test_metrics": summarize_events(current_events),
        })
        train_end_year = test_end_year

    selected_metrics = summarize_events(selected_oos_events)
    current_metrics = summarize_events(current_oos_events)
    profitable_folds = sum(
        1 for fold in folds
        if (fold["test_metrics"].get("avg_return") or 0) > 0
    )
    fold_profit_ratio = profitable_folds / len(folds) if folds else 0.0
    accepted = (
        int(selected_metrics["events"]) >= 15
        and float(selected_metrics["win_rate"] or 0) >= 0.60
        and float(selected_metrics["avg_return"] or 0) > 0
        and float(selected_metrics["p10_return"] or -1) > -0.15
        and fold_profit_ratio >= 0.60
    )
    selected_counter = Counter(json.dumps(fold["selected_params"], sort_keys=True) for fold in folds)
    stable_params = json.loads(selected_counter.most_common(1)[0][0]) if selected_counter else None
    return {
        "version": 2,
        "symbol": str(symbol),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_range": f"{data.iloc[0]['date']}~{data.iloc[-1]['date']}",
        "bars": len(data),
        "method": {
            "type": "expanding_walk_forward_event_study",
            "initial_train_years": initial_train_years,
            "test_years": test_years,
            "forward_horizon_trading_days": horizon,
            "event_cooldown_trading_days": cooldown,
            "entry": "signal confirmed at close, next trading day open with slippage",
            "exit": f"close after {horizon} trading days with slippage and both-side fees",
            "fee_rate_each_side": FEE_RATE,
            "slippage_each_side": SLIPPAGE,
            "selection_note": "每折只用训练期选择参数，再在随后测试期评估；结果不自动覆盖实盘配置",
            "price_regime_note": "价格状态只使用信号日及之前的中航光电日线；高波动优先，其次上升/下降趋势，否则震荡",
        },
        "current_live_params": current_params,
        "selected_oos": selected_metrics,
        "selected_oos_by_regime": summarize_events_by_regime(selected_oos_events),
        "current_rule_oos": current_metrics,
        "current_rule_oos_by_regime": summarize_events_by_regime(current_oos_events),
        "fold_profit_ratio": round(fold_profit_ratio, 6),
        "accepted_for_shadow_validation": accepted,
        "auto_apply": False,
        "stable_selected_params": stable_params,
        "selected_param_frequency": [
            {"params": json.loads(key), "folds": count}
            for key, count in selected_counter.most_common()
        ],
        "folds": folds,
        "selected_oos_events": selected_oos_events,
        "current_rule_oos_events": current_oos_events,
    }


def save_report(report: dict[str, Any], *, path: Optional[Path] = None) -> Path:
    target = path or report_path(str(report["symbol"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def load_report(symbol: str, *, path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    target = path or report_path(str(symbol))
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_and_save(symbol: str = "002179") -> dict[str, Any]:
    from .data_provider import fetch_backtest_daily

    data = fetch_backtest_daily(symbol, "2010-01-01")
    if data is None or data.empty:
        raise RuntimeError(f"未获取到 {symbol} 的日线数据")
    report = run_walk_forward(data, symbol=symbol)
    save_report(report)
    return report
