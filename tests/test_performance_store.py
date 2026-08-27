from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.performance_store import backfill_snapshots, get_performance, upsert_snapshot


class PerformanceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.position = {
            "strategy_budget": 2000,
            "fee_per_lot": 5,
            "opening": {
                "as_of": "2026-08-01",
                "core_lots": 1,
                "t_lots": 0,
                "cost_per_share": 10,
            },
            "trade_history": [],
        }
        self.series = [
            {"timestamp": "2026-08-01", "close": 10},
            {"timestamp": "2026-08-02", "close": 11},
            {"timestamp": "2026-08-03", "close": 8},
        ]

    def test_backfill_recomputes_high_water_and_drawdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "performance.json"
            summary = backfill_snapshots("002179", self.series, self.position, path=path)
            self.assertEqual(summary["snapshot_count"], 3)
            self.assertEqual(summary["high_water_equity"], 2100)
            self.assertAlmostEqual(summary["current_drawdown"], 1800 / 2100 - 1, places=6)
            self.assertEqual(summary["max_drawdown_date"], "2026-08-03")

    def test_same_day_upsert_does_not_duplicate_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "performance.json"
            upsert_snapshot("002179", "2026-08-01", 10, self.position, path=path)
            upsert_snapshot("002179", "2026-08-01", 10.5, self.position, path=path)
            result = get_performance("002179", path=path)
            self.assertEqual(len(result["snapshots"]), 1)
            self.assertEqual(result["snapshots"][0]["close"], 10.5)

    def test_backfill_can_remove_an_unconfirmed_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "performance.json"
            upsert_snapshot("002179", "2026-08-26", 11.0, self.position, path=path)
            summary = backfill_snapshots(
                "002179", self.series, self.position, path=path, exclude_dates=["2026-08-26"]
            )
            self.assertNotEqual(summary["last_date"], "2026-08-26")


if __name__ == "__main__":
    unittest.main()
