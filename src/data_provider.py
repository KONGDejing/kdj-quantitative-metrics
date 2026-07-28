from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from .logger import app_logger

# 回测日线本地缓存目录（实时源不可用时兜底，保证刷新结果一致）
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    column_map = {
        "日期": "date",
        "时间": "datetime",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
    }
    data = df.rename(columns=column_map).copy()
    for column in ["open", "close", "high", "low"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "close", "high", "low"])
    return data


def _period_for_timeframe(timeframe: str) -> str:
    mapping = {
        "5m": "5",
        "10m": "10",
        "15m": "15",
        "30m": "30",
        "60m": "60",
    }
    return mapping[timeframe]


def _sina_symbol(symbol: str) -> str:
    if symbol.startswith(("sh", "sz")):
        return symbol
    if symbol in {"000001", "000016", "000300", "000905", "000852"}:
        return f"sh{symbol}"
    if symbol.startswith(("0", "3")):
        return f"sz{symbol}"
    return f"sh{symbol}"


def _fetch_sina_minute(symbol: str, period: str) -> pd.DataFrame:
    url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
    response = requests.get(
        url,
        params={"symbol": _sina_symbol(symbol), "scale": period, "ma": "no", "datalen": "240"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    data = pd.DataFrame(rows)
    if data.empty:
        return data
    data = data.rename(columns={"day": "datetime"})
    for column in ["open", "close", "high", "low", "volume"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "close", "high", "low"])
    data["datetime"] = pd.to_datetime(data["datetime"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    return data


def _fetch_sina_daily(symbol: str, datalen: int = 1023) -> pd.DataFrame:
    url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
    response = requests.get(
        url,
        params={"symbol": _sina_symbol(symbol), "scale": "240", "ma": "no", "datalen": str(datalen)},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    data = pd.DataFrame(rows)
    if data.empty:
        return data
    data = data.rename(columns={"day": "date"})
    for column in ["open", "close", "high", "low", "volume"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["open", "close", "high", "low"])


def _today_only(data: pd.DataFrame) -> pd.DataFrame:
    if "datetime" not in data.columns:
        return data
    today = datetime.now().strftime("%Y-%m-%d")
    current = data[data["datetime"].astype(str).str.startswith(today)].copy()
    return current if not current.empty else data.tail(120).copy()


def _fetch_daily(ak, symbol: str) -> pd.DataFrame:
    try:
        return ak.index_zh_a_hist(symbol=symbol, period="daily")
    except Exception:
        return ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="")


def _fetch_minute(ak, symbol: str, period: str, start_date: datetime, end_date: datetime) -> pd.DataFrame:
    start = start_date.strftime("%Y-%m-%d %H:%M:%S")
    end = end_date.strftime("%Y-%m-%d %H:%M:%S")
    try:
        return ak.index_zh_a_hist_min_em(symbol=symbol, period=period, start_date=start, end_date=end)
    except Exception as exc:
        app_logger.warning("akshare index minute failed, fallback stock api: symbol=%s period=%s error=%s", symbol, period, exc)
    try:
        return ak.stock_zh_a_hist_min_em(symbol=symbol, period=period, start_date=start, end_date=end, adjust="")
    except Exception as exc:
        app_logger.warning("akshare stock minute failed, fallback sina api: symbol=%s period=%s error=%s", symbol, period, exc)
        return _fetch_sina_minute(symbol, period)


def fetch_kline(symbol: str, timeframe: str) -> pd.DataFrame:
    import akshare as ak

    if timeframe == "1d":
        try:
            raw = _fetch_daily(ak, symbol)
        except Exception as exc:
            app_logger.warning("akshare daily failed, fallback sina api: symbol=%s error=%s", symbol, exc)
            raw = _fetch_sina_daily(symbol, datalen=120)
        return _normalize_columns(raw).tail(120).reset_index(drop=True)

    period = _period_for_timeframe(timeframe)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=10)
    raw = _fetch_minute(ak, symbol, period, start_date, end_date)
    data = _normalize_columns(raw).reset_index(drop=True)
    if "datetime" not in data.columns and "date" in data.columns:
        data["datetime"] = data["date"]
    return data.tail(200).reset_index(drop=True)


def safe_fetch_kline(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    try:
        data = fetch_kline(symbol, timeframe)
        if data.empty:
            app_logger.warning("empty kline data: symbol=%s timeframe=%s", symbol, timeframe)
            return None
        return data
    except Exception as exc:
        app_logger.exception("fetch kline failed: symbol=%s timeframe=%s error=%s", symbol, timeframe, exc)
        return None


# ---------- 回测专用数据获取（更长历史） ----------


def _tencent_symbol(symbol: str) -> str:
    return _sina_symbol(symbol)


def _fetch_tencent_minute(symbol: str, period: str) -> pd.DataFrame:
    import akshare as ak

    raw = ak.stock_zh_a_minute(symbol=_tencent_symbol(symbol), period=period, adjust="")
    data = raw.rename(columns={"day": "datetime"}).copy()
    for column in ["open", "close", "high", "low", "volume"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["open", "close", "high", "low"]).reset_index(drop=True)


# 记录最近一次回测取数所用的数据源和降级告警（供 API 返回给前端）
last_backtest_source: Optional[str] = None
last_backtest_warning: Optional[str] = None


def _retry(fn, attempts: int = 3, delay: float = 2.0) -> pd.DataFrame:
    """网络接口偶发断连，重试几次再放弃。"""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    raise last_exc


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"daily_{symbol}.csv"


def _read_cache(symbol: str) -> Optional[pd.DataFrame]:
    path = _cache_path(symbol)
    if not path.exists():
        return None
    try:
        data = pd.read_csv(path, dtype={"date": str})
        for column in ["open", "close", "high", "low"]:
            data[column] = pd.to_numeric(data[column], errors="coerce")
        return data.dropna(subset=["open", "close", "high", "low"])
    except Exception as exc:
        app_logger.warning("read daily cache failed: symbol=%s error=%s", symbol, exc)
        return None


def _write_cache(symbol: str, data: pd.DataFrame) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        keep = [c for c in ["date", "open", "close", "high", "low", "volume"] if c in data.columns]
        data[keep].to_csv(_cache_path(symbol), index=False)
    except Exception as exc:
        app_logger.warning("write daily cache failed: symbol=%s error=%s", symbol, exc)


def fetch_backtest_daily(symbol: str, start_date: str = "2010-01-01") -> pd.DataFrame:
    """日线长历史，三级策略：

    1. 东财等实时全历史源（带重试），成功则更新本地缓存；
    2. 实时源全部失败 → 本地缓存（上次成功抓取的全历史），保证刷新结果一致；
    3. 无缓存 → 新浪日线（仅约千根），标记降级告警。
    """
    global last_backtest_source, last_backtest_warning
    import akshare as ak

    last_backtest_source = None
    last_backtest_warning = None

    sina_sym = _sina_symbol(symbol)
    primaries = []
    if symbol in {"000001", "000016", "000300", "000905", "000852"}:
        primaries.extend([
            ("akshare em index daily", lambda: _normalize_columns(
                ak.stock_zh_index_daily_em(symbol=sina_sym, start_date=start_date.replace("-", ""),
                                           end_date=datetime.now().strftime("%Y%m%d")))),
            ("akshare sina index daily", lambda: _normalize_columns(
                ak.stock_zh_index_daily(symbol=sina_sym))),
            ("akshare csindex", lambda: _normalize_columns(
                ak.stock_zh_index_hist_csindex(symbol=symbol, start_date=start_date.replace("-", ""),
                                               end_date=datetime.now().strftime("%Y%m%d")))),
        ])
    else:
        primaries.append(("akshare em stock daily", lambda: _normalize_columns(
            ak.stock_zh_a_hist(symbol=symbol, period="daily",
                               start_date=start_date.replace("-", ""),
                               end_date=datetime.now().strftime("%Y%m%d"), adjust=""))))

    for name, fn in primaries:
        try:
            data = _retry(fn)
            if data is not None and not data.empty:
                data = data[data["date"].astype(str) >= start_date].reset_index(drop=True)
                if not data.empty:
                    _write_cache(symbol, data)
                    last_backtest_source = name
                    app_logger.info("backtest daily data via %s: symbol=%s bars=%d", name, symbol, len(data))
                    return data
        except Exception as exc:
            app_logger.warning("backtest daily source %s failed after retries: symbol=%s error=%s", name, symbol, exc)

    cached = _read_cache(symbol)
    if cached is not None and not cached.empty:
        cached = cached[cached["date"].astype(str) >= start_date].reset_index(drop=True)
        if not cached.empty:
            last_day = str(cached["date"].iloc[-1])[:10]
            last_backtest_source = f"local cache(截至{last_day})"
            last_backtest_warning = (f"实时数据源暂不可用，本次使用本地缓存数据（{len(cached)} 根K线，"
                                     f"截至 {last_day}），结果基于缓存，可稍后刷新重试")
            app_logger.warning("backtest daily using local cache: symbol=%s bars=%d last=%s",
                               symbol, len(cached), last_day)
            return cached

    try:
        data = _retry(lambda: _fetch_sina_daily(symbol), attempts=2)
        if data is not None and not data.empty:
            data = data[data["date"].astype(str) >= start_date].reset_index(drop=True)
            if not data.empty:
                last_backtest_source = "sina daily"
                last_backtest_warning = (f"长历史数据源不可用且无本地缓存，已降级到 sina daily，"
                                         f"仅 {len(data)} 根K线（{str(data['date'].iloc[0])[:10]} 起），"
                                         f"回测区间受限，结果与全历史回测差异较大，可稍后刷新重试")
                app_logger.info("backtest daily data via sina daily: symbol=%s bars=%d", symbol, len(data))
                return data
    except Exception as exc:
        app_logger.warning("backtest daily source sina daily failed: symbol=%s error=%s", symbol, exc)
    raise RuntimeError(f"所有日线数据源均不可用: {symbol}")


def fetch_backtest_minute(symbol: str, timeframe: str) -> pd.DataFrame:
    """分钟线历史：腾讯（15m约半年/30m约1年/60m约3年）→ 新浪（约2-3个月）。"""
    period = _period_for_timeframe(timeframe)
    try:
        data = _fetch_tencent_minute(symbol, period)
        if not data.empty:
            return data
    except Exception as exc:
        app_logger.warning("tencent minute failed: symbol=%s period=%s error=%s", symbol, period, exc)
    url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
    response = requests.get(
        url,
        params={"symbol": _sina_symbol(symbol), "scale": period, "ma": "no", "datalen": "1023"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise RuntimeError(f"分钟线数据源均不可用: {symbol} {timeframe}")
    data = pd.DataFrame(rows).rename(columns={"day": "datetime"})
    for column in ["open", "close", "high", "low", "volume"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["open", "close", "high", "low"]).reset_index(drop=True)


def fetch_backtest_kline(symbol: str, timeframe: str, start_date: str = "2010-01-01") -> pd.DataFrame:
    if timeframe == "1d":
        return fetch_backtest_daily(symbol, start_date)
    return fetch_backtest_minute(symbol, timeframe)
