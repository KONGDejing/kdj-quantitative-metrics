from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from .config import WEB_DIR
from .runner import monitor_loop, run_once
from .state import DuplicateTradeError, state


class SymbolPayload(BaseModel):
    code: str
    name: Optional[str] = None


class TradeCorrectionPayload(BaseModel):
    code: str
    trade_id: str
    replacement: Optional[dict] = None
    delete: bool = False
    confirm: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(monitor_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="KDJ Quantitative Metrics", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def _is_unprotected_view_action(request: Request) -> bool:
    """Actions that only change the in-memory UI view, never config or trades."""
    return request.method.upper() == "POST" and request.url.path == "/api/current-symbol"


def _write_token_required() -> bool:
    return bool((state.config.get("web") or {}).get("require_write_token", True))


@app.middleware("http")
async def protect_write_apis(request: Request, call_next):
    is_write = request.url.path.startswith("/api/") and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    if is_write and _write_token_required() and not _is_unprotected_view_action(request):
        from .auth import verify_write_token
        provided = request.headers.get("X-API-Key")
        if not verify_write_token(provided):
            return JSONResponse(status_code=401, content={"detail": "写入操作需要有效的X-API-Key"})
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/status")
def status():
    return state.snapshot()


@app.get("/api/auth/status")
def write_auth_status():
    from .auth import auth_status
    return auth_status(required=_write_token_required())


@app.post("/api/auth/verify")
def verify_write_access():
    """The write-auth middleware has already verified the supplied token."""
    return {"ok": True}


@app.get("/api/runtime-status")
def persisted_runtime_status():
    from .runtime_state import runtime_status
    from .trading_calendar import calendar_status, next_session
    result = runtime_status()
    result["calendar"] = calendar_status(datetime.now(), state.config)
    result["next_session"] = next_session(datetime.now(), state.config)
    return result


@app.get("/api/trade-ledger")
def trade_ledger(symbol: Optional[str] = Query(None), as_of: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    try:
        return {"ledgers": state.trade_ledgers(symbol, as_of=as_of)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/decision-plan")
def decision_plan(symbol: str = Query(...)):
    from .decision_engine import build_decision_plan
    from .performance_store import get_performance
    from .runner import is_trading_time

    code = str(symbol)
    configured_positions = (((state.config.get("trade_plan") or {}).get("positions")) or {})
    position_key = next((key for key in configured_positions if str(key) == code), None)
    if position_key is None:
        raise HTTPException(status_code=404, detail="该标的没有配置交易计划")
    symbol_info = next((item for item in state.symbols if str(item.get("code")) == code), None)
    if not symbol_info:
        raise HTTPException(status_code=404, detail="该标的不在观察列表")
    latest_daily = ((state.latest.get(code) or {}).get("1d"))
    daily_series = ((state.series.get(code) or {}).get("1d")) or []
    if not latest_daily or not daily_series:
        raise HTTPException(status_code=503, detail="正式日线尚未加载")
    return build_decision_plan(
        symbol_code=code,
        symbol_name=str(symbol_info.get("name") or code),
        latest_daily=latest_daily,
        daily_series=daily_series,
        position=configured_positions[position_key],
        decision_date=datetime.now().strftime("%Y-%m-%d"),
        performance_state=get_performance(code)["summary"],
        intraday_series=((state.series.get(code) or {}).get("10m")) or [],
        intraday_execution_enabled=is_trading_time(),
    )


@app.get("/api/performance")
def strategy_performance(symbol: str = Query(...)):
    from .performance_store import get_performance
    result = get_performance(str(symbol))
    if not result["snapshots"]:
        raise HTTPException(status_code=404, detail="该标的还没有策略净值快照")
    return result


@app.get("/api/shadow-decisions")
def shadow_decisions(symbol: Optional[str] = Query(None)):
    from .shadow_tracker import get_shadow_decisions
    return get_shadow_decisions(str(symbol) if symbol is not None else None)


@app.get("/api/research/walk-forward")
def walk_forward_report(symbol: str = Query("002179")):
    from .walk_forward import load_report
    report = load_report(str(symbol))
    if report is None:
        raise HTTPException(status_code=404, detail="该标的还没有walk-forward报告")
    return report


@app.post("/api/research/walk-forward/{symbol}")
def refresh_walk_forward(symbol: str):
    from .walk_forward import run_and_save
    try:
        return run_and_save(str(symbol))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"walk-forward研究失败: {exc}") from exc


@app.get("/api/research/stage-capital")
def stage_capital_report(symbol: str = Query("002179")):
    from .stage_research import load_stage_report
    report = load_stage_report(str(symbol))
    if report is None:
        raise HTTPException(status_code=404, detail="该标的还没有分阶段资金曲线报告")
    return report


@app.post("/api/research/stage-capital/{symbol}")
def refresh_stage_capital(symbol: str):
    from .stage_research import refresh_stage_report
    positions = (((state.config.get("trade_plan") or {}).get("positions")) or {})
    key = next((item for item in positions if str(item) == str(symbol)), None)
    if key is None:
        raise HTTPException(status_code=404, detail="股票没有配置交易计划")
    try:
        return refresh_stage_report(str(symbol), positions[key])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"分阶段资金曲线研究失败: {exc}") from exc


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


@app.patch("/api/symbols/{code}")
def correct_symbol_name(code: str, payload: SymbolPayload):
    try:
        return state.update_symbol_name(str(code), str(payload.name or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    bucket = str(payload.get("bucket", "auto"))
    confirm_duplicate = bool(payload.get("confirm_duplicate", False))
    if not code:
        raise HTTPException(status_code=400, detail="code 不能为空")
    try:
        result = state.report_trade(
            code, side, lots, price=price, note=note, bucket=bucket,
            confirm_duplicate=confirm_duplicate,
        )
        return {"ok": True, "position": result}
    except DuplicateTradeError as exc:
        existing = exc.existing_trade
        raise HTTPException(status_code=409, detail={
            "code": "duplicate_trade",
            "message": str(exc),
            "existing_trade": {
                key: existing.get(key)
                for key in ("id", "side", "lots", "price", "bucket", "reported_at")
            },
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/trades")
def trades(symbol: str = Query(...)):
    positions = (((state.config.get("trade_plan") or {}).get("positions")) or {})
    key = next((item for item in positions if str(item) == str(symbol)), None)
    if key is None:
        raise HTTPException(status_code=404, detail="股票没有配置交易账本")
    return {"symbol": str(symbol), "trades": list(positions[key].get("trade_history") or [])}


@app.post("/api/trade-corrections")
def correct_trade(payload: TradeCorrectionPayload):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="纠正成交必须显式confirm=true")
    try:
        result = state.correct_trade(
            str(payload.code), str(payload.trade_id), replacement=payload.replacement, delete=payload.delete
        )
        return {"ok": True, "position": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/band-analysis/periods")
def band_periods(symbol: str = Query("002179")):
    """Return only research windows covered by the symbol's real history."""
    from .band_analysis import available_periods
    try:
        return available_periods(symbol)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取历史范围失败: {exc}") from exc


@app.get("/api/band-analysis/optimal")
def band_optimal(symbol: str = Query("002179"),
                 start_date: Optional[str] = Query(None),
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
                start_date: Optional[str] = Query(None)):
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
