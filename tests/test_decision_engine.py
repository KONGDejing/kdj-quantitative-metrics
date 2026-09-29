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

    def test_market_brake_blocks_even_a_formally_valid_expansion(self) -> None:
        result = build_decision_plan(
            symbol_code="002179", symbol_name="中航光电", latest_daily=self.series[-1],
            daily_series=self.series, position=self.position, decision_date="2026-08-03",
            market_risk={"block_new_buys": True, "reasons": ["创业板急跌"]},
        )
        self.assertEqual(result["decision"]["max_lots"], 0)
        self.assertIsNone(result["price_plan"])
        self.assertIn("创业板急跌", result["decision"]["summary"])

    def test_user_limit_order_cannot_skip_oversold_gate(self) -> None:
        self.series = [bar("2026-08-03", 10, 40, 50)]
        self.position["pending_orders"] = [{
            "id": "manual", "side": "buy", "lots": 1, "limit_price": 9.5, "status": "open",
        }]
        result = self.plan()
        self.assertEqual(result["decision"]["action"], "review_limit_buy")
        self.assertFalse(next(g for g in result["gates"] if g["name"] == "oversold")["passed"])
        self.assertEqual(self.position["pending_orders"][0]["status"], "open")

    def test_final_exit_target_overrides_reverse_t_and_sells_all_core(self) -> None:
        position = {
            **self.position,
            "final_exit_target": 40.0,
            "reverse_t": {
                "enabled": True, "allocation_ratio": 0.30, "fixed_quota_lots": 3,
                "core_floor_lots": 7, "max_lots_per_trade": 2,
                "sell_spike_ratio": 0.020, "intraday_k_high": 80,
                "buyback_gap_ratio": 0.015,
            },
        }
        result = self.plan(latest=bar("2026-08-03", 40.05, 70, 65), position=position)

        self.assertEqual(result["decision"]["action"], "sell_all_core")
        self.assertEqual(result["decision"]["max_lots"], 10)
        self.assertEqual(result["price_plan"]["execution"], "final_exit")
        rendered = format_decision_plan(result)
        self.assertIn("最终目标全部止盈", rendered)
        self.assertIn("卖出全部10手，不再回补", rendered)
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

    def test_long_term_reentry_shows_current_cost_and_cumulative_breakeven(self) -> None:
        position = {
            "strategy_mode": "long_term",
            "opening": {"as_of": "2026-08-01", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
            "trade_history": [
                {"side": "buy", "bucket": "core", "lots": 1, "price": 26.38,
                 "fee": 5, "reported_at": "2026-08-01 10:00:00"},
                {"side": "sell", "bucket": "core", "lots": 1, "price": 27.08,
                 "fee": 5, "position_exit": True, "reported_at": "2026-08-02 10:00:00"},
                {"side": "buy", "bucket": "core", "lots": 1, "price": 24.58,
                 "fee": 5, "reported_at": "2026-08-03 10:00:00"},
            ],
        }

        rendered = format_decision_plan(self.plan(position=position))

        self.assertIn("持仓含费成本24.630", rendered)
        self.assertIn("历史收益抵扣后累计保本成本24.030", rendered)

    def test_flat_reverse_t_exit_is_not_described_as_full_quota(self) -> None:
        position = {
            "strategy_mode": "long_term",
            "opening": {"as_of": "2026-08-01", "core_lots": 1, "t_lots": 0, "cost_per_share": 37},
            "reverse_t": {
                "enabled": True,
                "fixed_quota_lots": 1,
                "core_floor_lots": 0,
                "max_lots_per_trade": 1,
                "sell_spike_ratio": 0.022,
                "buyback_gap_ratio": 0.022,
                "flat_exit_is_not_pending_buyback": True,
            },
            "trade_history": [{
                "side": "sell", "bucket": "core", "lots": 1, "price": 40.33,
                "fee": 5, "reported_at": "2026-08-03 10:00:00",
            }],
        }

        rendered = format_decision_plan(self.plan(position=position))

        self.assertIn("待补回核心仓0手", rendered)
        self.assertIn("当前没有可卖老仓，不挂新卖单", rendered)
        self.assertNotIn("当前反T额度已占满", rendered)

    def test_long_term_open_limit_order_is_shown_without_changing_position(self) -> None:
        position = {
            **self.position,
            "strategy_mode": "long_term",
            "pending_orders": [{
                "id": "order-1", "side": "buy", "bucket": "core", "lots": 1,
                "limit_price": 40.0, "status": "open", "placed_at": "2026-08-03",
            }],
        }
        result = self.plan(position=position)
        self.assertEqual(result["decision"]["action"], "review_limit_buy")
        self.assertIsNone(result["price_plan"])
        self.assertEqual(result["facts"]["pending_orders"][0]["limit_price"], 40.0)
        self.assertEqual(position["pending_orders"][0]["status"], "open")
        self.assertEqual(result["facts"]["ledger"]["total_lots"], 10)

    def test_old_day_order_does_not_block_new_plan(self) -> None:
        position = {
            **self.position,
            "strategy_mode": "long_term",
            "opening": {"as_of": "2026-07-31", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
            "pending_orders": [{
                "id": "old-order", "side": "buy", "bucket": "core", "lots": 1,
                "limit_price": 38.5, "status": "open", "placed_at": "2026-08-02",
            }],
        }
        result = self.plan(position=position)
        self.assertEqual(result["facts"]["pending_orders"], [])
        self.assertEqual(result["decision"]["action"], "wait_new_entry")

    def test_today_order_is_not_carried_into_next_session(self) -> None:
        position = {
            **self.position,
            "strategy_mode": "long_term",
            "opening": {"as_of": "2026-07-31", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
            "pending_orders": [{
                "id": "today-order", "side": "buy", "bucket": "core", "lots": 1,
                "limit_price": 39.3, "status": "open", "placed_at": "2026-08-03",
            }],
        }
        result = build_decision_plan(
            symbol_code="600498", symbol_name="烽火通信",
            latest_daily=self.series[-1], daily_series=self.series,
            position=position, decision_date="2026-08-03", for_next_session=True,
        )
        self.assertEqual(result["facts"]["pending_orders"], [])
        self.assertEqual(result["decision"]["action"], "wait_new_entry")

    def test_expired_today_order_is_reported_as_history_not_live_order(self) -> None:
        position = {
            **self.position,
            "strategy_mode": "long_term",
            "opening": {"as_of": "2026-07-31", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
            "pending_orders": [{
                "id": "today-order", "side": "buy", "bucket": "core", "lots": 1,
                "limit_price": 39.3, "status": "expired", "placed_at": "2026-08-03",
            }],
        }
        result = self.plan(position=position)
        rendered = format_decision_plan(result)
        self.assertEqual(result["decision"]["action"], "wait_new_entry")
        self.assertIn("今日未成交且已失效的委托：39.30元买入1手", rendered)
        self.assertNotIn("已有39.30元买入1手挂单", rendered)

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
                "sell_spike_ratio": 0.020,
                "intraday_k_high": 80,
                "buyback_gap_ratio": 0.015,
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
            market_risk={"block_new_buys": True, "reasons": ["市场暂停新增，但不禁止原反T盈利回补"]},
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
        self.assertEqual(result["price_plan"]["profit_buyback"], 34.46)
        rendered = format_decision_plan(result)
        self.assertIn("34.46", rendered)
        self.assertIn("9.70 × 1.020 = 9.89元", rendered)
        self.assertIn("最多参考卖出1手", rendered)
        self.assertNotIn("缺少经验证的补回价格", rendered)

    def test_zhonghang_open_buy_and_sell_orders_are_both_preserved(self) -> None:
        position = {
            **self.position,
            "reverse_t": {
                "enabled": True, "allocation_ratio": 0.2, "max_lots_per_trade": 2,
                "max_daily_cycles": 1, "sell_spike_ratio": 0.020,
                "intraday_k_high": 80, "buyback_gap_ratio": 0.015,
            },
            "pending_orders": [
                {"id": "buy-3350", "side": "buy", "bucket": "core", "lots": 1,
                 "limit_price": 33.5, "status": "open"},
                {"id": "sell-3520", "side": "sell", "bucket": "core", "lots": 1,
                 "limit_price": 35.2, "conditional_buyback_price": 34.5, "status": "open"},
            ],
        }
        result = self.plan(position=position)
        self.assertEqual(result["decision"]["action"], "review_limit_buy")
        self.assertEqual(result["reverse_t"]["decision"]["action"], "wait_limit_sell")
        rendered = format_decision_plan(result)
        self.assertIn("33.50元买入1手", rendered)
        self.assertIn("35.20元卖出1手（成交后计划34.50元买回）", rendered)

    def test_next_day_plan_shows_three_lot_quota_but_two_lot_single_order_cap(self) -> None:
        position = {
            **self.position,
            "reverse_t": {
                "enabled": True,
                "allocation_ratio": 0.30,
                "fixed_quota_lots": 3,
                "core_floor_lots": 7,
                "max_lots_per_trade": 2,
                "max_daily_cycles": 1,
                "sell_spike_ratio": 0.020,
                "intraday_k_high": 80,
                "buyback_gap_ratio": 0.015,
            },
        }

        rendered = format_decision_plan(self.plan(position=position))

        self.assertIn("反T额度：总仓位30%，当前最多3手；单次最多2手", rendered)
        self.assertIn("反T冲高参考挂单价：9.70 × 1.020 = 9.89元；最多参考卖出2手", rendered)
        self.assertNotIn("1.030", rendered)

    def test_next_day_plan_lists_every_trade_from_the_decision_day(self) -> None:
        position = {
            **self.position,
            "reverse_t": {
                "enabled": True, "allocation_ratio": 0.2, "max_lots_per_trade": 1,
                "max_daily_cycles": 1, "sell_spike_ratio": 0.020,
                "intraday_k_high": 80, "buyback_gap_ratio": 0.015,
            },
            "trade_history": [
                {
                    "side": "sell", "bucket": "core", "lots": 1, "price": 35.50,
                    "fee": 5, "reported_at": "2026-08-03 10:18:00",
                },
                {
                    "side": "buy", "bucket": "core", "lots": 1, "price": 34.76,
                    "fee": 5, "reported_at": "2026-08-03 10:41:00",
                },
            ],
        }

        rendered = format_decision_plan(self.plan(position=position))

        sell_text = "卖出核心仓1手，35.50元"
        buy_text = "买入核心仓1手，34.76元"
        self.assertIn("当日成交：", rendered)
        self.assertIn(sell_text, rendered)
        self.assertIn(buy_text, rendered)
        self.assertLess(rendered.index(sell_text), rendered.index(buy_text))


if __name__ == "__main__":
    unittest.main()
