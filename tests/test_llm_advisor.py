from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src import llm_advisor


VALID_REVIEW = {
    "consistency_check": "主计划与账本一致",
    "main_risks": "盘中波动可能放大",
    "execution_discipline": "只执行确定性主计划",
    "requires_manual_review": False,
}


def advice_args() -> dict:
    return {
        "symbol_name": "中航光电",
        "symbol_code": "002179",
        "daily_data": {"close": 35.5, "k": 80},
        "position": {"ledger": {"core_lots": 9}},
        "strategy_context": "确定性计划是唯一主计划。",
        "trade_history": [],
        "deterministic_plan": {"action": "hold", "max_lots": 0},
        "advisor_config": {"provider_order": ["codex_cli", "axera"]},
    }


class LlmAdvisorTests(unittest.TestCase):
    def test_candidate_prompt_has_no_previous_price_anchor(self) -> None:
        inputs = [{
            "name": "甲", "code": "600001", "held": False, "close": 10.5,
            "today": {"change_percent": -1.2}, "returns_percent": {"5": -3.0},
            "ranges": {"20": {"low": 10.0, "high": 12.0}},
            "recent_daily_bars": [],
        }]
        prompt = llm_advisor._build_candidate_prompt("2026-09-24", inputs)
        self.assertIn("从头计算", prompt)
        self.assertIn("不提供昨日建议价", prompt)
        self.assertNotIn("previous_price", prompt)

    def test_candidate_advice_requires_exact_codes_and_precise_safe_price(self) -> None:
        inputs = [
            {"name": "甲", "code": "600001", "held": False, "close": 10.5},
            {"name": "乙", "code": "000002", "held": True, "close": 20.0},
        ]
        valid = {
            "market_view": "整体偏弱，等待支撑。",
            "candidates": [
                {
                    "code": "600001", "name": "甲", "trend": "weak",
                    "action": "limit_buy", "suggested_price": 9.87, "reason": "等待20日低点",
                },
                {
                    "code": "000002", "name": "乙", "trend": "neutral",
                    "action": "held_no_add", "suggested_price": 18.62, "reason": "已有持仓暂停新增",
                },
            ],
        }
        result = llm_advisor._validate_candidate_advice(valid, inputs)
        self.assertIsNotNone(result)
        self.assertEqual(result["candidates"][0]["suggested_price"], 9.87)

        invalid = json.loads(json.dumps(valid, ensure_ascii=False))
        invalid["candidates"][0]["suggested_price"] = 11.0
        self.assertIsNone(llm_advisor._validate_candidate_advice(invalid, inputs))

        coarse = json.loads(json.dumps(valid, ensure_ascii=False))
        coarse["candidates"][0]["suggested_price"] = 9.80
        self.assertIsNotNone(llm_advisor._validate_candidate_advice(coarse, inputs))

        two_buys = json.loads(json.dumps(valid, ensure_ascii=False))
        two_buys["candidates"][1]["action"] = "limit_buy"
        inputs_without_holding = [dict(inputs[0]), {**inputs[1], "held": False}]
        self.assertIsNone(llm_advisor._validate_candidate_advice(two_buys, inputs_without_holding))

    def test_candidate_codex_success_does_not_use_fallback(self) -> None:
        inputs = [{"name": "甲", "code": "600001", "held": False, "close": 10.5}]
        payload = {
            "market_view": "震荡",
            "candidates": [{
                "code": "600001", "name": "甲", "trend": "neutral",
                "action": "limit_buy", "suggested_price": 9.82, "reason": "接近近期支撑",
            }],
        }
        codex_result = {
            "ok": True, "provider": "codex_cli", "review": payload,
            "latency_ms": 18, "error": None,
        }
        with patch.object(llm_advisor, "_run_codex", return_value=codex_result), patch.object(
            llm_advisor, "_run_axera"
        ) as axera:
            result = llm_advisor.generate_candidate_price_advice(
                "2026-09-24", inputs, {"provider_order": ["codex_cli", "axera"]}
            )
        axera.assert_not_called()
        self.assertEqual(result["provider"], "codex_cli")
        self.assertEqual(result["candidates"][0]["suggested_price"], 9.82)

    def test_prompt_distinguishes_ledger_tactical_holdings_from_reverse_t_quota(self) -> None:
        prompt = llm_advisor._build_prompt(
            "中航光电",
            "002179",
            {"close": 34.9},
            {
                "strategy_mode": "expand_base",
                "t_lots": 1,
                "max_t_lots": 10,
                "t_lots_held": 0,
                "tactical_enabled": False,
                "ledger": {"core_lots": 10, "t_lots": 0, "total_lots": 10},
            },
            "最终事实",
            [],
            {
                "reverse_t": {
                    "enabled": True,
                    "total_position_lots": 10,
                    "core_floor_lots": 8,
                    "quota_lots": 2,
                    "max_lots_per_trade": 2,
                }
            },
        )
        self.assertNotIn("max_t_lots", prompt)
        self.assertNotIn("t_lots_held", prompt)
        self.assertIn('"reverse_t_quota_lots": 2', prompt)
        self.assertIn("核心仓10手、t_lots为0", prompt)
        self.assertIn("二者完全一致", prompt)

    def test_codex_uses_login_cli_proxy_and_structured_output(self) -> None:
        def fake_run(command: list[str], **kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(VALID_REVIEW, ensure_ascii=False), encoding="utf-8")
            self.assertIn("--sandbox", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn("--ephemeral", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertEqual(kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:10809")
            return subprocess.CompletedProcess(command, 0, "", "")

        config = {
            "codex": {
                "executable": "codex",
                "model": "gpt-5.6-sol",
                "https_proxy": "http://127.0.0.1:10809",
                "retries": 1,
            }
        }
        with patch.object(llm_advisor.subprocess, "run", side_effect=fake_run):
            result = llm_advisor._run_codex("test", config)

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "codex_cli")
        self.assertEqual(result["review"], VALID_REVIEW)

    def test_codex_success_does_not_call_axera(self) -> None:
        codex_result = {
            "ok": True, "provider": "codex_cli", "review": VALID_REVIEW,
            "latency_ms": 12, "error": None,
        }
        with patch.object(llm_advisor, "_run_codex", return_value=codex_result), patch.object(
            llm_advisor, "_run_axera"
        ) as axera:
            result = llm_advisor.generate_trading_advice(**advice_args())

        axera.assert_not_called()
        self.assertEqual(result["provider"], "codex_cli")
        self.assertFalse(result["fallback_used"])
        self.assertIn("①一致性检查", result["text"])

    def test_codex_failure_falls_back_to_axera(self) -> None:
        codex_result = {
            "ok": False, "provider": "codex_cli", "review": None,
            "latency_ms": 100, "error": "timeout(60s)",
        }
        axera_result = {
            "ok": True, "provider": "axera", "review": VALID_REVIEW,
            "latency_ms": 20, "error": None,
        }
        with patch.object(llm_advisor, "_run_codex", return_value=codex_result), patch.object(
            llm_advisor, "_run_axera", return_value=axera_result
        ):
            result = llm_advisor.generate_trading_advice(**advice_args())

        self.assertEqual(result["provider"], "axera")
        self.assertTrue(result["fallback_used"])

    def test_invalid_codex_output_is_rejected(self) -> None:
        def fake_run(command: list[str], **_kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text("不是JSON交易建议", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(llm_advisor.subprocess, "run", side_effect=fake_run):
            result = llm_advisor._run_codex("test", {"codex": {"retries": 1}})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_structured_output")

    def test_health_check_reports_healthy_fallback(self) -> None:
        codex_result = {
            "ok": False, "provider": "codex_cli", "review": None,
            "latency_ms": 100, "error": "exit_1",
        }
        axera_result = {
            "ok": True, "provider": "axera", "review": VALID_REVIEW,
            "latency_ms": 20, "error": None,
        }
        with patch.object(llm_advisor, "_run_codex", return_value=codex_result), patch.object(
            llm_advisor, "_run_axera", return_value=axera_result
        ):
            result = llm_advisor.health_check({"provider_order": ["codex_cli", "axera"]})

        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["primary_error"], "exit_1")

    def test_all_providers_failed_returns_none(self) -> None:
        failed = {"ok": False, "review": None, "latency_ms": 1, "error": "down"}
        with patch.object(llm_advisor, "_run_codex", return_value={**failed, "provider": "codex_cli"}), patch.object(
            llm_advisor, "_run_axera", return_value={**failed, "provider": "axera"}
        ):
            result = llm_advisor.generate_trading_advice(**advice_args())

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
