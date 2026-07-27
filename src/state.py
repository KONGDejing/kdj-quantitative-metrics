from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock
from typing import Any

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
                },
            }

    def alerts_for_date(self, date_text: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._alerts_for_date(date_text)[-100:]))

    def add_symbol(self, code: str, name: str | None = None) -> dict[str, str]:
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
