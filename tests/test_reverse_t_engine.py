from __future__ import annotations

import unittest
from datetime import date, timedelta

from src.reverse_t_engine import build_reverse_t_plan


def rising_daily() -> list[dict]:
    start = date(2026, 5, 1)
    rows = []
    for index in range(80):
        close = 25 + index * 0.12
        rows.append({
            "timestamp": (start + timedelta(days=index)).isoformat(),
            "open": close - 0.1,
            "high": close + 0.25,
            "low": close - 0.25,
            "close": close,
            "k": 60,
            "d": 55,
        })
    return rows


class ReverseTEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.position = {
            "reverse_t": {
                "enabled": True,
                "allocation_ratio": 0.20,
                "max_lots_per_trade": 1,
                "max_daily_cycles": 1,
                "trend_filter_enabled": False,
                "trend_ma_short": 20,
                "trend_ma_long": 60,
                "trend_slope_days": 5,
                "sell_spike_ratio": 0.018,
                "intraday_k_high": 80,
                "buyback_gap_ratio": 0.018,
                "protective_buyback_enabled": False,
            }
        }
        self.ledger = {
            "core_lots": 10,
            "core_target_lots": 10,
            "sellable_core_lots_today": 10,
            "pending_core_buyback_lots": 0,
            "pending_core_sell_reference_price": None,
        }

    def test_simple_spike_turn_down_sells_only_one_old_lot(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.65,
             "high": previous_close + 0.7, "low": previous_close + 0.6, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.62,
             "high": previous_close + 0.66, "low": previous_close + 0.6, "k": 74, "d": 77, "j": 68},
        ]
        result = build_reverse_t_plan(
            position=self.position,
            ledger=self.ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertTrue(result["trend"]["passed"])
        self.assertFalse(result["trend_filter_enabled"])
        self.assertEqual(result["quota_lots"], 2)
        self.assertEqual(result["core_floor_lots"], 8)
        self.assertEqual(result["buyback_gap_ratio"], 0.018)
        self.assertEqual(result["decision"]["action"], "sell_core_for_reverse_t")
        self.assertEqual(result["decision"]["max_lots"], 1)
        price_plan = result["price_plan"]
        self.assertEqual(price_plan["target_gap_ratio"], 0.018)
        self.assertGreaterEqual(
            (price_plan["sell_limit"] - price_plan["expected_buyback"]) / price_plan["sell_limit"],
            0.018,
        )

    def test_pending_sell_blocks_new_cycle_and_generates_profit_buyback(self) -> None:
        ledger = {
            **self.ledger,
            "core_lots": 9,
            "sellable_core_lots_today": 9,
            "pending_core_buyback_lots": 1,
            "pending_core_sell_reference_price": 36.48,
        }
        intraday = [{
            "timestamp": "2026-07-19 10:20:00", "close": 35.80,
            "high": 35.90, "low": 35.75, "k": 30, "d": 40, "j": 10,
        }]
        result = build_reverse_t_plan(
            position=self.position,
            ledger=ledger,
            daily_series=rising_daily(),
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["decision"]["action"], "buyback_core")
        self.assertEqual(result["decision"]["max_lots"], 1)
        self.assertEqual(result["price_plan"]["profit_buyback"], 35.82)
        self.assertEqual(result["price_plan"]["target_gap_ratio"], 0.018)

    def test_two_lot_limit_uses_full_twenty_percent_quota(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        position = {
            **self.position,
            "reverse_t": {**self.position["reverse_t"], "max_lots_per_trade": 2},
        }
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.65,
             "high": previous_close + 0.7, "low": previous_close + 0.6, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.62,
             "high": previous_close + 0.66, "low": previous_close + 0.6, "k": 74, "d": 77, "j": 68},
        ]
        result = build_reverse_t_plan(
            position=position,
            ledger=self.ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["quota_lots"], 2)
        self.assertEqual(result["core_floor_lots"], 8)
        self.assertEqual(result["decision"]["action"], "sell_core_for_reverse_t")
        self.assertEqual(result["decision"]["max_lots"], 2)
        self.assertIn("最多卖出2手", result["rule"]["summary"])

    def test_t1_lock_reduces_two_lot_quota_to_one_sellable_lot(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        position = {
            **self.position,
            "reverse_t": {**self.position["reverse_t"], "max_lots_per_trade": 2},
        }
        ledger = {**self.ledger, "sellable_core_lots_today": 9}
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.65,
             "high": previous_close + 0.7, "low": previous_close + 0.6, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.62,
             "high": previous_close + 0.66, "low": previous_close + 0.6, "k": 74, "d": 77, "j": 68},
        ]
        result = build_reverse_t_plan(
            position=position,
            ledger=ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["signal"]["available_lots"], 1)
        self.assertEqual(result["decision"]["max_lots"], 1)

    def test_buyback_completed_today_blocks_opening_another_daily_cycle(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        position = {
            **self.position,
            "reverse_t": {**self.position["reverse_t"], "max_lots_per_trade": 2},
        }
        ledger = {
            **self.ledger,
            "sellable_core_lots_today": 9,
            "completed_core_roundtrip_events_today": 1,
        }
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.65,
             "high": previous_close + 0.7, "low": previous_close + 0.6, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.62,
             "high": previous_close + 0.66, "low": previous_close + 0.6, "k": 74, "d": 77, "j": 68},
        ]
        result = build_reverse_t_plan(
            position=position,
            ledger=ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["signal"]["cycles_today"], 1)
        self.assertEqual(result["decision"]["action"], "hold")
        self.assertIn("今日已达到1轮", result["decision"]["summary"])

    def test_pending_sell_does_not_chase_higher_when_protection_is_disabled(self) -> None:
        ledger = {
            **self.ledger,
            "core_lots": 9,
            "sellable_core_lots_today": 9,
            "pending_core_buyback_lots": 1,
            "pending_core_sell_reference_price": 34.99,
        }
        intraday = [{
            "timestamp": "2026-07-19 10:20:00", "close": 35.50,
            "high": 35.55, "low": 35.45, "k": 92, "d": 85, "j": 106,
        }]
        result = build_reverse_t_plan(
            position=self.position,
            ledger=ledger,
            daily_series=rising_daily(),
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["decision"]["action"], "wait_buyback")
        self.assertNotIn("protective_buyback", result["price_plan"])

    def test_parked_trend_filter_does_not_block_simple_rule(self) -> None:
        daily = list(reversed(rising_daily()))
        for index, row in enumerate(daily):
            row["timestamp"] = (date(2026, 5, 1) + timedelta(days=index)).isoformat()
        previous_close = daily[-1]["close"]
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.50,
             "high": previous_close + 0.55, "low": previous_close + 0.45, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.48,
             "high": previous_close + 0.51, "low": previous_close + 0.44, "k": 74, "d": 77, "j": 68},
        ]
        result = build_reverse_t_plan(
            position=self.position,
            ledger=self.ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertFalse(result["trend"]["passed"])
        self.assertEqual(result["decision"]["action"], "sell_core_for_reverse_t")

    def test_trend_filter_can_be_reenabled_later(self) -> None:
        daily = list(reversed(rising_daily()))
        for index, row in enumerate(daily):
            row["timestamp"] = (date(2026, 5, 1) + timedelta(days=index)).isoformat()
        previous_close = daily[-1]["close"]
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.50,
             "high": previous_close + 0.55, "low": previous_close + 0.45, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.48,
             "high": previous_close + 0.51, "low": previous_close + 0.44, "k": 74, "d": 77, "j": 68},
        ]
        position = {
            **self.position,
            "reverse_t": {**self.position["reverse_t"], "trend_filter_enabled": True},
        }
        result = build_reverse_t_plan(
            position=position,
            ledger=self.ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertFalse(result["trend"]["passed"])
        self.assertEqual(result["decision"]["action"], "hold")
        self.assertIn("备用上升趋势过滤未通过", result["decision"]["summary"])

    def test_second_sell_same_day_is_blocked(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        position = {
            **self.position,
            "trade_history": [{
                "side": "sell", "bucket": "core", "lots": 1, "price": previous_close + 0.4,
                "reported_at": "2026-07-19 09:50:00",
            }],
        }
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.65,
             "high": previous_close + 0.7, "low": previous_close + 0.6, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.62,
             "high": previous_close + 0.66, "low": previous_close + 0.6, "k": 74, "d": 77, "j": 68},
        ]
        result = build_reverse_t_plan(
            position=position,
            ledger=self.ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["decision"]["action"], "hold")
        self.assertIn("今日已达到", result["decision"]["summary"])


if __name__ == "__main__":
    unittest.main()
