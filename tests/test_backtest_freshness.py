from datetime import datetime
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException, Response

from src.backtest import run_backtest
from src.backtest_data import BacktestDataUnavailable, fetch_current_backtest_data, latest_completed_bar


def frame(dates, column="date", close=30):
    return pd.DataFrame([{column: day, "open": close, "high": close + 1,
                          "low": close - 1, "close": close, "volume": 100} for day in dates])


class CutoffTests(unittest.TestCase):
    def test_daily_cutoff_rolls_each_day_and_after_close(self):
        for stamp, expected in (
            ("2026-10-13 10:30:00", "2026-10-12"),
            ("2026-10-13 15:00:00", "2026-10-12"),
            ("2026-10-13 15:01:00", "2026-10-13"),
            ("2026-10-14 09:00:00", "2026-10-13"),
            ("2026-10-11 15:30:00", "2026-10-09"),
            ("2026-10-07 16:00:00", "2026-09-30"),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(latest_completed_bar("1d", datetime.fromisoformat(stamp)), expected)

    def test_unknown_calendar_year_fails_closed(self):
        with self.assertRaises(BacktestDataUnavailable):
            latest_completed_bar("1d", datetime(2027, 10, 13, 10))

    def test_minute_cutoffs_respect_lunch_and_forming_bars(self):
        for stamp, expected in (
            ("2026-10-13 09:35:00", "2026-10-12 15:00:00"),
            ("2026-10-13 10:30:05", "2026-10-13 10:00:00"),
            ("2026-10-13 10:30:31", "2026-10-13 10:30:00"),
            ("2026-10-13 12:00:00", "2026-10-13 11:30:00"),
            ("2026-10-13 13:15:00", "2026-10-13 11:30:00"),
            ("2026-10-13 16:00:00", "2026-10-13 15:00:00"),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(latest_completed_bar("30m", datetime.fromisoformat(stamp)), expected)


class FreshDataTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 13, 10, 45)

    def test_all_symbols_refresh_on_every_query_and_exclude_unfinished_daily(self):
        with patch("src.backtest_data.provider.fetch_backtest_kline", return_value=frame([
            "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"
        ])) as fetch:
            for code in ("600498", "002179", "300547", "000938", "832982", "688001"):
                for _ in range(2):
                    result = fetch_current_backtest_data(code, "1d", "2020-01-01", now=self.now)
                    self.assertEqual(result.iloc[-1]["date"], "2026-10-12")
                    self.assertEqual(result.attrs["expected_end"], "2026-10-12")
            self.assertEqual(fetch.call_count, 12)

    def test_stale_success_uses_fresh_fallback_and_replaces_overlapping_prices(self):
        with patch("src.backtest_data.provider.fetch_backtest_kline", return_value=frame(["2026-10-08", "2026-10-09"])), \
             patch("src.backtest_data.provider._fetch_sina_daily", return_value=frame(["2026-10-09", "2026-10-12"], close=31)), \
             patch("src.backtest_data.provider._write_cache") as cache:
            result = fetch_current_backtest_data("600498", "1d", "2020-01-01", now=self.now)
        self.assertEqual(result["date"].tolist(), ["2026-10-08", "2026-10-09", "2026-10-12"])
        self.assertEqual(result.loc[1, "close"], 31)
        cache.assert_called_once()

    def test_all_stale_sources_are_rejected(self):
        old = frame(["2026-10-09"])
        with patch("src.backtest_data.provider.fetch_backtest_kline", return_value=old), \
             patch("src.backtest_data.provider._fetch_sina_daily", return_value=old), \
             patch("src.backtest_data.provider._fetch_tencent_daily", return_value=old):
            with self.assertRaisesRegex(BacktestDataUnavailable, "应截至 2026-10-12，实际仅到 2026-10-09"):
                fetch_current_backtest_data("600498", "1d", "2020-01-01", now=self.now)

    def test_history_gap_cannot_be_disguised_as_current(self):
        with patch("src.backtest_data.provider.fetch_backtest_kline", return_value=frame(["2020-01-02"])), \
             patch("src.backtest_data.provider._fetch_sina_daily", return_value=frame(["2026-10-12"])), \
             patch("src.backtest_data.provider._fetch_tencent_daily", return_value=frame(["2026-10-12"])):
            with self.assertRaises(BacktestDataUnavailable):
                fetch_current_backtest_data("600498", "1d", "2020-01-01", now=self.now)

    def test_fresh_cache_can_be_used_when_primary_fails(self):
        with patch("src.backtest_data.provider.fetch_backtest_kline", side_effect=RuntimeError("offline")), \
             patch("src.backtest_data.provider._read_cache", return_value=frame(["2026-10-12"])):
            result = fetch_current_backtest_data("600498", "1d", "2020-01-01", now=self.now)
        self.assertEqual(result.attrs["freshness"], "current")

    def test_minute_excludes_forming_bar(self):
        with patch("src.backtest_data.provider.fetch_backtest_kline", return_value=frame([
            "2026-10-13 10:00:00", "2026-10-13 10:30:00", "2026-10-13 11:00:00"
        ], "datetime")):
            result = fetch_current_backtest_data("600498", "30m", "2020-01-01", now=self.now)
        self.assertEqual(result.iloc[-1]["datetime"], "2026-10-13 10:30:00")

    def test_minute_stale_primary_uses_fallback(self):
        with patch("src.backtest_data.provider.fetch_backtest_kline", return_value=frame(["2026-10-13 10:00:00"], "datetime")), \
             patch("src.backtest_data.provider._fetch_sina_minute", return_value=frame(["2026-10-13 10:30:00"], "datetime")):
            result = fetch_current_backtest_data("600498", "30m", "2020-01-01", now=self.now)
        self.assertEqual(result.attrs["data_source"], "sina minute")

    def test_raw_minute_history_does_not_call_native_decoder(self):
        from src.data_provider import _fetch_sina_history_minute
        with patch("src.data_provider.requests.get") as get, \
             patch("akshare.stock_zh_a_minute", side_effect=AssertionError("native decoder forbidden")):
            get.return_value.text = '=([{"day":"2026-10-13 10:30:00","open":"30","close":"31","high":"32","low":"29","volume":"100"}]);'
            result = _fetch_sina_history_minute("600498", "30")
        self.assertEqual(result.iloc[-1]["datetime"], "2026-10-13 10:30:00")
        self.assertEqual(result.iloc[-1]["close"], 31)
        self.assertEqual(get.call_args.kwargs["timeout"], 20)


class CurveTests(unittest.TestCase):
    def test_sampled_curve_always_contains_first_and_last_and_at_most_200_points(self):
        for count in (1, 199, 200, 201, 400, 4018, 4050):
            with self.subTest(count=count):
                dates = pd.date_range("2010-01-01", periods=count).strftime("%Y-%m-%d")
                result = run_backtest(frame(dates), 10, 80)
                self.assertLessEqual(len(result["curve"]), 200)
                self.assertEqual(result["curve"][0]["time"], result["summary"]["start"])
                self.assertEqual(result["curve"][-1]["time"], result["summary"]["end"])
                self.assertEqual(result["curve"][-1]["equity"], result["summary"]["final_equity"])


class BacktestApiTests(unittest.TestCase):
    def test_saved_parameters_are_not_reoptimized_and_data_is_request_scoped(self):
        from src.api import backtest
        data = frame(["2026-10-09", "2026-10-12"])
        data.attrs.update(data_source="test", expected_end="2026-10-12", freshness="current",
                          checked_at="2026-10-13T10:45:00", completed_bars_only=True)
        response = Response()
        with patch("src.backtest_data.fetch_current_backtest_data", return_value=data), \
             patch("src.optimizer.get_best", return_value={"buy": 10, "sell": 80, "range": "2010-01-04~2026-07-30"}), \
             patch("src.optimizer.optimize_symbol_async") as optimize:
            result = backtest(response, symbol="600498", timeframe="1d", buy=15, sell=85, start="2010-01-01", auto=True)
        optimize.assert_not_called()
        self.assertEqual(result["summary"]["end"], "2026-10-12")
        self.assertEqual(result["params"], {"buy": 10, "sell": 80})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_stale_data_is_503_not_a_successful_old_report(self):
        from src.api import backtest
        with patch("src.backtest_data.fetch_current_backtest_data", side_effect=BacktestDataUnavailable("行情滞后")):
            with self.assertRaises(HTTPException) as caught:
                backtest(Response(), symbol="600498", timeframe="1d", buy=10, sell=80, start="2010-01-01", auto=False)
        self.assertEqual(caught.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
