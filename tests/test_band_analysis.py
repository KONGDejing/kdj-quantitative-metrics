from __future__ import annotations

import unittest
from datetime import date

from src.band_analysis import _available_periods_from_dates, default_start_date


class BandAnalysisTests(unittest.TestCase):
    def test_default_window_is_latest_ten_years(self) -> None:
        self.assertEqual(default_start_date(date(2026, 9, 1)), "2016-09-01")

    def test_leap_day_is_safe(self) -> None:
        self.assertEqual(default_start_date(date(2024, 2, 29)), "2014-02-28")

    def test_twelve_year_history_shows_at_most_ten_years(self) -> None:
        periods = _available_periods_from_dates(date(2014, 1, 1), date(2026, 9, 1))
        self.assertEqual([item["label"] for item in periods], ["近10年", "近5年", "近3年", "近2年", "近1年"])
        self.assertNotIn("上市以来", [item["label"] for item in periods])

    def test_four_year_history_hides_five_and_ten_years(self) -> None:
        periods = _available_periods_from_dates(date(2022, 6, 1), date(2026, 9, 1))
        self.assertEqual([item["label"] for item in periods], ["上市以来", "近3年", "近2年", "近1年"])

    def test_new_stock_only_shows_since_listing(self) -> None:
        periods = _available_periods_from_dates(date(2026, 2, 1), date(2026, 9, 1))
        self.assertEqual(periods, [{"key": "listed", "label": "上市以来", "start_date": "2026-02-01"}])


if __name__ == "__main__":
    unittest.main()
