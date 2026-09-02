from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from typing import Any, Optional


LOT_SIZE = 100


class LedgerError(ValueError):
    """Raised when an appended trade would make the ledger impossible."""


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _bucket(value: Any) -> str:
    text = str(value or "auto").strip().lower()
    aliases = {
        "base": "core",
        "核心": "core",
        "核心仓": "core",
        "t": "tactical",
        "t仓": "tactical",
        "机动": "tactical",
        "机动仓": "tactical",
    }
    return aliases.get(text, text if text in {"core", "tactical"} else "auto")


def _lots(batches: list[dict[str, Any]]) -> float:
    return sum(float(item["lots"]) for item in batches)


def _book_value(batches: list[dict[str, Any]]) -> float:
    return sum(float(item["lots"]) * float(item["cost_per_lot"]) for item in batches)


def _consume(
    batches: list[dict[str, Any]],
    requested_lots: float,
    trade_date: str,
) -> tuple[float, float]:
    """Consume sellable lots FIFO and return (lots, inventory book value)."""
    remaining = float(requested_lots)
    consumed = 0.0
    book_value = 0.0
    for batch in batches:
        if remaining <= 0:
            break
        if _date_text(batch.get("acquired_at")) >= trade_date:
            continue
        take = min(float(batch["lots"]), remaining)
        if take <= 0:
            continue
        batch["lots"] = float(batch["lots"]) - take
        consumed += take
        remaining -= take
        book_value += take * float(batch["cost_per_lot"])
    batches[:] = [item for item in batches if float(item["lots"]) > 1e-9]
    return consumed, book_value


def _opening_batches(position: dict[str, Any], warnings: list[str]) -> tuple[dict[str, list[dict[str, Any]]], float, float]:
    opening = position.get("opening") or {}
    if not opening:
        warnings.append("缺少opening起始仓位；已使用旧汇总字段兼容推断，请尽快补齐")
        opening = {
            "as_of": "1900-01-01",
            "core_lots": position.get("base_lots_remaining", position.get("base_lots", 0)),
            "t_lots": position.get("t_lots_held", 0),
            "cost_per_share": position.get("cost", 0),
        }

    opening_date = _date_text(opening.get("as_of") or "1900-01-01")
    core_lots = float(opening.get("core_lots", 0) or 0)
    tactical_lots = float(opening.get("t_lots", 0) or 0)
    core_cost = float(opening.get("cost_per_share", 0) or 0)
    tactical_cost = float(opening.get("t_cost_per_share", core_cost) or core_cost)

    batches: dict[str, list[dict[str, Any]]] = {"core": [], "tactical": []}
    if core_lots:
        batches["core"].append({
            "lots": core_lots,
            "cost_per_lot": core_cost * LOT_SIZE,
            "acquired_at": opening_date,
        })
    if tactical_lots:
        batches["tactical"].append({
            "lots": tactical_lots,
            "cost_per_lot": tactical_cost * LOT_SIZE,
            "acquired_at": opening_date,
        })
    opening_value = core_lots * core_cost * LOT_SIZE + tactical_lots * tactical_cost * LOT_SIZE
    return batches, core_lots, opening_value


def replay_position(
    position: dict[str, Any],
    *,
    as_of: Optional[str] = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Replay opening position plus append-only trades into a deterministic summary.

    Costs use two explicit meanings:
    - average_entry_cost: book cost of shares still held;
    - breakeven_cost: net cash invested after realized sale proceeds / shares held.
    """
    cutoff = _date_text(as_of or date.today().isoformat())
    warnings: list[str] = []
    errors: list[str] = []
    batches, opening_core_lots, opening_value = _opening_batches(position, warnings)
    core_target_lots = opening_core_lots
    net_cash_invested = opening_value
    realized_pnl = 0.0
    fees_total = 0.0
    completed_core_roundtrip_lots = 0.0
    completed_core_roundtrip_events = 0
    completed_core_roundtrip_events_today = 0
    core_roundtrip_gross_pnl = 0.0
    core_roundtrip_fees = 0.0
    processed_trades = 0
    pending_core_sales: list[dict[str, Any]] = []

    history = position.get("trade_history") or []
    previous_timestamp = ""
    for index, raw_trade in enumerate(history):
        trade = deepcopy(raw_trade)
        timestamp = str(trade.get("reported_at") or "")
        trade_date = _date_text(timestamp)
        if trade_date and trade_date > cutoff:
            continue
        if previous_timestamp and timestamp and timestamp < previous_timestamp:
            warnings.append(f"第{index + 1}笔成交时间早于上一笔，仍按流水顺序重放")
        previous_timestamp = timestamp or previous_timestamp

        side = str(trade.get("side") or "").strip().lower()
        lots = float(trade.get("lots", 0) or 0)
        price = trade.get("price")
        if side not in {"buy", "sell"} or lots <= 0:
            errors.append(f"第{index + 1}笔成交的方向或手数无效")
            continue
        if not trade_date:
            errors.append(f"第{index + 1}笔成交缺少reported_at")
            continue
        if price is None or float(price) <= 0:
            errors.append(f"第{index + 1}笔成交缺少有效成交价")
            continue

        price = float(price)
        fee = float(trade.get("fee", float(position.get("fee_per_lot", 5) or 5) * lots) or 0)
        fees_total += fee
        requested_bucket = _bucket(trade.get("bucket"))
        note_text = str(trade.get("note") or "")

        if side == "buy":
            if requested_bucket == "auto":
                requested_bucket = "tactical" if ("T仓" in note_text or "机动" in note_text) else "core"
            if requested_bucket == "core":
                remaining_buy = lots
                matched_buyback_lots = 0.0
                for pending in pending_core_sales:
                    if remaining_buy <= 1e-9:
                        break
                    matched = min(float(pending["lots"]), remaining_buy)
                    completed_core_roundtrip_lots += matched
                    matched_buyback_lots += matched
                    core_roundtrip_gross_pnl += (
                        float(pending["price"]) - price
                    ) * LOT_SIZE * matched
                    core_roundtrip_fees += (
                        float(pending.get("fee_per_lot", 0) or 0) + fee / lots
                    ) * matched
                    pending["lots"] = float(pending["lots"]) - matched
                    remaining_buy -= matched
                if matched_buyback_lots > 1e-9:
                    completed_core_roundtrip_events += 1
                    if trade_date == cutoff:
                        completed_core_roundtrip_events_today += 1
                pending_core_sales = [item for item in pending_core_sales if float(item["lots"]) > 1e-9]
            cost_per_lot = price * LOT_SIZE + fee / lots
            batches[requested_bucket].append({
                "lots": lots,
                "cost_per_lot": cost_per_lot,
                "acquired_at": trade_date,
            })
            net_cash_invested += price * LOT_SIZE * lots + fee
            if requested_bucket == "core":
                core_target_lots = max(core_target_lots, _lots(batches["core"]))
        else:
            order = [requested_bucket] if requested_bucket != "auto" else ["tactical", "core"]
            remaining = lots
            inventory_cost = 0.0
            sold_lots = 0.0
            for bucket_name in order:
                consumed, consumed_cost = _consume(batches[bucket_name], remaining, trade_date)
                remaining -= consumed
                sold_lots += consumed
                inventory_cost += consumed_cost
                if bucket_name == "core" and consumed > 0:
                    pending_core_sales.append({
                        "lots": consumed,
                        "price": price,
                        "reported_at": timestamp,
                        "fee_per_lot": fee / lots,
                    })
            if remaining > 1e-9:
                errors.append(
                    f"第{index + 1}笔卖出{lots:g}手违反持仓/T+1约束，"
                    f"当时最多可卖{sold_lots:g}手"
                )
            if sold_lots:
                allocated_fee = fee * sold_lots / lots
                proceeds = price * LOT_SIZE * sold_lots - allocated_fee
                net_cash_invested -= proceeds
                realized_pnl += proceeds - inventory_cost
        processed_trades += 1

    core_lots = _lots(batches["core"])
    tactical_lots = _lots(batches["tactical"])
    total_lots = core_lots + tactical_lots
    total_book_value = _book_value(batches["core"]) + _book_value(batches["tactical"])
    sellable_core = sum(
        float(item["lots"]) for item in batches["core"]
        if _date_text(item.get("acquired_at")) < cutoff
    )
    sellable_tactical = sum(
        float(item["lots"]) for item in batches["tactical"]
        if _date_text(item.get("acquired_at")) < cutoff
    )
    sellable_lots = sellable_core + sellable_tactical
    bought_today_lots = sum(
        float(item["lots"]) for bucket_batches in batches.values() for item in bucket_batches
        if _date_text(item.get("acquired_at")) == cutoff
    )

    max_t_lots = float(position.get("max_t_lots", 0) or 0)
    if max_t_lots and tactical_lots > max_t_lots:
        warnings.append(f"T仓{tactical_lots:g}手超过配置上限{max_t_lots:g}手")

    if strict and errors:
        raise LedgerError("；".join(errors))

    def clean_lots(value: float) -> int | float:
        return int(round(value)) if abs(value - round(value)) < 1e-9 else round(value, 4)

    share_count = total_lots * LOT_SIZE
    pending_reference_lots = sum(float(item["lots"]) for item in pending_core_sales)
    pending_reference_price = (
        sum(float(item["lots"]) * float(item["price"]) for item in pending_core_sales) / pending_reference_lots
        if pending_reference_lots else None
    )
    pending_reference_date = (
        max(_date_text(item.get("reported_at")) for item in pending_core_sales)
        if pending_core_sales else None
    )
    return {
        "as_of": cutoff,
        "core_lots": clean_lots(core_lots),
        "t_lots": clean_lots(tactical_lots),
        "total_lots": clean_lots(total_lots),
        "core_target_lots": clean_lots(core_target_lots),
        "pending_core_buyback_lots": clean_lots(max(0.0, core_target_lots - core_lots)),
        "pending_core_sell_reference_price": round(pending_reference_price, 4) if pending_reference_price else None,
        "pending_core_sell_reference_date": pending_reference_date,
        "sellable_lots_today": clean_lots(sellable_lots),
        "sellable_core_lots_today": clean_lots(sellable_core),
        "sellable_t_lots_today": clean_lots(sellable_tactical),
        "locked_t1_lots": clean_lots(max(0.0, total_lots - sellable_lots)),
        "bought_today_lots": clean_lots(bought_today_lots),
        "average_entry_cost": round(total_book_value / share_count, 4) if share_count else None,
        "breakeven_cost": round(net_cash_invested / share_count, 4) if share_count else None,
        "net_cash_invested": round(net_cash_invested, 2),
        "realized_pnl": round(realized_pnl, 2),
        "fees_total": round(fees_total, 2),
        "completed_core_roundtrip_lots": clean_lots(completed_core_roundtrip_lots),
        "completed_core_roundtrip_events": completed_core_roundtrip_events,
        "completed_core_roundtrip_events_today": completed_core_roundtrip_events_today,
        "core_roundtrip_gross_pnl": round(core_roundtrip_gross_pnl, 2),
        "core_roundtrip_fees": round(core_roundtrip_fees, 2),
        "core_roundtrip_net_pnl": round(core_roundtrip_gross_pnl - core_roundtrip_fees, 2),
        "processed_trades": processed_trades,
        "validation": {"ok": not errors, "errors": errors, "warnings": warnings},
    }


def apply_ledger_summary(position: dict[str, Any], summary: dict[str, Any]) -> None:
    """Refresh legacy aggregate fields for compatibility with the existing UI."""
    position["base_lots"] = summary["core_target_lots"]
    position["base_lots_remaining"] = summary["core_lots"]
    position["t_lots_held"] = summary["t_lots"]
    position["cost"] = summary["breakeven_cost"]
    position["average_entry_cost"] = summary["average_entry_cost"]
    position["realized_pnl"] = summary["realized_pnl"]
    position["sellable_lots_today"] = summary["sellable_lots_today"]
