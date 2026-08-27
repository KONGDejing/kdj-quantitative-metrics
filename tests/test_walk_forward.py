from __future__ import annotations

import unittest

import pandas as pd

from src.walk_forward import find_events, summarize_events, summarize_events_by_regime


class WalkForwardTests(unittest.TestCase):
    def test_signal_enters_only_at_next_open(self) -> None:
        data = pd.DataFrame([
            {"date": "2026-08-01", "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "k": 20, "d": 20},
            {"date": "2026-08-02", "open": 9.8, "high": 9.9, "low": 8.8, "close": 9.0, "k": 10, "d": 20},
            {"date": "2026-08-03", "open": 9.1, "high": 9.4, "low": 8.9, "close": 9.2, "k": 16, "d": 15},
            {"date": "2026-08-04", "open": 9.3, "high": 9.8, "low": 9.1, "close": 9.7, "k": 22, "d": 17},
            {"date": "2026-08-05", "open": 9.7, "high": 10.2, "low": 9.6, "close": 10.0, "k": 30, "d": 22},
            {"date": "2026-08-06", "open": 10.0, "high": 10.3, "low": 9.9, "close": 10.2, "k": 35, "d": 28},
        ])
        params = {"buy_k": 15, "oversold_lookback": 2, "confirmation_min": 2, "rebound_k_max": 25}
        events = find_events(data, params, horizon=2, cooldown=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["signal_date"], "2026-08-03")
        self.assertEqual(events[0]["entry_date"], "2026-08-04")
        self.assertEqual(events[0]["exit_date"], "2026-08-05")
        self.assertGreater(events[0]["return_pct"], 0)

    def test_empty_event_summary_is_explicit(self) -> None:
        summary = summarize_events([])
        self.assertEqual(summary["events"], 0)
        self.assertIsNone(summary["win_rate"])
        self.assertIsNone(summary["score"])

    def test_regime_summary_keeps_groups_separate(self) -> None:
        events = [
            {"price_regime": "uptrend", "return_pct": 0.1, "max_adverse_excursion": -0.02, "max_favorable_excursion": 0.12},
            {"price_regime": "downtrend", "return_pct": -0.1, "max_adverse_excursion": -0.15, "max_favorable_excursion": 0.01},
        ]
        grouped = summarize_events_by_regime(events)
        self.assertEqual(grouped["uptrend"]["events"], 1)
        self.assertEqual(grouped["downtrend"]["events"], 1)
        self.assertGreater(grouped["uptrend"]["avg_return"], grouped["downtrend"]["avg_return"])


if __name__ == "__main__":
    unittest.main()
