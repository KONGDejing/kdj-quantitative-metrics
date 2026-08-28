from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from src import runner


class FakeState:
    def __init__(self) -> None:
        self.alert_zones: dict[str, str] = {}
        self.alerts: list[dict] = []

    def should_alert(self, key: str, direction: str, _cooldown: int) -> bool:
        if self.alert_zones.get(key) == direction:
            return False
        self.alert_zones[key] = direction
        return True

    def clear_alert_zone(self, key: str) -> None:
        self.alert_zones.pop(key, None)

    def add_alert(self, alert: dict) -> None:
        self.alerts.append(alert)


def config_with_lots(lots: int = 0) -> dict:
    return {
        "alert": {"channels": ["pushplus"]},
        "price_alerts": {
            "600584": {
                "name": "长电科技",
                "target_price": 71,
                "tolerance_ratio": 0.005,
                "reset_ratio": 0.015,
                "lots": 1,
                "only_when_flat": True,
                "max_quote_age_seconds": 180,
            }
        },
        "trade_plan": {"positions": {
            "600584": {
                "opening": {
                    "as_of": "2026-08-01",
                    "core_lots": lots,
                    "t_lots": 0,
                    "cost_per_share": 70 if lots else 0,
                },
                "trade_history": [],
            }
        }},
    }


def quote(price: float, timestamp: str = "20260828100000") -> dict[str, dict]:
    return {"600584": {
        "symbol": "600584",
        "name": "长电科技",
        "price": price,
        "timestamp": timestamp,
        "change_ratio": -0.02,
        "source": "tencent_realtime",
    }}


class PriceTargetAlertTests(unittest.TestCase):
    def test_enters_target_zone_once_and_uses_one_lot(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 10, 1)
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(71.20)
        ), patch.object(runner, "notify_price_target") as notify:
            runner._maybe_send_price_target_alerts(config_with_lots(), 600, now=now)
            runner._maybe_send_price_target_alerts(config_with_lots(), 600, now=now)

        notify.assert_called_once()
        self.assertEqual(len(fake.alerts), 1)
        self.assertEqual(fake.alerts[0]["lots"], 1)
        self.assertEqual(fake.alerts[0]["trigger_price"], 71.355)

    def test_hysteresis_rearms_only_after_price_leaves_reset_zone(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 10, 1)
        with patch.object(runner, "state", fake), patch.object(
            runner, "notify_price_target"
        ) as notify:
            with patch.object(runner, "fetch_realtime_quotes", return_value=quote(71.20)):
                runner._maybe_send_price_target_alerts(config_with_lots(), 600, now=now)
            with patch.object(runner, "fetch_realtime_quotes", return_value=quote(72.20)):
                runner._maybe_send_price_target_alerts(config_with_lots(), 600, now=now)
            with patch.object(runner, "fetch_realtime_quotes", return_value=quote(71.20)):
                runner._maybe_send_price_target_alerts(config_with_lots(), 600, now=now)

        self.assertEqual(notify.call_count, 2)

    def test_stale_quote_is_fail_closed(self) -> None:
        fake = FakeState()
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(70.0, "20260827095900")
        ), patch.object(runner, "notify_price_target") as notify:
            runner._maybe_send_price_target_alerts(
                config_with_lots(), 600, now=datetime(2026, 8, 28, 10, 1)
            )

        notify.assert_not_called()
        self.assertEqual(fake.alerts, [])

    def test_flat_only_rule_skips_existing_position(self) -> None:
        fake = FakeState()
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(71.20)
        ), patch.object(runner, "notify_price_target") as notify:
            runner._maybe_send_price_target_alerts(
                config_with_lots(1), 600, now=datetime(2026, 8, 28, 10, 1)
            )

        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
