from __future__ import annotations

import unittest

from src.decision_engine import build_decision_plan, format_decision_plan


def bar(day: str, close: float, k: float, d: float, low: float | None = None) -> dict:
    return {
        "timestamp": day,
        "open": close,
        "high": close + 0.3,
        "low": low if low is not None else close - 0.3,
        "close": close,
        "k": k,
        "d": d,
        "j": 2 * k - d,
    }


class DecisionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.position = {
            "strategy_mode": "expand_base",
            "strategy_budget": 200000,
            "sleeve_high_water": 200000,
            "max_deployed_ratio": 0.85,
            "next_stage_base_lots": 15,
            "max_daily_add_lots": 5,
            "max_oversold_cycle_add_lots": 10,
            "max_t_lots": 10,
            "fee_per_lot": 5,
            "drawdown_pause": 0.10,
            "drawdown_review": 0.15,
            "drawdown_no_add": 0.20,
            "tactical_enabled": False,
            "fundamental_gate": {"status": "pass", "note": "测试中视为通过"},
            "signal_rules": {
                "buy_k": 15,
                "rebound_k_max": 25,
                "sell_k": 80,
                "oversold_lookback": 3,
                "confirmation_min": 2,
                "max_first_tranche_lots": 2,
                "max_chase_ratio": 0.03,
                "atr_zone_fraction": 0.25,
            },
            "opening": {
                "as_of": "2026-07-31",
                "core_lots": 10,
                "t_lots": 0,
                "cost_per_share": 10,
            },
            "trade_history": [],
        }
        self.series = [
            bar("2026-08-01", 10.0, 20, 25, 9.8),
            bar("2026-08-02", 9.5, 12, 20, 9.3),
            bar("2026-08-03", 9.7, 16, 15, 9.4),
        ]

    def plan(self, latest: dict | None = None, position: dict | None = None,
             performance_state: dict | None = None) -> dict:
        return build_decision_plan(
            symbol_code="002179",
            symbol_name="中航光电",
            latest_daily=latest or self.series[-1],
            daily_series=self.series,
            position=position or self.position,
            decision_date="2026-08-03",
            performance_state=performance_state,
        )

    def test_confirmed_oversold_rebound_allows_small_core_tranche(self) -> None:
        result = self.plan()
        self.assertEqual(result["decision"]["status"], "executable")
        self.assertEqual(result["decision"]["action"], "buy_core")
        self.assertEqual(result["decision"]["max_lots"], 2)
        self.assertEqual(result["after_action"]["core_lots"], 12)
        self.assertLessEqual(result["after_action"]["deployed_ratio"], 0.85)
        self.assertIn("执行价位", format_decision_plan(result))
        self.assertIn("账本重算保本成本", format_decision_plan(result))
        self.assertNotIn("平均买入成本", format_decision_plan(result))

    def test_intraday_estimate_cannot_authorize_buy(self) -> None:
        latest = {**self.series[-1], "estimated": True}
        result = self.plan(latest=latest)
        self.assertEqual(result["decision"]["action"], "hold")
        self.assertEqual(result["decision"]["status"], "blocked")
        self.assertIn("GATE_CONFIRMED_DAILY", result["decision"]["reason_codes"])

    def test_stale_confirmed_daily_cannot_authorize_buy(self) -> None:
        result = build_decision_plan(
            symbol_code="002179",
            symbol_name="中航光电",
            latest_daily=self.series[-1],
            daily_series=self.series,
            position=self.position,
            decision_date="2026-08-10",
        )
        self.assertTrue(result["market"]["confirmed_daily"])
        self.assertFalse(result["market"]["fresh_confirmed_daily"])
        self.assertEqual(result["decision"]["action"], "hold")
        self.assertEqual(result["decision"]["status"], "blocked")

    def test_fundamental_block_cancels_expansion(self) -> None:
        position = {**self.position, "fundamental_gate": {"status": "block", "note": "重大风险待核对"}}
        result = self.plan(position=position)
        self.assertEqual(result["decision"]["action"], "review")
        self.assertEqual(result["decision"]["status"], "blocked")
        self.assertIn("GATE_FUNDAMENTAL", result["decision"]["reason_codes"])

    def test_drawdown_pause_blocks_new_core_lots(self) -> None:
        result = self.plan(performance_state={"high_water_equity": 250000, "max_drawdown": -0.20})
        self.assertEqual(result["decision"]["action"], "review")
        self.assertEqual(result["decision"]["status"], "blocked")
        self.assertIn("GATE_DRAWDOWN", result["decision"]["reason_codes"])
        self.assertEqual(result["performance"]["historical_max_drawdown"], -0.20)

    def test_long_term_stock_never_uses_zhonghang_rules(self) -> None:
        position = {**self.position, "strategy_mode": "long_term"}
        result = self.plan(position=position)
        self.assertEqual(result["strategy_scope"], "long_term")
        self.assertEqual(result["decision"]["action"], "hold")
        self.assertEqual(result["decision"]["max_lots"], 0)

    def test_long_term_open_limit_order_is_shown_without_changing_position(self) -> None:
        position = {
            **self.position,
            "strategy_mode": "long_term",
            "pending_orders": [{
                "id": "order-1", "side": "buy", "bucket": "core", "lots": 1,
                "limit_price": 40.0, "status": "open", "placed_at": "2026-08-27",
            }],
        }
        result = self.plan(position=position)
        self.assertEqual(result["decision"]["action"], "wait_limit_buy")
        self.assertEqual(result["price_plan"]["execution"], "existing_limit_order")
        self.assertEqual(result["price_plan"]["price"], 40.0)
        self.assertEqual(result["facts"]["ledger"]["total_lots"], 10)

    def test_enabled_tactical_position_sells_only_t_lots_at_high_k(self) -> None:
        position = {
            **self.position,
            "tactical_enabled": True,
            "opening": {
                "as_of": "2026-07-31",
                "core_lots": 10,
                "t_lots": 2,
                "cost_per_share": 10,
            },
        }
        high = bar("2026-08-03", 11.0, 85, 80, 10.7)
        result = self.plan(latest=high, position=position)
        self.assertEqual(result["decision"]["action"], "sell_tactical")
        self.assertEqual(result["decision"]["max_lots"], 2)
        self.assertEqual(result["decision"]["bucket"], "tactical")

    def test_pending_reverse_t_sell_becomes_the_main_buyback_plan(self) -> None:
        position = {
            **self.position,
            "reverse_t": {
                "enabled": True,
                "allocation_ratio": 0.2,
                "max_lots_per_trade": 1,
                "max_daily_cycles": 1,
                "sell_spike_ratio": 0.018,
                "intraday_k_high": 80,
                "buyback_gap_ratio": 0.018,
                "protective_buyback_enabled": False,
            },
            "trade_history": [{
                "side": "sell", "bucket": "core", "lots": 1, "price": 34.99,
                "fee": 5, "reported_at": "2026-08-03 10:20:00",
            }],
        }
        result = build_decision_plan(
            symbol_code="002179",
            symbol_name="中航光电",
            latest_daily=self.series[-1],
            daily_series=self.series,
            position=position,
            decision_date="2026-08-03",
            intraday_series=[{
                "timestamp": "2026-08-03 10:30:00", "close": 35.06,
                "high": 35.17, "low": 35.05, "k": 84.46, "d": 71.72, "j": 109.93,
            }],
            intraday_execution_enabled=True,
        )
        self.assertEqual(result["decision"]["action"], "wait_buyback")
        self.assertEqual(result["decision"]["max_lots"], 1)
        self.assertEqual(result["price_plan"]["profit_buyback"], 34.36)
        self.assertIn("34.36", format_decision_plan(result))
        self.assertNotIn("缺少经验证的补回价格", format_decision_plan(result))


if __name__ == "__main__":
    unittest.main()
