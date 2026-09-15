from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from src import runner


class FakeState:
    def __init__(self, timestamp: str) -> None:
        self.latest = {"002179": {"10m": {
            "timestamp": timestamp, "k": 75, "d": 78, "j": 69, "close": 35,
        }}}
        self.alerts: list[dict] = []
        self.zones: dict[str, str] = {}

    def should_alert(self, key: str, direction: str, _cooldown: int) -> bool:
        self.zones[key] = direction
        return True

    def clear_alert_zone(self, key: str) -> None:
        self.zones.pop(key, None)

    def add_alert(self, alert: dict) -> None:
        self.alerts.append(alert)


def config() -> dict:
    return {"trade_plan": {"positions": {"002179": {"reverse_t": {"enabled": True}}}}}


def executable_plan(timestamp: str) -> dict:
    return {"reverse_t": {
        "decision": {
            "status": "executable", "action": "sell_core_for_reverse_t",
            "max_lots": 1, "summary": "全部条件通过",
        },
        "signal": {"intraday_timestamp": timestamp, "k": 75, "close": 35},
        "price_plan": {
            "sell_limit": 35, "expected_buyback": 34.47, "target_gap_ratio": 0.015,
        },
        "rule": {},
        "quota_lots": 2,
        "core_floor_lots": 8,
    }}


class ReverseTAlertTests(unittest.TestCase):
    def test_bar_end_is_not_complete_before_provider_boundary(self) -> None:
        end = "2026-09-15 14:10:00"
        self.assertFalse(runner._minute_bar_complete(end, datetime(2026, 9, 15, 14, 6)))
        self.assertFalse(runner._minute_bar_complete(end, datetime(2026, 9, 15, 14, 10)))
        self.assertFalse(runner._minute_bar_complete(end, datetime(2026, 9, 15, 14, 10, 5)))
        self.assertTrue(runner._minute_bar_complete(end, datetime(2026, 9, 15, 14, 10, 15)))
        self.assertFalse(runner._minute_bar_complete("bad timestamp", datetime(2026, 9, 15, 14, 20)))

    def test_fresh_complete_signal_sends_actionable_alert(self) -> None:
        now = datetime(2026, 9, 4, 10, 21)
        fake = FakeState("2026-09-04 10:20:00")
        with patch.object(runner, "state", fake), patch.object(
            runner, "_build_deterministic_plan",
            return_value=executable_plan("2026-09-04 10:20:00"),
        ), patch.object(runner, "notify_reverse_t") as notify:
            runner._maybe_send_reverse_t_alert(
                {"code": "002179", "name": "中航光电"}, config(), 600, now=now
            )

        notify.assert_called_once()
        self.assertEqual(fake.alerts[0]["reverse_t"]["decision"]["max_lots"], 1)

    def test_stale_ten_minute_bar_never_sends(self) -> None:
        now = datetime(2026, 9, 4, 10, 40)
        fake = FakeState("2026-09-04 10:20:00")
        with patch.object(runner, "state", fake), patch.object(
            runner, "_build_deterministic_plan",
            return_value=executable_plan("2026-09-04 10:20:00"),
        ), patch.object(runner, "notify_reverse_t") as notify:
            runner._maybe_send_reverse_t_alert(
                {"code": "002179", "name": "中航光电"}, config(), 600, now=now
            )

        notify.assert_not_called()
        self.assertEqual(fake.alerts, [])

    def test_current_bar_end_timestamp_is_accepted_as_provisional_signal(self) -> None:
        now = datetime(2026, 9, 7, 14, 42)
        fake = FakeState("2026-09-07 14:50:00")
        with patch.object(runner, "state", fake), patch.object(
            runner, "_build_deterministic_plan",
            return_value=executable_plan("2026-09-07 14:50:00"),
        ), patch.object(runner, "notify_reverse_t") as notify:
            runner._maybe_send_reverse_t_alert(
                {"code": "002179", "name": "中航光电"}, config(), 600, now=now
            )

        notify.assert_called_once()
        self.assertEqual(len(fake.alerts), 1)


if __name__ == "__main__":
    unittest.main()
