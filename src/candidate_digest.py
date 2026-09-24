"""Read the separate candidate-price memo and build a fresh daily digest.

These prices are discussion notes, not watchlist entries or executable orders.
The whole digest fails closed if any candidate lacks today's completed daily bar.
"""

from __future__ import annotations

import re
from math import isfinite
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from .config import BASE_DIR
from .data_provider import _fetch_tencent_daily, safe_fetch_kline
from .logger import app_logger


MEMO_PATH = BASE_DIR / "CANDIDATE_LIMIT_PRICES.md"


def load_candidate_prices(path: Path = MEMO_PATH) -> list[dict[str, object]]:
    """Parse the human-owned memo without adding its stocks to system config."""
    content = path.read_text(encoding="utf-8")
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    in_table = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("| 股票 | 代码 |"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line:
            break
        if re.fullmatch(r"\|[\s:|\-]+\|", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4:
            raise ValueError(f"invalid candidate memo row: {line}")
        name, code, raw_price, _note = cells
        if not name or not re.fullmatch(r"\d{6}", code) or code in seen:
            raise ValueError(f"invalid or repeated candidate code: {code}")
        try:
            price = Decimal(raw_price)
        except InvalidOperation as exc:
            raise ValueError(f"invalid candidate price: {code}") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError(f"invalid candidate price: {code}")
        seen.add(code)
        candidates.append({"name": name, "code": code, "reference_price": price})
    if not candidates:
        raise ValueError("candidate memo has no price rows")
    return candidates


def _fresh_daily(
    code: str,
    day: str,
    *,
    primary_fetch: Callable[[str], pd.DataFrame] = _fetch_tencent_daily,
    fallback_fetch: Callable[[str, str], Optional[pd.DataFrame]] = safe_fetch_kline,
) -> Optional[pd.Series]:
    """Prefer Tencent's completed daily bar; try the normal multi-source path."""
    for source, fetch in (
        ("tencent_daily", lambda: primary_fetch(code)),
        ("multi_source_daily", lambda: fallback_fetch(code, "1d")),
    ):
        try:
            data = fetch()
            if data is None or data.empty or "date" not in data.columns:
                continue
            data = data.sort_values("date")
            row = data.iloc[-1]
            actual_day = str(row.get("date") or "")[:10]
            if actual_day != day:
                app_logger.warning(
                    "candidate digest waiting: symbol=%s source=%s date=%s expected=%s",
                    code, source, actual_day or "missing", day,
                )
                continue
            close = float(row["close"])
            low = float(row["low"])
            high = float(row["high"])
            if not all(isfinite(value) for value in (close, low, high)) or not (0 < low <= close <= high):
                app_logger.warning("candidate digest invalid daily bar: symbol=%s source=%s", code, source)
                continue
            row = row.copy()
            row["source"] = source
            return row
        except Exception as exc:
            app_logger.warning("candidate digest daily fetch failed: symbol=%s source=%s error=%s", code, source, exc)
    return None


def build_candidate_digest(
    day: str,
    *,
    memo_path: Path = MEMO_PATH,
    daily_fetch: Callable[[str, str], Optional[pd.Series]] = _fresh_daily,
) -> Optional[dict[str, object]]:
    try:
        candidates = load_candidate_prices(memo_path)
    except (OSError, ValueError) as exc:
        app_logger.warning("candidate digest memo unavailable: %s", exc)
        return None

    rows: list[dict[str, object]] = []
    for candidate in candidates:
        code = str(candidate["code"])
        daily = daily_fetch(code, day)
        if daily is None:
            return None
        if str(daily.get("date") or "")[:10] != day:
            app_logger.warning("candidate digest waiting: symbol=%s has no %s daily bar", code, day)
            return None
        price = candidate["reference_price"]
        try:
            close = Decimal(str(daily["close"]))
            low = Decimal(str(daily["low"]))
            high = Decimal(str(daily["high"]))
        except (InvalidOperation, KeyError, TypeError):
            app_logger.warning("candidate digest invalid daily bar: symbol=%s", code)
            return None
        if not all(value.is_finite() for value in (close, low, high)) or not (0 < low <= close <= high):
            app_logger.warning("candidate digest invalid daily bar: symbol=%s", code)
            return None
        if close < price:
            status = "收盘低于参考价，需重新评估，非自动买入"
        elif low <= price:
            status = "盘中到价，核对成交并观察是否止跌"
        else:
            status = "未到价，继续等待"
        rows.append({
            "name": candidate["name"],
            "code": code,
            "reference_price": price,
            "close": close,
            "low": low,
            "gap_percent": (Decimal("1") - price / close) * Decimal("100"),
            "status": status,
        })
    return {"day": day, "rows": rows}


def format_candidate_digest(report: dict[str, object]) -> str:
    lines = [
        "候选股票买价每日复核",
        f"正式收盘日期：{report['day']}",
        "以下是备忘价与当天正式收盘的对照，不是已挂委托或买入指令。",
        "",
    ]
    for row in report["rows"]:
        gap = row["gap_percent"]
        distance_text = (
            f"再跌{gap:.1f}%到参考价"
            if gap >= 0
            else f"参考价高于收盘{-gap:.1f}%"
        )
        lines.append(
            f"{row['name']}({row['code']})｜收{row['close']:.2f}｜"
            f"参考买价{row['reference_price']:.2f}｜"
            f"{distance_text}｜{row['status']}"
        )
    lines.extend([
        "",
        "参考价来自独立备忘录；每天复核行情，但不自动改价、添加监控或下单。",
        "若需调整具体价格，讨论确认后更新备忘录；下个交易日自动使用新价。",
    ])
    return "\n".join(lines)
