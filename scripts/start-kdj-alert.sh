#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs

# .env 内保存 KDJ_EMAIL_PASSWORD / KDJ_PUSHPLUS_TOKEN 等敏感环境变量。
# 这里用 source 加载，兼容 `export KEY=value` 和 `KEY=value` 两种写法。
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
else
  echo "WARN: $ROOT_DIR/.env not found; notification credentials may be unavailable" >&2
fi

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3 || command -v python)"
fi

exec "$PYTHON" "$ROOT_DIR/main.py"
