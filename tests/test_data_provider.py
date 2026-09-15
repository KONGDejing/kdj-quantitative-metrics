from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd

from src.data_provider import (
    _fetch_tencent_daily,
    fetch_backtest_daily,
    fetch_realtime_quotes,
    filter_confirmed_daily,
)


class DataProviderTests(unittest.TestCase):
    @patch("src.data_provider._write_cache")
    @patch("src.data_provider._fetch_sina_daily")
    @patch("src.data_provider._read_cache")
    @patch("src.data_provider._retry", side_effect=lambda fn, attempts=3, delay=2.0: fn())
    @patch("akshare.stock_zh_a_hist", side_effect=RuntimeError("primary down"))
    def test_short_cache_does_not_replace_requested_long_history(
        self, _primary: Mock, _retry: Mock, read_cache: Mock,
        sina_daily: Mock, write_cache: Mock,
    ) -> None:
        read_cache.return_value = pd.DataFrame([
            {"date": "2026-07-01", "open": 30, "close": 30, "high": 31, "low": 29},
            {"date": "2026-09-09", "open": 33, "close": 33, "high": 34, "low": 32},
        ])
        sina_daily.return_value = pd.DataFrame([
            {"date": "2023-09-11", "open": 20, "close": 20, "high": 21, "low": 19},
            {"date": "2026-09-09", "open": 33, "close": 33, "high": 34, "low": 32},
        ])

        result = fetch_backtest_daily("000938", "2023-09-11")

        sina_daily.assert_called_once_with("000938")
        self.assertEqual(result["date"].tolist(), ["2023-09-11", "2026-07-01", "2026-09-09"])
        write_cache.assert_called_once()

    def test_partial_current_daily_bar_is_removed_before_close(self) -> None:
        data = pd.DataFrame([
            {"date": "2026-08-25", "close": 10},
            {"date": "2026-08-26", "close": 11},
        ])
        filtered = filter_confirmed_daily(data, datetime(2026, 8, 26, 11, 30))
        self.assertEqual(filtered["date"].tolist(), ["2026-08-25"])

    def test_current_daily_bar_is_kept_after_close(self) -> None:
        data = pd.DataFrame([{"date": "2026-08-26", "close": 11}])
        filtered = filter_confirmed_daily(data, datetime(2026, 8, 26, 15, 2))
        self.assertEqual(filtered["date"].tolist(), ["2026-08-26"])

    @patch("src.data_provider.requests.get")
    def test_tencent_daily_fallback_parses_current_bar(self, mocked_get: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "data": {
                "sz002179": {
                    "day": [["2026-08-26", "33.48", "33.37", "33.68", "33.25", "149315"]]
                }
            }
        }
        mocked_get.return_value = response

        data = _fetch_tencent_daily("002179")

        self.assertEqual(data.iloc[-1]["date"], "2026-08-26")
        self.assertEqual(float(data.iloc[-1]["close"]), 33.37)
        self.assertEqual(data.attrs["data_source"], "tencent_daily")

    @patch("src.data_provider.requests.get")
    def test_tencent_daily_ignores_appended_provider_field(self, mocked_get: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "data": {
                "sh600584": {
                    "day": [["2026-09-04", "71.39", "67.36", "71.92", "66.63", "123456", "0.99"]]
                }
            }
        }
        mocked_get.return_value = response

        data = _fetch_tencent_daily("600584")

        self.assertEqual(data.iloc[-1]["date"], "2026-09-04")
        self.assertEqual(float(data.iloc[-1]["close"]), 67.36)

    @patch("src.data_provider.requests.get")
    def test_realtime_quote_parses_price_and_exchange_timestamp(self, mocked_get: Mock) -> None:
        fields = [""] * 35
        fields[1] = "长电科技"
        fields[2] = "600584"
        fields[3] = "71.20"
        fields[4] = "72.00"
        fields[30] = "20260828103005"
        fields[32] = "-1.11"
        fields[33] = "72.10"
        fields[34] = "70.90"
        response = Mock()
        response.content = f'v_sh600584="{"~".join(fields)}";'.encode("gb18030")
        mocked_get.return_value = response

        quote = fetch_realtime_quotes(["600584"])["600584"]

        self.assertEqual(quote["price"], 71.2)
        self.assertEqual(quote["timestamp"], "20260828103005")
        self.assertAlmostEqual(float(quote["change_ratio"]), -0.0111)
