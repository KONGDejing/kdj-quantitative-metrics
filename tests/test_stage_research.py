from __future__ import annotations

import unittest

from src.stage_research import _non_overlapping, _stage_curve


class StageResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = [
            {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-02-01", "entry_price": 10, "return_pct": 0.10, "max_adverse_excursion": -0.05, "price_regime": "range"},
            {"signal_date": "2020-01-15", "entry_date": "2020-01-16", "exit_date": "2020-02-15", "entry_price": 10, "return_pct": 0.20, "max_adverse_excursion": -0.03, "price_regime": "range"},
            {"signal_date": "2020-03-01", "entry_date": "2020-03-02", "exit_date": "2020-04-01", "entry_price": 10, "return_pct": -0.05, "max_adverse_excursion": -0.15, "price_regime": "downtrend"},
        ]

    def test_overlapping_events_are_not_double_counted(self) -> None:
        selected = _non_overlapping(self.events)
        self.assertEqual([item["signal_date"] for item in selected], ["2020-01-01", "2020-03-01"])

    def test_more_lots_scale_sleeve_risk(self) -> None:
        events = _non_overlapping(self.events)
        small = _stage_curve(events, 10, budget=200000, max_deployed_ratio=0.85)
        large = _stage_curve(events, 40, budget=200000, max_deployed_ratio=0.85)
        self.assertLessEqual(large["max_drawdown"], small["max_drawdown"])
        self.assertGreater(abs(large["total_return"]), abs(small["total_return"]))


if __name__ == "__main__":
    unittest.main()
