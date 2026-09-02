from __future__ import annotations

import unittest
from datetime import datetime
from threading import Lock
from unittest.mock import patch

from src.state import AppState, DuplicateTradeError


class TradeDuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        self.existing = {
            "id": "existing", "side": "buy", "bucket": "core", "lots": 1,
            "price": 34.0, "fee": 5, "reported_at": f"{today} 10:00:00",
        }
        self.app = AppState.__new__(AppState)
        self.app._lock = Lock()
        self.app.config = {"trade_plan": {"positions": {"002179": {
            "fee_per_lot": 5,
            "opening": {"as_of": "2026-01-01", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
            "trade_history": [self.existing],
        }}}}

    @patch("src.state.save_config")
    def test_same_day_semantic_duplicate_is_rejected(self, save_config) -> None:
        with self.assertRaises(DuplicateTradeError) as raised:
            self.app.report_trade("002179", "buy", 1, 34, bucket="core", note="different note")
        self.assertEqual(raised.exception.existing_trade["id"], "existing")
        self.assertEqual(len(self.app.config["trade_plan"]["positions"]["002179"]["trade_history"]), 1)
        save_config.assert_not_called()

    @patch("src.state.save_config")
    def test_confirm_allows_a_real_second_trade(self, save_config) -> None:
        result = self.app.report_trade(
            "002179", "buy", 1, 34, bucket="core", confirm_duplicate=True,
        )
        self.assertEqual(len(result["trade_history"]), 2)
        self.assertEqual(result["ledger"]["core_lots"], 2)
        save_config.assert_called_once()

    @patch("src.state.save_config")
    def test_different_price_is_not_duplicate(self, save_config) -> None:
        result = self.app.report_trade("002179", "buy", 1, 33.99, bucket="core")
        self.assertEqual(len(result["trade_history"]), 2)
        save_config.assert_called_once()


if __name__ == "__main__":
    unittest.main()
