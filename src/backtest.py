"""KDJ 机械交易策略回测引擎（供 API 调用）。

策略：K < buy_threshold 全仓买入，K > sell_threshold 清仓，
信号当根K线收盘确认，下一根K线开盘价成交。
"""

from __future__ import annotations

import math

import pandas as pd

from .kdj import calculate_kdj

FEE_RATE = 0.0005
SLIPPAGE = 0.001

# 每年K线根数估算（用于年化收益/夏普换算）
BARS_PER_YEAR = {"15m": 16 * 244, "30m": 8 * 244, "60m": 4 * 244, "1d": 244}


def run_backtest(df: pd.DataFrame, buy_threshold: float, sell_threshold: float,
                 n: int = 9, m1: int = 3, m2: int = 3,
                 fee_rate: float = FEE_RATE, slippage: float = SLIPPAGE) -> dict:
    """对K线数据执行回测，返回统计指标和交易明细。"""
    data = df.reset_index(drop=True)
    kdj = calculate_kdj(data, n=n, m1=m1, m2=m2)
    k_values = kdj["k"].reset_index(drop=True)

    time_col = "datetime" if "datetime" in data.columns else "date"

    cash, units = 1.0, 0.0
    pending = None
    trades = []
    equity = []

    for i in range(len(data)):
        row = data.iloc[i]
        open_p, close_p = float(row["open"]), float(row["close"])
        timestamp = str(row[time_col])

        if pending == "buy" and cash > 0:
            price = round(open_p * (1 + slippage), 3)
            units = cash * (1 - fee_rate) / price
            cash = 0.0
            trades.append({"time": timestamp, "side": "buy", "price": price,
                           "k": round(float(k_values.iloc[i - 1]), 1)})
        elif pending == "sell" and units > 0:
            price = round(open_p * (1 - slippage), 3)
            cash = units * price * (1 - fee_rate)
            units = 0.0
            trades.append({"time": timestamp, "side": "sell", "price": price,
                           "k": round(float(k_values.iloc[i - 1]), 1)})
        pending = None

        kv = k_values.iloc[i]
        if pd.notna(kv):
            kv = float(kv)
            if kv < buy_threshold and units == 0 and cash > 0:
                pending = "buy"
            elif kv > sell_threshold and units > 0:
                pending = "sell"

        equity.append(cash + units * close_p)

    return _build_result(data, equity, trades, time_col, k_values)


def _build_result(data, equity, trades, time_col, k_values) -> dict:
    eq = pd.Series(equity)
    closes = data["close"].astype(float).reset_index(drop=True)

    # 基准：首根开盘价买入持有
    first_open = float(data.iloc[0]["open"]) * (1 + SLIPPAGE)
    bench = (closes / first_open * (1 - FEE_RATE))

    round_trips = _round_trips(trades, float(closes.iloc[-1]))
    wins = sum(1 for t in round_trips if t["return_pct"] > 0)

    summary = {
        "start": str(data[time_col].iloc[0]),
        "end": str(data[time_col].iloc[-1]),
        "bars": len(data),
        "final_equity": round(float(eq.iloc[-1]), 4),
        "total_return": round(float(eq.iloc[-1] - 1), 4),
        "max_drawdown": round(float((eq / eq.cummax() - 1).min()), 4),
        "bench_return": round(float(bench.iloc[-1] - 1), 4),
        "bench_max_drawdown": round(float((bench / bench.cummax() - 1).min()), 4),
        "round_trips": len(round_trips),
        "open_position": bool(round_trips and round_trips[-1].get("open")),
        "win_rate": round(wins / len(round_trips), 3) if round_trips else None,
        "avg_return": round(sum(t["return_pct"] for t in round_trips) / len(round_trips), 4) if round_trips else None,
        "best_return": round(max((t["return_pct"] for t in round_trips), default=0), 4),
        "worst_return": round(min((t["return_pct"] for t in round_trips), default=0), 4),
    }

    # 抽样净值曲线（最多200个点，供前端画线）
    step = max(1, len(data) // 200)
    curve = [{"time": str(data[time_col].iloc[i]),
              "equity": round(float(eq.iloc[i]), 4),
              "bench": round(float(bench.iloc[i]), 4),
              "k": round(float(k_values.iloc[i]), 1) if pd.notna(k_values.iloc[i]) else None}
             for i in range(0, len(data), step)]

    return {"summary": summary, "trades": trades, "round_trips": round_trips, "curve": curve}


def _round_trips(trades, last_close) -> list:
    trips = []
    buy = None
    for t in trades:
        if t["side"] == "buy":
            buy = t
        elif t["side"] == "sell" and buy:
            trips.append({
                "buy_time": buy["time"], "buy_price": buy["price"],
                "sell_time": t["time"], "sell_price": t["price"],
                "return_pct": round(t["price"] / buy["price"] - 1, 4),
            })
            buy = None
    if buy:
        trips.append({
            "buy_time": buy["time"], "buy_price": buy["price"],
            "sell_time": None, "sell_price": None,
            "return_pct": round(last_close / buy["price"] - 1, 4),
            "open": True,
        })
    return trips
