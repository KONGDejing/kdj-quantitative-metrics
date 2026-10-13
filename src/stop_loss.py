"""Mandatory price stops, independent of KDJ, reverse-T quotas and market brakes."""
from __future__ import annotations

from math import isfinite
from typing import Any


def stop_price(position: dict) -> float | None:
    value = position.get("stop_loss")
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
        if not isfinite(price) or price <= 0 or abs(price - round(price, 2)) > 1e-8:
            return None
        return round(price, 2)
    except (TypeError, ValueError):
        return None


def entry_stop_error(position: dict, entry_price: float) -> str | None:
    if position.get("stop_loss_exempt") is True:
        return None
    stop = stop_price(position)
    if stop is None:
        return "缺少有效的强制止损价，禁止发送买入指令"
    if not isfinite(entry_price) or entry_price <= stop:
        return f"买入价必须高于强制止损价{stop:.2f}元，禁止在止损区买入"
    return None


def position_cycle(position: dict) -> str:
    """Adding or partially selling shares cannot erase a breached stop."""
    opening = position.get("opening") or {}
    remaining = float(opening.get("core_lots", 0) or 0) + float(opening.get("t_lots", 0) or 0)
    cycle = "opening:" + str(opening.get("as_of") or "initial")
    for trade in sorted(position.get("trade_history") or [], key=lambda t: str(t.get("reported_at") or "")):
        lots = float(trade.get("lots", 0) or 0)
        remaining += lots if trade.get("side") == "buy" else -lots
        if trade.get("side") == "sell" and trade.get("position_exit") and remaining <= 1e-9:
            cycle = str(trade.get("id") or trade.get("reported_at"))
    return cycle


def stop_latched(position: dict, breach: dict | None) -> bool:
    return bool(breach and breach.get("cycle") == position_cycle(position))


def enforce_stop_loss(
    plan: dict[str, Any], position: dict, *, price: float | None,
    breach: dict | None = None, for_next_session: bool = False,
) -> dict[str, Any]:
    if position.get("stop_loss_exempt") is True:
        plan["stop_loss"] = {"required": False, "price": None, "triggered": False,
                             "status": "exempt", "basis": position.get("stop_loss_exempt_reason") or
                             "用户明确不设固定价格止损；原扩仓、资金及回撤约束保留"}
        return plan
    stop = stop_price(position)
    ledger = plan["facts"]["ledger"]
    total = int(ledger.get("total_lots", 0))
    triggered = bool(total and (stop_latched(position, breach) or
                     (stop is not None and price is not None and price <= stop)))
    plan["stop_loss"] = {
        "required": True, "price": stop, "reference_price": price,
        "triggered": triggered, "latched": stop_latched(position, breach),
        "status": "triggered" if triggered else ("missing" if stop is None else "armed"),
        "basis": position.get("stop_loss_note") or "已确认的固定风险退出价",
    }
    plan["gates"].append({"name": "mandatory_stop_loss", "passed": stop is not None,
                          "severity": "block", "detail": f"强制止损价{stop:.2f}元" if stop else
                          "缺少有效的强制止损价，禁止买入或回补"})
    reverse = plan.get("reverse_t") or {}
    if triggered:
        lots = total if for_next_session else int(ledger.get("sellable_lots_today", 0))
        executable = bool(lots and (price is not None or for_next_session))
        plan["decision"] = {
            "status": "executable" if executable else "blocked", "action": "sell_stop" if lots else "stop_t1_locked",
            "bucket": "all", "max_lots": lots, "reason_codes": ["MANDATORY_STOP_LOSS"],
            "summary": (f"强制止损已触发，退出全部可卖{lots}手，不等反弹、不补仓。" if lots else
                        "强制止损已触发，但今日全部T+1锁定；不能当天卖出，下一交易日继续处理。"),
        }
        if lots and price is None and not for_next_session:
            plan["decision"]["summary"] = "本轮止损已触发，退出任务保留；先恢复新鲜行情再给出可执行卖出提醒。"
        plan["price_plan"] = {"execution": "stop_exit", "stop_loss": stop, "lots": lots}
        plan["after_action"] = None
        plan["cancel_conditions"] = ["停止新增、扩仓和反T回补；未成交买单需本人在券商端撤销",
                                     "止损优先于反T额度、每日一轮和底仓保留；成交价可能低于触发价"]
        if reverse:
            reverse["decision"] = {"status": "blocked", "action": "hold", "max_lots": 0,
                                   "summary": "强制止损优先，暂停全部反T操作。"}
            reverse["price_plan"] = None
        return plan

    errors = []
    if stop is None:
        errors.append("缺少有效的强制止损价")
    elif price is None:
        errors.append("缺少新鲜报价，无法确认价格高于止损线")
    elif price <= stop:
        errors.append(f"现价不高于止损价{stop:.2f}元")
    # Validate every prospective fill, not just the current quote.
    prices = plan.get("price_plan") or {}
    proposed = [prices.get(k) for k in ("lower", "price", "profit_buyback") if prices.get(k) is not None]
    proposed += [o.get("limit_price") for o in plan["facts"].get("pending_orders", []) if o.get("side") == "buy"]
    t_prices = reverse.get("price_plan") or {}
    proposed += [t_prices.get(k) for k in ("profit_buyback", "protective_buyback") if t_prices.get(k) is not None]
    if stop is not None and any(float(p or 0) <= stop for p in proposed):
        errors.append("计划买入或回补价格不高于强制止损线")
    if errors:
        reason = "；".join(errors) + "，禁止买入或回补。"
        buy_actions = {"buy_core", "buyback_core", "protective_buyback", "wait_buyback", "review_core_buyback",
                       "wait_limit_buy", "manage_existing_buyback"}
        has_buys = any(o.get("side") == "buy" for o in plan["facts"].get("pending_orders", []))
        if plan["decision"].get("action") in buy_actions or has_buys:
            plan["decision"] = {"status": "blocked", "action": "review_limit_buy" if has_buys else "hold",
                                "bucket": None, "max_lots": 0, "reason_codes": ["STOP_LOSS_ENTRY_BLOCK"],
                                "summary": reason}
            plan["price_plan"] = None
            plan["after_action"] = None
        if (reverse.get("decision") or {}).get("action") in buy_actions:
            reverse["decision"] = {"status": "blocked", "action": "hold", "max_lots": 0, "summary": reason}
            reverse["price_plan"] = None
        plan["cancel_conditions"].append(reason)
    return plan
