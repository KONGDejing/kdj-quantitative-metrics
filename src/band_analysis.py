"""中航光电(002179) 波段买卖分析引擎。

策略：价格跌到≤B时全仓买入，涨到≥S时清仓卖出（利用每日最低价/最高价判断）。
遍历(B,S)组合找出最优，或对指定(B,S)返回完整交易明细。
"""

from __future__ import annotations

import math
import time
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
FEE = 0.0005       # 单边手续费 0.05%
STEP = 0.1         # 搜索步长
INIT_CAPITAL = 10000


def default_start_date(today: Optional[date] = None) -> str:
    """Return the first day of the latest ten-year research window."""
    current = today or date.today()
    try:
        start = current.replace(year=current.year - 10)
    except ValueError:
        start = current.replace(year=current.year - 10, day=28)
    return start.isoformat()

# 尝试加载 numba 加速
try:
    from numba import njit
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False
    def njit(*args, **kwargs):
        return lambda f: f


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_data(symbol: str) -> pd.DataFrame:
    path = CACHE_DIR / f"daily_{symbol}.csv"
    if path.exists():
        df = pd.read_csv(path, dtype={"date": str})
    else:
        try:
            from .data_provider import fetch_backtest_daily
            df = fetch_backtest_daily(symbol, start_date="2010-01-01")
        except Exception:
            raise FileNotFoundError(f"缓存数据不存在: {path}")

    try:
        from .data_provider import _fetch_sina_daily
        live = _fetch_sina_daily(symbol, datalen=120)
        if not live.empty:
            live = live.rename(columns={"day": "date"})
            live["date"] = pd.to_datetime(live["date"]).dt.strftime("%Y-%m-%d")
            df = df[df["date"].astype(str) < live["date"].min()].copy() if "date" in df.columns else df
            df = pd.concat([df, live], ignore_index=True)
    except Exception:
        pass

    for col in ["open", "close", "high", "low"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "close", "high", "low"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 核心模拟（纯 Python 回退版）
# ---------------------------------------------------------------------------
def _simulate_py(lows, highs, closes, B, S, fee=FEE):
    """纯 Python 版模拟，返回 (final_cash, round_trips, events)"""
    n_days = len(lows)
    cash = float(INIT_CAPITAL)
    shares = 0.0
    rts = 0
    events = []

    for d in range(n_days):
        if shares > 0 and highs[d] >= S:
            cash = shares * S * (1.0 - fee)
            shares = 0.0
            rts += 1
            events.append({"day": d, "type": "sell", "price": S, "cash": cash, "shares": 0.0})
        if shares == 0 and cash > 0 and lows[d] <= B:
            shares = cash * (1.0 - fee) / B
            cash = 0.0
            events.append({"day": d, "type": "buy", "price": B, "cash": 0.0, "shares": shares})

    final_holding = shares > 0
    if final_holding:
        cash = shares * closes[-1] * (1.0 - fee)
        events.append({"day": n_days - 1, "type": "close_out", "price": float(closes[-1]),
                       "cash": cash, "shares": 0.0})

    return cash, rts, events, final_holding


# ---------------------------------------------------------------------------
# Numba 加速搜索
# ---------------------------------------------------------------------------
if _HAS_NUMBA:
    @njit
    def _search_numba(lows, highs, closes, prices, fee):
        n_days = len(lows)
        N = len(prices)
        lc = closes[-1]
        results = []
        for i in range(N):
            B = prices[i]
            for j in range(i + 1, N):
                S = prices[j]
                cash = float(INIT_CAPITAL)
                shares = 0.0
                rts = 0
                for d in range(n_days):
                    if shares > 0 and highs[d] >= S:
                        cash = shares * S * (1.0 - fee)
                        shares = 0.0
                        rts += 1
                    if shares == 0 and cash > 0 and lows[d] <= B:
                        shares = cash * (1.0 - fee) / B
                        cash = 0.0
                if shares > 0:
                    cash = shares * lc * (1.0 - fee)
                results.append((B, S, rts, cash / INIT_CAPITAL - 1.0))
        return results


def _search_fallback(lows, highs, closes, prices, fee=FEE):
    """纯 Python 搜索（无 numba 时使用，较慢）"""
    n_days = len(lows)
    lc = closes[-1]
    results = []
    for B in prices:
        for S in prices:
            if S <= B:
                continue
            cash, rts, _, _ = _simulate_py(lows, highs, closes, B, S, fee)
            results.append((B, S, rts, cash / INIT_CAPITAL - 1.0))
    return results


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def find_optimal(symbol: str, start_date: Optional[str] = None,
                 b_min: float = None, b_max: float = None,
                 top_n: int = 5) -> dict:
    """搜索给定时间段内最优的 (B, S) 组合。

    返回:
        { optimal: [{B, S, round_trips, return_rate, final_value, annual_return}, ...],
          buy_hold_return, date_range, price_range, trading_days }
    """
    start_date = start_date or default_start_date()
    df = load_data(symbol)
    mask = df["date"] >= start_date
    sub = df[mask].reset_index(drop=True)
    if len(sub) < 50:
        raise ValueError(f"数据不足（仅 {len(sub)} 个交易日）")

    lows = sub["low"].values.astype(np.float64)
    highs = sub["high"].values.astype(np.float64)
    closes = sub["close"].values.astype(np.float64)

    if b_min is None:
        b_min = math.floor(max(20.0, lows.min() - 0.5) * 10) / 10
    if b_max is None:
        b_max = math.ceil(highs.max() * 10) / 10

    prices = np.arange(b_min, b_max + STEP / 2, STEP)

    t0 = time.time()
    if _HAS_NUMBA:
        results = _search_numba(lows, highs, closes, prices, FEE)
    else:
        results = _search_fallback(lows, highs, closes, prices, FEE)
    elapsed = time.time() - t0

    results.sort(key=lambda x: x[3], reverse=True)
    years = len(sub) / 244
    bh_ret = float(closes[-1] / closes[0] - 1.0)

    optimal = []
    for B, S, rts, ret in results[:top_n]:
        annual = (1 + ret) ** (1 / years) - 1 if years > 0 and ret > -1 else 0
        optimal.append({
            "B": round(float(B), 1),
            "S": round(float(S), 1),
            "round_trips": int(rts),
            "return_rate": round(float(ret), 6),
            "final_value": round(float(INIT_CAPITAL * (1 + ret)), 2),
            "annual_return": round(float(annual), 6),
            "spread_pct": round((S - B) / B * 100, 2),
        })

    return {
        "symbol": symbol,
        "date_range": f"{sub['date'].iloc[0]} ~ {sub['date'].iloc[-1]}",
        "price_range": f"{lows.min():.1f}~{highs.max():.1f}",
        "trading_days": len(sub),
        "search_time_s": round(elapsed, 1),
        "buy_hold_return": round(bh_ret, 6),
        "buy_hold_final": round(float(INIT_CAPITAL * (1 + bh_ret)), 2),
        "optimal": optimal,
    }


def simulate_detail(symbol: str, B: float, S: float,
                    start_date: Optional[str] = None) -> dict:
    """对指定 (B, S) 进行模拟，返回完整交易明细。

    返回:
        { round_trips, return_rate, final_value, annual_return, buy_hold_return,
          trades: [{date, direction, price, cash, shares}, ...] }
    """
    start_date = start_date or default_start_date()
    df = load_data(symbol)
    mask = df["date"] >= start_date
    sub = df[mask].reset_index(drop=True)
    if len(sub) < 1:
        raise ValueError("数据为空")

    lows = sub["low"].values.astype(np.float64)
    highs = sub["high"].values.astype(np.float64)
    closes = sub["close"].values.astype(np.float64)
    dates = sub["date"].values

    final_cash, rts, events, final_holding = _simulate_py(
        lows, highs, closes, B, S, FEE)

    # 构建交易明细
    trades = []
    trip_count = 0
    for evt in events:
        d = evt["day"]
        record = {
            "date": str(dates[d]),
            "direction": evt["type"],
            "price": round(evt["price"], 2),
        }
        if evt["type"] == "sell":
            trip_count += 1
            record["cash"] = round(evt["cash"], 2)
            record["trip_num"] = trip_count
        elif evt["type"] == "buy":
            record["shares"] = int(evt["shares"])
            record["trip_num"] = trip_count + 1  # 属于下一轮
        elif evt["type"] == "close_out":
            record["cash"] = round(evt["cash"], 2)
            record["trip_num"] = "期末清仓"
        trades.append(record)

    years = len(sub) / 244
    total_ret = final_cash / INIT_CAPITAL - 1.0
    annual = (1 + total_ret) ** (1 / years) - 1 if years > 0 and total_ret > -1 else 0
    bh_ret = float(closes[-1] / closes[0] - 1.0)
    per_trip_mult = (S / B) * (1 - FEE) ** 2

    return {
        "symbol": symbol,
        "B": B,
        "S": S,
        "start_date": str(dates[0]),
        "end_date": str(dates[-1]),
        "trading_days": len(sub),
        "round_trips": rts,
        "return_rate": round(float(total_ret), 6),
        "final_value": round(float(final_cash), 2),
        "annual_return": round(float(annual), 6),
        "buy_hold_return": round(float(bh_ret), 6),
        "per_trip_mult": round(float(per_trip_mult), 4),
        "theoretical_return": round(float(per_trip_mult ** rts - 1) if rts > 0 else 0, 6),
        "final_holding": final_holding,
        "last_close": round(float(closes[-1]), 2),
        "trades": trades,
    }
