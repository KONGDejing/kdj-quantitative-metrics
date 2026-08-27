from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src import runner


def daily_frame(last_day: str) -> pd.DataFrame:
    rows = []
    for index in range(12):
        day = f"2026-08-{index + 14:02d}" if index < 11 else last_day
        close = 30.0 + index / 10
        rows.append({
            "date": day,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
        })
    data = pd.DataFrame(rows)
    data.attrs["data_source"] = "tencent_daily"
    return data


class NextDayPlanFreshnessTests(unittest.TestCase):
    def fake_state(self) -> SimpleNamespace:
        fake = SimpleNamespace(
            config={
                "kdj": {"n": 9, "m1": 3, "m2": 3},
                "trade_plan": {"positions": {"002179": {"strategy_mode": "long_term"}}},
            },
            symbols=[{"code": "002179", "name": "中航光电"}],
            latest={},
            series={},
        )
        fake.update_latest = lambda code, timeframe, value: fake.latest.setdefault(code, {}).__setitem__(timeframe, value)
        fake.update_series = lambda code, timeframe, value: fake.series.setdefault(code, {}).__setitem__(timeframe, value)
        return fake

    def test_previous_session_bar_blocks_post_close_plan(self) -> None:
        fake = self.fake_state()
        with patch.object(runner, "state", fake), patch.object(
            runner, "safe_fetch_kline", return_value=daily_frame("2026-08-25")
        ):
            ready = runner._refresh_formal_daily_for_plan("2026-08-26")

        self.assertFalse(ready)
        self.assertEqual(fake.latest, {})

    def test_same_day_bar_allows_plan_and_records_source(self) -> None:
        fake = self.fake_state()
        with patch.object(runner, "state", fake), patch.object(
            runner, "safe_fetch_kline", return_value=daily_frame("2026-08-26")
        ):
            ready = runner._refresh_formal_daily_for_plan("2026-08-26")

        self.assertTrue(ready)
        latest = fake.latest["002179"]["1d"]
        self.assertEqual(latest["timestamp"], "2026-08-26")
        self.assertEqual(latest["data_source"], "tencent_daily")


if __name__ == "__main__":
    unittest.main()
