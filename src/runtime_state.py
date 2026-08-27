from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from .config import BASE_DIR


RUNTIME_STATE_PATH = BASE_DIR / "runtime" / "app_state.json"
STATE_VERSION = 1
_lock = RLock()


def _empty() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "alerts": [],
        "cooldowns": {},
        "alert_zones": {},
        "tasks": {},
        "correction_audit": [],
    }


def _load_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    result = _empty()
    result.update(data)
    return result


def _save_unlocked(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_runtime_state(*, path: Path = RUNTIME_STATE_PATH) -> dict[str, Any]:
    with _lock:
        return deepcopy(_load_unlocked(path))


def save_monitor_state(
    *,
    alerts: list[dict[str, Any]],
    cooldowns: dict[str, datetime],
    alert_zones: dict[str, str],
    path: Path = RUNTIME_STATE_PATH,
) -> None:
    with _lock:
        data = _load_unlocked(path)
        data["alerts"] = deepcopy(alerts[-2000:])
        data["cooldowns"] = {key: value.isoformat() for key, value in cooldowns.items()}
        data["alert_zones"] = dict(alert_zones)
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_unlocked(data, path)


def task_channel_complete(task: str, day: str, channel: str, *, path: Path = RUNTIME_STATE_PATH) -> bool:
    with _lock:
        data = _load_unlocked(path)
        task_data = ((data.get("tasks") or {}).get(f"{day}:{task}") or {})
        return bool((task_data.get("channels") or {}).get(channel, {}).get("ok"))


def mark_task_channel(
    task: str,
    day: str,
    channel: str,
    ok: bool,
    *,
    detail: Optional[str] = None,
    path: Path = RUNTIME_STATE_PATH,
) -> None:
    with _lock:
        data = _load_unlocked(path)
        tasks = data.setdefault("tasks", {})
        task_data = tasks.setdefault(f"{day}:{task}", {"task": task, "date": day, "channels": {}})
        task_data.setdefault("channels", {})[channel] = {
            "ok": bool(ok),
            "attempted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "detail": detail,
        }
        task_data["complete"] = bool(task_data["channels"]) and all(
            bool(value.get("ok")) for value in task_data["channels"].values()
        )
        # Keep roughly three years of daily task history.
        for old_key in sorted(tasks)[:-4000]:
            tasks.pop(old_key, None)
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_unlocked(data, path)


def task_complete(task: str, day: str, channels: list[str], *, path: Path = RUNTIME_STATE_PATH) -> bool:
    if not channels:
        return False
    return all(task_channel_complete(task, day, channel, path=path) for channel in channels)


def add_correction_audit(
    symbol: str,
    trade_id: str,
    fields_changed: list[str],
    action: str,
    *,
    path: Path = RUNTIME_STATE_PATH,
) -> None:
    """Keep only audit metadata; never retain superseded erroneous trade values."""
    with _lock:
        data = _load_unlocked(path)
        audit = data.setdefault("correction_audit", [])
        audit.append({
            "symbol": str(symbol),
            "trade_id": str(trade_id),
            "action": str(action),
            "fields_changed": sorted(set(fields_changed)),
            "corrected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        data["correction_audit"] = audit[-500:]
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_unlocked(data, path)


def runtime_status(*, path: Path = RUNTIME_STATE_PATH) -> dict[str, Any]:
    with _lock:
        data = _load_unlocked(path)
        return {
            "updated_at": data.get("updated_at"),
            "persisted_alerts": len(data.get("alerts") or []),
            "persisted_tasks": len(data.get("tasks") or {}),
            "corrections": len(data.get("correction_audit") or []),
            "tasks": deepcopy(data.get("tasks") or {}),
            "correction_audit": deepcopy(data.get("correction_audit") or []),
        }
