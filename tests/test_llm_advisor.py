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
