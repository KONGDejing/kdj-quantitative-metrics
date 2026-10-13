from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
from threading import Lock
import unittest
from unittest.mock import patch

from src import notifier, runner, llm_advisor
from src.stop_loss import enforce_stop_loss, entry_stop_error, position_cycle, stop_latched, stop_price
from src.runtime_state import load_runtime_state, record_stop_breach
from src.state import AppState
from test_price_alerts import FakeState, config_with_lots, quote, stabilized_bars


def plan(total=1, sellable=1):
    return {"facts": {"ledger": {"total_lots": total, "sellable_lots_today": sellable}, "pending_orders": []},
            "gates": [], "decision": {"action": "buy_core", "status": "executable", "max_lots": 1},
            "price_plan": {"lower": 10}, "after_action": {}, "cancel_conditions": [],
            "reverse_t": {"decision": {"action": "buyback_core", "status": "executable", "max_lots": 1},
                          "price_plan": {"profit_buyback": 10}}}


class StopPolicyTests(unittest.TestCase):
    def test_invalid_stops_never_allow_entry(self):
        for value in (None, 0, -1, True, "bad", float("nan"), float("inf"), 9.123):
            with self.subTest(value=value):
                self.assertIsNone(stop_price({"stop_loss": value}))
                self.assertIsNotNone(entry_stop_error({"stop_loss": value}, 10))

    def test_missing_stop_blocks_core_and_buyback(self):
        p = enforce_stop_loss(plan(), {}, price=10)
        self.assertEqual(p["decision"]["status"], "blocked")
        self.assertEqual(p["reverse_t"]["decision"]["status"], "blocked")
        self.assertIsNone(p["price_plan"])

    def test_stop_triggers_at_equality_and_preempts_buyback(self):
        p = enforce_stop_loss(plan(11, 10), {"stop_loss": 9}, price=9)
        self.assertEqual(p["decision"]["action"], "sell_stop")
        self.assertEqual(p["decision"]["max_lots"], 10)
        self.assertEqual(p["reverse_t"]["decision"]["status"], "blocked")

    def test_t1_is_not_bypassed(self):
        p = enforce_stop_loss(plan(1, 0), {"stop_loss": 9}, price=8)
        self.assertEqual(p["decision"]["action"], "stop_t1_locked")
        self.assertEqual(p["decision"]["max_lots"], 0)
        p = enforce_stop_loss(plan(1, 0), {"stop_loss": 9}, price=8, for_next_session=True)
        self.assertEqual(p["decision"]["max_lots"], 1)

    def test_flat_position_never_sells_and_cannot_buy_below_stop(self):
        p = enforce_stop_loss(plan(0, 0), {"stop_loss": 9}, price=8)
        self.assertFalse(p["stop_loss"]["triggered"])
        self.assertEqual(p["decision"]["status"], "blocked")

    def test_no_quote_cannot_allow_new_buy(self):
        p = enforce_stop_loss(plan(), {"stop_loss": 9}, price=None)
        self.assertEqual(p["decision"]["status"], "blocked")

    def test_buyback_price_itself_must_be_above_stop(self):
        p = plan()
        p["reverse_t"]["price_plan"]["profit_buyback"] = 8.90
        result = enforce_stop_loss(p, {"stop_loss": 9}, price=10)
        self.assertEqual(result["reverse_t"]["decision"]["status"], "blocked")

    def test_explicit_zhonghang_exception_preserves_original_plan(self):
        original = plan(11, 11)
        p = enforce_stop_loss(deepcopy(original), {"stop_loss_exempt": True, "stop_loss": 31.56}, price=30)
        self.assertEqual(p["decision"], original["decision"])
        self.assertEqual(p["reverse_t"], original["reverse_t"])
        self.assertEqual(p["stop_loss"]["status"], "exempt")
        self.assertIsNone(p["stop_loss"]["price"])

    def test_breach_survives_restart_rebound_and_additional_buy(self):
        position = {"stop_loss": 9, "opening": {"as_of": "2026-09-28"}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            record_stop_breach("600498", position_cycle(position), 9, "20261008110000", path=path)
            breach = load_runtime_state(path=path)["stop_loss_breaches"]["600498"]
            position["trade_history"] = [{"side": "buy", "id": "added"}]
            self.assertTrue(stop_latched(position, breach))
            p = enforce_stop_loss(plan(), position, price=10, breach=breach)
            self.assertEqual(p["decision"]["action"], "sell_stop")
            position["trade_history"].append({"id": "exit", "side": "sell", "position_exit": True})
            self.assertFalse(stop_latched(position, breach))

    def test_notifier_cannot_send_buy_without_stop(self):
        with patch.object(notifier, "send_pushplus") as send:
            notifier.notify_price_target({"alert": {"channels": ["pushplus"]}}, {"symbol": "000938", "close": 30})
        send.assert_not_called()

    def test_quote_loop_exit_ignores_t_quota_and_warns_locked_lots(self):
        now = datetime(2026, 10, 8, 11)
        cfg = {"symbols": [{"code": "600498", "name": "烽火"}], "trade_plan": {"positions": {"600498": {
            "strategy_mode": "long_term", "stop_loss": 36.15,
            "opening": {"as_of": "2026-09-28", "core_lots": 2, "t_lots": 0, "cost_per_share": 39.29},
            "trade_history": [{"side": "buy", "lots": 1, "price": 35, "fee": 5, "reported_at": "2026-10-08 10:00:00"}],
            "reverse_t": {"max_lots_per_trade": 1, "core_floor_lots": 2},
        }}}}
        quotes = {"600498": {"price": 35, "timestamp": "20261008110000"}}
        fake = FakeState()
        with patch.object(runner, "state", fake), patch.object(runner, "load_runtime_state", return_value={}), \
             patch.object(runner, "record_stop_breach") as record, patch.object(runner, "notify_observation_exit") as notify:
            runner._maybe_send_observation_exit_alerts(cfg, 600, now=now, quotes=quotes)
        record.assert_called_once()
        notify.assert_called_once()
        self.assertEqual(fake.alerts[0]["lots"], 2)
        self.assertEqual(fake.alerts[0]["locked_lots"], 1)
        self.assertTrue(fake.alerts[0]["position_exit"])

    def test_stale_quote_cannot_trigger_or_latch_stop(self):
        cfg = {"trade_plan": {"positions": {"600498": {"strategy_mode": "long_term", "stop_loss": 36.15,
            "opening": {"as_of": "2026-09-28", "core_lots": 1, "t_lots": 0, "cost_per_share": 39.29}}}}}
        with patch.object(runner, "record_stop_breach") as record, patch.object(runner, "notify_observation_exit") as notify:
            runner._maybe_send_observation_exit_alerts(cfg, 600, now=datetime(2026, 10, 8, 11),
                quotes={"600498": {"price": 35, "timestamp": "20260930150000"}})
        record.assert_not_called()
        notify.assert_not_called()

    def test_explicit_exempt_core_never_sends_stop(self):
        cfg = {"trade_plan": {"positions": {"002179": {"strategy_mode": "expand_base", "final_exit_target": 40,
            "stop_loss_exempt": True, "stop_loss": 31.56,
            "opening": {"as_of": "2026-09-28", "core_lots": 11, "t_lots": 0, "cost_per_share": 33.37}}}}}
        with patch.object(runner, "record_stop_breach") as record, patch.object(runner, "notify_observation_exit") as notify, \
             patch.object(runner, "state", FakeState()):
            runner._maybe_send_observation_exit_alerts(cfg, 600, now=datetime(2026, 10, 8, 11),
                quotes={"002179": {"price": 30, "timestamp": "20261008110000"}})
        record.assert_not_called()
        notify.assert_not_called()


class StopTradeTests(unittest.TestCase):
    def setUp(self):
        self.app = AppState.__new__(AppState)
        self.app._lock = Lock()
        self.position = {"opening": {"as_of": "2026-01-01", "core_lots": 0, "t_lots": 0, "cost_per_share": 0}}
        self.app.config = {"trade_plan": {"positions": {"000938": self.position}}}
        patcher = patch("src.state.save_config")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_recorded_buy_persists_explicit_stop(self):
        result = self.app.report_trade("000938", "buy", 1, 30, stop_loss=28)
        self.assertEqual(result["stop_loss"], 28)
        self.assertEqual(result["last_report"]["stop_loss"], 28)
        self.assertIsNone(result["risk_warning"])

    def test_real_trade_is_recorded_even_if_risk_plan_missing(self):
        result = self.app.report_trade("000938", "buy", 1, 30)
        self.assertEqual(result["ledger"]["total_lots"], 1)
        self.assertIn("止损", result["risk_warning"])

    def test_add_cannot_lower_existing_stop(self):
        self.position["opening"]["core_lots"] = 1
        self.position["opening"]["cost_per_share"] = 30
        self.position["stop_loss"] = 28
        with self.assertRaisesRegex(ValueError, "不得.*下移"):
            self.app.report_trade("000938", "buy", 1, 29, stop_loss=27)
        self.assertNotIn("trade_history", self.position)

    def test_bad_stop_does_not_mutate_trade_history(self):
        with self.assertRaises(ValueError):
            self.app.report_trade("000938", "buy", 1, 30, stop_loss=31)
        self.assertNotIn("trade_history", self.position)

    def test_partial_stop_exit_does_not_create_buyback_layer(self):
        self.position["opening"].update(core_lots=2, cost_per_share=30)
        self.position["stop_loss"] = 28
        self.position["trade_history"] = [{"id": "today-buy", "side": "buy", "lots": 1, "price": 29,
                                           "reported_at": datetime.now().strftime("%Y-%m-%d 09:40:00")}]
        breach = {"cycle": position_cycle(self.position), "stop_loss": 28}
        with patch("src.state.load_runtime_state", return_value={"stop_loss_breaches": {"000938": breach}}):
            result = self.app.report_trade("000938", "sell", 2, 27)
        self.assertEqual(result["ledger"]["total_lots"], 1)
        self.assertTrue(result["last_report"]["position_exit"])
        self.assertEqual(result["ledger"]["pending_core_buyback_lots"], 0)
        self.assertTrue(stop_latched(result, breach))


class StopCandidateTests(unittest.TestCase):
    def test_candidate_model_must_supply_valid_stop_and_reason(self):
        source = [{"code": "600001", "name": "测试", "close": 10, "held": False}]
        row = {"code": "600001", "name": "测试", "trend": "weak", "action": "wait",
               "suggested_price": 9.5, "reason": "支撑附近", "stop_loss": 9, "stop_loss_reason": "支撑失效"}
        def check(item):
            return llm_advisor._validate_candidate_advice({"market_view": "偏弱", "candidates": [item]}, source)
        self.assertIsNotNone(check(row))
        for change in ({"stop_loss": 9.5}, {"stop_loss": float("nan")}, {"stop_loss": -1},
                       {"stop_loss": 9.123}, {"stop_loss_reason": ""}):
            self.assertIsNone(check({**row, **change}))
        row.pop("stop_loss")
        self.assertIsNone(check(row))

    def test_missing_stop_blocks_entry_before_fetching_kline_or_claiming_slot(self):
        cfg = config_with_lots()
        del cfg["trade_plan"]["positions"]["600584"]["stop_loss"]
        fake = FakeState()
        with patch.object(runner, "state", fake), patch.object(runner, "fetch_realtime_quotes", return_value=quote(71.2)), \
             patch.object(runner, "load_runtime_state", return_value={}), \
             patch.object(runner, "safe_fetch_kline") as fetch, patch.object(runner, "claim_observation_entry") as claim, \
             patch.object(runner, "notify_price_target") as notify:
            runner._maybe_send_price_target_alerts(cfg, 600, now=datetime(2026, 8, 28, 14, 50),
                                                  market_context={"block_new_buys": False})
        fetch.assert_not_called()
        claim.assert_not_called()
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
