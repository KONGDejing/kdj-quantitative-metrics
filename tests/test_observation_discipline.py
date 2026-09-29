from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from src import notifier
from src.observation_discipline import (
    evaluate_stabilization,
    format_observation_discipline,
    observation_discipline,
)


class ObservationDisciplineTests(unittest.TestCase):
    def test_defaults_are_stable(self) -> None:
        rule = observation_discipline({})
        self.assertEqual(rule["earliest_entry_time"], "14:45")
        self.assertEqual(rule["latest_entry_time"], "14:55")
        self.assertEqual(rule["no_new_low_minutes"], 30)
        self.assertEqual(rule["max_new_symbols_per_day"], 1)
        self.assertEqual(rule["total_capital_limit"], 20_000)
        self.assertEqual(rule["min_days_before_add"], 5)
        self.assertFalse(rule["allow_preplanned_limit_orders"])
        self.assertEqual(rule["preplanned_limit_lots_per_symbol"], 1)
        self.assertTrue(rule["forbid_raise_limit_price"])

    def test_daily_reminder_keeps_single_new_stock_limit(self) -> None:
        text = format_observation_discipline({})
        self.assertIn("收到当日止跌确认后才挂单", text)
        self.assertNotIn("限价单并存", text)
        self.assertIn("不得抬价追单", text)
        self.assertIn("14:45", text)
        self.assertIn("30分钟不创新低", text)
        self.assertIn("同一天最多新买1只股票", text)
        self.assertIn("不超过20000元", text)
        self.assertIn("至少5个交易日不加仓", text)

    def test_price_alert_is_an_actionable_buy_signal(self) -> None:
        alert = {
            "name": "测试股票",
            "symbol": "600000",
            "close": 10,
            "target_price": 10,
            "trigger_price": 10.05,
            "max_buy_price": 10.05,
            "lots": 1,
            "estimated_cash": 1005,
            "timestamp": "20260904100000",
            "created_at": "2026-09-04 10:00:00",
            "reason": "测试",
            "risk_note": "测试风险",
            "observation_discipline": observation_discipline({}),
            "stabilization": {
                "ready": True,
                "window_minutes": 30,
                "session_low": 9.90,
                "rebound_ratio": 0.01,
                "latest_low_time": "2026-09-04 14:10:00",
            },
        }
        with patch.object(notifier, "send_pushplus", return_value=True) as send:
            notifier.notify_price_target({"alert": {"channels": ["pushplus"]}}, alert)

        _, subject, content = send.call_args.args
        self.assertIn("现在可买1手", subject)
        self.assertIn("现在允许限价买入1手", content)
        self.assertIn("最高买入价：10.05元", content)
        self.assertIn("连续30分钟不创新低", content)
        self.assertIn("每只最多买1手", content)
        self.assertIn("同一天最多新买1只观察股票", content)

    def test_stabilization_requires_fresh_rebound_after_old_low(self) -> None:
        now = datetime(2026, 9, 4, 14, 50)
        bars = []
        for index in range(9):
            point = now - timedelta(minutes=40 - index * 5)
            close = 10 + index * 0.02
            bars.append({
                "datetime": point.strftime("%Y-%m-%d %H:%M:%S"),
                "low": 9.90 if index == 0 else close - 0.01,
                "close": close,
            })
        result = evaluate_stabilization(
            bars,
            now=now,
            current_price=10.16,
            discipline=observation_discipline({}),
        )
        self.assertTrue(result["ready"])
        self.assertGreater(result["rebound_ratio"], 0.005)

    def test_latest_five_minute_bar_must_not_be_falling(self) -> None:
        now = datetime(2026, 9, 4, 14, 50)
        closes = [10.00, 10.02, 10.04, 10.06, 10.08, 10.10, 10.12, 10.15, 10.13]
        bars = [{
            "datetime": (now - timedelta(minutes=40 - index * 5)).strftime("%Y-%m-%d %H:%M:%S"),
            "low": 9.90 if index == 0 else close - 0.01,
            "close": close,
        } for index, close in enumerate(closes)]
        result = evaluate_stabilization(
            bars,
            now=now,
            current_price=10.13,
            discipline=observation_discipline({}),
        )
        self.assertFalse(result["ready"])
        self.assertIn("最新5分钟线", result["reason"])


if __name__ == "__main__":
    unittest.main()
