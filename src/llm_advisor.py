"""Claude API client for generating trading advice."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import requests


# 重试配置
MAX_RETRIES = 3
RETRY_BACKOFF = [30, 60, 90]  # 每次重试的超时时间（秒）


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
) -> Optional[str]:
    """
    Call Claude API to generate next-day trading advice.
    带重试机制，最多尝试3次，超时时间递增。
    """
    api_key, base_url, model = _get_api_config()
    if not api_key:
        return None

    prompt = _build_prompt(symbol_name, symbol_code, daily_data, position, strategy_context, trade_history)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 2000,
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
) -> str:
    """Build user prompt with all context data."""
    lines = [
        "例子：",
        "用户：中航光电，收盘33.10，K=19，D=41，持仓8手成本33.95",
        "助手：次日T+1操作指引",
        "1）低开/下跌：不挂低吸买单，等32.27以下或K金叉再补",
        "2）高开/冲高：涨3%到34.09卖1手T；涨5%到34.76再卖1手；不足3%持有不动",
        "3）卖出后买回：回落2%必须买回，保持底仓只增不减",
        "4）不做：不为了做T卖飞底仓",
        "5）T+1：今天买入的明天才能卖",
        "底仓原则：8/20手扩仓中，只增不减等41",
        "",
        f"用户：{symbol_name}，收盘{daily_data.get('close', 'N/A')}，K={daily_data.get('k', 'N/A')}，D={daily_data.get('d', 'N/A')}，持仓{position.get('base_lots', 0)}手成本{position.get('cost', 'N/A')}",
        "助手：",
    ]

    return "\n".join(lines)
