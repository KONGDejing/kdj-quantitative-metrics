from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src import candidate_digest, runner


class CandidateDigestTests(unittest.TestCase):
    def test_loads_separate_memo_without_config_watchlist(self) -> None:
        rows = candidate_digest.load_candidate_prices()
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[0]["code"], "601138")
        self.assertEqual(rows[0]["reference_price"], Decimal("60.50"))
        tiantong = next(row for row in rows if row["code"] == "600330")
        self.assertEqual(tiantong["reference_price"], Decimal("26.00"))

    def test_duplicate_or_invalid_memo_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memo.md"
            path.write_text(
                "| 股票 | 代码 | 单手买入参考价（元/股） | 考虑 |\n"
                "| --- | --- | ---: | --- |\n"
                "| 甲 | 600001 | 10.00 | - |\n"
                "| 乙 | 600001 | 11.00 | - |\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                candidate_digest.load_candidate_prices(path)

    def test_stale_primary_uses_fresh_fallback(self) -> None:
        stale = pd.DataFrame([{"date": "2026-09-17", "close": 12, "low": 11, "high": 13}])
        fresh = pd.DataFrame([{"date": "2026-09-18", "close": 12.5, "low": 12, "high": 13}])
        row = candidate_digest._fresh_daily(
            "600001", "2026-09-18",
            primary_fetch=lambda _code: stale,
            fallback_fetch=lambda _code, _timeframe: fresh,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "multi_source_daily")

    def test_any_stale_candidate_blocks_entire_message(self) -> None:
        def fetch(code: str, day: str) -> pd.Series | None:
            if code == "601208":
                return None
            return pd.Series({"date": day, "close": 100, "low": 99, "high": 101})

        self.assertIsNone(candidate_digest.build_candidate_digest("2026-09-18", daily_fetch=fetch))

    def test_stale_or_invalid_row_from_fetcher_blocks_entire_message(self) -> None:
        stale = lambda _code, _day: pd.Series({
            "date": "2026-09-17", "close": 100, "low": 99, "high": 101,
        })
        invalid = lambda _code, day: pd.Series({
            "date": day, "close": 100, "low": 99, "high": 98,
        })
        self.assertIsNone(candidate_digest.build_candidate_digest("2026-09-18", daily_fetch=stale))
        self.assertIsNone(candidate_digest.build_candidate_digest("2026-09-18", daily_fetch=invalid))

    def test_format_marks_price_touch_without_claiming_trade(self) -> None:
        report = candidate_digest.build_candidate_digest(
            "2026-09-18",
            daily_fetch=lambda _code, day: pd.Series(
                {"date": day, "close": 70, "low": 25, "high": 72}
            ),
        )
        self.assertIsNotNone(report)
        content = candidate_digest.format_candidate_digest(report)
        self.assertIn("正式收盘日期：2026-09-18", content)
        self.assertIn("川环科技(300547)", content)
        self.assertIn("盘中到价，核对成交并观察是否止跌", content)
        self.assertIn("不自动改价、添加监控或下单", content)

    def test_weekend_or_before_close_never_sends(self) -> None:
        fake = SimpleNamespace(config={})
        with patch.object(runner, "state", fake), patch.object(
            runner, "build_candidate_digest"
        ) as build:
            runner._send_candidate_price_digest(now=datetime(2026, 9, 19, 15, 30))
            runner._send_candidate_price_digest(now=datetime(2026, 9, 18, 15, 14))
        build.assert_not_called()

    def test_success_uses_pushplus_and_marks_daily_channel(self) -> None:
        fake = SimpleNamespace(config={"alert": {"channels": ["email", "pushplus"]}})
        report = {"day": "2026-09-18", "rows": [{
            "name": "甲", "code": "600001", "reference_price": Decimal("10"),
            "close": Decimal("12"), "low": Decimal("11"),
            "gap_percent": Decimal("16.67"), "status": "未到价，继续等待",
        }]}
        with patch.object(runner, "state", fake), patch.object(
            runner, "build_candidate_digest", return_value=report
        ), patch.object(runner, "task_channel_complete", return_value=False), patch.object(
            runner, "mark_task_channel"
        ) as mark, patch("src.notifier.send_pushplus", return_value=True) as send:
            runner._send_candidate_price_digest(now=datetime(2026, 9, 18, 15, 30))

        send.assert_called_once()
        self.assertIn("候选股票买价每日复核 2026-09-18", send.call_args.args)
        mark.assert_called_once_with(
            "candidate_price_digest", "2026-09-18", "pushplus", True, detail=None
        )

    def test_completed_task_does_not_resend(self) -> None:
        fake = SimpleNamespace(config={})
        with patch.object(runner, "state", fake), patch.object(
            runner, "task_channel_complete", return_value=True
        ), patch.object(runner, "build_candidate_digest") as build:
            runner._send_candidate_price_digest(now=datetime(2026, 9, 18, 15, 30))
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
