from __future__ import annotations

import os
import secrets
from hmac import compare_digest
from pathlib import Path
from threading import Lock
from typing import Optional

from .config import BASE_DIR


TOKEN_PATH = BASE_DIR / "runtime" / "api_write_token"
_lock = Lock()


def get_write_token(*, path: Path = TOKEN_PATH) -> str:
    configured = os.environ.get("KDJ_API_WRITE_TOKEN", "").strip()
    if configured:
        return configured
    with _lock:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        path.write_text(token + "\n", encoding="utf-8")
        path.chmod(0o600)
        return token


def verify_write_token(provided: Optional[str], *, path: Path = TOKEN_PATH) -> bool:
    if not provided:
        return False
    return compare_digest(str(provided).strip(), get_write_token(path=path))


def auth_status() -> dict[str, object]:
    get_write_token()
    return {
        "write_token_required": True,
        "header": "X-API-Key",
        "token_source": "environment" if os.environ.get("KDJ_API_WRITE_TOKEN", "").strip() else "runtime_file",
        "token_file": str(TOKEN_PATH),
    }
