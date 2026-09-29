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


def fake_analysis(_day: str, inputs: list[dict]) -> dict:
    return {
        "market_view": "候选股今日整体偏弱，明日继续保守挂单。",
        "provider": "codex_cli",
        "fallback_used": False,
        "latency_ms": 12,
        "candidates": [
            {
                "code": item["code"],
                "name": item["name"],
                "trend": "weak",
                "action": "held_no_add" if item["held"] else "limit_buy",
                "suggested_price": round(float(item["close"]) * 0.9473, 2),
                "reason": "今日收在日内偏低位置，挂在近期支撑附近等待。",
            }
            for item in inputs
        ],
    }


class CandidateDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        risk = patch.object(runner, "refresh_market_risk", return_value={"block_new_buys": False})
        risk.start()
        self.addCleanup(risk.stop)

    def test_loads_separate_memo_without_config_watchlist(self) -> None:
        rows = candidate_digest.load_candidate_prices()
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[0]["code"], "601138")
        self.assertNotIn("reference_price", rows[0])
        tiantong = next(row for row in rows if row["code"] == "600330")
        self.assertIn("每日价格仍须按当前行情重算", tiantong["note"])

    def test_atr_accounts_for_overnight_gap(self) -> None:
        data = pd.DataFrame([
            {"date": "2026-09-23", "open": 10, "close": 10, "high": 10.1, "low": 9.9},
            {"date": "2026-09-24", "open": 8, "close": 8, "high": 8.1, "low": 7.9},
        ])
        inputs = candidate_digest._candidate_input({"code": "000938", "name": "紫光"}, data, held=False)
        self.assertAlmostEqual(inputs["atr_percent"]["10"], 14.375, delta=0.01)  # mean(0.2, 2.1) / 8

    def test_wait_and_priority_both_remain_reference_only(self) -> None:
        report = candidate_digest.build_candidate_digest(
            "2026-09-24", daily_fetch=lambda _code, day: pd.Series({"date": day, "close": 30, "low": 29, "high": 31}),
            analyzer=fake_analysis, market_risk={"block_new_buys": False},
        )
        report["rows"][0]["action"] = "wait"
        content = candidate_digest.format_candidate_digest(report)
        self.assertIn("仅观察，暂不下单", content)
        self.assertIn("优先观察，仍须明日确认", content)
        self.assertNotIn("可挂单等待", content)
        self.assertNotIn("建议挂单", content)

    def test_duplicate_or_invalid_memo_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memo.md"
            path.write_text(
                "| 股票 | 代码 | 长期观察说明 |\n"
                "| --- | --- | --- |\n"
                "| 甲 | 600001 | - |\n"
                "| 乙 | 600001 | - |\n",
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

        self.assertIsNone(candidate_digest.build_candidate_digest(
            "2026-09-18", daily_fetch=fetch, analyzer=fake_analysis
        ))

    def test_stale_or_invalid_row_from_fetcher_blocks_entire_message(self) -> None:
        stale = lambda _code, _day: pd.Series({
            "date": "2026-09-17", "close": 100, "low": 99, "high": 101,
        })
        invalid = lambda _code, day: pd.Series({
            "date": day, "close": 100, "low": 99, "high": 98,
        })
        self.assertIsNone(candidate_digest.build_candidate_digest(
            "2026-09-18", daily_fetch=stale, analyzer=fake_analysis
        ))
        self.assertIsNone(candidate_digest.build_candidate_digest(
            "2026-09-18", daily_fetch=invalid, analyzer=fake_analysis
        ))

    def test_format_marks_price_touch_without_claiming_trade(self) -> None:
        report = candidate_digest.build_candidate_digest(
            "2026-09-18",
            daily_fetch=lambda _code, day: pd.Series(
                {"date": day, "close": 70, "low": 25, "high": 72}
            ),
            analyzer=fake_analysis,
        )
        self.assertIsNotNone(report)
        content = candidate_digest.format_candidate_digest(report)
        self.assertIn("正式收盘日期：2026-09-18", content)
        self.assertIn("川环科技(300547)", content)
        self.assertIn("参考买价66.31", content)
        self.assertNotIn("明日建议挂单", content)
        self.assertIn("不允许直接预挂买单", content)
        self.assertIn("不沿用昨日建议价", content)
        self.assertIn("不会自动添加监控或下单", content)
        self.assertIn("次日参考买价汇总", content)
        self.assertIn("股票｜当前价格｜参考买价｜执行状态", content)
        self.assertIn("工业富联｜70.00｜66.31", content)

    def test_model_input_contains_current_trend_but_no_previous_price(self) -> None:
        captured: list[dict] = []

        def analyzer(day: str, inputs: list[dict]) -> dict:
            self.assertEqual(day, "2026-09-24")
            captured.extend(inputs)
            return fake_analysis(day, inputs)

        report = candidate_digest.build_candidate_digest(
            "2026-09-24",
            daily_fetch=lambda _code, day: pd.Series(
                {"date": day, "open": 101, "close": 100, "low": 99, "high": 102, "volume": 1000}
            ),
            analyzer=analyzer,
        )
        self.assertIsNotNone(report)
        self.assertEqual(len(captured), 11)
        self.assertIn("returns_percent", captured[0])
        self.assertIn("recent_daily_bars", captured[0])
        self.assertNotIn("previous_price", captured[0])
        self.assertNotIn("reference_price", captured[0])

    def test_held_candidate_is_shown_without_a_new_buy_price(self) -> None:
        report = candidate_digest.build_candidate_digest(
            "2026-09-24",
            daily_fetch=lambda _code, day: pd.Series(
                {"date": day, "close": 30, "low": 29, "high": 31}
            ),
            held_codes={"300547"},
            analyzer=fake_analysis,
        )
        self.assertIsNotNone(report)
        content = candidate_digest.format_candidate_digest(report)
        chuan_line = next(line for line in content.splitlines() if "川环科技(300547)" in line)
        self.assertIn("当前已持仓，暂停新增", chuan_line)
        self.assertIn("以后空仓参考", chuan_line)
        self.assertNotIn("明日建议挂单", chuan_line)
        self.assertIn("川环科技｜30.00｜暂停新增", content)

    def test_weekend_or_before_close_never_sends(self) -> None:
        fake = SimpleNamespace(config={})
        with patch.object(runner, "state", fake), patch.object(
            runner, "build_candidate_digest"
        ) as build:
            runner._send_candidate_price_digest(now=datetime(2026, 9, 19, 15, 30))
            runner._send_candidate_price_digest(now=datetime(2026, 9, 18, 15, 14))
        build.assert_not_called()

    def test_success_uses_pushplus_and_marks_daily_channel(self) -> None:
        fake = SimpleNamespace(config={
            "use_llm_advice": True,
            "alert": {"channels": ["email", "pushplus"]},
            "trade_plan": {"positions": {}},
        })
        report = {
            "day": "2026-09-18", "provider": "codex_cli", "market_view": "偏弱",
            "rows": [{
                "name": "甲", "code": "600001", "suggested_price": Decimal("10.27"),
                "close": Decimal("12"), "change_percent": Decimal("-1.2"),
                "gap_percent": Decimal("14.42"), "trend": "weak", "action": "limit_buy",
                "reason": "等待支撑", "held": False,
            }],
        }
        with patch.object(runner, "state", fake), patch.object(
            runner, "build_candidate_digest", return_value=report
        ), patch.object(runner, "task_channel_complete", return_value=False), patch.object(
            runner, "mark_task_channel"
        ) as mark, patch("src.notifier.send_pushplus", return_value=True) as send:
            runner._send_candidate_price_digest(now=datetime(2026, 9, 18, 15, 30))

        send.assert_called_once()
        self.assertIn("候选股票次日参考买价（等待确认）2026-09-18", send.call_args.args)
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
