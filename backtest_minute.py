"""分钟线 KDJ 策略回测（15分钟/30分钟，腾讯接口可得的最长历史区间）。

策略：K < buy_th 全仓买入，K > 85 清仓，信号次一根K线开盘价成交。
手续费 0.05%，滑点 0.1%。对比同期买入持有。
"""

import akshare as ak
import pandas as pd
from src.kdj import calculate_kdj

FEE = 0.0005
SLIP = 0.001


def run_bt(df, k, buy_th, sell_th):
    cash, units = 1.0, 0.0
    trades = []
    pending = None
    equity = []
    for i in range(len(df)):
        row = df.iloc[i]
        t, o, c = str(row["day"]), float(row["open"]), float(row["close"])
        if pending == "buy" and cash > 0:
            price = o * (1 + SLIP)
            units = cash * (1 - FEE) / price
            cash = 0.0
            trades.append((t, "买", round(price, 3), round(float(k.iloc[i - 1]), 1)))
        elif pending == "sell" and units > 0:
            price = o * (1 - SLIP)
            cash = units * price * (1 - FEE)
            units = 0.0
            trades.append((t, "卖", round(price, 3), round(float(k.iloc[i - 1]), 1)))
        pending = None
        kv = float(k.iloc[i])
        if pd.notna(kv):
            if kv < buy_th and units == 0 and cash > 0:
                pending = "buy"
            elif kv > sell_th and units > 0:
                pending = "sell"
        equity.append(cash + units * c)
    return equity, trades


def stats(equity, bars_per_day, label):
    eq = pd.Series(equity)
    years = len(eq) / (bars_per_day * 244)
    total = eq.iloc[-1] - 1
    annual = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    ret = eq.pct_change().dropna()
    sharpe = ret.mean() / ret.std() * ((bars_per_day * 244) ** 0.5) if ret.std() > 0 else 0
    print(f"  {label:<24}净值{eq.iloc[-1]:>7.3f} 总收益{total:>+7.2%} 年化{annual:>+7.2%} 回撤{dd:>6.1%} 夏普{sharpe:>5.2f}")
    return total


def report(df, period_name, bars_per_day):
    kdj = calculate_kdj(df, n=9, m1=3, m2=3)
    k = kdj["k"]
    print(f"\n===== {period_name} | {df['day'].iloc[0]} ~ {df['day'].iloc[-1]} 共{len(df)}根K线 =====")

    bh = (df["close"] / (float(df.iloc[0]["open"]) * (1 + SLIP)) * (1 - FEE)).tolist()
    stats(bh, bars_per_day, "基准:买入持有")

    for buy_th in (10, 15):
        equity, trades = run_bt(df, k, buy_th, 85)
        label = f"K<{buy_th}买/K>85卖"
        stats(equity, bars_per_day, label)
        buys = [t for t in trades if t[1] == "买"]
        sells = [t for t in trades if t[1] == "卖"]
        if sells:
            wins = sum(1 for b, s in zip(buys, sells) if s[2] > b[2])
            rets = [s[2] / b[2] - 1 for b, s in zip(buys, sells)]
            days = len(df) / bars_per_day
            print(f"    往返{len(sells)}次 胜率{wins/len(sells):.0%} "
                  f"平均每次{sum(rets)/len(rets):+.2%} 最好{max(rets):+.2%} 最差{min(rets):+.2%} "
                  f"（约{len(sells)/days*5:.1f}次/周）")


def main():
    for period, name, bpd in [("30", "30分钟线", 8), ("15", "15分钟线", 16)]:
        df = ak.stock_zh_a_minute(symbol="sh000300", period=period, adjust="")
        if df is None or df.empty:
            print(f"{name}: 数据获取失败")
            continue
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        report(df, name, bpd)


if __name__ == "__main__":
    main()
