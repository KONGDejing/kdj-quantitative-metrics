"""沪深300指数 KDJ 机械交易策略回测（2010年至今）。

策略：K < lower 时全仓买入，K > upper 时清仓卖出，信号次日开盘价成交。
同时计算买入持有作为基准对比。
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import akshare as ak
from src.kdj import calculate_kdj


def run_backtest(df: pd.DataFrame, k: pd.Series, lower: float, upper: float,
                 fee_rate: float = 0.0005, slip: float = 0.001):
    """机械策略回测：K<lower 全仓买入，K>upper 清仓，信号次日开盘价成交。"""
    df = df.reset_index(drop=True).copy()
    k = k.reset_index(drop=True)

    position = 0.0  # 持有份额
    cash = 1.0      # 初始资金 1 元
    trades = []     # (日期, 方向, 成交价, K值)
    equity_curve = []

    pending_signal = None  # 今天产生、明天执行的信号

    for i in range(len(df)):
        row = df.iloc[i]
        date = row["date"] if "date" in df.columns else i
        open_p, close_p = float(row["open"]), float(row["close"])

        # 先执行昨天留下的信号（按今天开盘价成交）
        if pending_signal == "buy" and cash > 0:
            price = open_p * (1 + slip)
            position = cash * (1 - fee_rate) / price
            trades.append((date, "买入", round(price, 3), round(float(k.iloc[i - 1]), 1)))
            cash = 0.0
        elif pending_signal == "sell" and position > 0:
            price = open_p * (1 - slip)
            cash = position * price * (1 - fee_rate)
            trades.append((date, "卖出", round(price, 3), round(float(k.iloc[i - 1]), 1)))
            position = 0.0
        pending_signal = None

        # 今天收盘后产生新信号
        kv = float(k.iloc[i])
        if pd.notna(kv):
            if kv < lower and position == 0 and cash > 0:
                pending_signal = "buy"
            elif kv > upper and position > 0:
                pending_signal = "sell"

        equity_curve.append(cash + position * close_p)

    final = equity_curve[-1]
    return final, trades, equity_curve, df


def stats(equity, df, label):
    eq = pd.Series(equity)
    years = len(eq) / 244
    total_ret = eq.iloc[-1] / eq.iloc[0] - 1
    annual = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    dd = (eq / eq.cummax() - 1).min()
    daily_ret = eq.pct_change().dropna()
    sharpe = daily_ret.mean() / daily_ret.std() * (244 ** 0.5) if daily_ret.std() > 0 else 0
    print(f"\n===== {label} =====")
    print(f"期末净值: {eq.iloc[-1]:.3f}   总收益率: {total_ret:+.1%}   年化: {annual:+.2%}")
    print(f"最大回撤: {dd:.1%}   夏普比率: {sharpe:.2f}")
    return {"label": label, "final": eq.iloc[-1], "total": total_ret,
            "annual": annual, "maxdd": dd, "sharpe": sharpe}


def main():
    print("正在获取沪深300指数日线数据（2010年至今）...")
    df = ak.stock_zh_index_daily(symbol="sh000300")
    if df is None or df.empty:
        print("数据获取失败")
        return
    df["date"] = df["date"].astype(str)

    date_col = "date"
    df = df[df[date_col] >= "2010-01-01"].reset_index(drop=True)
    print(f"数据区间: {df[date_col].iloc[0]} ~ {df[date_col].iloc[-1]}, 共 {len(df)} 个交易日")

    results = []

    # 基准：买入持有
    bh = (df["close"] / df["close"].iloc[0]).tolist()
    results.append(stats(bh, df, "基准：买入持有"))

    # KDJ 策略，多组参数对比
    kdj = calculate_kdj(df, n=9, m1=3, m2=3)
    for lower, upper in [(15, 80), (20, 80), (10, 85), (20, 85)]:
        final, trades, equity, _ = run_backtest(df, kdj["k"], lower, upper)
        r = stats(equity, df, f"KDJ策略: K<{lower}买, K>{upper}卖")
        r["trades"] = len(trades)
        # 胜率统计
        buys = [t for t in trades if t[1] == "买入"]
        sells = [t for t in trades if t[1] == "卖出"]
        wins = sum(1 for b, s in zip(buys, sells) if s[2] > b[2])
        r["win_rate"] = wins / len(sells) if sells else 0
        r["round_trips"] = len(sells)
        results.append(r)
        print(f"交易次数: {len(trades)}（完整买卖往返 {len(sells)} 次，胜率 {r['win_rate']:.0%}）")
        print("最近5笔交易:")
        for t in trades[-5:]:
            print(f"  {t[0]} {t[1]} @ {t[2]}  (K={t[3]})")

    print("\n===== 汇总对比 =====")
    print(f"{'策略':<28}{'期末净值':>8}{'总收益':>10}{'年化':>8}{'最大回撤':>9}{'夏普':>7}")
    for r in results:
        print(f"{r['label']:<28}{r['final']:>8.3f}{r['total']:>+9.1%}{r['annual']:>+8.2%}"
              f"{r['maxdd']:>9.1%}{r['sharpe']:>7.2f}")


if __name__ == "__main__":
    main()
