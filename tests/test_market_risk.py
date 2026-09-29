from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from src.market_risk import evaluate_market_risk, stock_entry_risk
from src.observation_discipline import evaluate_entry_support
from src.runtime_state import claim_observation_entry, load_runtime_state, save_market_risk


class MarketRiskTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 14, 50)
        self.quotes = {
            code: {"name": code, "price": 3000, "change_ratio": -0.01, "timestamp": "20260928145000"}
            for code in ("000001", "399006")
        }

    def test_normal_market_does_not_disable_valid_entry(self):
        self.assertFalse(evaluate_market_risk(self.quotes, now=self.now, config={})["block_new_buys"])

    def test_severe_growth_selloff_latches_even_after_recovery(self):
        self.quotes["399006"]["change_ratio"] = -0.0432
        risk = evaluate_market_risk(self.quotes, now=self.now, config={})
        self.assertTrue(risk["latched"])
        self.quotes["399006"]["change_ratio"] = 0.01
        self.assertTrue(evaluate_market_risk(self.quotes, now=self.now, config={}, prior=risk)["block_new_buys"])
        for quote in self.quotes.values():
            quote["timestamp"] = "20260929145000"
        tomorrow = evaluate_market_risk(self.quotes, now=self.now + timedelta(days=1), config={}, prior=risk)
        self.assertFalse(tomorrow["block_new_buys"])

    def test_missing_stale_and_future_quotes_fail_closed(self):
        for stamp in ("20260924150000", "20260928140000", "20260928150000", "bad"):
            self.quotes["399006"]["timestamp"] = stamp
            risk = evaluate_market_risk(self.quotes, now=self.now, config={})
            self.assertTrue(risk["block_new_buys"])
            self.assertFalse(risk["latched"])
        self.assertTrue(evaluate_market_risk({}, now=self.now, config={})["block_new_buys"])

    def test_close_snapshot_is_valid_for_evening_only_same_day(self):
        for quote in self.quotes.values():
            quote["timestamp"] = "20260928150000"
        self.assertFalse(evaluate_market_risk(self.quotes, now=self.now.replace(hour=16), config={})["block_new_buys"])
        self.assertTrue(evaluate_market_risk(self.quotes, now=self.now + timedelta(days=1), config={})["block_new_buys"])

    def test_individual_crash_is_blocked_without_kdj(self):
        self.assertIsNotNone(stock_entry_risk({"change_ratio": -0.05}, {}))
        self.assertIsNotNone(stock_entry_risk({"change_ratio": float("nan")}, {}))
        self.assertIsNone(stock_entry_risk({"change_ratio": -0.01}, {}))

    def test_restart_and_concurrent_refresh_cannot_erase_same_day_brake(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = {"date": "2026-09-28", "latched": True, "block_new_buys": True, "reasons": ["急跌"]}
            save_market_risk(first, path=path)
            save_market_risk({"date": "2026-09-28", "latched": False}, path=path)
            self.assertTrue(load_runtime_state(path=path)["market_risk"]["block_new_buys"])

    def test_only_one_signal_claim_survives_restart_and_parallel_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda n: claim_observation_entry("2026-09-28", str(n), 3000, path=path), range(8)))
            self.assertEqual(sum(results), 1)
            self.assertFalse(claim_observation_entry("2026-09-28", "new", 2000, path=path))
            self.assertTrue(claim_observation_entry("2026-09-29", "new", 2000, path=path))

    def test_broken_support_does_not_become_bargain_after_rebound(self):
        # The last formal session before 9/28 is 9/24 (holiday closure).
        bars = [{"date": f"2026-08-{n:02d}", "low": 32.0} for n in range(1, 20)]
        bars.append({"date": "2026-09-24", "low": 31.9})
        quote = {"price": 32.2, "low": 31.5}
        result = evaluate_entry_support(bars, quote, now=self.now)
        self.assertFalse(result["ready"])
        self.assertIn("跌破", result["reason"])
        quote["low"] = 32.0
        self.assertTrue(evaluate_entry_support(bars, quote, now=self.now)["ready"])
        bars[-1]["date"] = "2026-09-23"
        self.assertFalse(evaluate_entry_support(bars, quote, now=self.now)["ready"])


if __name__ == "__main__":
    unittest.main()
