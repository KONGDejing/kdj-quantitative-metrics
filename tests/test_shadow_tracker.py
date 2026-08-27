from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.shadow_tracker import classify_price_regime, get_shadow_decisions, record_and_evaluate


def bars(closes: list[float]) -> list[dict]:
    result = []
    for index, close in enumerate(closes, start=1):
        result.append({
            "timestamp": f"2026-01-{index:02d}",
            "open": close,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
        })
    return result


def plan(action: str, price_plan: dict | None = None) -> dict:
    return {
        "version": 2,
        "symbol": {"code": "002179", "name": "中航光电"},
        "decision_date": "2026-01-01",
        "signal_date": "2026-01-01",
        "decision": {"action": action, "status": "watch", "max_lots": 0},
        "price_plan": price_plan,
    }


class ShadowTrackerTests(unittest.TestCase):
    def test_record_is_idempotent_and_hold_scores_incremental_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.json"
            daily = bars([10.0, 10.0, 10.2, 10.3, 10.4, 11.0])
            record_and_evaluate(plan("hold"), daily, horizons=[5], path=path, recorded_at="2026-01-01 16:00:00")
            record_and_evaluate(plan("hold"), daily, horizons=[5], path=path, recorded_at="2026-01-02 16:00:00")
            result = get_shadow_decisions("002179", path=path)
            self.assertEqual(result["summary"]["records"], 1)
            score = result["records"][0]["horizons"]["5"]
            self.assertEqual(score["status"], "evaluated")
            self.assertFalse(score["favorable"])
            self.assertLess(score["decision_score"], 0)

    def test_buy_requires_next_open_inside_zone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.json"
            price_plan = {
                "execution": "limit_zone", "lower": 9.8, "upper": 10.2,
                "do_not_chase_above": 10.3, "invalidate_below": 9.5,
            }
            result = record_and_evaluate(
                plan("buy_core", price_plan), bars([10.0, 10.0, 10.2, 10.5, 10.8, 11.0]),
                horizons=[5], path=path, recorded_at="2026-01-01 16:00:00",
            )
            record = result["records"][0]
            self.assertEqual(record["execution"]["status"], "filled")
            self.assertTrue(record["horizons"]["5"]["favorable"])

    def test_high_open_is_not_assumed_filled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.json"
            price_plan = {
                "execution": "limit_zone", "lower": 9.8, "upper": 10.2,
                "do_not_chase_above": 10.3, "invalidate_below": 9.5,
            }
            result = record_and_evaluate(
                plan("buy_core", price_plan), bars([10.0, 10.5, 10.6, 10.7, 10.8, 11.0]),
                horizons=[5], path=path,
            )
            record = result["records"][0]
            self.assertEqual(record["execution"]["status"], "not_filled")
            self.assertEqual(record["horizons"]["5"]["status"], "unscored")

    def test_regime_uses_only_supplied_history(self) -> None:
        history = [10.0 + index * 0.1 for index in range(80)]
        before = classify_price_regime(history)
        classify_price_regime(history + [1.0])
        after = classify_price_regime(history)
        self.assertEqual(before, after)
        self.assertEqual(before["label"], "uptrend")
        self.assertEqual(classify_price_regime([10.0] * 80)["label"], "range")


if __name__ == "__main__":
    unittest.main()
