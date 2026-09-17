from __future__ import annotations

import unittest

from src.trade_fees import estimate_trade_fee
from src.trade_ledger import replay_position


class TradeFeeTests(unittest.TestCase):
    def test_small_trades_keep_five_yuan_per_lot(self) -> None:
        self.assertEqual(estimate_trade_fee(33.0, 1), 5.0)
        self.assertEqual(estimate_trade_fee(32.45, 2), 10.0)
        self.assertEqual(estimate_trade_fee(33.4, 5), 25.0)  # 16700 yuan exactly

    def test_larger_trades_use_three_per_ten_thousand(self) -> None:
        self.assertEqual(estimate_trade_fee(30.0, 6), 5.4)
        self.assertEqual(estimate_trade_fee(35.0, 10), 10.5)

    def test_explicit_historical_fee_is_not_repriced(self) -> None:
        position = {
            "opening": {"as_of": "2026-08-01", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
            "trade_history": [{
                "side": "buy", "lots": 2, "price": 32.45,
                "fee": 10, "reported_at": "2026-08-25 10:00:00",
            }],
        }
        result = replay_position(position, as_of="2026-08-25", strict=True)
        self.assertEqual(result["fees_total"], 10.0)


if __name__ == "__main__":
    unittest.main()
