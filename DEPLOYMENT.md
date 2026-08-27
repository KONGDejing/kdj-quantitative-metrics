# 部署与守护进程说明

本文档记录本项目的长期后台运行方案，目标是避免 SSH 断开、终端关闭或程序异常退出后服务无人值守地停止。

## 推荐方案：systemd user service

本项目现在推荐使用 **systemd 用户服务**托管，而不是继续使用 `nohup`。

原因：

- `Restart=always`：Python 进程异常退出后自动重启。
- `enable --now`：开机/用户服务启动时自动拉起。
- 统一日志：可用 `journalctl --user -u kdj-alert.service -f` 查看服务 stdout/stderr，同时项目自身仍写 `logs/app.log`。
- 启动脚本会自动加载 `.env`，避免忘记 `source .env` 导致邮件/微信 token 丢失。

## 服务器环境建议

线上建议使用独立虚拟环境，不要直接混用 Anaconda base 环境。Python 3.9 可以运行本项目，但要避免 NumPy 2 与旧二进制包（如 `numexpr`、`bottleneck`）不兼容。

当前 `requirements.txt` 已固定：

```text
numpy<2
pandas<2.2
```

如果日志中出现 `_ARRAY_API not found` 或 `A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x`，说明环境里 NumPy / pandas / numexpr / bottleneck ABI 不匹配，按下面方式修复：

```bash
cd /path/to/quantitative-metrics
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/pip install -r requirements.txt
source .env && nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
```

`nohup: ignoring input` 只是 nohup 的提示，不是错误；真正导致服务退出的是后面的 Python Traceback。

如果必须继续使用现有 Anaconda 环境，则执行：

```bash
python3 -m pip install -U --force-reinstall "numpy<2" "pandas<2.2" numexpr bottleneck
```

## 一键安装

先确认依赖和私有配置已准备好：

```bash
cd /data/kongdejing/workspace/kdj/quantitative-metrics
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml，并创建 .env 写入 KDJ_EMAIL_PASSWORD / KDJ_PUSHPLUS_TOKEN
```

安装并启动用户服务：

```bash
chmod +x scripts/*.sh
./scripts/install-systemd-user.sh
```

如果希望退出 SSH 后用户服务仍继续运行，请执行一次：

```bash
sudo loginctl enable-linger "$USER"
```

实际访问端口以 `config.yaml` 的 `web.port` 为准；排查端口占用时不要写死端口号。

## 常用运维命令

```bash
# 查看状态
systemctl --user status kdj-alert.service

# 实时查看 systemd 日志
journalctl --user -u kdj-alert.service -f

# 修改代码或配置后重启
systemctl --user restart kdj-alert.service

# 停止服务
systemctl --user stop kdj-alert.service

# 禁用开机/用户服务启动自动拉起
systemctl --user disable kdj-alert.service
```

查看Web/API写入令牌（只在可信终端执行，不要发送到聊天或截图）：

```bash
./scripts/show-write-token.sh
```

也可以在`.env`中设置`KDJ_API_WRITE_TOKEN`后重启服务。页面查询不需要令牌；成交录入、纠错、观察池修改、手动研究刷新等写操作必须携带`X-API-Key`。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `deploy/systemd/kdj-alert.service` | systemd 用户服务单元，配置自动重启、工作目录和日志输出。 |
| `scripts/start-kdj-alert.sh` | 统一启动入口：进入项目目录、创建 `logs/`、加载 `.env`、优先使用 `.venv/bin/python` 启动 `main.py`。 |
| `scripts/install-systemd-user.sh` | 安装服务单元到 `~/.config/systemd/user/` 并执行 `enable --now`。 |

## 注意事项

- 修改代码后必须执行 `systemctl --user restart kdj-alert.service`，旧进程不会自动加载新代码。
- `.env` 不提交 git，但生产服务依赖它提供 `KDJ_EMAIL_PASSWORD`、`KDJ_PUSHPLUS_TOKEN` / `KDJ_PUSHPLUS_TOKENS`。
- `config.yaml`、`best_params.json`、`cache/`、`logs/`、`runtime/` 都是本地运行产物，不应提交。`config.yaml`和`runtime/`共同构成成交与策略状态，建议定期做受控备份。
- 如果服务反复重启，先看：

```bash
journalctl --user -u kdj-alert.service -n 100 --no-pager
```

常见原因是 `.venv` 依赖未安装、`config.yaml` 缺失、`.env` 缺失或 `web.port` 配置的端口已被旧进程占用。

## 当前运行态注意事项

- KDJ和K线仍在进程内存中并在重启后自动回填；提醒、冷却和每日发送结果已保存在`runtime/app_state.json`。
- 当前交易所休市日历覆盖2026年。跨到新年份前必须依据交易所年度公告更新`src/trading_calendar.py`；未知年份会保守停止定时任务，可从`/api/runtime-status`检查。
- LLM 次日指引可能经历多次长超时。排障时查看 `logs/app.log` 中的 `LLM API retry`，不要仅根据服务仍处于 `active` 判断任务成功。
- 服务监听`0.0.0.0`；写API有令牌保护，但查询API仍包含持仓信息且没有HTTPS，只应放在可信局域网或防火墙后，不要直接暴露公网。
- 本地迁移压缩包可能包含 `.env`/私有配置，不要上传到 Git、网盘或开放目录。
