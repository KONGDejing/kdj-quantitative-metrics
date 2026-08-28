from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from threading import Lock
from typing import Any, Optional
from uuid import uuid4

from .config import load_config, save_config
from .runtime_state import add_correction_audit, load_runtime_state, save_monitor_state
from .trade_ledger import apply_ledger_summary, replay_position


class AppState:
    def __init__(self) -> None:
        self._lock = Lock()
        self.config = load_config()
        self.symbols = list(self.config.get("symbols", []))
        configured_default = str((self.config.get("web") or {}).get("default_symbol") or "")
        self.current_symbol = (
            configured_default
            if any(str(item.get("code")) == configured_default for item in self.symbols)
            else (self.symbols[0]["code"] if self.symbols else "")
        )
        self.latest: dict[str, dict[str, Any]] = {}
        self.series: dict[str, dict[str, list[dict[str, Any]]]] = {}
        persisted = load_runtime_state()
        self.alerts: list[dict[str, Any]] = list(persisted.get("alerts") or [])[-2000:]
        self.cooldowns: dict[str, datetime] = {}
        for key, value in (persisted.get("cooldowns") or {}).items():
            try:
                self.cooldowns[str(key)] = datetime.fromisoformat(str(value))
            except ValueError:
                continue
        self.alert_zones: dict[str, str] = {
            str(key): str(value) for key, value in (persisted.get("alert_zones") or {}).items()
        }
        if self._ensure_trade_ids():
            save_config(self.config)

    @staticmethod
    def _legacy_trade_id(code: str, trade: dict[str, Any], index: int) -> str:
        raw = "|".join([
            str(code), str(index), str(trade.get("reported_at") or ""),
            str(trade.get("side") or ""), str(trade.get("lots") or ""), str(trade.get("price") or ""),
        ])
        return f"legacy-{sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    def _ensure_trade_ids(self) -> bool:
        changed = False
        positions = ((self.config.get("trade_plan") or {}).get("positions") or {})
        for code, position in positions.items():
            history = position.get("trade_history") or []
            for index, trade in enumerate(history):
                if not trade.get("id"):
                    trade["id"] = self._legacy_trade_id(str(code), trade, index)
                    changed = True
            if history:
                position["last_report"] = history[-1]
        return changed

    def _persist_monitor_state(self) -> None:
        save_monitor_state(alerts=self.alerts, cooldowns=self.cooldowns, alert_zones=self.alert_zones)

    def _alerts_for_date(self, date_text: str) -> list[dict[str, Any]]:
        return [alert for alert in self.alerts if str(alert.get("created_at", "")).startswith(date_text)]

    def snapshot(self) -> dict[str, Any]:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            positions = ((self.config.get("trade_plan") or {}).get("positions") or {})
            ledgers = {
                str(code): replay_position(position, as_of=today)
                for code, position in positions.items()
            }
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
                    "price_alerts": self.config.get("price_alerts", {}),
                    "web": self.config.get("web", {}),
                    "trade_plan": self.config.get("trade_plan", {}),
                    "trade_ledgers": ledgers,
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
                normalized_name = str(name or "").strip()
                if normalized_name and normalized_name != str(existing.get("name") or ""):
                    existing["name"] = normalized_name
                    self.config["symbols"] = self.symbols
                    save_config(self.config)
                    add_correction_audit(normalized, "symbol", ["name"], "replace")
                return existing

            symbol = {"code": normalized, "name": name or normalized}
            self.symbols.append(symbol)
            self.current_symbol = normalized
            self.config["symbols"] = self.symbols
            save_config(self.config)
            return symbol

    def update_symbol_name(self, code: str, name: str) -> dict[str, str]:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("股票名称不能为空")
        with self._lock:
            symbol = next((item for item in self.symbols if str(item.get("code")) == str(code)), None)
            if symbol is None:
                raise ValueError("股票不在观察列表中")
            symbol["name"] = normalized_name
            self.config["symbols"] = self.symbols
            save_config(self.config)
        add_correction_audit(str(code), "symbol", ["name"], "replace")
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
                     note: Optional[str] = None, bucket: str = "auto") -> dict[str, Any]:
        side = side.strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side 必须是 buy 或 sell")
        if lots <= 0:
            raise ValueError("lots 必须大于 0")
        if price is None or float(price) <= 0:
            raise ValueError("price 必须是有效成交价")

        with self._lock:
            trade_plan = self.config.setdefault("trade_plan", {})
            positions = trade_plan.setdefault("positions", {})
            position_key = next((key for key in positions if str(key) == code), code)
            pos = positions.setdefault(position_key, {})

            reported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fee = float(pos.get("fee_per_lot", 5)) * lots
            report = {
                "id": uuid4().hex,
                "side": side,
                "lots": lots,
                "price": float(price),
                "fee": round(fee, 2),
                "note": note,
                "reported_at": reported_at,
            }
            normalized_bucket = str(bucket or "auto").strip().lower()
            if normalized_bucket not in {"auto", "core", "tactical"}:
                raise ValueError("bucket 必须是 auto、core 或 tactical")
            if normalized_bucket != "auto":
                report["bucket"] = normalized_bucket
            # Keep an append-only record so next-day guidance can use today's
            # actual executions instead of inferring them from current totals.
            candidate = deepcopy(pos)
            candidate_history = list(candidate.get("trade_history") or [])
            candidate_history.append(report)
            candidate["trade_history"] = candidate_history[-500:]
            ledger = replay_position(candidate, as_of=reported_at[:10], strict=True)

            pos["trade_history"] = candidate["trade_history"]
            pos["last_report"] = report
            apply_ledger_summary(pos, ledger)
            self.config["trade_plan"] = trade_plan
            save_config(self.config)
            return {**pos, "ledger": ledger}

    def correct_trade(
        self,
        code: str,
        trade_id: str,
        *,
        replacement: Optional[dict[str, Any]] = None,
        delete: bool = False,
    ) -> dict[str, Any]:
        replacement = replacement or {}
        allowed = {"side", "lots", "price", "fee", "note", "bucket", "reported_at"}
        unexpected = set(replacement) - allowed
        if unexpected:
            raise ValueError(f"不允许纠正字段：{', '.join(sorted(unexpected))}")
        if delete and replacement:
            raise ValueError("删除成交时不能同时提交替换字段")
        if not delete and not replacement:
            raise ValueError("至少提供一个需要纠正的字段")

        with self._lock:
            positions = ((self.config.get("trade_plan") or {}).get("positions") or {})
            position_key = next((key for key in positions if str(key) == str(code)), None)
            if position_key is None:
                raise ValueError("股票没有配置交易账本")
            pos = positions[position_key]
            history = deepcopy(pos.get("trade_history") or [])
            index = next((i for i, item in enumerate(history) if str(item.get("id")) == str(trade_id)), None)
            if index is None:
                raise ValueError("没有找到指定成交ID")
            changed_fields = list(replacement)
            if delete:
                history.pop(index)
                changed_fields = ["trade"]
            else:
                corrected = {**history[index], **replacement, "id": str(trade_id)}
                if "lots" in replacement and "fee" not in replacement:
                    corrected["fee"] = float(pos.get("fee_per_lot", 5) or 5) * float(corrected["lots"])
                if "side" in corrected:
                    corrected["side"] = str(corrected["side"]).strip().lower()
                if "bucket" in corrected:
                    bucket = str(corrected["bucket"] or "auto").strip().lower()
                    if bucket not in {"auto", "core", "tactical"}:
                        raise ValueError("bucket 必须是 auto、core 或 tactical")
                    if bucket == "auto":
                        corrected.pop("bucket", None)
                    else:
                        corrected["bucket"] = bucket
                history[index] = corrected

            candidate = deepcopy(pos)
            candidate["trade_history"] = history
            ledger = replay_position(candidate, as_of=datetime.now().strftime("%Y-%m-%d"), strict=True)
            pos["trade_history"] = history
            pos["last_report"] = history[-1] if history else None
            apply_ledger_summary(pos, ledger)
            save_config(self.config)
            result = {**pos, "ledger": ledger}

        add_correction_audit(str(code), str(trade_id), changed_fields, "delete" if delete else "replace")
        return result

    def trade_ledgers(self, code: Optional[str] = None, as_of: Optional[str] = None) -> dict[str, Any]:
        cutoff = as_of or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            positions = ((self.config.get("trade_plan") or {}).get("positions") or {})
            result = {
                str(key): replay_position(position, as_of=cutoff)
                for key, position in positions.items()
                if code is None or str(key) == str(code)
            }
        if code is not None and str(code) not in result:
            raise ValueError("股票没有配置交易账本")
        return result

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
            self._persist_monitor_state()
            return True

    def clear_alert_zone(self, key: str) -> None:
        with self._lock:
            removed = self.alert_zones.pop(key, None)
            if removed is not None:
                self._persist_monitor_state()

    def add_alert(self, alert: dict[str, Any]) -> None:
        with self._lock:
            self.alerts.append(alert)
            self.alerts = self.alerts[-2000:]
            self._persist_monitor_state()


state = AppState()
