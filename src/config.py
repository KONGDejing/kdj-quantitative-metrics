from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_DIR = BASE_DIR / "logs"
WEB_DIR = BASE_DIR / "web"
DOTENV_PATH = BASE_DIR / ".env"


def _load_dotenv() -> None:
    if not DOTENV_PATH.exists():
        return

    for raw_line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
