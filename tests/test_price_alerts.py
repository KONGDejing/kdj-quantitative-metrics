from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

from src import runner
from src import optimizer
from src.kdj_policy import kdj_alerts_enabled


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


def quote(price: float, timestamp: str = "20260828145000") -> dict[str, dict]:
    return {"600584": {
        "symbol": "600584",
        "name": "长电科技",
        "price": price,
        "timestamp": timestamp,
        "change_ratio": -0.02,
        "source": "tencent_realtime",
    }}


def stabilized_bars(now: datetime) -> pd.DataFrame:
    rows = []
    closes = [70.00, 70.10, 70.25, 70.35, 70.50, 70.70, 70.90, 71.05, 71.20]
    for index, close in enumerate(closes):
        point = now - timedelta(minutes=40 - index * 5)
        rows.append({
            "datetime": point.strftime("%Y-%m-%d %H:%M:%S"),
            "open": close - 0.05,
            "high": close + 0.05,
            "low": 69.80 if index == 0 else close - 0.10,
            "close": close,
        })
    return pd.DataFrame(rows)


class PriceTargetAlertTests(unittest.TestCase):
    def test_kdj_exception_keeps_indicator_policy_but_disables_alerts(self) -> None:
        config = {"kdj_alerts": {"enabled": True, "excluded_symbols": ["000938"]}}

        self.assertFalse(kdj_alerts_enabled(config, "000938"))
        self.assertTrue(kdj_alerts_enabled(config, "002179"))

    def test_unqualified_optimizer_result_never_overrides_safe_defaults(self) -> None:
        with patch.object(optimizer, "get_best", return_value={
            "buy": 25,
            "sell": 75,
            "qualified": False,
            "round_trips": 3,
        }):
            result = runner._best_thresholds("000938", {"lower": 20, "upper": 80})

        self.assertEqual(result, {"buy": 20.0, "sell": 80.0, "auto": False})

    def test_target_touch_before_cutoff_sends_nothing(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 10, 1)
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(71.20, "20260828100000")
        ), patch.object(runner, "safe_fetch_kline") as bars, patch.object(
            runner, "notify_price_target"
        ) as notify:
            runner._maybe_send_price_target_alerts(config_with_lots(), 600, now=now)

        bars.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(fake.alerts, [])

    def test_stabilized_target_zone_sends_actionable_signal_once(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 14, 50)
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(71.20)
        ), patch.object(
            runner, "safe_fetch_kline", return_value=stabilized_bars(now)
        ), patch.object(runner, "notify_price_target") as notify:
            runner._maybe_send_price_target_alerts(config_with_lots(), 600, now=now)
            runner._maybe_send_price_target_alerts(config_with_lots(), 600, now=now)

        notify.assert_called_once()
        self.assertEqual(len(fake.alerts), 1)
        self.assertEqual(fake.alerts[0]["lots"], 1)
        self.assertEqual(fake.alerts[0]["trigger_price"], 71.355)
        self.assertEqual(fake.alerts[0]["max_buy_price"], 71.35)
        self.assertEqual(fake.alerts[0]["type"], "observation_buy_ready")
        self.assertTrue(fake.alerts[0]["stabilization"]["ready"])
        self.assertEqual(fake.alerts[0]["observation_discipline"]["earliest_entry_time"], "14:45")

    def test_hysteresis_rearms_only_after_price_leaves_reset_zone(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 14, 50)
        with patch.object(runner, "state", fake), patch.object(
            runner, "safe_fetch_kline", return_value=stabilized_bars(now)
        ), patch.object(runner, "notify_price_target"
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
                config_with_lots(), 600, now=datetime(2026, 8, 28, 14, 50)
            )

        notify.assert_not_called()
        self.assertEqual(fake.alerts, [])

    def test_flat_only_rule_skips_existing_position(self) -> None:
        fake = FakeState()
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(71.20)
        ), patch.object(runner, "safe_fetch_kline") as bars, patch.object(
            runner, "notify_price_target"
        ) as notify:
            runner._maybe_send_price_target_alerts(
                config_with_lots(1), 600, now=datetime(2026, 8, 28, 14, 50)
            )

        bars.assert_not_called()
        notify.assert_not_called()

    def test_existing_new_symbol_today_blocks_another(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 14, 50)
        config = config_with_lots()
        config["trade_plan"]["positions"]["600001"] = {
            "strategy_mode": "long_term",
            "opening": {"as_of": "2026-08-01", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
            "trade_history": [{
                "side": "buy", "bucket": "core", "lots": 1, "price": 10, "fee": 5,
                "reported_at": "2026-08-28 10:00:00",
            }],
        }
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(71.20)
        ), patch.object(runner, "safe_fetch_kline", return_value=stabilized_bars(now)) as bars, patch.object(
            runner, "notify_price_target"
        ) as notify:
            runner._maybe_send_price_target_alerts(config, 600, now=now)

        bars.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(fake.alerts, [])

    def test_existing_buy_order_suppresses_duplicate_entry_signal(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 14, 50)
        config = config_with_lots()
        config["trade_plan"]["positions"]["600584"]["pending_orders"] = [{
            "id": "buy-1", "side": "buy", "lots": 1, "limit_price": 71.0, "status": "open",
        }]
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(71.20)
        ), patch.object(runner, "safe_fetch_kline") as bars, patch.object(
            runner, "notify_price_target"
        ) as notify:
            runner._maybe_send_price_target_alerts(config, 600, now=now)

        bars.assert_not_called()
        notify.assert_not_called()

    def test_bounded_entry_zone_does_not_alert_below_minimum(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 14, 50)
        config = config_with_lots()
        config["price_alerts"]["600584"].update({
            "target_price": 26.0,
            "minimum_price": 25.0,
            "tolerance_ratio": 0.0,
        })
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote(24.90)
        ), patch.object(runner, "safe_fetch_kline") as bars, patch.object(
            runner, "notify_price_target"
        ) as notify:
            runner._maybe_send_price_target_alerts(config, 600, now=now)

        bars.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(fake.alerts, [])


class ObservationExitAlertTests(unittest.TestCase):
    def _config(self, opening_day: str = "2026-08-01") -> dict:
        return {
            "alert": {"channels": ["pushplus"]},
            "symbols": [{"code": "600498", "name": "烽火通信"}],
            "trade_plan": {"positions": {"600498": {
                "strategy_mode": "long_term",
                "target_sell": 40.6,
                "stop_loss": 35.0,
                "opening": {
                    "as_of": opening_day, "core_lots": 1, "t_lots": 0, "cost_per_share": 39,
                },
                "trade_history": [],
            }}},
        }

    def test_take_profit_sends_actionable_sell_signal(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 14, 50)
        exit_quote = {"600498": {
            "symbol": "600498", "name": "烽火通信", "price": 40.65,
            "timestamp": "20260828145000", "source": "tencent_realtime",
        }}
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=exit_quote
        ), patch.object(runner, "notify_observation_exit") as notify:
            runner._maybe_send_observation_exit_alerts(self._config(), 600, now=now)

        notify.assert_called_once()
        self.assertEqual(fake.alerts[0]["direction"], "sell_take_profit")
        self.assertEqual(fake.alerts[0]["lots"], 1)

    def test_zhonghang_final_target_sells_all_sellable_lots(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 14, 50)
        config = {
            "alert": {"channels": ["pushplus"]},
            "symbols": [{"code": "002179", "name": "中航光电"}],
            "trade_plan": {"positions": {"002179": {
                "strategy_mode": "expand_base",
                "final_exit_target": 40.0,
                "opening": {
                    "as_of": "2026-08-01", "core_lots": 10,
                    "t_lots": 0, "cost_per_share": 33.5,
                },
                "trade_history": [],
            }}},
        }
        quote = {"002179": {
            "symbol": "002179", "name": "中航光电", "price": 40.05,
            "timestamp": "20260828145000", "source": "tencent_realtime",
        }}
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=quote
        ), patch.object(runner, "notify_observation_exit") as notify:
            runner._maybe_send_observation_exit_alerts(config, 600, now=now)

        notify.assert_called_once()
        self.assertEqual(fake.alerts[0]["lots"], 10)
        self.assertTrue(fake.alerts[0]["position_exit"])

    def test_t1_locked_position_does_not_send_sell_signal(self) -> None:
        fake = FakeState()
        now = datetime(2026, 8, 28, 14, 50)
        exit_quote = {"600498": {
            "symbol": "600498", "name": "烽火通信", "price": 34.90,
            "timestamp": "20260828145000", "source": "tencent_realtime",
        }}
        with patch.object(runner, "state", fake), patch.object(
            runner, "fetch_realtime_quotes", return_value=exit_quote
        ), patch.object(runner, "notify_observation_exit") as notify:
            runner._maybe_send_observation_exit_alerts(self._config("2026-08-28"), 600, now=now)

        notify.assert_not_called()
        self.assertEqual(fake.alerts, [])


if __name__ == "__main__":
    unittest.main()
