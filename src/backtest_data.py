"""Fresh, completed-bar inputs for interactive backtests; never update thresholds."""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

from . import data_provider as provider
from .trading_calendar import calendar_status


class BacktestDataUnavailable(RuntimeError):
    pass


def _previous_session(day, config):
    for offset in range(1, 370):
        candidate = day - timedelta(days=offset)
        status = calendar_status(candidate, config)
        if not status["verified"]:
            raise BacktestDataUnavailable("交易日历未核验，无法确认回测最新截止日期")
        if status["is_session"]:
            return candidate
    raise BacktestDataUnavailable("未找到已核验的上一交易日")


def latest_completed_bar(timeframe: str, now: datetime, config: dict | None = None) -> str:
    status = calendar_status(now, config)
    if not status["verified"]:
        raise BacktestDataUnavailable("交易日历未核验，无法确认回测最新截止日期")
    if timeframe == "1d":
        day = now.date() if status["is_session"] and now.time() >= time(15, 1) else _previous_session(now.date(), config)
        return day.isoformat()
    minutes = {"15m": 15, "30m": 30, "60m": 60}[timeframe]
    # Provider publication grace period; never include a forming bar.
    cutoff = now - timedelta(seconds=30)
    if status["is_session"]:
        for start, end in ((time(13), time(15)), (time(9, 30), time(11, 30))):
            opening = datetime.combine(now.date(), start)
            closing = datetime.combine(now.date(), end)
            elapsed = (min(cutoff, closing) - opening).total_seconds()
            bars = int(elapsed // (minutes * 60))
            if bars >= 1:
                return (opening + timedelta(minutes=minutes * bars)).isoformat(sep=" ")
    return datetime.combine(_previous_session(now.date(), config), time(15)).isoformat(sep=" ")


def _clean(data: pd.DataFrame, timeframe: str, start: str, cutoff: str) -> pd.DataFrame:
    if data is None or data.empty:
        return pd.DataFrame()
    result = data.copy()
    column = "date" if timeframe == "1d" else "datetime"
    stamps = pd.to_datetime(result[column], errors="coerce")
    result[column] = stamps.dt.strftime("%Y-%m-%d" if timeframe == "1d" else "%Y-%m-%d %H:%M:%S")
    for name in ("open", "high", "low", "close"):
        result[name] = pd.to_numeric(result[name], errors="coerce")
    result = result.dropna(subset=[column, "open", "high", "low", "close"])
    result = result[(result[column] >= start) & (result[column] <= cutoff)]
    return result.drop_duplicates(column, keep="last").sort_values(column).reset_index(drop=True)


def _join_daily(history: pd.DataFrame, recent: pd.DataFrame) -> pd.DataFrame:
    if history.empty:
        return recent
    if recent.empty:
        return history
    # Require an overlap; do not silently join histories separated by a gap.
    if not set(history["date"]).intersection(recent["date"]):
        raise BacktestDataUnavailable("备用日线与历史没有重叠，无法验证连续性")
    return pd.concat([history, recent], ignore_index=True).drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def fetch_current_backtest_data(symbol: str, timeframe: str, start: str,
                                *, now: datetime | None = None, config: dict | None = None) -> pd.DataFrame:
    current = now or datetime.now()
    cutoff = latest_completed_bar(timeframe, current, config)
    column = "date" if timeframe == "1d" else "datetime"
    latest_seen = "无数据"
    data = pd.DataFrame()

    def ready(frame):
        nonlocal latest_seen
        if not frame.empty:
            latest_seen = max(latest_seen if latest_seen != "无数据" else "", str(frame[column].iloc[-1]))
            return str(frame[column].iloc[-1]) == cutoff
        return False

    def finish(frame, source, warning=None):
        frame.attrs.update(data_source=source, warning=warning,
                           expected_end=cutoff, checked_at=current.isoformat(timespec="seconds"),
                           freshness="current", completed_bars_only=True)
        return frame

    try:
        raw = provider.fetch_backtest_kline(symbol, timeframe, start)
        data = _clean(raw, timeframe, start, cutoff)
        if ready(data):
            return finish(data, raw.attrs.get("data_source", "历史行情接口"), raw.attrs.get("warning"))
    except Exception as exc:
        provider.app_logger.warning("interactive backtest primary unavailable: %s %s", symbol, exc)

    if timeframe == "1d":
        if data.empty:
            data = _clean(provider._read_cache(symbol), timeframe, start, cutoff)
            if ready(data):
                return finish(data, "已核验截止日期的本地日线", "网络源暂不可用；缓存已覆盖最新正式交易日")
        fallbacks = (("sina daily", lambda: provider._fetch_sina_daily(symbol)),
                     ("tencent daily", lambda: provider._fetch_tencent_daily(symbol, datalen=640)))
    else:
        fallbacks = (("sina minute", lambda: provider._fetch_sina_minute(symbol, provider._period_for_timeframe(timeframe))),)
    for source, fetch in fallbacks:
        try:
            recent = _clean(fetch(), timeframe, start, cutoff)
            if timeframe == "1d":
                combined = _join_daily(data, recent)
                if ready(combined):
                    provider._write_cache(symbol, combined)
                    return finish(combined, source + " + 历史缓存", "已用备用源刷新日线；以本次实际覆盖区间为准")
            elif ready(recent):
                return finish(recent, source, "主分钟源未更新，使用备用源；以本次实际覆盖区间为准")
        except Exception as exc:
            provider.app_logger.warning("interactive backtest fallback unavailable: %s %s %s", symbol, source, exc)
    raise BacktestDataUnavailable(
        f"最新回测数据尚未就绪：应截至 {cutoff}，实际仅到 {latest_seen}。"
        "已尝试备用源；可能行情延迟或标的停牌，请稍后重试。本次不展示过期回测结果。"
    )
