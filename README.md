# KDJ 量化盯盘提醒系统

一个轻量级 **A股 KDJ 盯盘提醒系统**：盘中自动获取行情、计算 KDJ 指标，当 K 值进入超买/超卖区间时**邮件 + 微信提醒**，并附带日线回测与参数寻优工具，帮助执行机械化交易准则。

> 项目定位是「**提醒用户做交易决策**」，不是自动下单系统。
> 交易规则见 [TRADING_RULES.md](TRADING_RULES.md)，项目演进见 [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md)。

## 功能概览

- **实时监控**：盘中每 60 秒拉取监控标的的日线 + 分钟线，计算 KDJ(9,3,3)。
- **邮件 + 微信双通道提醒**：K ≥ 80（高位）或 K ≤ 20（低位）时同时发邮件和微信（Pushplus 公众号推送）；状态化去重（进入区间只提醒一次，回到正常区间后再次进入才重新提醒），另有冷却期兜底。
- **交易时段感知**：只在 A 股交易时段（9:30–11:30 / 13:00–15:00）拉取行情，收盘后自动暂停，不浪费请求。
- **Web 面板**：局域网访问，管理监控列表、查看 K 线图（标记超买/超卖点）、查看当日/历史提醒、手动触发回测。
- **历史回测**：K < buy 买入、K > sell 卖出，信号次根 K 线开盘价成交的机械策略回测。
- **参数寻优**：添加股票后自动在后台扫描 25 组 (buy, sell) 阈值组合，按「总收益优先 + 交易次数/回撤约束」选出该标的的最优参数并持久化。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # 填入邮箱、监控股票等非敏感配置
# 创建 .env，写入 KDJ_EMAIL_PASSWORD / KDJ_PUSHPLUS_TOKEN
python main.py                       # 监听 0.0.0.0:8010，后台监控自动启动
```

浏览器访问 `http://<服务器IP>:8010` 打开 Web 面板。

长期后台运行推荐使用 systemd 用户服务（异常退出自动重启，退出 SSH 后也可继续运行）：

```bash
chmod +x scripts/*.sh
./scripts/install-systemd-user.sh
sudo loginctl enable-linger "$USER"   # 只需执行一次：允许退出 SSH 后用户服务继续运行
```

常用运维命令：

```bash
systemctl --user status kdj-alert.service
journalctl --user -u kdj-alert.service -f
systemctl --user restart kdj-alert.service
```

> ⚠️ 修改代码后需 `systemctl --user restart kdj-alert.service` 重启进程才能生效（进程启动时加载代码，不会热更新）。

更多部署细节见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 配置说明（config.yaml）

```yaml
poll_interval_seconds: 60      # 盘中轮询间隔
symbols:                       # 监控列表（也可在 Web 页面增删）
  - code: '000300'
    name: 沪深300
timeframes: [1d, 10m]          # K线周期：1d / 5m / 10m / 15m / 30m / 60m
kdj:
  n: 9; m1: 3; m2: 3           # KDJ 参数
  upper: 80; lower: 20         # 提醒阈值（按 K 值判断）
alert:
  cooldown_seconds: 600        # 提醒冷却期（兜底防轰炸）
  channels: [email, pushplus]  # 通知渠道：邮件 + 微信
email:
  smtp_host: smtp.163.com      # 或 smtp.qq.com
  password: ''                 # 授权码用环境变量提供，不写死在配置文件
  to_addrs: [...]              # 收件邮箱列表
pushplus:
  token: ''                    # token 用环境变量提供，不写死在配置文件
```

**敏感信息管理**：SMTP 授权码和 Pushplus token 通过环境变量注入，避免写进任何配置文件：

```bash
# .env（已在 .gitignore 中，不会提交）
export KDJ_EMAIL_PASSWORD=你的邮箱SMTP授权码
export KDJ_PUSHPLUS_TOKEN=你的PushplusToken

# 启动时加载（临时手动运行）
source .env && python main.py

# 长期运行使用 systemd，启动脚本会自动加载 .env
./scripts/install-systemd-user.sh
```

优先级：环境变量 > `config.yaml`（留空或写占位符时回退读取环境变量）。`config.yaml`、`.env` 均已在 `.gitignore` 中排除，请勿提交。

## 服务器环境建议

线上建议使用 Python 3.9/3.10/3.11 的独立虚拟环境，不要直接混用系统 Anaconda base 环境。`requirements.txt` 已固定 `numpy<2`、`pandas<2.2`，用于避开 Python 3.9 + Anaconda 常见的 NumPy 2 与 `numexpr` / `bottleneck` 二进制兼容问题。

如果旧服务器已经装过依赖，建议重建虚拟环境或至少执行：

```bash
python3 -m pip install -U --force-reinstall "numpy<2" "pandas<2.2" numexpr bottleneck
```

## 项目结构

```text
quantitative-metrics/
  main.py                # 入口：启动 FastAPI(uvicorn) + 监控循环
  config.example.yaml    # 配置模板（复制为 config.yaml 使用）
  src/
    api.py               # REST API：状态/提醒/股票管理/回测/寻优
    runner.py            # 监控主循环：交易时段判断、每轮 KDJ 计算与信号检查
    data_provider.py     # 行情获取：东方财富(akshare) 为主，新浪接口自动降级
    kdj.py               # KDJ 指标计算（RSV→K→D→J）
    strategy.py          # 信号判断：K 值超买/超卖
    notifier.py          # 提醒发送：邮件（SMTP SSL）+ 微信（Pushplus 公众号）
    state.py             # 运行时状态：监控列表、K线缓存、提醒去重
    optimizer.py         # 参数寻优：网格扫描 (buy,sell) 组合，结果存 best_params.json
    backtest.py          # 回测引擎（服务内）
    config.py / logger.py
  web/                   # 纯 HTML/JS/SVG 前端（K线图、提醒记录、回测面板）
  backtest.py            # 独立脚本：沪深300 日线回测
  backtest_combo.py      # 独立脚本：底仓+机动仓组合策略回测
  backtest_minute.py     # 独立脚本：分钟级回测
  TRADING_RULES.md       # 交易准则 v1.0（底仓+机动仓，机械化执行规则）
  DEPLOYMENT.md           # systemd 用户服务部署与运维说明
  deploy/systemd/         # systemd 服务单元
  scripts/                # 启动和安装脚本
  logs/                  # app.log（5MB×3轮转）/ alerts.log / server.log
```

## 监控与提醒逻辑

```text
每分钟（仅交易时段）：
  遍历 股票 × 周期
    → 拉取K线（东方财富失败 → 自动降级新浪）
    → 计算 KDJ(9,3,3)
    → K ≥ 80 → 高位信号；K ≤ 20 → 低位信号
    → 状态化去重 + 600s 冷却
    → 发送邮件 + 记录 alerts.log
```

交易时段判断（`src/runner.py`）：工作日 9:30–11:30、13:00–15:00；收盘后保留 90 秒宽限期确保 15:00 最后一根 K 线入库；非交易时段每分钟只记录一条 `paused` 日志，不请求行情接口。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 状态快照（各标的最新 KDJ、K线序列） |
| GET | `/api/alerts?date=YYYY-MM-DD` | 按日期查询提醒记录 |
| POST/DELETE | `/api/symbols` | 添加/删除监控股票（添加后自动后台寻优） |
| POST | `/api/current-symbol` | 切换当前观察股票 |
| POST | `/api/run-once` | 手动触发一轮监控 |
| GET | `/api/backtest?symbol=...&auto=true` | 回测；auto=true 使用已寻优的最优参数 |
| GET | `/api/best-params` | 查询已保存的最优参数 |
| POST | `/api/optimize/{symbol}` | 手动触发重新寻优 |

## 回测与寻优

- **独立脚本**（根目录 `backtest*.py`）用于沪深300 研究，结论沉淀在 [TRADING_RULES.md](TRADING_RULES.md)。
- **服务内回测**（`/api/backtest`）：机械规则 K < buy 买入、K > sell 卖出，次根开盘价成交。
- **自动寻优**（`src/optimizer.py`）：buy ∈ {5,10,15,20,25} × sell ∈ {75,80,85,90,95} 网格扫描；合格约束为交易次数 ≥ 8 且最大回撤 ≥ -45%，按总收益选最优。结果存 `best_params.json`（运行产物，不入库）。

## 开发约定

- 提醒保持「进入区间提醒一次、连续区间不重复」的状态化去重。
- 不加自动下单功能，除非用户明确要求。
- 每次重要修改沉淀到 `PROJECT_SUMMARY.md` 的「修改记录」一节。
- 提交前确认 `config.yaml`、`best_params.json`、`cache/`、`logs/` 未被加入 git。
