from __future__ import annotations

import unittest
from datetime import date

from src.band_analysis import default_start_date


class BandAnalysisTests(unittest.TestCase):
    def test_default_window_is_latest_ten_years(self) -> None:
        self.assertEqual(default_start_date(date(2026, 9, 1)), "2016-09-01")

    def test_leap_day_is_safe(self) -> None:
        self.assertEqual(default_start_date(date(2024, 2, 29)), "2014-02-28")


if __name__ == "__main__":
    unittest.main()
