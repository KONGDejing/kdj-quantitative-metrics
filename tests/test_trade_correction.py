from __future__ import annotations

import unittest
from threading import Lock
from unittest.mock import patch

from src.state import AppState


class TradeCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = AppState.__new__(AppState)
        self.app._lock = Lock()
        self.app.symbols = [{"code": "002179", "name": "中航光电"}]
        self.app.config = {
            "symbols": self.app.symbols,
            "trade_plan": {"positions": {"002179": {
                "fee_per_lot": 5,
                "opening": {"as_of": "2026-08-01", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
                "trade_history": [{
                    "id": "trade-1", "side": "buy", "bucket": "core", "lots": 1,
                    "price": 10, "fee": 5, "reported_at": "2026-08-02 10:00:00",
                }],
            }}},
        }

    @patch("src.state.add_correction_audit")
    @patch("src.state.save_config")
    def test_replaces_wrong_value_without_retaining_it(self, _save, audit) -> None:
        result = self.app.correct_trade("002179", "trade-1", replacement={"price": 11})
        trade = self.app.config["trade_plan"]["positions"]["002179"]["trade_history"][0]
        self.assertEqual(trade["price"], 11)
        self.assertNotIn(10, trade.values())
        self.assertAlmostEqual(result["ledger"]["breakeven_cost"], 11.05, places=2)
        audit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
