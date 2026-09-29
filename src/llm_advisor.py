"""LLM read-only review with Codex primary and Axera fallback."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Optional

import requests


AXERA_MAX_RETRIES = 3
AXERA_RETRY_TIMEOUTS = [60, 90, 120]
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_REASONING_EFFORT = "medium"

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "consistency_check": {"type": "string", "minLength": 1},
        "main_risks": {"type": "string", "minLength": 1},
        "execution_discipline": {"type": "string", "minLength": 1},
        "requires_manual_review": {"type": "boolean"},
    },
    "required": [
        "consistency_check",
        "main_risks",
        "execution_discipline",
        "requires_manual_review",
    ],
    "additionalProperties": False,
}

CANDIDATE_PRICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "market_view": {"type": "string", "minLength": 1},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "trend": {
                        "type": "string",
                        "enum": ["weak", "neutral", "strong", "high_volatility"],
                    },
                    "action": {
                        "type": "string",
                        "enum": ["limit_buy", "wait", "held_no_add"],
                    },
                    "suggested_price": {"type": "number", "exclusiveMinimum": 0},
                    "reason": {"type": "string", "minLength": 1},
                },
                "required": ["code", "name", "trend", "action", "suggested_price", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_view", "candidates"],
    "additionalProperties": False,
}


def _load_settings() -> dict[str, Any]:
    """Load the existing private Axera settings file."""
    possible_paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "settings.json_axera_k3"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json_axera_k3"),
        "/data/kongdejing/workspace/kdj/settings.json_axera_k3",
    ]
    for settings_path in possible_paths:
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
    return {}


def _get_api_config() -> tuple[str, str, str]:
    """Get the Axera API key, base URL and model."""
    settings = _load_settings()
    env = settings.get("env", {})
    api_key = env.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    base_url = env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model = env.get("ANTHROPIC_MODEL") or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
    return api_key, base_url.rstrip("/"), model


def _provider_order(advisor_config: Optional[dict[str, Any]]) -> list[str]:
    configured = (advisor_config or {}).get("provider_order", ["codex_cli", "axera"])
    supported = {"codex_cli", "axera"}
    result = [str(provider) for provider in configured if str(provider) in supported]
    return result or ["codex_cli", "axera"]


def _validate_review(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != set(REVIEW_SCHEMA["required"]):
        return None
    for field in ("consistency_check", "main_risks", "execution_discipline"):
        text = value.get(field)
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > 1200:
            return None
        value[field] = text.strip()
    if not isinstance(value.get("requires_manual_review"), bool):
        return None
    return value


def _parse_review(raw: str) -> Optional[dict[str, Any]]:
    return _parse_structured(raw, _validate_review)


def _parse_structured(
    raw: str,
    validator: Callable[[Any], Optional[dict[str, Any]]],
) -> Optional[dict[str, Any]]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return validator(json.loads(text))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _format_review(review: dict[str, Any]) -> str:
    manual = "是" if review["requires_manual_review"] else "否"
    return "\n".join([
        f"①一致性检查：{review['consistency_check']}",
        f"②主要风险：{review['main_risks']}",
        f"③执行纪律提醒：{review['execution_discipline']}",
        f"需要人工复核：{manual}",
    ])


def _codex_config(advisor_config: Optional[dict[str, Any]]) -> dict[str, Any]:
    return ((advisor_config or {}).get("codex") or {})


def _run_codex(
    prompt: str,
    advisor_config: Optional[dict[str, Any]],
    *,
    health: bool = False,
    schema: Optional[dict[str, Any]] = None,
    validator: Optional[Callable[[Any], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    config = _codex_config(advisor_config)
    executable = str(config.get("executable") or shutil.which("codex") or "codex")
    model = str(config.get("model") or DEFAULT_CODEX_MODEL)
    effort = str(config.get("reasoning_effort") or DEFAULT_CODEX_REASONING_EFFORT)
    timeout = int(config.get("health_timeout_seconds" if health else "timeout_seconds", 60 if health else 180))
    retries = max(1, int(config.get("health_retries" if health else "retries", 1 if health else 2)))
    codex_env = os.environ.copy()
    if config.get("https_proxy"):
        codex_env["HTTPS_PROXY"] = str(config["https_proxy"])
    last_error = "unknown"
    started = time.monotonic()

    for attempt in range(retries):
        try:
            with tempfile.TemporaryDirectory(prefix="kdj-codex-review-") as tmp_dir:
                tmp_path = Path(tmp_dir)
                schema_path = tmp_path / "review-schema.json"
                output_path = tmp_path / "review.json"
                schema_path.write_text(
                    json.dumps(schema or REVIEW_SCHEMA, ensure_ascii=False), encoding="utf-8"
                )
                command = [
                    executable,
                    "exec",
                    "--ephemeral",
                    "--sandbox", "read-only",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--skip-git-repo-check",
                    "--model", model,
                    "-c", f'model_reasoning_effort="{effort}"',
                    "-C", tmp_dir,
                    "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path),
                    "-",
                ]
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                    env=codex_env,
                )
                if completed.returncode != 0:
                    last_error = f"exit_{completed.returncode}"
                elif not output_path.exists():
                    last_error = "missing_output"
                else:
                    review = _parse_structured(
                        output_path.read_text(encoding="utf-8"), validator or _validate_review
                    )
                    if review:
                        return {
                            "ok": True,
                            "provider": "codex_cli",
                            "review": review,
                            "latency_ms": int((time.monotonic() - started) * 1000),
                            "error": None,
                        }
                    last_error = "invalid_structured_output"
        except subprocess.TimeoutExpired:
            last_error = f"timeout({timeout}s)"
        except FileNotFoundError:
            last_error = "codex_not_found"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        _log_retry("Codex", attempt, retries, last_error)

    return {
        "ok": False,
        "provider": "codex_cli",
        "review": None,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": last_error,
    }


def _run_axera(
    prompt: str,
    *,
    health: bool = False,
    validator: Optional[Callable[[Any], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    api_key, base_url, model = _get_api_config()
    if not api_key:
        return {"ok": False, "provider": "axera", "review": None, "latency_ms": 0, "error": "no_api_key"}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 100 if health else 4000,
        "messages": [{"role": "user", "content": prompt}],
    }
    timeouts = [10] if health else AXERA_RETRY_TIMEOUTS
    started = time.monotonic()
    last_error = "unknown"

    for attempt, timeout in enumerate(timeouts):
        try:
            response = requests.post(
                f"{base_url}/v1/messages",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            result = response.json()
            review = _parse_structured(
                result["content"][0]["text"], validator or _validate_review
            )
            if review:
                return {
                    "ok": True,
                    "provider": "axera",
                    "review": review,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "error": None,
                }
            last_error = "invalid_structured_output"
        except requests.exceptions.Timeout:
            last_error = f"timeout({timeout}s)"
        except requests.exceptions.ConnectionError:
            last_error = "connection_error"
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code
            last_error = f"http_{status}"
            if 400 <= status < 500:
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        _log_retry("Axera", attempt, len(timeouts), last_error)
        if not health and attempt < len(timeouts) - 1:
            time.sleep(2 ** attempt)

    return {
        "ok": False,
        "provider": "axera",
        "review": None,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": last_error,
    }


def generate_trading_advice(
    symbol_name: str,
    symbol_code: str,
    daily_data: dict[str, Any],
    position: dict[str, Any],
    strategy_context: str,
    trade_history: list[dict[str, Any]],
    deterministic_plan: dict[str, Any],
    advisor_config: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Review the deterministic plan using Codex first, then Axera."""
    prompt = _build_prompt(
        symbol_name, symbol_code, daily_data, position, strategy_context,
        trade_history, deterministic_plan,
    )
    errors: list[str] = []
    for provider in _provider_order(advisor_config):
        result = _run_codex(prompt, advisor_config) if provider == "codex_cli" else _run_axera(prompt)
        if result["ok"]:
            return {
                "text": _format_review(result["review"]),
                "provider": result["provider"],
                "fallback_used": provider != _provider_order(advisor_config)[0],
                "latency_ms": result["latency_ms"],
            }
        errors.append(f"{provider}={result['error']}")
        _log_error(f"{provider} unavailable: {result['error']}")
    _log_error("all providers unavailable: " + "; ".join(errors))
    return None


def _validate_candidate_advice(
    value: Any,
    inputs: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"market_view", "candidates"}:
        return None
    market_view = value.get("market_view")
    rows = value.get("candidates")
    if not isinstance(market_view, str) or not market_view.strip() or len(market_view.strip()) > 500:
        return None
    if not isinstance(rows, list) or len(rows) != len(inputs):
        return None
    expected = {str(item["code"]): item for item in inputs}
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_trends = {"weak", "neutral", "strong", "high_volatility"}
    allowed_actions = {"limit_buy", "wait", "held_no_add"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "code", "name", "trend", "action", "suggested_price", "reason"
        }:
            return None
        code = str(row.get("code") or "")
        source = expected.get(code)
        if source is None or code in seen or str(row.get("name")) != str(source["name"]):
            return None
        trend = str(row.get("trend") or "")
        action = str(row.get("action") or "")
        reason = str(row.get("reason") or "").strip()
        try:
            price = float(row.get("suggested_price"))
            close = float(source["close"])
        except (TypeError, ValueError, KeyError):
            return None
        if (
            trend not in allowed_trends
            or action not in allowed_actions
            or not reason
            or len(reason) > 300
            or not isfinite(price)
            or price <= 0
            or price > close
            or price < close * 0.70
        ):
            return None
        held = bool(source.get("held"))
        if (held and action != "held_no_add") or (not held and action == "held_no_add"):
            return None
        if (source.get("market_risk") or {}).get("block_new_buys") and action == "limit_buy":
            return None
        seen.add(code)
        validated.append({
            "code": code,
            "name": str(row["name"]),
            "trend": trend,
            "action": action,
            "suggested_price": round(price, 2),
            "reason": reason,
        })
    if seen != set(expected):
        return None
    if sum(1 for row in validated if row["action"] == "limit_buy") > 1:
        return None
    ordered = {row["code"]: row for row in validated}
    return {
        "market_view": market_view.strip(),
        "candidates": [ordered[str(item["code"])] for item in inputs],
    }


def _build_candidate_prompt(day: str, inputs: list[dict[str, Any]]) -> str:
    payload = json.dumps(inputs, ensure_ascii=False, separators=(",", ":"))
    return "\n".join([
        "你是A股候选股票次日限价单分析器。不要调用工具、读取文件或访问网络，只能使用提示中的正式收盘数据。",
        f"分析日为{day}，目标是逐只从头计算下一交易日的建议挂单买价，而不是沿用或微调昨天的价格。",
        "系统故意不提供昨日建议价；你不得猜测昨日价格，也不得为了让结果平滑而锚定历史建议。",
        "综合每只股票当天K线强弱、收盘位置、3/5/10/20日收益、近期高低点、ATR波动、量能、KDJ和最近10根日线独立判断。",
        "suggested_price必须是保守的限价买入参考价：不得高于当日收盘，也不得低于收盘价的70%。",
        "价格依据近期支撑位及真实ATR（含跳空）计算到0.01元；不得整体固定下调0.5或1元。计算恰好得到整角或整元也应保留，不为制造精确感改动一分钱。",
        "强势上涨或位置偏高时不能追涨，应给出更深的等待价并可将action设为wait；弱势下跌时也不能仅因下跌就立即抄底，应考虑支撑和止跌空间。",
        "held=true的股票action必须为held_no_add；其他股票不得使用held_no_add。每只股票都仍需给出suggested_price，作为以后空仓时的观察参考，但持仓股不会向用户展示新增指令。",
        "用户纪律：候选观察仓总额不超过2万元、通常一只一手、同一天最多新买一只、不为成交而抬价。",
        "因此全部非持仓候选中action=limit_buy最多只能有一只；其他股票即使给出观察价格，action也必须设为wait。",
        "所有价格仅为下一交易日观察参考，limit_buy只代表优先观察，绝非已获买入许可；不能建议预挂买单。",
        "真正执行还须当日14:45—14:55止跌确认、实时市场风险通过以及当日单只名额和资金检查。",
        "market_risk.block_new_buys=true时非持仓股票一律action=wait。必须指出多个科技股可能同时下跌，不把一只一手误认为充分分散风险。",
        "未提供新闻及公司公告时须说明事件风险未核验，不得捏造已核验的政策、消息原因或胜率。不得将支撑价承诺为最低价。",
        "reason必须引用当天或近期数据说明为什么是这个价，使用简短中文；market_view概括候选股今天的整体趋势和明日挂单态度。",
        "必须完整覆盖输入中的全部股票，每个代码恰好一次并保持输入顺序。只输出符合JSON Schema的对象，不要Markdown。",
        f"输入数据：{payload}",
    ])


def generate_candidate_price_advice(
    day: str,
    inputs: list[dict[str, Any]],
    advisor_config: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Recalculate next-session candidate limit prices through the provider chain."""
    if not inputs:
        return None
    prompt = _build_candidate_prompt(day, inputs)
    validator = lambda value: _validate_candidate_advice(value, inputs)
    order = _provider_order(advisor_config)
    errors: list[str] = []
    for index, provider in enumerate(order):
        result = (
            _run_codex(
                prompt,
                advisor_config,
                schema=CANDIDATE_PRICE_SCHEMA,
                validator=validator,
            )
            if provider == "codex_cli"
            else _run_axera(prompt, validator=validator)
        )
        if result["ok"]:
            return {
                **result["review"],
                "provider": result["provider"],
                "fallback_used": index > 0,
                "latency_ms": result["latency_ms"],
            }
        errors.append(f"{provider}={result['error']}")
        _log_error(f"candidate prices: {provider} unavailable: {result['error']}")
    _log_error("candidate prices: all providers unavailable: " + "; ".join(errors))
    return None


def health_check(advisor_config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Check the usable provider chain, accepting a healthy fallback as available."""
    prompt = """你是健康检查程序。不要调用工具。只返回符合给定JSON Schema的对象：
consistency_check填写“健康检查通过”，main_risks填写“无”，execution_discipline填写“无”，requires_manual_review填写false。"""
    errors: dict[str, str] = {}
    total_started = time.monotonic()
    order = _provider_order(advisor_config)
    for index, provider in enumerate(order):
        result = _run_codex(prompt, advisor_config, health=True) if provider == "codex_cli" else _run_axera(prompt, health=True)
        if result["ok"]:
            return {
                "ok": True,
                "provider": provider,
                "fallback_used": index > 0,
                "latency_ms": int((time.monotonic() - total_started) * 1000),
                "error": None,
                "primary_error": errors.get(order[0]),
            }
        errors[provider] = str(result["error"])
    return {
        "ok": False,
        "provider": None,
        "fallback_used": False,
        "latency_ms": int((time.monotonic() - total_started) * 1000),
        "error": "; ".join(f"{name}={error}" for name, error in errors.items()),
        "primary_error": errors.get(order[0]),
    }


def _log_retry(provider: str, attempt: int, max_retries: int, reason: str) -> None:
    from .logger import app_logger
    app_logger.info("%s LLM attempt %d/%d failed: %s", provider, attempt + 1, max_retries, reason)


def _log_error(msg: str) -> None:
    from .logger import app_logger
    app_logger.error("LLM: %s", msg)


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
    ledger = dict(position.get("ledger") or {})
    reverse_t = dict(deterministic_plan.get("reverse_t") or {})
    review_position = {
        "strategy_mode": position.get("strategy_mode"),
        "strategy_budget": position.get("strategy_budget"),
        "actual_ledger": ledger,
        "extra_buy_in_tactical_enabled": bool(position.get("tactical_enabled", False)),
        "reverse_t_operational_allocation": {
            "enabled": bool(reverse_t.get("enabled", False)),
            "total_position_lots": reverse_t.get("total_position_lots", ledger.get("total_lots")),
            "long_term_core_floor_lots": reverse_t.get("core_floor_lots"),
            "reverse_t_quota_lots": reverse_t.get("quota_lots"),
            "max_lots_per_trade": reverse_t.get("max_lots_per_trade"),
            "meaning": "从现有核心老仓中划出的操作额度，不是额外买入型T仓的实际持仓",
        },
    }
    return "\n".join([
        "你是A股交易计划的只读复核工具，不是计划生成器。只能使用本提示提供的数据，不得调用工具、读取文件或访问网络。",
        "程序生成的deterministic_plan是唯一可执行主计划。",
        "不得修改或另行给出action、status、bucket、max_lots、价格区间、T+1数量和取消条件，不得提供替代买卖价或手数。",
        "如果发现矛盾，只能将requires_manual_review设为true并说明矛盾；不能自行修补为另一套交易建议。",
        "如果持仓汇总与逐笔流水冲突，以已确认起始仓位和逐笔流水重算结果为准，并指出需要人工核对。",
        "ledger是程序逐笔重放后的确定性结果；保本成本和T+1可卖手数必须以ledger为准。",
        "术语必须严格区分：ledger.t_lots只表示额外买入、独立持有的T仓；reverse_t_operational_allocation.reverse_t_quota_lots表示从现有核心老仓中划出的反T操作额度。",
        "因此，ledger显示核心仓10手、t_lots为0，同时操作划分为长期底仓至少8手、反T额度2手，二者完全一致，不得报告为冲突，也不得要求人工核对。",
        "必须遵守：当天买入当天不能卖，下一交易日可以卖；不同股票不能混用策略；建议只做提醒，不自动下单。",
        "不要复述任何错误录入或纠正过程，只使用最终确认事实。",
        "输出必须是JSON对象且只有consistency_check、main_risks、execution_discipline、requires_manual_review四个字段。",
        "前三个字段使用简短中文，不重复完整主计划；最后一个字段是布尔值。不要输出Markdown。",
        "",
        f"标的：{symbol_name}({symbol_code})",
        f"最新行情：{json.dumps(daily_data, ensure_ascii=False, sort_keys=True)}",
        f"当前持仓语义化摘要：{json.dumps(review_position, ensure_ascii=False, sort_keys=True)}",
        f"最近成交流水：{json.dumps(recent_history, ensure_ascii=False, sort_keys=True)}",
        f"deterministic_plan：{json.dumps(deterministic_plan, ensure_ascii=False, sort_keys=True)}",
        "策略与最终事实：",
        strategy_context.strip() or "未提供；不得自行推测策略。",
    ])
