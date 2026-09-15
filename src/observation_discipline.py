from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


DEFAULT_OBSERVATION_DISCIPLINE: dict[str, Any] = {
    "enabled": True,
    "entry_mode": "stabilized_signal_only",
    "allow_preplanned_limit_orders": True,
    "preplanned_limit_lots_per_symbol": 1,
    "forbid_raise_limit_price": True,
    "earliest_entry_time": "14:45",
    "latest_entry_time": "14:55",
    "no_new_low_minutes": 30,
    "stabilization_rebound_ratio": 0.005,
    "max_bar_age_minutes": 10,
    "total_capital_limit": 20_000,
    "max_new_symbols_per_day": 1,
    "min_days_before_add": 5,
}


def observation_discipline(config: dict[str, Any]) -> dict[str, Any]:
    """Return the stable candidate-entry discipline with safe defaults."""
    configured = config.get("observation_discipline") or {}
    return {**DEFAULT_OBSERVATION_DISCIPLINE, **configured}


def format_observation_discipline(config: dict[str, Any]) -> str:
    rule = observation_discipline(config)
    if not bool(rule.get("enabled", True)):
        return "观察仓纪律：当前未启用。"
    preplanned = (
        "允许每只最多"
        f"{int(rule['preplanned_limit_lots_per_symbol'])}手经确认的低价限价单并存，"
        "但最坏全部成交后仍须满足观察仓总额上限，且盘中不得抬价追单；"
        if bool(rule.get("allow_preplanned_limit_orders", True)) else
        "不预挂买单；"
    )
    return (
        "观察仓纪律：空仓候选到价后由系统在后台判断止跌；"
        f"{preplanned}"
        f"只在{rule['earliest_entry_time']}—{rule['latest_entry_time']}之间，且连续"
        f"{int(rule['no_new_low_minutes'])}分钟不创新低、出现回稳后，"
        "系统才发送允许买入消息；"
        f"观察仓总额不超过{float(rule['total_capital_limit']):.0f}元；"
        f"同一天最多新买{int(rule['max_new_symbols_per_day'])}只股票；"
        f"买入后至少{int(rule['min_days_before_add'])}个交易日不加仓。"
    )


def evaluate_stabilization(
    bars: list[dict[str, Any]],
    *,
    now: datetime,
    current_price: float,
    discipline: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate a conservative, price-only intraday stabilization rule.

    This is deliberately not a prediction.  It requires a fresh 5-minute
    series, at least 30 minutes without a new session low, a small rebound
    from that low, and a non-negative last-30-minute price direction.
    """
    earliest_text = str(discipline.get("earliest_entry_time") or "14:45")
    try:
        earliest = datetime.strptime(earliest_text, "%H:%M").time()
    except ValueError:
        earliest = datetime.strptime("14:45", "%H:%M").time()
    if now.time() < earliest:
        return {"ready": False, "reason": f"尚未到{earliest_text}"}
    latest_text = str(discipline.get("latest_entry_time") or "14:55")
    try:
        latest = datetime.strptime(latest_text, "%H:%M").time()
    except ValueError:
        latest = datetime.strptime("14:55", "%H:%M").time()
    if now.time() > latest:
        return {"ready": False, "reason": f"已超过{latest_text}可执行时间"}

    today = now.strftime("%Y-%m-%d")
    parsed: list[dict[str, Any]] = []
    for raw in bars:
        timestamp = raw.get("datetime") or raw.get("timestamp")
        try:
            point_time = datetime.fromisoformat(str(timestamp))
            low = float(raw["low"])
            close = float(raw["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if point_time.strftime("%Y-%m-%d") != today or point_time > now + timedelta(minutes=1):
            continue
        parsed.append({"time": point_time, "low": low, "close": close})
    parsed.sort(key=lambda item: item["time"])
    if len(parsed) < 7:
        return {"ready": False, "reason": "当天5分钟线不足30分钟"}

    max_age = int(discipline.get("max_bar_age_minutes", 10) or 10)
    if now - parsed[-1]["time"] > timedelta(minutes=max_age):
        return {"ready": False, "reason": "5分钟线不新鲜"}

    session_low = min(item["low"] for item in parsed)
    latest_low_time = max(item["time"] for item in parsed if abs(item["low"] - session_low) < 1e-9)
    stable_minutes = int(discipline.get("no_new_low_minutes", 30) or 30)
    if now - latest_low_time < timedelta(minutes=stable_minutes):
        return {"ready": False, "reason": f"距最近新低不足{stable_minutes}分钟"}

    window_start = now - timedelta(minutes=stable_minutes)
    recent = [item for item in parsed if item["time"] >= window_start]
    if len(recent) < 5:
        return {"ready": False, "reason": "回稳观察窗口数据不足"}
    if current_price + 1e-9 < recent[0]["close"]:
        return {"ready": False, "reason": f"最近{stable_minutes}分钟仍在走低"}
    if parsed[-1]["close"] + 1e-9 < parsed[-2]["close"]:
        return {"ready": False, "reason": "最新5分钟线仍在回落"}

    rebound_required = float(discipline.get("stabilization_rebound_ratio", 0.005) or 0)
    rebound_ratio = current_price / session_low - 1 if session_low > 0 else 0.0
    if rebound_ratio + 1e-12 < rebound_required:
        return {"ready": False, "reason": "尚未形成最低回升幅度"}

    return {
        "ready": True,
        "reason": "到价后已连续观察且价格回稳",
        "session_low": round(session_low, 4),
        "latest_low_time": latest_low_time.strftime("%Y-%m-%d %H:%M:%S"),
        "rebound_ratio": round(rebound_ratio, 6),
        "window_minutes": stable_minutes,
        "bar_time": parsed[-1]["time"].strftime("%Y-%m-%d %H:%M:%S"),
        "last_bar_non_declining": True,
    }
