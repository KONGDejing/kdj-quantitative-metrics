from __future__ import annotations

from datetime import date, datetime
from math import floor
from typing import Any, Optional

from .reverse_t_engine import build_reverse_t_plan
from .trade_ledger import LOT_SIZE, replay_position


ENGINE_VERSION = "1.0"


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _round_price(value: float) -> float:
    return round(max(0.0, value) + 1e-9, 2)


def _calendar_age_days(newer: str, older: str) -> Optional[int]:
    try:
        return max(0, (datetime.strptime(newer, "%Y-%m-%d") - datetime.strptime(older, "%Y-%m-%d")).days)
    except (TypeError, ValueError):
        return None


def _position_scope(position: dict[str, Any]) -> str:
    mode = str(position.get("strategy_mode") or "").strip().lower()
    if mode == "expand_base":
        return "zhonghang_core_tactical"
    if mode == "long_term":
        return "long_term"
    return "unconfigured"


def _bars_to_signal(daily_series: list[dict[str, Any]], signal_date: str) -> list[dict[str, Any]]:
    bars = [item for item in daily_series if _day(item.get("timestamp") or item.get("date")) <= signal_date]
    return bars[-120:]


def _atr(bars: list[dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(bars) < 2:
        return None
    ranges: list[float] = []
    start = max(1, len(bars) - period)
    for index in range(start, len(bars)):
        current = bars[index]
        previous_close = float(bars[index - 1]["close"])
        high = float(current["high"])
        low = float(current["low"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges) / len(ranges) if ranges else None


def _recent_trade(position: dict[str, Any]) -> Optional[dict[str, Any]]:
    history = position.get("trade_history") or []
    return history[-1] if history else position.get("last_report")


def _open_pending_orders(position: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in position.get("pending_orders") or []
        if str(item.get("status") or "open").lower() == "open"
    ]


def _oversold_cycle_start(bars: list[dict[str, Any]], buy_k: float) -> Optional[str]:
    start: Optional[str] = None
    for bar in reversed(bars):
        if float(bar.get("k", 100)) < buy_k:
            start = _day(bar.get("timestamp") or bar.get("date"))
            continue
        if start:
            break
    return start


def _cycle_core_buys(position: dict[str, Any], cycle_start: Optional[str]) -> int:
    if not cycle_start:
        return 0
    total = 0
    for trade in position.get("trade_history") or []:
        if _day(trade.get("reported_at")) < cycle_start or trade.get("side") != "buy":
            continue
        bucket = str(trade.get("bucket") or "core").lower()
        if bucket in {"core", "auto", "base", "核心", "核心仓"}:
            total += int(trade.get("lots", 0) or 0)
    return total


def _gate(name: str, passed: bool, detail: str, severity: str = "block") -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "severity": severity, "detail": detail}


def _performance(
    position: dict[str, Any],
    ledger: dict[str, Any],
    close: float,
    performance_state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    budget = float(position.get("strategy_budget", 0) or 0)
    market_value = float(ledger["total_lots"]) * LOT_SIZE * close
    book_value = (
        float(ledger["total_lots"]) * LOT_SIZE * float(ledger["average_entry_cost"])
        if ledger.get("average_entry_cost") is not None else 0.0
    )
    net_invested = float(ledger.get("net_cash_invested", 0) or 0)
    cash = budget - net_invested if budget else None
    sleeve_equity = cash + market_value if cash is not None else None
    performance_state = performance_state or {}
    configured_high_water = float(
        performance_state.get("high_water_equity")
        or position.get("sleeve_high_water", budget)
        or budget
    )
    high_water = max(configured_high_water, sleeve_equity or 0.0)
    drawdown = (sleeve_equity / high_water - 1.0) if sleeve_equity is not None and high_water else None
    return {
        "strategy_budget": round(budget, 2) if budget else None,
        "market_value": round(market_value, 2),
        "cash": round(cash, 2) if cash is not None else None,
        "sleeve_equity": round(sleeve_equity, 2) if sleeve_equity is not None else None,
        "sleeve_return": round(sleeve_equity / budget - 1.0, 6) if sleeve_equity is not None and budget else None,
        "deployed_position_return": round(market_value / net_invested - 1.0, 6) if net_invested > 0 else None,
        "inventory_return": round(market_value / book_value - 1.0, 6) if book_value else None,
        "breakeven_return": round(close / float(ledger["breakeven_cost"]) - 1.0, 6)
        if ledger.get("breakeven_cost") else None,
        "deployed_ratio": round(market_value / budget, 6) if budget else None,
        "cash_ratio": round(cash / budget, 6) if cash is not None and budget else None,
        "high_water_equity": round(high_water, 2) if high_water else None,
        "drawdown_from_high_water": round(drawdown, 6) if drawdown is not None else None,
        "historical_max_drawdown": performance_state.get("max_drawdown"),
        "performance_first_date": performance_state.get("first_date"),
        "performance_snapshot_count": int(performance_state.get("snapshot_count", 0) or 0),
    }


def build_decision_plan(
    *,
    symbol_code: str,
    symbol_name: str,
    latest_daily: dict[str, Any],
    daily_series: list[dict[str, Any]],
    position: dict[str, Any],
    decision_date: Optional[str] = None,
    performance_state: Optional[dict[str, Any]] = None,
    intraday_series: Optional[list[dict[str, Any]]] = None,
    intraday_execution_enabled: bool = False,
) -> dict[str, Any]:
    """Build a deterministic, non-executing plan for the current/next trading session."""
    decision_date = decision_date or date.today().isoformat()
    signal_date = _day(latest_daily.get("timestamp") or latest_daily.get("date"))
    is_confirmed_daily = bool(signal_date) and not bool(latest_daily.get("estimated"))
    signal_age_days = _calendar_age_days(decision_date, signal_date)
    max_signal_age_days = int(position.get("max_signal_age_days", 4) or 4)
    confirmed = is_confirmed_daily and signal_age_days is not None and signal_age_days <= max_signal_age_days
    ledger = replay_position(position, as_of=decision_date)
    scope = _position_scope(position)
    close = float(latest_daily.get("close", 0) or 0)
    k_value = float(latest_daily.get("k", 0) or 0)
    d_value = float(latest_daily.get("d", 0) or 0)
    j_value = float(latest_daily.get("j", 0) or 0)
    bars = _bars_to_signal(daily_series, signal_date) if signal_date else []
    current_bar = bars[-1] if bars else latest_daily
    previous_bar = bars[-2] if len(bars) >= 2 else None
    performance = _performance(position, ledger, close, performance_state) if close > 0 else {}

    recent_trade = _recent_trade(position)
    pending_orders = _open_pending_orders(position)
    facts = {
        "latest_trade": recent_trade,
        "pending_orders": pending_orders,
        "ledger": ledger,
        "t1": {
            "sellable_lots_now": ledger["sellable_lots_today"],
            "locked_lots_now": ledger["locked_t1_lots"],
            "sellable_lots_next_session": ledger["total_lots"],
        },
    }

    plan: dict[str, Any] = {
        "version": ENGINE_VERSION,
        "symbol": {"code": str(symbol_code), "name": symbol_name},
        "decision_date": decision_date,
        "signal_date": signal_date,
        "planned_for": "current_session" if decision_date > signal_date else "next_trading_session",
        "strategy_scope": scope,
        "facts": facts,
        "market": {
            "confirmed_daily": is_confirmed_daily,
            "fresh_confirmed_daily": confirmed,
            "signal_age_days": signal_age_days,
            "max_signal_age_days": max_signal_age_days,
            "close": close,
            "k": k_value,
            "d": d_value,
            "j": j_value,
            "atr14": round(_atr(bars) or 0.0, 4),
            "data_source": latest_daily.get("data_source"),
        },
        "performance": performance,
        "signal": {},
        "gates": [],
        "decision": {},
        "price_plan": None,
        "after_action": None,
        "cancel_conditions": [],
    }
    plan["reverse_t"] = build_reverse_t_plan(
        position=position,
        ledger=ledger,
        daily_series=bars,
        intraday_series=intraday_series,
        decision_date=decision_date,
        execution_enabled=intraday_execution_enabled,
    )

    if scope == "long_term":
        pending_buy = next((item for item in pending_orders if str(item.get("side")).lower() == "buy"), None)
        if pending_buy:
            lots = int(pending_buy.get("lots", 0) or 0)
            limit_price = float(pending_buy.get("limit_price", 0) or 0)
            plan["decision"] = {
                "status": "watch",
                "action": "wait_limit_buy",
                "bucket": str(pending_buy.get("bucket") or "core"),
                "max_lots": 0,
                "reason_codes": ["OPEN_LIMIT_BUY"],
                "summary": f"已有{limit_price:.2f}元买入{lots}手挂单，尚未成交；不重复挂单。",
            }
            plan["price_plan"] = {
                "execution": "existing_limit_order",
                "side": "buy",
                "price": limit_price,
                "lots": lots,
                "order_id": pending_buy.get("id"),
            }
            plan["gates"].append(_gate("strategy_scope", True, "长期仓等待已存在的买入挂单", "info"))
            return plan
        plan["decision"] = {
            "status": "watch",
            "action": "hold",
            "bucket": "core",
            "max_lots": 0,
            "reason_codes": ["LONG_TERM_SCOPE"],
            "summary": "长期仓不套用中航核心仓/T仓交易规则，保持持有。",
        }
        plan["gates"].append(_gate("strategy_scope", True, "该标的配置为长期持有", "info"))
        return plan

    if scope != "zhonghang_core_tactical":
        plan["decision"] = {
            "status": "blocked",
            "action": "review",
            "bucket": None,
            "max_lots": 0,
            "reason_codes": ["STRATEGY_UNCONFIGURED"],
            "summary": "没有匹配到允许执行的确定性策略。",
        }
        plan["gates"].append(_gate("strategy_scope", False, "策略范围未配置"))
        return plan

    signal_cfg = position.get("signal_rules") or {}
    buy_k = float(signal_cfg.get("buy_k", 15) or 15)
    rebound_k_max = float(signal_cfg.get("rebound_k_max", 25) or 25)
    sell_k = float(signal_cfg.get("sell_k", 80) or 80)
    oversold_lookback = max(1, int(signal_cfg.get("oversold_lookback", 3) or 3))
    confirmation_min = max(1, int(signal_cfg.get("confirmation_min", 2) or 2))
    max_chase_ratio = float(signal_cfg.get("max_chase_ratio", 0.03) or 0.03)
    atr_zone_fraction = float(signal_cfg.get("atr_zone_fraction", 0.25) or 0.25)

    recent_bars = bars[-oversold_lookback:] if bars else []
    oversold_bars = [bar for bar in recent_bars if float(bar.get("k", 100)) < buy_k]
    recent_oversold = bool(oversold_bars)
    oversold_reference = oversold_bars[-1] if oversold_bars else None
    previous_k = float(previous_bar.get("k", 0)) if previous_bar else None
    previous_d = float(previous_bar.get("d", 0)) if previous_bar else None
    previous_close = float(previous_bar.get("close", 0)) if previous_bar else None
    prior_lows = [float(item["low"]) for item in bars[-4:-1] if item.get("low") is not None]
    confirmations: list[dict[str, Any]] = []
    if previous_bar:
        confirmations.extend([
            {"name": "k_turn_up", "passed": k_value > float(previous_k), "detail": f"K {previous_k:.2f}→{k_value:.2f}"},
            {"name": "k_cross_d", "passed": k_value >= d_value and float(previous_k) < float(previous_d), "detail": f"K={k_value:.2f}, D={d_value:.2f}"},
            {"name": "close_not_lower", "passed": close >= float(previous_close), "detail": f"收盘 {previous_close:.2f}→{close:.2f}"},
        ])
    if prior_lows:
        confirmations.append({
            "name": "no_new_recent_low",
            "passed": float(current_bar.get("low", close)) >= min(prior_lows),
            "detail": f"当日低点{float(current_bar.get('low', close)):.2f}，此前3日低点{min(prior_lows):.2f}",
        })
    confirmation_count = sum(1 for item in confirmations if item["passed"])
    cycle_start = _oversold_cycle_start(recent_bars, buy_k) if recent_oversold else None
    cycle_buys = _cycle_core_buys(position, cycle_start)

    plan["signal"] = {
        "buy_k": buy_k,
        "sell_k": sell_k,
        "recent_oversold": recent_oversold,
        "oversold_reference_date": _day((oversold_reference or {}).get("timestamp")) or None,
        "oversold_reference_close": float((oversold_reference or {}).get("close", 0) or 0) or None,
        "confirmation_count": confirmation_count,
        "confirmation_required": confirmation_min,
        "confirmations": confirmations,
        "cycle_start": cycle_start,
        "cycle_core_buys": cycle_buys,
    }

    ledger_ok = bool((ledger.get("validation") or {}).get("ok"))
    plan["gates"].append(_gate("ledger", ledger_ok, "交易账本一致" if ledger_ok else "交易账本存在错误"))
    data_detail = (
        f"使用正式日线，距计划日{signal_age_days}个自然日"
        if confirmed else f"正式日线过期、缺失或仅有盘中估算（允许最多{max_signal_age_days}个自然日）"
    )
    plan["gates"].append(_gate("confirmed_daily", confirmed, data_detail))

    fundamental = position.get("fundamental_gate") or {}
    fundamental_status = str(fundamental.get("status") or "unknown").lower()
    fundamental_allows_add = fundamental_status in {"pass", "caution"}
    plan["gates"].append(_gate(
        "fundamental",
        fundamental_allows_add,
        str(fundamental.get("note") or "基本面状态未核对"),
        "caution" if fundamental_status == "caution" else "block",
    ))

    drawdown = performance.get("drawdown_from_high_water")
    drawdown_abs = abs(min(float(drawdown or 0), 0.0))
    pause_line = float(position.get("drawdown_pause", 0.10) or 0.10)
    review_line = float(position.get("drawdown_review", 0.15) or 0.15)
    no_add_line = float(position.get("drawdown_no_add", 0.20) or 0.20)
    drawdown_allows_add = drawdown_abs < pause_line
    drawdown_detail = f"当前回撤{drawdown_abs * 100:.2f}%，暂停/复核/禁加线={pause_line:.0%}/{review_line:.0%}/{no_add_line:.0%}"
    plan["gates"].append(_gate("drawdown", drawdown_allows_add, drawdown_detail))

    tactical_enabled = bool(position.get("tactical_enabled", False))
    if tactical_enabled and int(ledger["t_lots"]) > 0 and k_value >= sell_k and confirmed:
        sell_lots = min(int(ledger["t_lots"]), int(ledger["total_lots"]))
        plan["decision"] = {
            "status": "executable",
            "action": "sell_tactical",
            "bucket": "tactical",
            "max_lots": sell_lots,
            "reason_codes": ["TACTICAL_OVERBOUGHT"],
            "summary": f"日线K达到{sell_k:g}以上，下一交易时段卖出T仓，核心仓不动。",
        }
        plan["price_plan"] = {"execution": "next_session_open", "reference_close": close}
        plan["cancel_conditions"] = ["账本显示可卖T仓不足时取消超出部分", "出现重大停牌或交易限制时不执行"]
        return plan

    hard_block = not ledger_ok or not confirmed
    if int(ledger.get("pending_core_buyback_lots", 0) or 0) > 0:
        reverse_decision = dict((plan.get("reverse_t") or {}).get("decision") or {})
        reverse_price_plan = (plan.get("reverse_t") or {}).get("price_plan")
        if reverse_price_plan and reverse_decision.get("action") in {
            "wait_buyback", "buyback_core", "protective_buyback",
        }:
            plan["decision"] = {
                **reverse_decision,
                "bucket": "core",
                "reason_codes": ["PENDING_CORE_BUYBACK"],
            }
            plan["price_plan"] = dict(reverse_price_plan)
            plan["cancel_conditions"] = list(
                (plan.get("reverse_t") or {}).get("cancel_conditions") or []
            )
        else:
            plan["decision"] = {
                "status": "blocked" if hard_block or not fundamental_allows_add else "watch",
                "action": "review_core_buyback",
                "bucket": "core",
                "max_lots": 0,
                "reason_codes": ["PENDING_CORE_BUYBACK", "PRICE_REFERENCE_REQUIRED"],
                "summary": "存在待补回核心仓，但缺少经验证的补回价格条件，暂不自动生成买单。",
            }
        return plan

    stage_target = int(position.get("next_stage_base_lots", ledger["core_lots"]) or ledger["core_lots"])
    stage_gap = max(0, stage_target - int(ledger["core_lots"]))
    budget = float(position.get("strategy_budget", 0) or 0)
    max_deployed_ratio = float(position.get("max_deployed_ratio", 0.85) or 0.85)
    fee_per_lot = float(position.get("fee_per_lot", 5) or 5)
    market_value = float(performance.get("market_value", 0) or 0)
    capital_room = max(0.0, budget * max_deployed_ratio - market_value) if budget else 0.0
    capital_lots = floor(capital_room / (close * LOT_SIZE + fee_per_lot)) if close > 0 else 0
    max_daily_add = int(position.get("max_daily_add_lots", 5) or 5)
    max_cycle_add = int(position.get("max_oversold_cycle_add_lots", 10) or 10)
    cycle_room = max(0, max_cycle_add - cycle_buys)
    first_tranche = int(signal_cfg.get("max_first_tranche_lots", 2) or 2)
    if fundamental_status == "caution":
        first_tranche = min(first_tranche, int(signal_cfg.get("caution_max_lots", 2) or 2))
    max_lots = min(stage_gap, capital_lots, max_daily_add, cycle_room, first_tranche)
    plan["gates"].extend([
        _gate("stage_position", stage_gap > 0, f"当前核心{ledger['core_lots']}手，下一阶段{stage_target}手，余量{stage_gap}手"),
        _gate("capital", capital_lots > 0, f"85%部署上限内最多还可增加{capital_lots}手"),
        _gate("oversold", recent_oversold, f"最近{oversold_lookback}根正式日线{'出现' if recent_oversold else '未出现'}K<{buy_k:g}"),
        _gate("rebound_zone", k_value <= rebound_k_max, f"当前K={k_value:.2f}，反弹确认上限{rebound_k_max:g}"),
        _gate("confirmation", confirmation_count >= confirmation_min, f"止跌确认{confirmation_count}/{confirmation_min}"),
        _gate("cycle_limit", cycle_room > 0, f"本轮已加{cycle_buys}手，上限{max_cycle_add}手"),
    ])

    buy_ready = (
        not hard_block
        and fundamental_allows_add
        and drawdown_allows_add
        and recent_oversold
        and k_value <= rebound_k_max
        and confirmation_count >= confirmation_min
        and max_lots > 0
    )
    if not buy_ready:
        failed = [gate["name"] for gate in plan["gates"] if not gate["passed"]]
        status = "blocked" if any(name in failed for name in {"ledger", "confirmed_daily", "fundamental", "drawdown"}) else "watch"
        action = "hold"
        summary = "当前没有满足全部条件的买卖动作，保持现有仓位。"
        if not ledger_ok:
            action = "review"
            summary = "交易账本校验失败，停止生成交易动作并人工复核。"
        elif fundamental_status in {"block", "unknown"}:
            action = "review"
            summary = "基本面闸门未通过，停止新增仓位并人工复核；不自动卖出核心仓。"
        elif drawdown_abs >= review_line:
            action = "review"
            summary = (
                "策略回撤达到禁止加仓线，进入风险处置复核，不自动摊低成本。"
                if drawdown_abs >= no_add_line else
                "策略回撤达到强制复核线，暂停新增仓位并检查基本面与策略有效性。"
            )
        elif recent_oversold and confirmation_count < confirmation_min:
            summary = "已进入超卖候选区，但止跌确认不足，等待而不是抢反弹。"
        elif not recent_oversold:
            summary = f"最近{oversold_lookback}根正式日线没有K<{buy_k:g}，不因盘中估算或主观猜底加仓。"
        plan["decision"] = {
            "status": status,
            "action": action,
            "bucket": None,
            "max_lots": 0,
            "reason_codes": [f"GATE_{name.upper()}" for name in failed] or ["NO_ACTION"],
            "summary": summary,
        }
        plan["cancel_conditions"] = ["没有正式日线K<15候选信号时不加仓", "盘中K值不能替代收盘确认"]
        return plan

    atr_value = float(plan["market"]["atr14"] or 0)
    reference_close = float((oversold_reference or {}).get("close", close))
    recent_low = min(float(item.get("low", close)) for item in bars[-5:]) if bars else close
    chase_cap = reference_close * (1.0 + max_chase_ratio)
    lower = max(recent_low, close - atr_value * atr_zone_fraction) if atr_value else max(recent_low, close * 0.99)
    upper = min(chase_cap, close + atr_value * atr_zone_fraction) if atr_value else min(chase_cap, close * 1.01)
    if lower > upper:
        plan["decision"] = {
            "status": "blocked",
            "action": "hold",
            "bucket": None,
            "max_lots": 0,
            "reason_codes": ["PRICE_ZONE_INVALID"],
            "summary": "确认后价格已经脱离允许买入区间，不追涨。",
        }
        plan["cancel_conditions"] = [f"价格高于{_round_price(chase_cap):.2f}不追"]
        return plan

    plan["decision"] = {
        "status": "executable",
        "action": "buy_core",
        "bucket": "core",
        "max_lots": max_lots,
        "reason_codes": ["OVERSOLD_REBOUND_CONFIRMED", "ALL_RISK_GATES_PASS"],
        "summary": f"正式日线超卖后已有{confirmation_count}项止跌确认，最多分批增加核心仓{max_lots}手。",
    }
    plan["price_plan"] = {
        "execution": "limit_zone",
        "lower": _round_price(lower),
        "upper": _round_price(upper),
        "reference_close": close,
        "do_not_chase_above": _round_price(chase_cap),
        "invalidate_below": _round_price(recent_low),
    }
    estimated_cost = max_lots * (upper * LOT_SIZE + fee_per_lot)
    after_market_value = market_value + max_lots * upper * LOT_SIZE
    plan["after_action"] = {
        "core_lots": int(ledger["core_lots"]) + max_lots,
        "t_lots": int(ledger["t_lots"]),
        "estimated_cash": round(float(performance.get("cash", 0) or 0) - estimated_cost, 2),
        "deployed_ratio": round(after_market_value / budget, 6) if budget else None,
    }
    plan["cancel_conditions"] = [
        f"价格跌破{_round_price(recent_low):.2f}并创近期新低时取消",
        f"价格高于{_round_price(chase_cap):.2f}时不追",
        "出现重大负面公告或基本面闸门改为block时取消",
        "任何账本、T+1或资金校验不通过时取消",
    ]
    return plan


def format_decision_plan(plan: dict[str, Any]) -> str:
    """Render the deterministic plan in the fixed trade-memory order."""
    symbol = plan["symbol"]
    facts = plan["facts"]
    ledger = facts["ledger"]
    latest_trade = facts.get("latest_trade")
    market = plan["market"]
    performance = plan.get("performance") or {}
    decision = plan["decision"]
    action_labels = {
        "hold": "持有/不操作",
        "buy_core": "分批买入核心仓",
        "sell_tactical": "卖出T仓",
        "review": "人工复核",
        "review_core_buyback": "检查待补回核心仓",
        "wait_buyback": "等待补回核心仓",
        "buyback_core": "盈利补回核心仓",
        "protective_buyback": "保护性补回核心仓",
        "wait_limit_buy": "等待现有买入挂单",
    }

    lines = [f"{symbol['name']}({symbol['code']})｜确定性计划 v{plan['version']}"]
    if latest_trade:
        side = "买入" if latest_trade.get("side") == "buy" else "卖出"
        bucket = "T仓" if latest_trade.get("bucket") == "tactical" else "核心仓"
        price = float(latest_trade.get("price", 0) or 0)
        lines.append(f"最近成交：{_day(latest_trade.get('reported_at'))} {side}{bucket}{int(latest_trade.get('lots', 0) or 0)}手，{price:.2f}元。")
    else:
        lines.append("最近成交：无已记录流水。")
    lines.append(
        f"当前真实持仓：核心仓{ledger['core_lots']}手，T仓{ledger['t_lots']}手；"
        f"账本重算保本成本{ledger.get('breakeven_cost') or 0:.3f}。"
    )
    lines.append(f"待处理仓位：待补回核心仓{ledger['pending_core_buyback_lots']}手。")
    lines.append(
        f"T+1：当前可卖{facts['t1']['sellable_lots_now']}手、锁定{facts['t1']['locked_lots_now']}手；"
        f"下一交易时段现有仓位最多可卖{facts['t1']['sellable_lots_next_session']}手。"
    )
    source_labels = {
        "akshare_daily": "主日线源",
        "sina_daily": "新浪日线",
        "tencent_daily": "腾讯日线",
        "completed_intraday_10m": "完整分钟线合成",
    }
    source_text = source_labels.get(market.get("data_source"), "日线数据源")
    lines.append(
        f"正式日线：{plan['signal_date']} 收盘{market['close']:.2f}，"
        f"K={market['k']:.2f} D={market['d']:.2f} J={market['j']:.2f}；"
        f"止跌确认{plan.get('signal', {}).get('confirmation_count', 0)}/{plan.get('signal', {}).get('confirmation_required', 0)}；"
        f"来源={source_text}。"
    )
    if performance.get("strategy_budget"):
        lines.append(
            f"资金：策略账户收益{performance['sleeve_return'] * 100:+.2f}%，"
            f"持仓收益{performance['deployed_position_return'] * 100:+.2f}%，"
            f"已部署{performance['deployed_ratio'] * 100:.2f}%，现金{performance['cash']:.2f}元。"
        )
    lines.append(
        f"机械结论：{action_labels.get(decision['action'], decision['action'])}；"
        f"状态={decision['status']}，最多{decision['max_lots']}手。{decision['summary']}"
    )
    price_plan = plan.get("price_plan")
    if price_plan and price_plan.get("execution") == "limit_zone":
        lines.append(
            f"执行价位：{price_plan['lower']:.2f}—{price_plan['upper']:.2f}；"
            f"高于{price_plan['do_not_chase_above']:.2f}不追，低于{price_plan['invalidate_below']:.2f}失效。"
        )
    elif price_plan and price_plan.get("execution") == "next_session_open":
        lines.append("执行价位：下一交易时段开盘执行T仓减仓，核心仓不动。")
    elif price_plan and price_plan.get("execution") == "existing_limit_order":
        lines.append(
            f"执行价位：已有{price_plan['price']:.2f}元买入{price_plan['lots']}手挂单，等待成交，不重复下单。"
        )
    elif (
        price_plan
        and price_plan.get("profit_buyback") is not None
        and price_plan.get("protective_buyback") is None
    ):
        lines.append(
            f"执行价位：只在{price_plan['profit_buyback']:.2f}元盈利回补；"
            "上涨时不高价追回，允许暂时少持。"
        )
    elif price_plan and price_plan.get("profit_buyback") is not None:
        lines.append(
            f"执行价位：盈利补回{price_plan['profit_buyback']:.2f}元；"
            f"若继续上冲至{price_plan['protective_buyback']:.2f}元，执行保护性补回。"
        )
    else:
        lines.append("执行价位：当前无有效买卖价位，不挂单。")
    if plan.get("cancel_conditions"):
        lines.append("不做/取消条件：" + "；".join(plan["cancel_conditions"]) + "。")
    failed_gates = [gate["detail"] for gate in plan.get("gates", []) if not gate["passed"]]
    if failed_gates:
        lines.append("未通过检查：" + "；".join(failed_gates) + "。")
    reverse_t = plan.get("reverse_t") or {}
    if reverse_t.get("enabled"):
        t_decision = reverse_t.get("decision") or {}
        rule = reverse_t.get("rule") or {}
        t_price = reverse_t.get("price_plan") or {}
        reverse_labels = {
            "hold": "等待",
            "sell_core_for_reverse_t": "冲高卖出老仓",
            "wait_buyback": "等待回补",
            "buyback_core": "盈利回补",
            "protective_buyback": "保护性回补",
            "review": "人工复核",
        }
        lines.append(
            f"反T额度：总仓位20%，当前最多{reverse_t.get('quota_lots', 0)}手；"
            f"单次最多{reverse_t.get('max_lots_per_trade', 1)}手，至少保留{reverse_t.get('core_floor_lots', 0)}手核心仓；"
            f"盈利回补目标约{float(reverse_t.get('buyback_gap_ratio', 0)) * 100:.1f}%。"
        )
        lines.append(
            f"反T规则：{rule.get('summary') or '等待价格冲高和10分钟K高位拐头'}"
        )
        lines.append(
            f"反T结论：{reverse_labels.get(t_decision.get('action'), t_decision.get('action', '等待'))}；"
            f"最多{t_decision.get('max_lots', 0)}手。"
            f"{t_decision.get('summary') or ''}"
        )
        if t_price.get("sell_limit") is not None and t_price.get("protective_buyback") is None:
            lines.append(
                f"反T价位：{t_price['sell_limit']:.2f}附近卖出；"
                f"只在{t_price['expected_buyback']:.2f}盈利回补，上涨时不高价追回。"
            )
        elif t_price.get("sell_limit") is not None:
            lines.append(
                f"反T价位：{t_price['sell_limit']:.2f}附近卖出；"
                f"按约{float(t_price.get('target_gap_ratio', 0)) * 100:.1f}%价差在{t_price['expected_buyback']:.2f}回补，"
                f"向上{t_price['protective_buyback']:.2f}保护性补回。"
            )
        elif t_price.get("profit_buyback") is not None and t_price.get("protective_buyback") is None:
            lines.append(
                f"反T回补：按约{float(t_price.get('target_gap_ratio', 0)) * 100:.1f}%价差"
                f"只在{t_price['profit_buyback']:.2f}盈利回补；上涨时不高价追回。"
            )
        elif t_price.get("profit_buyback") is not None:
            lines.append(
                f"反T回补：按约{float(t_price.get('target_gap_ratio', 0)) * 100:.1f}%价差盈利回补{t_price['profit_buyback']:.2f}；"
                f"向上{t_price['protective_buyback']:.2f}保护性补回。"
            )
    return "\n".join(lines)
