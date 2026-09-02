from __future__ import annotations

import unittest

from src.trade_ledger import LedgerError, replay_position


class TradeLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.position = {
            "fee_per_lot": 5,
            "max_t_lots": 10,
            "opening": {
                "as_of": "2026-08-12",
                "core_lots": 5,
                "t_lots": 0,
                "cost_per_share": 33.978,
            },
            "trade_history": [
                {"side": "sell", "lots": 1, "price": 35.94, "fee": 5, "reported_at": "2026-08-13 10:42:05"},
                {"side": "sell", "lots": 1, "price": 36.48, "fee": 5, "reported_at": "2026-08-14 10:29:18"},
                {"side": "buy", "lots": 1, "price": 35.93, "fee": 5, "reported_at": "2026-08-14 15:34:42"},
                {"side": "buy", "lots": 1, "price": 35.40, "fee": 5, "reported_at": "2026-08-17 10:00:51"},
                {"side": "buy", "lots": 1, "price": 35.00, "fee": 5, "reported_at": "2026-08-19 10:46:37"},
                {"side": "buy", "lots": 1, "price": 34.45, "fee": 5, "reported_at": "2026-08-19 14:46:26"},
                {"side": "buy", "lots": 1, "price": 32.90, "fee": 5, "reported_at": "2026-08-24 14:30:00"},
                {"side": "buy", "lots": 2, "price": 32.45, "fee": 10, "reported_at": "2026-08-25 10:29:55"},
            ],
        }

    def test_replays_verified_zhonghang_position(self) -> None:
        result = replay_position(self.position, as_of="2026-08-25", strict=True)
        self.assertEqual(result["core_lots"], 10)
        self.assertEqual(result["t_lots"], 0)
        self.assertEqual(result["pending_core_buyback_lots"], 0)
        self.assertEqual(result["sellable_lots_today"], 8)
        self.assertEqual(result["locked_t1_lots"], 2)
        self.assertAlmostEqual(result["average_entry_cost"], 34.0864, places=3)
        self.assertAlmostEqual(result["breakeven_cost"], 33.65, places=3)
        self.assertAlmostEqual(result["realized_pnl"], 436.4, places=2)
        self.assertEqual(result["completed_core_roundtrip_lots"], 2)
        self.assertEqual(result["completed_core_roundtrip_events"], 2)
        self.assertAlmostEqual(result["core_roundtrip_gross_pnl"], 109.0, places=2)
        self.assertAlmostEqual(result["core_roundtrip_fees"], 20.0, places=2)
        self.assertAlmostEqual(result["core_roundtrip_net_pnl"], 89.0, places=2)
        self.assertTrue(result["validation"]["ok"])

    def test_next_trading_day_unlocks_today_buys(self) -> None:
        result = replay_position(self.position, as_of="2026-08-26", strict=True)
        self.assertEqual(result["sellable_lots_today"], 10)
        self.assertEqual(result["locked_t1_lots"], 0)

    def test_explicit_tactical_bucket_is_kept_separate(self) -> None:
        self.position["trade_history"].append({
            "side": "buy",
            "bucket": "tactical",
            "lots": 2,
            "price": 32.00,
            "fee": 10,
            "reported_at": "2026-08-26 10:00:00",
        })
        result = replay_position(self.position, as_of="2026-08-26", strict=True)
        self.assertEqual(result["core_lots"], 10)
        self.assertEqual(result["t_lots"], 2)
        self.assertEqual(result["locked_t1_lots"], 2)

    def test_rejects_same_day_sell_of_new_lots(self) -> None:
        position = {
            "fee_per_lot": 5,
            "opening": {"as_of": "2026-08-24", "core_lots": 0, "t_lots": 0, "cost_per_share": 0},
            "trade_history": [
                {"side": "buy", "lots": 1, "price": 10, "fee": 5, "reported_at": "2026-08-25 09:40:00"},
                {"side": "sell", "lots": 1, "price": 10.2, "fee": 5, "reported_at": "2026-08-25 14:40:00"},
            ],
        }
        with self.assertRaises(LedgerError):
            replay_position(position, as_of="2026-08-25", strict=True)

    def test_core_sell_tracks_reference_until_buyback(self) -> None:
        position = {
            "fee_per_lot": 5,
            "opening": {"as_of": "2026-08-12", "core_lots": 5, "t_lots": 0, "cost_per_share": 33.978},
            "trade_history": [{
                "side": "sell", "bucket": "core", "lots": 1, "price": 36.48,
                "fee": 5, "reported_at": "2026-08-14 10:29:18",
            }],
        }
        sold = replay_position(position, as_of="2026-08-14")
        self.assertEqual(sold["pending_core_buyback_lots"], 1)
        self.assertEqual(sold["pending_core_sell_reference_price"], 36.48)

        position["trade_history"].append({
            "side": "buy", "bucket": "core", "lots": 1, "price": 35.93,
            "fee": 5, "reported_at": "2026-08-14 14:30:00",
        })
        restored = replay_position(position, as_of="2026-08-14")
        self.assertEqual(restored["pending_core_buyback_lots"], 0)
        self.assertIsNone(restored["pending_core_sell_reference_price"])

    def test_third_reverse_t_roundtrip_is_derived_from_trade_history(self) -> None:
        self.position["trade_history"].extend([
            {
                "side": "sell", "bucket": "core", "lots": 1, "price": 34.99,
                "fee": 5, "reported_at": "2026-08-27 10:21:28",
            },
            {
                "side": "buy", "bucket": "core", "lots": 1, "price": 34.00,
                "fee": 5, "reported_at": "2026-09-02 10:46:34",
            },
        ])
        result = replay_position(self.position, as_of="2026-09-02", strict=True)
        self.assertEqual(result["core_lots"], 10)
        self.assertEqual(result["sellable_core_lots_today"], 9)
        self.assertEqual(result["locked_t1_lots"], 1)
        self.assertEqual(result["completed_core_roundtrip_lots"], 3)
        self.assertEqual(result["completed_core_roundtrip_events"], 3)
        self.assertEqual(result["completed_core_roundtrip_events_today"], 1)
        self.assertAlmostEqual(result["core_roundtrip_gross_pnl"], 208.0, places=2)
        self.assertAlmostEqual(result["core_roundtrip_fees"], 30.0, places=2)
        self.assertAlmostEqual(result["core_roundtrip_net_pnl"], 178.0, places=2)


if __name__ == "__main__":
    unittest.main()
