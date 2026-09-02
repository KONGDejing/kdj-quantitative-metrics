from __future__ import annotations

from math import ceil, floor
from typing import Any, Optional


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _round_price(value: float) -> float:
    return round(max(0.0, float(value)) + 1e-9, 2)


def _floor_price(value: float) -> float:
    """Round a buy limit down to the A-share one-cent tick."""
    return floor(max(0.0, float(value)) * 100 + 1e-9) / 100


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _prepared_daily(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in series:
        try:
            result.append({
                "date": _day(item.get("timestamp") or item.get("date")),
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "k": float(item.get("k", 0) or 0),
                "d": float(item.get("d", 0) or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return sorted((item for item in result if item["date"]), key=lambda item: item["date"])[-120:]


def _prepared_intraday(series: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    result = []
    for item in series:
        timestamp = str(item.get("timestamp") or item.get("datetime") or "")
        if _day(timestamp) != day:
            continue
        try:
            result.append({
                "timestamp": timestamp,
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "k": float(item.get("k", 0) or 0),
                "d": float(item.get("d", 0) or 0),
                "j": float(item.get("j", 0) or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(result, key=lambda item: item["timestamp"])


def _atr(bars: list[dict[str, Any]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    ranges = []
    for index in range(max(1, len(bars) - period), len(bars)):
        current = bars[index]
        previous_close = float(bars[index - 1]["close"])
        ranges.append(max(
            float(current["high"]) - float(current["low"]),
            abs(float(current["high"]) - previous_close),
            abs(float(current["low"]) - previous_close),
        ))
    return float(_mean(ranges) or 0.0)


def build_reverse_t_plan(
    *,
    position: dict[str, Any],
    ledger: dict[str, Any],
    daily_series: list[dict[str, Any]],
    intraday_series: Optional[list[dict[str, Any]]] = None,
    decision_date: str,
    execution_enabled: bool = False,
) -> dict[str, Any]:
    """Build a sell-first reverse-T plan without changing the confirmed ledger."""
    config = position.get("reverse_t") or {}
    enabled = bool(config.get("enabled", False))
    ratio = min(0.5, max(0.0, float(config.get("allocation_ratio", 0.20) or 0.20)))
    buyback_gap_ratio = min(0.10, max(0.001, float(config.get("buyback_gap_ratio", 0.018) or 0.018)))
    sell_spike_ratio = min(0.10, max(0.001, float(config.get("sell_spike_ratio", 0.018) or 0.018)))
    target_lots = max(int(ledger.get("core_target_lots", 0) or 0), int(ledger.get("core_lots", 0) or 0))
    quota_lots = floor(target_lots * ratio + 1e-9)
    core_floor_lots = ceil(target_lots * (1.0 - ratio) - 1e-9)
    max_per_trade = max(1, int(config.get("max_lots_per_trade", 1) or 1))
    max_daily_cycles = max(1, int(config.get("max_daily_cycles", 1) or 1))
    trend_filter_enabled = bool(config.get("trend_filter_enabled", False))
    protective_buyback_enabled = bool(config.get("protective_buyback_enabled", False))
    daily = _prepared_daily(daily_series)
    intraday = _prepared_intraday(intraday_series or [], decision_date)

    plan: dict[str, Any] = {
        "enabled": enabled,
        "mode": "simple_spike_reverse_t",
        "quota_kind": "existing_core_shares_operational_quota",
        "allocation_ratio": ratio,
        "buyback_gap_ratio": round(buyback_gap_ratio, 6),
        "sell_spike_ratio": round(sell_spike_ratio, 6),
        "quota_lots": quota_lots,
        "core_floor_lots": core_floor_lots,
        "total_position_lots": int(ledger.get("total_lots", 0) or 0),
        "completed_roundtrip_cycles": int(ledger.get("completed_core_roundtrip_events", 0) or 0),
        "completed_roundtrip_lots": int(ledger.get("completed_core_roundtrip_lots", 0) or 0),
        "completed_roundtrip_net_pnl": float(ledger.get("core_roundtrip_net_pnl", 0) or 0),
        "max_lots_per_trade": max_per_trade,
        "max_daily_cycles": max_daily_cycles,
        "trend_filter_enabled": trend_filter_enabled,
        "protective_buyback_enabled": protective_buyback_enabled,
        "rule": {
            "summary": (
                f"价格较前收冲高约{sell_spike_ratio * 100:.1f}%，"
                f"且10分钟K从80以上拐头时最多卖出{max_per_trade}手；不使用MA均线。"
            ),
        },
        "signal": {},
        "decision": {
            "status": "disabled" if not enabled else "watch",
            "action": "hold",
            "max_lots": 0,
            "summary": "反T功能未开启。" if not enabled else "等待价格冲高和10分钟K高位拐头。",
        },
        "price_plan": None,
        "cancel_conditions": [],
    }
    if not enabled:
        return plan

    # Retain the original MA trend filter as a parked, configurable rule.
    # It is calculated for future review but does not affect execution while
    # trend_filter_enabled is false.
    short_window = max(2, int(config.get("trend_ma_short", 20) or 20))
    long_window = max(short_window + 1, int(config.get("trend_ma_long", 60) or 60))
    slope_days = max(1, int(config.get("trend_slope_days", 5) or 5))
    closes = [float(item["close"]) for item in daily]
    enough_daily = len(closes) >= long_window
    ma_short = _mean(closes[-short_window:]) if enough_daily else None
    ma_long = _mean(closes[-long_window:]) if enough_daily else None
    prior_values = closes[-short_window - slope_days:-slope_days] if len(closes) >= short_window + slope_days else []
    prior_ma_short = _mean(prior_values)
    latest_daily = daily[-1] if daily else None
    uptrend = bool(
        latest_daily and ma_short is not None and ma_long is not None and prior_ma_short is not None
        and float(latest_daily["close"]) > ma_short > ma_long
        and ma_short > prior_ma_short
    )
    plan["trend"] = {
        "filter_enabled": trend_filter_enabled,
        "passed": uptrend,
        "close": round(float(latest_daily["close"]), 4) if latest_daily else None,
        "ma_short": round(float(ma_short), 4) if ma_short is not None else None,
        "ma_long": round(float(ma_long), 4) if ma_long is not None else None,
        "ma_short_prior": round(float(prior_ma_short), 4) if prior_ma_short is not None else None,
    }
    if trend_filter_enabled:
        plan["rule"]["summary"] = (
            f"备用趋势过滤已启用；在价格较前收冲高约{sell_spike_ratio * 100:.1f}%、"
            f"10分钟K从80以上拐头时最多卖出{max_per_trade}手。"
        )

    atr = _atr(daily)
    protection_spread = max(
        float(config.get("min_protection_amount", 0.20) or 0.20),
        atr * float(config.get("atr_protection_fraction", 0.20) or 0.20),
    )
    pending = int(ledger.get("pending_core_buyback_lots", 0) or 0)
    reference_sell = ledger.get("pending_core_sell_reference_price")
    latest_intraday = intraday[-1] if intraday else None

    if pending > 0:
        if reference_sell is None:
            plan["decision"] = {
                "status": "blocked",
                "action": "review",
                "max_lots": 0,
                "summary": "存在待补回仓位，但账本缺少对应卖出价，停止自动给价并人工复核。",
            }
            return plan
        target = _floor_price(float(reference_sell) * (1.0 - buyback_gap_ratio))
        minimum_spread = float(reference_sell) - target
        plan["price_plan"] = {
            "sell_reference": _round_price(float(reference_sell)),
            "profit_buyback": target,
            "target_gap_ratio": round(buyback_gap_ratio, 6),
            "minimum_spread": _round_price(minimum_spread),
        }
        if protective_buyback_enabled:
            plan["price_plan"]["protective_buyback"] = _round_price(
                float(reference_sell) + protection_spread
            )
        lots = min(pending, max_per_trade)
        current = float(latest_intraday["close"]) if latest_intraday else None
        if execution_enabled and current is not None and current <= target:
            plan["decision"] = {
                "status": "executable",
                "action": "buyback_core",
                "max_lots": lots,
                "summary": f"价格已回落到盈利回补位，补回{lots}手核心仓，完成反T。",
            }
        elif (
            protective_buyback_enabled
            and execution_enabled
            and current is not None
            and current >= float(plan["price_plan"]["protective_buyback"])
        ):
            plan["decision"] = {
                "status": "executable",
                "action": "protective_buyback",
                "max_lots": lots,
                "summary": f"卖出后价格向上突破保护位，补回{lots}手，限制踏空损失。",
            }
        else:
            plan["decision"] = {
                "status": "watch",
                "action": "wait_buyback",
                "max_lots": lots,
                "summary": f"已有{pending}手待补回，优先等待回补，期间禁止再次卖出做T。",
            }
        plan["cancel_conditions"] = ["待补回仓位完成前禁止开启下一轮反T"]
        return plan

    sellable_core = int(ledger.get("sellable_core_lots_today", 0) or 0)
    available_lots = max(0, min(quota_lots, sellable_core - core_floor_lots))
    sell_cycles_today = sum(
        1
        for trade in position.get("trade_history") or []
        if _day(trade.get("reported_at")) == decision_date
        and str(trade.get("side") or "").lower() == "sell"
        and str(trade.get("bucket") or "core").lower() != "tactical"
    )
    buyback_cycles_today = int(ledger.get("completed_core_roundtrip_events_today", 0) or 0)
    cycles_today = max(sell_cycles_today, buyback_cycles_today)
    k_high = float(config.get("intraday_k_high", 80) or 80)
    previous_close = float(daily[-1]["close"]) if daily else None
    current = float(latest_intraday["close"]) if latest_intraday else None
    prior_intraday = intraday[-2] if len(intraday) >= 2 else None
    spike_price = _round_price(previous_close * (1.0 + sell_spike_ratio)) if previous_close is not None else None
    turn_down = bool(
        latest_intraday and prior_intraday
        and float(prior_intraday["k"]) >= k_high
        and float(latest_intraday["k"]) < float(prior_intraday["k"])
    )
    price_extended = bool(current is not None and spike_price is not None and current >= spike_price)
    plan["signal"] = {
        "intraday_date": _day(latest_intraday.get("timestamp")) if latest_intraday else None,
        "close": round(current, 4) if current is not None else None,
        "k": round(float(latest_intraday["k"]), 2) if latest_intraday else None,
        "previous_k": round(float(prior_intraday["k"]), 2) if prior_intraday else None,
        "turn_down_from_high": turn_down,
        "previous_close": round(previous_close, 4) if previous_close is not None else None,
        "spike_ratio": round(sell_spike_ratio, 6),
        "spike_price": spike_price,
        "price_extended": price_extended,
        "available_lots": available_lots,
        "sell_cycles_today": sell_cycles_today,
        "buyback_cycles_today": buyback_cycles_today,
        "cycles_today": cycles_today,
    }
    sell_ready = (
        execution_enabled and (not trend_filter_enabled or uptrend) and turn_down and price_extended
        and available_lots > 0 and cycles_today < max_daily_cycles
    )
    if sell_ready:
        lots = min(max_per_trade, available_lots)
        expected_buyback = _floor_price(float(current) * (1.0 - buyback_gap_ratio))
        minimum_spread = float(current) - expected_buyback
        plan["decision"] = {
            "status": "executable",
            "action": "sell_core_for_reverse_t",
            "max_lots": lots,
            "summary": (
                f"价格较前收冲高约{sell_spike_ratio * 100:.1f}%，"
                f"10分钟K从{int(k_high)}以上拐头，卖出{lots}手可卖老仓做反T。"
            ),
        }
        plan["price_plan"] = {
            "sell_limit": _round_price(float(current)),
            "expected_buyback": expected_buyback,
            "target_gap_ratio": round(buyback_gap_ratio, 6),
            "minimum_spread": _round_price(minimum_spread),
        }
        if protective_buyback_enabled:
            plan["price_plan"]["protective_buyback"] = _round_price(
                float(current) + protection_spread
            )
        plan["cancel_conditions"] = [
            f"低于{_round_price(float(current)):.2f}不追卖",
            "成交后必须按核心仓卖出录入，系统才能建立待补回任务",
        ]
        if protective_buyback_enabled:
            plan["cancel_conditions"].append(
                f"价格向上突破{plan['price_plan']['protective_buyback']:.2f}时执行保护性补回"
            )
    else:
        blockers = []
        if trend_filter_enabled and not uptrend:
            blockers.append("备用上升趋势过滤未通过")
        if available_lots <= 0:
            blockers.append(f"必须保留至少{core_floor_lots}手核心仓")
        if cycles_today >= max_daily_cycles:
            blockers.append(f"今日已达到{max_daily_cycles}轮反T上限")
        if not intraday:
            blockers.append("没有当日10分钟线")
        elif not turn_down:
            blockers.append(f"10分钟K尚未从{int(k_high)}以上拐头")
        if spike_price is not None and not price_extended:
            blockers.append(f"价格尚未达到前收上方约{sell_spike_ratio * 100:.1f}%的{spike_price:.2f}")
        plan["decision"] = {
            "status": "watch",
            "action": "hold",
            "max_lots": 0,
            "summary": "；".join(blockers) if blockers else "等待反T信号。",
        }
    return plan
