from __future__ import annotations

import pandas as pd


def calculate_kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    data = df.copy()
    low_min = data["low"].rolling(window=n, min_periods=1).min()
    high_max = data["high"].rolling(window=n, min_periods=1).max()
    denominator = (high_max - low_min).replace(0, pd.NA)

    data["rsv"] = ((data["close"] - low_min) / denominator * 100).fillna(50)
    data["k"] = data["rsv"].ewm(com=m1 - 1, adjust=False).mean()
    data["d"] = data["k"].ewm(com=m2 - 1, adjust=False).mean()
    data["j"] = 3 * data["k"] - 2 * data["d"]
    return data
