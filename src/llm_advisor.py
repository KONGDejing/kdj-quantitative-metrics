"""Claude API client for generating trading advice."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import requests


# 重试配置
MAX_RETRIES = 3
RETRY_BACKOFF = [60, 90, 120]  # 每次重试的超时时间（秒）


def _load_settings() -> dict[str, Any]:
    """Load settings from settings.json_axera_k3."""
    possible_paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "settings.json_axera_k3"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json_axera_k3"),
        "/data/kongdejing/workspace/kdj/settings.json_axera_k3",
    ]

    for settings_path in possible_paths:
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                return json.load(f)

    return {}


def _get_api_config() -> tuple[str, str, str]:
    """Get API key, base URL, and model from settings or environment."""
    settings = _load_settings()
    env = settings.get("env", {})

    api_key = env.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    base_url = env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model = env.get("ANTHROPIC_MODEL") or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

    return api_key, base_url, model


def generate_trading_advice(
    symbol_name: str,
    symbol_code: str,
    daily_data: dict[str, Any],
    position: dict[str, Any],
    strategy_context: str,
    trade_history: list[dict[str, Any]],
    deterministic_plan: dict[str, Any],
) -> Optional[str]:
    """
    Call Claude API to generate next-day trading advice.
    带重试机制，最多尝试3次，超时时间递增。
    """
    api_key, base_url, model = _get_api_config()
    if not api_key:
        return None

    prompt = _build_prompt(
        symbol_name, symbol_code, daily_data, position, strategy_context,
        trade_history, deterministic_plan,
    )
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 4000,  # kimi-k3需要大量token用于reasoning，必须给足
        "messages": [{"role": "user", "content": prompt}],
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        timeout = RETRY_BACKOFF[attempt]
        try:
            response = requests.post(
                f"{base_url}/v1/messages",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            result = response.json()
            return result["content"][0]["text"]
        except requests.exceptions.Timeout as exc:
            last_error = f"timeout({timeout}s)"
            _log_retry(attempt, MAX_RETRIES, f"timeout after {timeout}s")
        except requests.exceptions.ConnectionError as exc:
            last_error = f"connection_error"
            _log_retry(attempt, MAX_RETRIES, "connection error")
        except requests.exceptions.HTTPError as exc:
            last_error = f"http_{exc.response.status_code}"
            # 4xx错误不重试（如认证失败）
            if 400 <= exc.response.status_code < 500:
                _log_error(f"HTTP {exc.response.status_code}, no retry: {exc}")
                return None
            _log_retry(attempt, MAX_RETRIES, f"HTTP {exc.response.status_code}")
        except Exception as exc:
            last_error = f"unknown: {type(exc).__name__}"
            _log_retry(attempt, MAX_RETRIES, str(exc))

        # 重试前等待（指数退避）
        if attempt < MAX_RETRIES - 1:
            time.sleep(2 ** attempt)

    _log_error(f"LLM API failed after {MAX_RETRIES} retries, last error: {last_error}")
    return None


def _log_retry(attempt: int, max_retries: int, reason: str) -> None:
    from .logger import app_logger
    app_logger.info("LLM API retry %d/%d: %s", attempt + 1, max_retries, reason)


def _log_error(msg: str) -> None:
    from .logger import app_logger
    app_logger.error("LLM API: %s", msg)


def health_check() -> dict[str, Any]:
    """
    健康检查：测试LLM API是否可用。
    返回 {'ok': bool, 'latency_ms': int, 'error': str|None}
    """
    api_key, base_url, model = _get_api_config()
    if not api_key:
        return {"ok": False, "latency_ms": 0, "error": "no_api_key"}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    }

    start = time.time()
    try:
        response = requests.post(
            f"{base_url}/v1/messages",
            headers=headers,
            json=payload,
            timeout=10,
        )
        latency = int((time.time() - start) * 1000)
        response.raise_for_status()
        return {"ok": True, "latency_ms": latency, "error": None}
    except Exception as exc:
        latency = int((time.time() - start) * 1000)
        return {"ok": False, "latency_ms": latency, "error": str(exc)}


def _build_prompt(
    symbol_name: str,
    symbol_code: str,
    daily_data: dict[str, Any],
    position: dict[str, Any],
    strategy_context: str,
    trade_history: list[dict[str, Any]],
    deterministic_plan: dict[str, Any],
) -> str:
    """Ask the model to audit, never rewrite, the deterministic plan."""
    recent_history = trade_history[-50:]
    return "\n".join([
        "你是A股交易计划的只读复核工具，不是计划生成器。程序生成的deterministic_plan是唯一可执行主计划。",
        "不得修改或另行给出action、status、bucket、max_lots、价格区间、T+1数量和取消条件，不得提供替代买卖价或手数。",
        "如果发现矛盾，只能标记“需要人工复核”并说明矛盾；不能自行修补为另一套交易建议。",
        "如果持仓汇总与逐笔流水冲突，以已确认起始仓位和逐笔流水重算结果为准，并明确指出需要人工核对。",
        "当前持仓中的ledger字段是程序逐笔重放后的确定性结果；核心仓、T仓、保本成本和T+1可卖手数必须以ledger为准。",
        "必须遵守：当天买入当天不能卖，下一交易日可以卖；不同股票不能混用策略；建议只做提醒，不自动下单。",
        "只输出三小段：①一致性检查；②主要风险；③执行纪律提醒。保持简短，不重复完整主计划。",
        "不要复述任何错误录入或纠正过程，只使用最终确认事实。",
        "",
        f"标的：{symbol_name}({symbol_code})",
        f"最新行情：{json.dumps(daily_data, ensure_ascii=False, sort_keys=True)}",
        f"当前持仓汇总（辅助字段）：{json.dumps(position, ensure_ascii=False, sort_keys=True)}",
        f"最近成交流水：{json.dumps(recent_history, ensure_ascii=False, sort_keys=True)}",
        f"deterministic_plan：{json.dumps(deterministic_plan, ensure_ascii=False, sort_keys=True)}",
        "策略与最终事实：",
        strategy_context.strip() or "未提供；不得自行推测策略。",
    ])
