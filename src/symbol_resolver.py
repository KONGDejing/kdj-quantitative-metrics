"""Resolve a user-entered A-share/index name to the canonical six-digit code."""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

import requests


SINA_SUGGEST_URL = "https://suggest3.sinajs.cn/suggest/type=&key={query}"
_SUPPORTED_MARKET_SYMBOL = re.compile(r"^(?:sh|sz|bj)(\d{6})$", re.IGNORECASE)
_CODE = re.compile(r"^\d{6}$")


def _normalized_name(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _local_symbols(config: dict[str, Any]) -> list[dict[str, str]]:
    """Collect known code/name pairs without turning the candidate memo into a watchlist."""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def append(code: object, name: object) -> None:
        normalized_code = str(code or "").strip()
        normalized_name = str(name or "").strip()
        key = (normalized_code, normalized_name)
        if not _CODE.fullmatch(normalized_code) or not normalized_name or key in seen:
            return
        seen.add(key)
        result.append({"code": normalized_code, "name": normalized_name})

    for symbol in config.get("symbols") or []:
        append(symbol.get("code"), symbol.get("name"))
    for code, rule in (config.get("price_alerts") or {}).items():
        append(code, (rule or {}).get("name"))

    try:
        from .candidate_digest import load_candidate_prices

        for candidate in load_candidate_prices():
            append(candidate.get("code"), candidate.get("name"))
    except (OSError, ValueError):
        # Name lookup still has the remote source when the optional memo is absent.
        pass
    return result


def _parse_sina_suggestions(text: str) -> list[dict[str, str]]:
    match = re.search(r'="(.*)"\s*;?\s*$', str(text or ""), re.DOTALL)
    if not match:
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_item in match.group(1).split(";"):
        fields = raw_item.split(",")
        if len(fields) < 4:
            continue
        name = fields[0].strip()
        code = fields[2].strip()
        market_match = _SUPPORTED_MARKET_SYMBOL.fullmatch(fields[3].strip())
        if not name or not _CODE.fullmatch(code) or not market_match or market_match.group(1) != code:
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "name": name})
    return result


def _fetch_sina_suggestions(query: str) -> list[dict[str, str]]:
    response = requests.get(
        SINA_SUGGEST_URL.format(query=requests.utils.quote(query)),
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        timeout=10,
    )
    response.raise_for_status()
    return _parse_sina_suggestions(response.content.decode("gb18030", errors="replace"))


def resolve_symbol_input(
    code: object,
    name: object,
    config: dict[str, Any],
    *,
    remote_lookup: Optional[Callable[[str], list[dict[str, str]]]] = None,
) -> dict[str, str]:
    """Accept a code or an exact stock name and return a canonical pair.

    Exact name matching is intentional: silently selecting the first fuzzy result can
    make the monitor track the wrong security.
    """
    raw_code = str(code or "").strip()
    raw_name = str(name or "").strip()
    local = _local_symbols(config)

    if _CODE.fullmatch(raw_code):
        resolved_name = raw_name
        if not resolved_name:
            known = next((item for item in local if item["code"] == raw_code), None)
            resolved_name = str((known or {}).get("name") or raw_code)
        return {"code": raw_code, "name": resolved_name}

    if raw_code and raw_name and _normalized_name(raw_code) != _normalized_name(raw_name):
        raise ValueError("代码框不是6位代码；按名称添加时请只输入一个股票名称")

    query = raw_code or raw_name
    if not query:
        raise ValueError("请输入6位股票代码或完整股票名称")

    exact_local = [item for item in local if _normalized_name(item["name"]) == _normalized_name(query)]
    if len(exact_local) == 1:
        return exact_local[0]
    if len(exact_local) > 1:
        options = "、".join(f"{item['name']}({item['code']})" for item in exact_local)
        raise ValueError(f"股票名称不唯一，请改用代码：{options}")

    lookup = remote_lookup or _fetch_sina_suggestions
    try:
        suggestions = lookup(query)
    except (requests.RequestException, UnicodeError, ValueError) as exc:
        raise ValueError("股票名称查询暂时不可用，请稍后重试或直接输入6位代码") from exc

    exact = [item for item in suggestions if _normalized_name(item["name"]) == _normalized_name(query)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        options = "、".join(f"{item['name']}({item['code']})" for item in exact)
        raise ValueError(f"股票名称不唯一，请改用代码：{options}")

    nearby = "、".join(f"{item['name']}({item['code']})" for item in suggestions[:5])
    if nearby:
        raise ValueError(f"未找到完全匹配的股票名称；可能是：{nearby}")
    raise ValueError("未找到该股票名称，请核对名称或直接输入6位代码")
