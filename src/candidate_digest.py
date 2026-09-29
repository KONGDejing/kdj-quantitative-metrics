"""Build a fresh, LLM-recalculated candidate limit-price digest.

The memo owns only the candidate universe and durable notes, never yesterday's price.
The whole digest fails closed if any candidate lacks today's completed daily bar.
"""

from __future__ import annotations

import re
from math import isfinite
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from .config import BASE_DIR
from .data_provider import _fetch_tencent_daily, safe_fetch_kline
from .kdj import calculate_kdj
from .logger import app_logger


MEMO_PATH = BASE_DIR / "CANDIDATE_LIMIT_PRICES.md"


def load_candidate_prices(path: Path = MEMO_PATH) -> list[dict[str, object]]:
    """Parse the human-owned candidate universe without yesterday-price anchoring."""
    content = path.read_text(encoding="utf-8")
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    in_table = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("| 股票 | 代码 | 长期观察说明 |"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line:
            break
        if re.fullmatch(r"\|[\s:|\-]+\|", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 3:
            raise ValueError(f"invalid candidate memo row: {line}")
        name, code, note = cells
        if not name or not re.fullmatch(r"\d{6}", code) or code in seen:
            raise ValueError(f"invalid or repeated candidate code: {code}")
        seen.add(code)
        candidates.append({
            "name": name,
            "code": code,
            "note": note,
        })
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


def _fresh_history(
    code: str,
    day: str,
    *,
    primary_fetch: Callable[[str], pd.DataFrame] = _fetch_tencent_daily,
    fallback_fetch: Callable[[str, str], Optional[pd.DataFrame]] = safe_fetch_kline,
) -> Optional[pd.DataFrame]:
    """Fetch enough fresh formal bars for the daily candidate-price model."""
    for source, fetch in (
        ("tencent_daily", lambda: primary_fetch(code)),
        ("multi_source_daily", lambda: fallback_fetch(code, "1d")),
    ):
        try:
            data = fetch()
            if data is None or data.empty or "date" not in data.columns:
                continue
            data = data.sort_values("date").copy()
            actual_day = str(data.iloc[-1].get("date") or "")[:10]
            if actual_day != day:
                app_logger.warning(
                    "candidate model waiting: symbol=%s source=%s date=%s expected=%s",
                    code, source, actual_day or "missing", day,
                )
                continue
            for column in ("close", "low", "high"):
                data[column] = pd.to_numeric(data[column], errors="coerce")
            data = data.dropna(subset=["close", "low", "high"])
            if data.empty or str(data.iloc[-1].get("date") or "")[:10] != day:
                continue
            data.attrs["source"] = source
            return data.tail(60).reset_index(drop=True)
        except Exception as exc:
            app_logger.warning("candidate model history failed: symbol=%s source=%s error=%s", code, source, exc)
    return None


def _round_metric(value: Any, digits: int = 2) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if isfinite(number) else None


def _period_return(closes: pd.Series, sessions: int) -> Optional[float]:
    if len(closes) <= sessions:
        return None
    base = float(closes.iloc[-sessions - 1])
    return _round_metric((float(closes.iloc[-1]) / base - 1) * 100) if base > 0 else None


def _candidate_input(
    candidate: dict[str, object],
    history: pd.DataFrame,
    *,
    held: bool,
) -> Optional[dict[str, Any]]:
    data = history.sort_values("date").copy()
    for column in ("open", "close", "low", "high", "volume"):
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["close", "low", "high"])
    if data.empty:
        return None
    if "open" not in data.columns:
        data["open"] = data["close"]
    data["open"] = data["open"].fillna(data["close"])
    calculated = calculate_kdj(data)
    latest = calculated.iloc[-1]
    close = float(latest["close"])
    previous_close = float(calculated.iloc[-2]["close"]) if len(calculated) >= 2 else close
    high = float(latest["high"])
    low = float(latest["low"])
    day_range = high - low
    day_position = (close - low) / day_range * 100 if day_range > 0 else 50.0
    closes = calculated["close"]

    ranges: dict[str, dict[str, Optional[float]]] = {}
    for sessions in (5, 10, 20, 60):
        window = calculated.tail(sessions)
        ranges[str(sessions)] = {
            "low": _round_metric(window["low"].min()),
            "high": _round_metric(window["high"].max()),
        }
    previous_closes = calculated["close"].shift(1)
    true_ranges = pd.concat([
        calculated["high"] - calculated["low"],
        (calculated["high"] - previous_closes).abs(),
        (calculated["low"] - previous_closes).abs(),
    ], axis=1).max(axis=1)
    volume_ratio_5 = None
    volume_ratio_20 = None
    if "volume" in calculated.columns and pd.notna(latest.get("volume")):
        volume = float(latest["volume"])
        prior_volume = calculated["volume"].iloc[:-1].dropna()
        if not prior_volume.empty:
            avg5 = float(prior_volume.tail(5).mean())
            avg20 = float(prior_volume.tail(20).mean())
            volume_ratio_5 = _round_metric(volume / avg5) if avg5 > 0 else None
            volume_ratio_20 = _round_metric(volume / avg20) if avg20 > 0 else None

    bars = []
    for _, bar in calculated.tail(10).iterrows():
        bars.append({
            "date": str(bar.get("date") or "")[:10],
            "open": _round_metric(bar.get("open")),
            "high": _round_metric(bar.get("high")),
            "low": _round_metric(bar.get("low")),
            "close": _round_metric(bar.get("close")),
            "volume": _round_metric(bar.get("volume"), 0),
        })
    return {
        "name": str(candidate["name"]),
        "code": str(candidate["code"]),
        "held": held,
        "durable_note": str(candidate.get("note") or ""),
        "close": _round_metric(close),
        "one_lot_value": _round_metric(close * 100),
        "today": {
            "open": _round_metric(latest["open"]),
            "high": _round_metric(high),
            "low": _round_metric(low),
            "close": _round_metric(close),
            "change_percent": _round_metric((close / previous_close - 1) * 100) if previous_close else None,
            "close_position_percent": _round_metric(day_position),
        },
        "returns_percent": {
            str(sessions): _period_return(closes, sessions) for sessions in (3, 5, 10, 20)
        },
        "ranges": ranges,
        "atr_percent": {
            "5": _round_metric(float(true_ranges.tail(5).mean()) / close * 100),
            "10": _round_metric(float(true_ranges.tail(10).mean()) / close * 100),
        },
        "volume_ratio": {"5": volume_ratio_5, "20": volume_ratio_20},
        "kdj": {
            "k": _round_metric(latest["k"]),
            "d": _round_metric(latest["d"]),
            "j": _round_metric(latest["j"]),
        },
        "recent_daily_bars": bars,
    }


def build_candidate_digest(
    day: str,
    *,
    memo_path: Path = MEMO_PATH,
    daily_fetch: Optional[Callable[[str, str], Optional[pd.Series]]] = None,
    history_fetch: Callable[[str, str], Optional[pd.DataFrame]] = _fresh_history,
    held_codes: Optional[set[str]] = None,
    market_risk: Optional[dict] = None,
    analyzer: Optional[Callable[[str, list[dict[str, Any]]], Optional[dict[str, Any]]]] = None,
) -> Optional[dict[str, object]]:
    try:
        candidates = load_candidate_prices(memo_path)
    except (OSError, ValueError) as exc:
        app_logger.warning("candidate digest memo unavailable: %s", exc)
        return None

    model_inputs: list[dict[str, Any]] = []
    market_rows: dict[str, dict[str, Decimal]] = {}
    held = {str(code) for code in (held_codes or set())}
    for candidate in candidates:
        code = str(candidate["code"])
        if daily_fetch is not None:
            daily = daily_fetch(code, day)
            history = pd.DataFrame([daily]) if daily is not None else None
        else:
            history = history_fetch(code, day)
            daily = history.iloc[-1] if history is not None and not history.empty else None
        if daily is None or history is None:
            return None
        if str(daily.get("date") or "")[:10] != day:
            app_logger.warning("candidate digest waiting: symbol=%s has no %s daily bar", code, day)
            return None
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
        is_held = code in held
        model_input = _candidate_input(candidate, history, held=is_held)
        if model_input is None:
            return None
        model_inputs.append(model_input)
        market_rows[code] = {"close": close, "low": low, "high": high}

    if analyzer is None:
        app_logger.error("candidate digest requires an LLM analyzer; refusing static-price fallback")
        return None
    for item in model_inputs:
        item["market_risk"] = market_risk or {"status": "unavailable", "block_new_buys": True}
    analysis = analyzer(day, model_inputs)
    if analysis is None:
        app_logger.warning("candidate digest LLM analysis unavailable; no message will be sent")
        return None
    recommendations = analysis.get("candidates") or []
    if len(recommendations) != len(model_inputs):
        return None
    rows: list[dict[str, object]] = []
    inputs_by_code = {str(item["code"]): item for item in model_inputs}
    for recommendation in recommendations:
        code = str(recommendation.get("code") or "")
        source = inputs_by_code.get(code)
        daily_values = market_rows.get(code)
        if source is None or daily_values is None:
            return None
        try:
            price = Decimal(str(recommendation["suggested_price"])).quantize(Decimal("0.01"))
        except (InvalidOperation, KeyError, TypeError):
            return None
        close = daily_values["close"]
        if (
            not price.is_finite()
            or price <= 0
            or price > close
            or price < close * Decimal("0.70")
        ):
            return None
        rows.append({
            "name": source["name"],
            "code": code,
            "close": close,
            "change_percent": Decimal(str((source.get("today") or {}).get("change_percent") or 0)),
            "suggested_price": price,
            "gap_percent": (Decimal("1") - price / close) * Decimal("100"),
            "trend": recommendation.get("trend"),
            "action": recommendation.get("action"),
            "reason": recommendation.get("reason"),
            "held": bool(source.get("held")),
        })
    return {
        "day": day,
        "market_risk": market_risk or {"status": "unavailable", "block_new_buys": True},
        "rows": rows,
        "market_view": analysis.get("market_view"),
        "provider": analysis.get("provider"),
        "fallback_used": bool(analysis.get("fallback_used")),
        "latency_ms": analysis.get("latency_ms"),
    }


def format_candidate_digest(report: dict[str, object]) -> str:
    lines = [
        "候选股票次日参考买价｜AI每日重算",
        f"正式收盘日期：{report['day']}",
        f"模型：{'Codex' if report.get('provider') == 'codex_cli' else 'Axera备用'}",
        f"今日整体判断：{report.get('market_view') or '无'}",
        "以下价格由今日行情重新分析，不沿用昨日建议价；仅供观察，不允许直接预挂买单。",
        "明日14:45—14:55需通过实时市场检查和止跌确认，收到单独买入信号后才挂单；同日最多一只。",
        "",
    ]
    risk = report.get("market_risk") or {}
    if risk.get("block_new_buys", True):
        lines.insert(5, "风险状态：暂停新增；" + "；".join(risk.get("reasons") or ["市场风险尚未核验"]) + "。下一交易日重新检查。")
    trend_labels = {
        "weak": "偏弱",
        "neutral": "震荡",
        "strong": "偏强",
        "high_volatility": "高波动",
    }
    for row in report["rows"]:
        if row.get("held"):
            lines.append(
                f"{row['name']}({row['code']})｜收{row['close']:.2f}（今日{row['change_percent']:+.2f}%）｜"
                f"趋势{trend_labels.get(row.get('trend'), '待判断')}｜当前已持仓，暂停新增｜"
                f"以后空仓参考{row['suggested_price']:.2f}｜{row['reason']}"
            )
            continue
        action_text = "优先观察，仍须明日确认" if row.get("action") == "limit_buy" else "仅观察，暂不下单"
        if risk.get("block_new_buys", True):
            action_text = "暂停新增，仅保留参考价"
        lines.append(
            f"{row['name']}({row['code']})｜收{row['close']:.2f}（今日{row['change_percent']:+.2f}%）｜"
            f"趋势{trend_labels.get(row.get('trend'), '待判断')}｜参考买价{row['suggested_price']:.2f}｜"
            f"距收盘-{row['gap_percent']:.1f}%｜{action_text}｜{row['reason']}"
        )
    lines.extend([
        "",
        "每天都用当天正式日线和近期结构从头分析；模型失败或数据不全时不发送旧价格充数。",
        "系统不会自动添加监控或下单；候选表中的股票未必有盘中监控，不收到确认信号就不执行。",
        "同日最多新买一只，观察仓总额不超过2万元，不为成交抬价；跌破支撑需重新评估。",
        "",
        "次日参考买价汇总（未获买入许可）",
        "股票｜当前价格｜参考买价｜执行状态",
    ])
    for row in report["rows"]:
        suggested = "暂停新增" if row.get("held") else f"{row['suggested_price']:.2f}"
        status = "暂停新增" if row.get("held") or risk.get("block_new_buys", True) else "等待明日信号"
        lines.append(f"{row['name']}｜{row['close']:.2f}｜{suggested}｜{status}")
    return "\n".join(lines)
