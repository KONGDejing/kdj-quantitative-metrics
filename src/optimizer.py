"""KDJ 参数自动寻优：为每只股票扫描日线最优 (买入K, 卖出K) 组合并持久化绑定。

评分规则：总收益率优先；候选需满足 round_trips >= MIN_TRIPS 且 最大回撤 >= MAX_DRAWDOWN_LIMIT，
不满足条件的组合只在无合格候选时兜底使用（避免过拟合到稀疏信号）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from .config import BASE_DIR
from .logger import app_logger

BEST_PARAMS_PATH = BASE_DIR / "best_params.json"

BUY_GRID = [5, 10, 15, 20, 25]
SELL_GRID = [75, 80, 85, 90, 95]

# 合格候选的约束：信号太少统计上不可靠，回撤太大不符合风控偏好
MIN_TRIPS = 8
MAX_DRAWDOWN_LIMIT = -0.45

# 正在后台寻优的代码（避免重复触发）
_pending: set[str] = set()
_pending_lock = threading.Lock()


def load_best_params() -> dict:
    if not BEST_PARAMS_PATH.exists():
        return {}
    try:
        return json.loads(BEST_PARAMS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        app_logger.warning("load best params failed: %s", exc)
        return {}


def get_best(symbol: str) -> dict | None:
    return load_best_params().get(symbol)


def save_best(symbol: str, entry: dict) -> None:
    data = load_best_params()
    data[symbol] = entry
    BEST_PARAMS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def optimize_symbol(symbol: str, start_date: str = "2010-01-01") -> dict:
    """扫描全部参数组合，选出最优并保存。返回保存的条目。"""
    from .backtest import run_backtest
    from .data_provider import fetch_backtest_daily

    df = fetch_backtest_daily(symbol, start_date)
    if df is None or df.empty:
        raise RuntimeError(f"未获取到 {symbol} 的日线数据")

    results = []
    for buy in BUY_GRID:
        for sell in SELL_GRID:
            summary = run_backtest(df, buy_threshold=buy, sell_threshold=sell)["summary"]
            results.append({
                "buy": buy,
                "sell": sell,
                "total_return": summary["total_return"],
                "max_drawdown": summary["max_drawdown"],
                "round_trips": summary["round_trips"],
                "win_rate": summary["win_rate"],
            })

    qualified = [r for r in results
                 if r["round_trips"] >= MIN_TRIPS and r["max_drawdown"] >= MAX_DRAWDOWN_LIMIT]
    pool = qualified if qualified else results
    best = max(pool, key=lambda r: r["total_return"])

    entry = {
        **best,
        "qualified": bool(qualified),
        "bars": len(df),
        "range": f"{str(df['date'].iloc[0])[:10]}~{str(df['date'].iloc[-1])[:10]}",
        "optimized_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "grid": sorted(results, key=lambda r: -r["total_return"]),
    }
    save_best(symbol, entry)
    app_logger.info(
        "optimized %s: K<%d买 K>%d卖 return=%+.1f%% dd=%.1f%% trips=%d qualified=%s",
        symbol, best["buy"], best["sell"], best["total_return"] * 100,
        best["max_drawdown"] * 100, best["round_trips"], bool(qualified),
    )
    return entry


def optimize_symbol_async(symbol: str, start_date: str = "2010-01-01") -> bool:
    """后台线程寻优；已在寻优中则跳过。返回是否成功启动。"""
    with _pending_lock:
        if symbol in _pending:
            return False
        _pending.add(symbol)

    def _run():
        try:
            optimize_symbol(symbol, start_date)
        except Exception as exc:
            app_logger.warning("optimize %s failed: %s", symbol, exc)
        finally:
            with _pending_lock:
                _pending.discard(symbol)

    threading.Thread(target=_run, daemon=True).start()
    return True


def is_pending(symbol: str) -> bool:
    with _pending_lock:
        return symbol in _pending
