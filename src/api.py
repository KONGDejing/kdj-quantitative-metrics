from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from .config import WEB_DIR
from .runner import monitor_loop, run_once
from .state import state


class SymbolPayload(BaseModel):
    code: str
    name: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(monitor_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="KDJ Quantitative Metrics", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/status")
def status():
    return state.snapshot()


@app.get("/api/alerts")
def alerts(date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
    return {"date": date, "alerts": state.alerts_for_date(date)}


@app.post("/api/symbols")
def add_symbol(payload: SymbolPayload):
    try:
        result = state.add_symbol(payload.code, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 新添加的标的后台自动寻优最优K值区间
    from .optimizer import optimize_symbol_async
    optimize_symbol_async(payload.code)
    return result


@app.get("/api/best-params")
def best_params(symbol: Optional[str] = Query(None)):
    """查询已保存的最优参数绑定；传 symbol 只查单只。"""
    from .optimizer import get_best, is_pending, load_best_params
    if symbol:
        return {"symbol": symbol, "best": get_best(symbol), "optimizing": is_pending(symbol)}
    return load_best_params()


@app.post("/api/optimize/{symbol}")
def optimize(symbol: str):
    """手动触发（重新）寻优，后台执行。"""
    from .optimizer import optimize_symbol_async
    started = optimize_symbol_async(symbol)
    return {"ok": True, "started": started}


@app.delete("/api/symbols/{code}")
def remove_symbol(code: str):
    state.remove_symbol(code)
    return {"ok": True}


@app.post("/api/current-symbol")
def switch_symbol(payload: SymbolPayload):
    try:
        state.switch_symbol(payload.code)
        return {"ok": True, "current_symbol": payload.code}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/run-once")
def trigger_run_once():
    run_once()
    return {"ok": True}


@app.get("/api/backtest")
def backtest(symbol: str = Query(...),
             timeframe: str = Query("1d", pattern=r"^(15m|30m|60m|1d)$"),
             buy: float = Query(10, ge=1, le=50),
             sell: float = Query(85, ge=50, le=99),
             start: str = Query("2010-01-01"),
             auto: bool = Query(False)):
    """KDJ机械策略回测：K<buy 买入，K>sell 卖出，信号次根K线开盘价成交。

    auto=true 时使用该标的已保存的最优参数（日线，自动寻优结果）。"""
    from .backtest import run_backtest
    from .data_provider import fetch_backtest_kline
    from .optimizer import get_best, optimize_symbol_async

    if auto:
        best = get_best(symbol)
        if best is None:
            # 尚未寻优过：后台启动寻优，本次先用默认参数，并提示前端稍后重试
            optimize_symbol_async(symbol)
            raise HTTPException(status_code=202,
                                detail="该标的尚未寻优，已启动自动寻优，约30秒后请重试")
        buy, sell = best["buy"], best["sell"]
        if timeframe != "1d":
            # 最优参数基于日线寻优，分钟周期直接复用阈值
            pass

    try:
        data = fetch_backtest_kline(symbol, timeframe, start)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"行情数据获取失败: {exc}") from exc
    if data is None or data.empty:
        raise HTTPException(status_code=404, detail=f"未获取到 {symbol} 的 {timeframe} 行情数据")
    result = run_backtest(data, buy_threshold=buy, sell_threshold=sell)
    result["symbol"] = symbol
    result["timeframe"] = timeframe
    result["params"] = {"buy": buy, "sell": sell}
    result["params_auto"] = bool(auto)
    if auto:
        result["best_meta"] = {k: best[k] for k in
                               ("total_return", "max_drawdown", "round_trips", "win_rate",
                                "range", "optimized_at") if k in best}
    if timeframe == "1d":
        from . import data_provider
        result["data_source"] = data_provider.last_backtest_source
        if data_provider.last_backtest_warning:
            result["warning"] = data_provider.last_backtest_warning
    return result


# ---------------------------------------------------------------------------
# 波段买卖分析（中航光电 002179）
# ---------------------------------------------------------------------------

@app.post("/api/trade-report")
def trade_report(payload: dict):
    """记录人工买卖结果，用于次日 T+1 操作指引计算可买/可卖仓位。"""
    code = str(payload.get("code", "")).strip()
    side = str(payload.get("side", "")).strip()
    lots = int(payload.get("lots", 0))
    price = payload.get("price")
    note = payload.get("note")
    if not code:
        raise HTTPException(status_code=400, detail="code 不能为空")
    try:
        result = state.report_trade(code, side, lots, price=price, note=note)
        return {"ok": True, "position": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/band-analysis/optimal")
def band_optimal(symbol: str = Query("002179"),
                 start_date: str = Query("2010-01-01"),
                 top_n: int = Query(5, ge=1, le=20)):
    """搜索指定时间段内的最优 (B, S) 波段组合。"""
    from .band_analysis import find_optimal
    try:
        return find_optimal(symbol, start_date=start_date, top_n=top_n)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"分析失败: {exc}") from exc


@app.get("/api/band-analysis/detail")
def band_detail(symbol: str = Query("002179"),
                B: float = Query(..., description="买入价位"),
                S: float = Query(..., description="卖出价位"),
                start_date: str = Query("2010-01-01")):
    """对指定 (B, S) 进行模拟，返回完整交易明细。"""
    from .band_analysis import simulate_detail
    if B >= S:
        raise HTTPException(status_code=400, detail="B 必须小于 S")
    try:
        return simulate_detail(symbol, B=B, S=S, start_date=start_date)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"模拟失败: {exc}") from exc
