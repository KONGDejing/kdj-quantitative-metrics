"""Claude API client for generating trading advice."""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import requests


def _load_settings() -> dict[str, Any]:
    """Load settings from settings.json_axera_k3."""
    # Try multiple possible locations
    possible_paths = [
        # 项目根目录的上一级（workspace/kdj/）
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "settings.json_axera_k3"),
        # 项目根目录
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json_axera_k3"),
        # 绝对路径
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

    Args:
        symbol_name: Stock name (e.g., "中航光电")
        symbol_code: Stock code (e.g., "002179")
        daily_data: Latest daily kline data with KDJ
        position: Current position from config
        strategy_context: User strategy description from memory
        trade_history: Recent trade history

    Returns:
        Generated advice text, or None if API call fails
    """
    api_key, base_url, model = _get_api_config()
    if not api_key:
        return None

    # Build prompt
    prompt = _build_prompt(symbol_name, symbol_code, daily_data, position, strategy_context, trade_history)

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "model": model,
        "max_tokens": 2000,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    try:
        response = requests.post(
            f"{base_url}/v1/messages",
            headers=headers,
            json=payload,
            timeout=120,  # 延长超时，kimi-k3响应较慢
        )
        response.raise_for_status()
        result = response.json()
        return result["content"][0]["text"]
    except Exception as exc:
        from .logger import app_logger
        app_logger.warning("Claude API call failed: %s", exc)
        return None


def _get_system_prompt() -> str:
    """System prompt for trading advice generation."""
    return """例子：
用户：中航光电，收盘33.10，K=19，D=41，持仓8手成本33.95
助手：次日T+1操作指引
1）低开/下跌：不挂低吸买单，等32.27以下或K金叉再补
2）高开/冲高：涨3%到34.09卖1手T；涨5%到34.76再卖1手；不足3%持有不动
3）卖出后买回：回落2%必须买回，保持底仓只增不减
4）不做：不为了做T卖飞底仓
5）T+1：今天买入的明天才能卖
底仓原则：8/20手扩仓中，只增不减等41

请模仿以上格式，基于用户提供的最新数据生成建议。"""


def _build_prompt(
    symbol_name: str,
    symbol_code: str,
    daily_data: dict[str, Any],
    position: dict[str, Any],
    strategy_context: str,
    trade_history: list[dict[str, Any]],
) -> str:
    """Build user prompt with all context data."""
    # 使用简洁的few-shot格式，避免kimi-k3因prompt过长而耗尽reasoning tokens
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
