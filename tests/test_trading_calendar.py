from __future__ import annotations

import unittest

from src.trading_calendar import calendar_status, is_session_date, next_session


class TradingCalendarTests(unittest.TestCase):
    def test_official_2026_holiday_is_closed(self) -> None:
        status = calendar_status("2026-10-05")
        self.assertFalse(status["is_session"])
        self.assertTrue(status["verified"])
        self.assertEqual(status["reason"], "official_holiday")

    def test_normal_weekday_is_session(self) -> None:
        self.assertTrue(is_session_date("2026-08-26"))

    def test_unknown_year_fails_closed(self) -> None:
        status = calendar_status("2027-01-04", {"market_calendar": {"fail_closed_unknown_year": True}})
        self.assertFalse(status["is_session"])
        self.assertFalse(status["verified"])

    def test_next_session_skips_national_day_holiday(self) -> None:
        self.assertEqual(next_session("2026-09-30"), "2026-10-08")


if __name__ == "__main__":
    unittest.main()
