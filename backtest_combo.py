"""组合策略回测：底仓长期持有 + 机动仓按KDJ极端信号操作。

底仓：初始资金的 base_ratio 部分，买入后不动，吃指数长期收益。
机动仓：剩余资金，K<buy_th 时买入，K>sell_th 时卖出，可选止损。
信号当日收盘产生，次日开盘价成交，手续费0.05%，滑点0.1%。
"""

import pandas as pd
import akshare as ak
from src.kdj import calculate_kdj

FEE = 0.0005
SLIP = 0.001


def run_combo(df, k, buy_th, sell_th, base_ratio=0.5, stop_loss=None, staged=False):
    """返回 (净值序列, 交易记录)。"""
    base_value = 0.5  # 底仓初始市值（占初始资金一半，按首日开盘价买入）
    first_open = float(df.iloc[0]["open"])
    base_units = base_value * (1 - FEE) / (first_open * (1 + SLIP))

    swing_cash = 1 - base_value
    swing_units = 0.0
    swing_entry_price = None

    trades = []
    pending = None
    equity = []

    for i in range(len(df)):
        row = df.iloc[i]
        date = row["date"]
        open_p, close_p = float(row["open"]), float(row["close"])

        # 止损检查（盘中，用开盘价近似）：持仓中且开盘价相对买入价跌幅超过阈值
        if swing_units > 0 and stop_loss is not None and swing_entry_price:
            if open_p / swing_entry_price - 1 <= -stop_loss:
                price = open_p * (1 - SLIP)
                swing_cash = swing_units * price * (1 - FEE)
                trades.append((date, f"止损卖出(-{stop_loss:.0%})", round(price, 3),
                               round(float(k.iloc[i - 1]), 1) if i > 0 else None))
                swing_units, swing_entry_price = 0.0, None
                pending = None

        # 执行昨日信号
        if pending == "buy" and swing_cash > 0:
            price = open_p * (1 + SLIP)
            invest = swing_cash if not staged else swing_cash  # staged时下面单独处理
            swing_units = invest * (1 - FEE) / price
            swing_entry_price = price
            trades.append((date, "机动买入", round(price, 3), round(float(k.iloc[i - 1]), 1)))
            swing_cash = 0.0
        elif pending == "sell" and swing_units > 0:
            price = open_p * (1 - SLIP)
            swing_cash = swing_units * price * (1 - FEE)
            trades.append((date, "机动卖出", round(price, 3), round(float(k.iloc[i - 1]), 1)))
            swing_units, swing_entry_price = 0.0, None
        pending = None

        # 收盘产生新信号
        kv = float(k.iloc[i])
        if pd.notna(kv):
            if kv < buy_th and swing_units == 0 and swing_cash > 0:
                pending = "buy"
            elif kv > sell_th and swing_units > 0:
                pending = "sell"

        equity.append(base_units * close_p + swing_units * close_p + swing_cash)

    return equity, trades


def stats(equity, label, n_days):
    eq = pd.Series(equity)
    years = n_days / 244
    total = eq.iloc[-1] / eq.iloc[0] - 1
    annual = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    dd = (eq / eq.cummax() - 1).min()
    ret = eq.pct_change().dropna()
    sharpe = ret.mean() / ret.std() * (244 ** 0.5)
    print(f"{label:<44}净值{eq.iloc[-1]:>7.3f} 总收益{total:>+8.1%} 年化{annual:>+7.2%} "
          f"回撤{dd:>7.1%} 夏普{sharpe:>5.2f}")
    return {"label": label, "total": total, "annual": annual, "dd": dd, "sharpe": sharpe}


def main():
    df_full = ak.stock_zh_index_daily(symbol="sh000300")
    df_full["date"] = df_full["date"].astype(str)
    kdj_full = calculate_kdj(df_full, n=9, m1=3, m2=3)

    mask = df_full["date"] >= "2010-01-01"
    df = df_full[mask].reset_index(drop=True)
    k = kdj_full["k"][mask].reset_index(drop=True)
    print(f"数据区间: {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}, 共 {len(df)} 个交易日\n")

    # 基准
    bh = (df["close"] / (float(df.iloc[0]["open"]) * (1 + SLIP)) * (1 - FEE)).tolist()
    stats(bh, "基准：100%买入持有", len(df))

    # 组合策略各变体
    variants = [
        ("50%底仓+机动仓 K<10买/K>85卖", dict(buy_th=10, sell_th=85)),
        ("50%底仓+机动仓 K<10买/K>85卖 止损8%", dict(buy_th=10, sell_th=85, stop_loss=0.08)),
        ("50%底仓+机动仓 K<15买/K>85卖 止损8%", dict(buy_th=15, sell_th=85, stop_loss=0.08)),
        ("50%底仓+机动仓 K<15买/K>80卖 止损8%", dict(buy_th=15, sell_th=80, stop_loss=0.08)),
        ("50%底仓+机动仓 K<20买/K>80卖 止损8%", dict(buy_th=20, sell_th=80, stop_loss=0.08)),
    ]
    best_trades = None
    for label, kw in variants:
        equity, trades = run_combo(df, k, **kw)
        stats(equity, label, len(df))
        n_trades = len(trades)
        buys = [t for t in trades if "买入" in t[1]]
        sells = [t for t in trades if "卖出" in t[1]]
        wins = sum(1 for b, s in zip(buys, sells) if s[2] > b[2])
        print(f"    机动仓交易 {n_trades} 笔，完整往返 {len(sells)} 次，胜率 {wins/len(sells):.0%}" if sells else "    无完整往返")
        if "K<15买/K>85卖" in label:
            best_trades = trades

    print("\n===== 推荐方案（K<15买/K>85卖+止损8%）机动仓交易明细 =====")
    for t in best_trades:
        print(f"  {t[0]} {t[1]} @ {t[2]}  (K={t[3]})")


if __name__ == "__main__":
    main()
