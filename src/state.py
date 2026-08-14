from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Optional

from .config import load_config, save_config


class AppState:
    def __init__(self) -> None:
        self._lock = Lock()
        self.config = load_config()
        self.symbols = list(self.config.get("symbols", []))
        self.current_symbol = self.symbols[0]["code"] if self.symbols else ""
        self.latest: dict[str, dict[str, Any]] = {}
        self.series: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.alerts: list[dict[str, Any]] = []
        self.cooldowns: dict[str, datetime] = {}
        self.alert_zones: dict[str, str] = {}

    def _alerts_for_date(self, date_text: str) -> list[dict[str, Any]]:
        return [alert for alert in self.alerts if str(alert.get("created_at", "")).startswith(date_text)]

    def snapshot(self) -> dict[str, Any]:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            alert_dates = sorted(
                {str(alert.get("created_at", ""))[:10] for alert in self.alerts if alert.get("created_at")},
                reverse=True,
            )
            return {
                "symbols": list(self.symbols),
                "current_symbol": self.current_symbol,
                "latest": self.latest,
                "series": self.series,
                "alerts": list(reversed(self._alerts_for_date(today)[-100:])),
                "alert_dates": alert_dates,
                "config": {
                    "poll_interval_seconds": self.config.get("poll_interval_seconds"),
                    "timeframes": self.config.get("timeframes", []),
                    "kdj": self.config.get("kdj", {}),
                    "web": self.config.get("web", {}),
                    "trade_plan": self.config.get("trade_plan", {}),
                },
            }

    def alerts_for_date(self, date_text: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._alerts_for_date(date_text)[-100:]))

    def add_symbol(self, code: str, name: Optional[str] = None) -> dict[str, str]:
        normalized = code.strip()
        if not normalized:
            raise ValueError("股票代码不能为空")

        with self._lock:
            existing = next((item for item in self.symbols if item["code"] == normalized), None)
            if existing:
                self.current_symbol = normalized
                return existing

            symbol = {"code": normalized, "name": name or normalized}
            self.symbols.append(symbol)
            self.current_symbol = normalized
            self.config["symbols"] = self.symbols
            save_config(self.config)
            return symbol

    def remove_symbol(self, code: str) -> None:
        with self._lock:
            self.symbols = [item for item in self.symbols if item["code"] != code]
            self.config["symbols"] = self.symbols
            if self.current_symbol == code:
                self.current_symbol = self.symbols[0]["code"] if self.symbols else ""
            self.latest.pop(code, None)
            self.series.pop(code, None)
            save_config(self.config)

    def switch_symbol(self, code: str) -> None:
        with self._lock:
            if not any(item["code"] == code for item in self.symbols):
                raise ValueError("股票不在观察列表中")
            self.current_symbol = code

    def update_latest(self, symbol: str, timeframe: str, data: dict[str, Any]) -> None:
        with self._lock:
            self.latest.setdefault(symbol, {})[timeframe] = data

    def report_trade(self, code: str, side: str, lots: int, price: Optional[float] = None,
                     note: Optional[str] = None) -> dict[str, Any]:
        side = side.strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side 必须是 buy 或 sell")
        if lots <= 0:
            raise ValueError("lots 必须大于 0")

        with self._lock:
            trade_plan = self.config.setdefault("trade_plan", {})
            positions = trade_plan.setdefault("positions", {})
            position_key = next((key for key in positions if str(key) == code), code)
            pos = positions.setdefault(position_key, {})

            base_lots = int(pos.get("base_lots", 0))
            base_remaining = int(pos.get("base_lots_remaining", base_lots))
            t_lots_held = int(pos.get("t_lots_held", 0))

            if side == "buy":
                t_lots_held += lots
            else:
                sell_from_t = min(t_lots_held, lots)
                t_lots_held -= sell_from_t
                base_remaining = max(0, base_remaining - (lots - sell_from_t))

            pos["base_lots"] = base_lots
            pos["base_lots_remaining"] = base_remaining
            pos["t_lots_held"] = t_lots_held
            reported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fee = float(pos.get("fee_per_lot", 5)) * lots
            report = {
                "side": side,
                "lots": lots,
                "price": price,
                "fee": round(fee, 2),
                "note": note,
                "reported_at": reported_at,
            }
            # Keep an append-only record so next-day guidance can use today's
            # actual executions instead of inferring them from current totals.
            history = pos.setdefault("trade_history", [])
            history.append(report)
            pos["trade_history"] = history[-500:]
            pos["last_report"] = report
            self.config["trade_plan"] = trade_plan
            save_config(self.config)
            return pos

    def update_position(self, code: str, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            trade_plan = self.config.setdefault("trade_plan", {})
            positions = trade_plan.setdefault("positions", {})
            position_key = next((key for key in positions if str(key) == code), code)
            pos = positions.setdefault(position_key, {})
            pos.update(kwargs)
            if "base_lots" in kwargs and "base_lots_remaining" not in pos:
                pos["base_lots_remaining"] = int(kwargs["base_lots"])
            self.config["trade_plan"] = trade_plan
            save_config(self.config)
            return pos

    def update_series(self, symbol: str, timeframe: str, data: list[dict[str, Any]]) -> None:
        with self._lock:
            self.series.setdefault(symbol, {})[timeframe] = data

    def should_alert(self, key: str, direction: str, cooldown_seconds: int) -> bool:
        now = datetime.now()
        with self._lock:
            current_zone = self.alert_zones.get(key)
            if current_zone == direction:
                return False

            last_time = self.cooldowns.get(key)
            if last_time and now - last_time < timedelta(seconds=cooldown_seconds):
                return False

            self.alert_zones[key] = direction
            self.cooldowns[key] = now
            return True

    def clear_alert_zone(self, key: str) -> None:
        with self._lock:
            self.alert_zones.pop(key, None)

    def add_alert(self, alert: dict[str, Any]) -> None:
        with self._lock:
            self.alerts.append(alert)
            self.alerts = self.alerts[-500:]


state = AppState()
