#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT_DIR/deploy/systemd/kdj-alert.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/kdj-alert.service"

if [[ ! -f "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "未找到 $ROOT_DIR/.venv/bin/python" >&2
  echo "请先在项目目录执行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  echo "未找到 $ROOT_DIR/.env" >&2
  echo "请先创建 .env，并写入 KDJ_EMAIL_PASSWORD / KDJ_PUSHPLUS_TOKEN 等环境变量。" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
install -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl --user daemon-reload
systemctl --user enable --now kdj-alert.service

cat <<'MSG'

已安装并启动 kdj-alert.service。

常用命令：
  systemctl --user status kdj-alert.service
  journalctl --user -u kdj-alert.service -f
  systemctl --user restart kdj-alert.service
  systemctl --user stop kdj-alert.service

如需退出 SSH 后仍保持用户服务运行，请执行一次：
  sudo loginctl enable-linger "$USER"
MSG
