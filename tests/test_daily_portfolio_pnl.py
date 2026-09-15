from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src import runner


class DailyPortfolioPnlTests(unittest.TestCase):
    def fake_state(self) -> SimpleNamespace:
        positions = {
            "600001": {
                "opening": {
                    "as_of": "2026-09-10",
                    "core_lots": 1,
                    "t_lots": 0,
                    "cost_per_share": 10,
                },
                "trade_history": [{
                    "side": "sell",
                    "bucket": "core",
                    "lots": 1,
                    "price": 12,
                    "fee": 5,
                    "position_exit": True,
                    "reported_at": "2026-09-11 10:00:00",
                }],
            },
            "600002": {
                "opening": {
                    "as_of": "2026-09-10",
                    "core_lots": 0,
                    "t_lots": 0,
                    "cost_per_share": 0,
                },
                "trade_history": [{
                    "side": "buy",
                    "bucket": "core",
                    "lots": 1,
                    "price": 20,
                    "fee": 5,
                    "reported_at": "2026-09-11 10:00:00",
                }],
            },
            "600003": {
                "opening": {
                    "as_of": "2026-09-09", "core_lots": 0,
                    "t_lots": 0, "cost_per_share": 0,
                },
                "trade_history": [
                    {"side": "buy", "lots": 1, "price": 10, "fee": 5,
                     "reported_at": "2026-09-09 10:00:00"},
                    {"side": "sell", "lots": 1, "price": 11, "fee": 5,
                     "position_exit": True, "reported_at": "2026-09-10 10:00:00"},
                ],
                "pending_orders": [{"side": "buy", "lots": 1, "limit_price": 9,
                                    "status": "open", "placed_at": "2026-09-11"}],
            },
        }
        return SimpleNamespace(
            symbols=[{"code": "600001", "name": "甲股票"}],
            config={
                "trade_plan": {"positions": positions},
                "price_alerts": {
                    "600002": {"name": "乙股票"},
                    "600003": {"name": "丙股票"},
                },
            },
            series={
                "600001": {"1d": [
                    {"timestamp": "2026-09-10", "close": 11},
                    {"timestamp": "2026-09-11", "close": 12},
                ]},
                "600002": {"1d": [
                    {"timestamp": "2026-09-10", "close": 19},
                    {"timestamp": "2026-09-11", "close": 21},
                ]},
            },
        )

    def test_report_includes_fees_trades_and_held_stock_outside_watchlist(self) -> None:
        fake = self.fake_state()
        with patch.object(runner, "state", fake):
            report = runner._build_daily_portfolio_pnl(fake.config, "2026-09-11")

        self.assertIsNotNone(report)
        self.assertEqual(report["daily_pnl"], 190.0)
        self.assertEqual(report["cumulative_pnl"], 290.0)
        self.assertEqual(report["ledger_cumulative_pnl"], 380.0)
        self.assertEqual([row["symbol"] for row in report["rows"]], ["600001", "600002"])
        self.assertEqual([row["daily_pnl"] for row in report["rows"]], [95.0, 95.0])
        self.assertTrue(report["rows"][0]["exited_today"])
        self.assertEqual(report["rows"][0]["today_realized_pnl"], 195.0)
        self.assertEqual(report["rows"][1]["breakeven_cost"], 20.05)
        text = runner._format_daily_portfolio_pnl(report)
        self.assertIn("甲股票(600001)：今日+95.00元", text)
        self.assertIn("今日已清仓，卖出已实现+195.00元", text)
        self.assertIn("乙股票(600002)：今日+95.00元", text)
        self.assertIn("持仓盈亏+95.00元；摊薄保本成本20.050元", text)
        self.assertNotIn("丙股票", text)
        self.assertIn("今日合计：+190.00元", text)
        self.assertIn("所列股票账本累计合计：+290.00元", text)
        self.assertIn("账本累计合计：+380.00元", text)

    def test_stale_daily_data_blocks_the_whole_message(self) -> None:
        fake = self.fake_state()
        fake.series["600002"]["1d"] = [{"timestamp": "2026-09-10", "close": 19}]
        with patch.object(runner, "state", fake), patch.object(
            runner, "safe_fetch_kline", return_value=None
        ):
            report = runner._build_daily_portfolio_pnl(fake.config, "2026-09-11")

        self.assertIsNone(report)

    def test_sold_stock_missing_from_watchlist_gets_fresh_formal_daily_data(self) -> None:
        fake = self.fake_state()
        fake.symbols = []
        fake.config["price_alerts"]["600001"] = {"name": "甲股票"}
        fake.series.pop("600001")
        fresh = pd.DataFrame([
            {"date": "2026-09-10", "close": 11},
            {"date": "2026-09-11", "close": 12},
        ])
        with patch.object(runner, "state", fake), patch.object(
            runner, "safe_fetch_kline", return_value=fresh
        ) as fetch:
            report = runner._build_daily_portfolio_pnl(fake.config, "2026-09-11")

        fetch.assert_called_once_with("600001", "1d")
        self.assertEqual([row["name"] for row in report["rows"]], ["甲股票", "乙股票"])
        self.assertTrue(report["rows"][0]["exited_today"])

    def test_yesterday_exited_stock_is_absent_the_next_trading_day(self) -> None:
        fake = self.fake_state()
        fake.series["600002"]["1d"].append({"timestamp": "2026-09-12", "close": 22})
        with patch.object(runner, "state", fake), patch.object(
            runner, "safe_fetch_kline"
        ) as fetch:
            report = runner._build_daily_portfolio_pnl(fake.config, "2026-09-12")

        fetch.assert_not_called()
        self.assertEqual([row["symbol"] for row in report["rows"]], ["600002"])
        self.assertEqual(report["cumulative_pnl"], 195.0)
        self.assertEqual(report["ledger_cumulative_pnl"], 480.0)

    def test_exited_stock_returns_to_report_after_new_buy(self) -> None:
        fake = self.fake_state()
        fake.config["trade_plan"]["positions"]["600001"]["trade_history"].append({
            "side": "buy", "bucket": "core", "lots": 1, "price": 11.5,
            "fee": 5, "reported_at": "2026-09-12 10:00:00",
        })
        fake.series["600001"]["1d"].append({"timestamp": "2026-09-12", "close": 12})
        fake.series["600002"]["1d"].append({"timestamp": "2026-09-12", "close": 22})
        with patch.object(runner, "state", fake):
            report = runner._build_daily_portfolio_pnl(fake.config, "2026-09-12")

        self.assertEqual([row["symbol"] for row in report["rows"]], ["600001", "600002"])
        self.assertEqual(report["rows"][0]["lots"], 1)
        self.assertFalse(report["rows"][0]["exited_today"])
        self.assertEqual(report["ledger_cumulative_pnl"], 525.0)
        self.assertIn("持仓盈亏", runner._format_daily_portfolio_pnl(report))


if __name__ == "__main__":
    unittest.main()
