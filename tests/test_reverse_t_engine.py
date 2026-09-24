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
                "sell_spike_ratio": 0.020,
                "intraday_k_high": 80,
                "buyback_gap_ratio": 0.015,
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
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.78,
             "high": previous_close + 0.82, "low": previous_close + 0.74, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.75,
             "high": previous_close + 0.79, "low": previous_close + 0.73, "k": 74, "d": 77, "j": 68},
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
        self.assertEqual(result["buyback_gap_ratio"], 0.015)
        self.assertEqual(result["sell_spike_ratio"], 0.020)
        self.assertEqual(result["decision"]["action"], "sell_core_for_reverse_t")
        self.assertEqual(result["decision"]["max_lots"], 1)
        price_plan = result["price_plan"]
        self.assertEqual(price_plan["target_gap_ratio"], 0.015)
        self.assertGreaterEqual(
            (price_plan["sell_limit"] - price_plan["expected_buyback"]) / price_plan["sell_limit"],
            0.015,
        )

    def test_forming_ten_minute_k_keeps_real_time_turn_signal(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close * 1.023,
             "high": previous_close * 1.024, "low": previous_close * 1.021,
             "k": 85, "d": 78, "j": 99, "complete": True},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close * 1.022,
             "high": previous_close * 1.023, "low": previous_close * 1.020,
             "k": 74, "d": 77, "j": 68, "complete": False},
        ]
        result = build_reverse_t_plan(
            position=self.position, ledger=self.ledger, daily_series=daily,
            intraday_series=intraday, decision_date="2026-07-19", execution_enabled=True,
        )
        self.assertEqual(result["signal"]["intraday_timestamp"], "2026-07-19 10:20:00")
        self.assertTrue(result["signal"]["intraday_forming"])
        self.assertTrue(result["signal"]["turn_down_from_high"])
        self.assertEqual(result["decision"]["action"], "sell_core_for_reverse_t")

    def test_three_lot_quota_uses_same_trigger_and_never_sells_three_at_once(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        position = {
            **self.position,
            "reverse_t": {
                **self.position["reverse_t"],
                "allocation_ratio": 0.30,
                "fixed_quota_lots": 3,
                "core_floor_lots": 7,
                "max_lots_per_trade": 2,
            },
        }
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close * 1.024,
             "high": previous_close * 1.025, "low": previous_close * 1.022, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close * 1.021,
             "high": previous_close * 1.023, "low": previous_close * 1.020, "k": 74, "d": 77, "j": 68},
        ]
        result = build_reverse_t_plan(
            position=position,
            ledger=self.ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["quota_lots"], 3)
        self.assertEqual(result["core_floor_lots"], 7)
        self.assertEqual(result["decision"]["max_lots"], 2)
        self.assertNotIn("强信号", result["rule"]["summary"])

        one_slot_left = {
            **self.ledger,
            "core_lots": 8,
            "total_lots": 8,
            "sellable_core_lots_today": 8,
            "pending_core_buyback_lots": 2,
            "pending_core_sell_reference_price": 35.5,
        }
        reduced = build_reverse_t_plan(
            position=position,
            ledger=one_slot_left,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(reduced["signal"]["remaining_quota_lots"], 1)
        self.assertEqual(reduced["decision"]["max_lots"], 1)

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
        self.assertEqual(result["price_plan"]["profit_buyback"], 35.93)
        self.assertEqual(result["price_plan"]["target_gap_ratio"], 0.015)

    def test_multiple_pending_sales_use_separate_latest_buyback_layer(self) -> None:
        ledger = {
            **self.ledger,
            "core_lots": 8,
            "total_lots": 8,
            "sellable_core_lots_today": 8,
            "pending_core_buyback_lots": 2,
            "pending_core_sell_reference_price": 35.075,
            "pending_core_buyback_batches": [
                {"lots": 1, "sell_price": 35.50, "sell_date": "2026-09-11", "sell_trade_id": "newer"},
                {"lots": 1, "sell_price": 34.65, "sell_date": "2026-09-09", "sell_trade_id": "older"},
            ],
        }
        result = build_reverse_t_plan(
            position=self.position,
            ledger=ledger,
            daily_series=rising_daily(),
            intraday_series=[{
                "timestamp": "2026-09-11 10:20:00", "close": 35.20,
                "high": 35.30, "low": 35.15, "k": 50, "d": 55, "j": 40,
            }],
            decision_date="2026-09-11",
            execution_enabled=True,
        )
        self.assertEqual(result["price_plan"]["sell_reference"], 35.5)
        self.assertEqual(result["price_plan"]["profit_buyback"], 34.96)
        self.assertEqual(result["decision"]["max_lots"], 1)
        self.assertEqual(
            [item["profit_buyback"] for item in result["price_plan"]["buyback_layers"]],
            [34.96, 34.13],
        )

    def test_existing_buyback_order_keeps_formula_reference_and_waits_above_zone(self) -> None:
        position = {
            **self.position,
            "pending_orders": [{
                "id": "user-20260909-002179-buy-3410",
                "side": "buy",
                "bucket": "core",
                "lots": 1,
                "limit_price": 34.10,
                "status": "open",
                "linked_sell_trade_id": "user-20260909-002179-sell-3465",
            }],
        }
        ledger = {
            **self.ledger,
            "core_lots": 9,
            "sellable_core_lots_today": 9,
            "pending_core_buyback_lots": 1,
            "pending_core_sell_reference_price": 34.65,
        }
        intraday = [{
            "timestamp": "2026-09-09 14:20:00", "close": 34.20,
            "high": 34.25, "low": 34.15, "k": 30, "d": 40, "j": 10,
        }]
        result = build_reverse_t_plan(
            position=position,
            ledger=ledger,
            daily_series=rising_daily(),
            intraday_series=intraday,
            decision_date="2026-09-09",
            execution_enabled=True,
        )
        self.assertEqual(result["decision"]["action"], "wait_limit_buy")
        self.assertEqual(result["decision"]["status"], "watch")
        self.assertEqual(result["decision"]["max_lots"], 0)
        self.assertEqual(result["price_plan"]["profit_buyback"], 34.13)
        self.assertEqual(result["price_plan"]["existing_order_price"], 34.10)
        self.assertTrue(result["price_plan"]["order_within_tolerance"])
        self.assertEqual(result["price_plan"]["order_id"], "user-20260909-002179-buy-3410")
        self.assertEqual(result["price_plan"]["target_gap_ratio"], 0.015)
        self.assertAlmostEqual(result["price_plan"]["existing_order_gap_ratio"], 0.015873, places=6)

    def test_formula_zone_prompts_management_of_nearby_existing_order(self) -> None:
        position = {
            **self.position,
            "pending_orders": [{
                "id": "buyback-order",
                "side": "buy",
                "bucket": "core",
                "lots": 1,
                "limit_price": 34.10,
                "status": "open",
            }],
        }
        ledger = {
            **self.ledger,
            "core_lots": 9,
            "sellable_core_lots_today": 9,
            "pending_core_buyback_lots": 1,
            "pending_core_sell_reference_price": 34.65,
        }
        result = build_reverse_t_plan(
            position=position,
            ledger=ledger,
            daily_series=rising_daily(),
            intraday_series=[{
                "timestamp": "2026-09-09 14:40:00", "close": 34.12,
                "high": 34.18, "low": 34.10, "k": 30, "d": 40, "j": 10,
            }],
            decision_date="2026-09-09",
            execution_enabled=True,
        )
        self.assertEqual(result["decision"]["status"], "executable")
        self.assertEqual(result["decision"]["action"], "manage_existing_buyback")
        self.assertEqual(result["decision"]["max_lots"], 1)
        self.assertEqual(result["price_plan"]["profit_buyback"], 34.13)
        self.assertEqual(result["price_plan"]["existing_order_price"], 34.10)

    def test_flat_observation_exit_is_not_forced_to_buy_back(self) -> None:
        position = {
            **self.position,
            "reverse_t": {
                **self.position["reverse_t"],
                "fixed_quota_lots": 1,
                "core_floor_lots": 0,
                "buyback_gap_ratio": 0.022,
                "sell_spike_ratio": 0.022,
                "flat_exit_is_not_pending_buyback": True,
            },
        }
        ledger = {
            **self.ledger,
            "core_lots": 0,
            "total_lots": 0,
            "sellable_core_lots_today": 0,
            "pending_core_buyback_lots": 1,
            "pending_core_sell_reference_price": 40.33,
        }
        result = build_reverse_t_plan(
            position=position,
            ledger=ledger,
            daily_series=rising_daily(),
            intraday_series=[],
            decision_date="2026-09-08",
            execution_enabled=True,
        )
        self.assertEqual(result["decision"]["action"], "wait_new_entry")
        self.assertEqual(result["decision"]["max_lots"], 0)
        self.assertIsNone(result["price_plan"])
        self.assertEqual(result["buyback_gap_ratio"], 0.022)
        self.assertEqual(result["quota_lots"], 1)
        self.assertEqual(result["core_floor_lots"], 0)

    def test_one_lot_observation_position_uses_independent_two_point_two_percent_rule(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        position = {
            **self.position,
            "reverse_t": {
                **self.position["reverse_t"],
                "fixed_quota_lots": 1,
                "core_floor_lots": 0,
                "buyback_gap_ratio": 0.022,
                "sell_spike_ratio": 0.022,
            },
        }
        ledger = {
            **self.ledger,
            "core_lots": 1,
            "core_target_lots": 1,
            "total_lots": 1,
            "sellable_core_lots_today": 1,
        }
        intraday = [
            {"timestamp": "2026-09-08 10:10:00", "close": previous_close * 1.025,
             "high": previous_close * 1.026, "low": previous_close * 1.02, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-09-08 10:20:00", "close": previous_close * 1.023,
             "high": previous_close * 1.025, "low": previous_close * 1.02, "k": 76, "d": 78, "j": 72},
        ]
        result = build_reverse_t_plan(
            position=position,
            ledger=ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-09-08",
            execution_enabled=True,
        )
        self.assertEqual(result["decision"]["action"], "sell_core_for_reverse_t")
        self.assertEqual(result["decision"]["max_lots"], 1)
        self.assertEqual(result["buyback_gap_ratio"], 0.022)
        self.assertEqual(result["core_floor_lots"], 0)

    def test_two_lot_limit_uses_full_twenty_percent_quota(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        position = {
            **self.position,
            "reverse_t": {**self.position["reverse_t"], "max_lots_per_trade": 2},
        }
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.78,
             "high": previous_close + 0.82, "low": previous_close + 0.74, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.75,
             "high": previous_close + 0.79, "low": previous_close + 0.73, "k": 74, "d": 77, "j": 68},
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

    def test_t1_locked_lot_still_counts_toward_preserved_core_floor(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        position = {
            **self.position,
            "reverse_t": {**self.position["reverse_t"], "max_lots_per_trade": 2},
        }
        ledger = {**self.ledger, "sellable_core_lots_today": 9}
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.78,
             "high": previous_close + 0.82, "low": previous_close + 0.74, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.75,
             "high": previous_close + 0.79, "low": previous_close + 0.73, "k": 74, "d": 77, "j": 68},
        ]
        result = build_reverse_t_plan(
            position=position,
            ledger=ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["signal"]["available_lots"], 2)
        self.assertEqual(result["decision"]["max_lots"], 2)

    def test_one_pending_lot_leaves_one_slot_for_a_new_reverse_t_signal(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        ledger = {
            **self.ledger,
            "core_lots": 9,
            "total_lots": 9,
            "sellable_core_lots_today": 9,
            "pending_core_buyback_lots": 1,
            "pending_core_sell_reference_price": 34.65,
            "pending_core_buyback_batches": [{
                "lots": 1,
                "sell_price": 34.65,
                "sell_date": "2026-07-18",
                "sell_trade_id": "older-layer",
            }],
        }
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.78,
             "high": previous_close + 0.82, "low": previous_close + 0.74, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.75,
             "high": previous_close + 0.79, "low": previous_close + 0.73, "k": 74, "d": 77, "j": 68},
        ]

        result = build_reverse_t_plan(
            position=self.position,
            ledger=ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )

        self.assertEqual(result["signal"]["pending_buyback_lots"], 1)
        self.assertEqual(result["signal"]["remaining_quota_lots"], 1)
        self.assertEqual(result["signal"]["available_lots"], 1)
        self.assertEqual(result["decision"]["action"], "sell_core_for_reverse_t")
        self.assertEqual(result["decision"]["max_lots"], 1)
        self.assertIn("已有1手待补回", result["decision"]["summary"])
        self.assertEqual(
            result["price_plan"]["existing_buyback_layers"][0]["profit_buyback"],
            34.13,
        )

    def test_full_pending_quota_blocks_another_reverse_t_sell(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        ledger = {
            **self.ledger,
            "core_lots": 8,
            "total_lots": 8,
            "sellable_core_lots_today": 8,
            "pending_core_buyback_lots": 2,
            "pending_core_sell_reference_price": 35.50,
        }
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.78,
             "high": previous_close + 0.82, "low": previous_close + 0.74, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.75,
             "high": previous_close + 0.79, "low": previous_close + 0.73, "k": 74, "d": 77, "j": 68},
        ]

        result = build_reverse_t_plan(
            position=self.position,
            ledger=ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )

        self.assertEqual(result["signal"]["remaining_quota_lots"], 0)
        self.assertEqual(result["signal"]["available_lots"], 0)
        self.assertEqual(result["decision"]["action"], "wait_buyback")
        self.assertIn("占满2手", result["cancel_conditions"][0])

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

    def test_completed_cycle_with_pending_layer_does_not_claim_sell_quota_is_usable_today(self) -> None:
        daily = rising_daily()
        previous_close = daily[-1]["close"]
        ledger = {
            **self.ledger,
            "core_lots": 9,
            "total_lots": 9,
            "sellable_core_lots_today": 7,
            "pending_core_buyback_lots": 1,
            "pending_core_sell_reference_price": 35.0,
            "pending_core_buyback_batches": [{
                "lots": 1,
                "sell_price": 35.0,
                "sell_date": "2026-07-18",
                "sell_trade_id": "older-layer",
            }],
            "completed_core_roundtrip_events_today": 1,
        }
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.78,
             "high": previous_close + 0.82, "low": previous_close + 0.74, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.75,
             "high": previous_close + 0.79, "low": previous_close + 0.73, "k": 74, "d": 77, "j": 68},
        ]

        result = build_reverse_t_plan(
            position=self.position,
            ledger=ledger,
            daily_series=daily,
            intraday_series=intraday,
            decision_date="2026-07-19",
            execution_enabled=True,
        )

        self.assertEqual(result["decision"]["action"], "wait_buyback")
        self.assertIn("达到每日1轮上限", result["decision"]["summary"])
        self.assertIn("不再开启新的卖出层", result["cancel_conditions"][0])
        self.assertNotIn("可继续使用", result["cancel_conditions"][0])

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

    def test_existing_sell_order_is_shown_without_duplicate_sell_signal(self) -> None:
        position = {
            **self.position,
            "pending_orders": [{
                "id": "sell-3520", "side": "sell", "bucket": "core", "lots": 1,
                "limit_price": 35.2, "conditional_buyback_price": 34.5, "status": "open",
            }],
        }
        result = build_reverse_t_plan(
            position=position,
            ledger=self.ledger,
            daily_series=rising_daily(),
            intraday_series=[],
            decision_date="2026-07-19",
            execution_enabled=True,
        )
        self.assertEqual(result["decision"]["action"], "wait_limit_sell")
        self.assertEqual(result["decision"]["max_lots"], 0)
        self.assertEqual(result["price_plan"]["sell_limit"], 35.2)
        self.assertEqual(result["price_plan"]["expected_buyback"], 34.5)

    def test_parked_trend_filter_does_not_block_simple_rule(self) -> None:
        daily = list(reversed(rising_daily()))
        for index, row in enumerate(daily):
            row["timestamp"] = (date(2026, 5, 1) + timedelta(days=index)).isoformat()
        previous_close = daily[-1]["close"]
        intraday = [
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.78,
             "high": previous_close + 0.82, "low": previous_close + 0.74, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.75,
             "high": previous_close + 0.79, "low": previous_close + 0.73, "k": 74, "d": 77, "j": 68},
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
            {"timestamp": "2026-07-19 10:10:00", "close": previous_close + 0.78,
             "high": previous_close + 0.82, "low": previous_close + 0.74, "k": 85, "d": 78, "j": 99},
            {"timestamp": "2026-07-19 10:20:00", "close": previous_close + 0.75,
             "high": previous_close + 0.79, "low": previous_close + 0.73, "k": 74, "d": 77, "j": 68},
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
