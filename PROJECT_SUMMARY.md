# 项目总结：KDJ 量化盯盘提醒系统

> 最近更新：2026-07-27
> 本文档用于快速说明当前工程要做什么、已经做了什么、接下来要做什么，方便恢复上下文。

## 一、项目定位

一个轻量级 **A股 KDJ 盯盘提醒系统**：

- 后台持续运行，**仅在交易时段**获取行情数据并计算 KDJ 指标。
- 当 K 值进入超买（`K >= 80`）或超卖（`K <= 20`）区间时，通过**邮件**提醒用户。
- 附带沪深300 历史回测工具与交易准则（TRADING_RULES.md），支撑机械化中线波段执行。
- 项目目标是"**提醒用户做交易决策**"，**不是自动下单交易系统**。

## 二、已实现功能（当前状态）

### 后端（FastAPI + Python）

- **行情获取**（`src/data_provider.py`）：AkShare/东方财富为主数据源，失败时自动降级到新浪行情接口；支持日线和 5/10/15/30/60 分钟线。
- **KDJ 计算与展示**（`src/kdj.py` + `src/runner.py`）：标准 9,3,3 参数，RSV → K → D → J；日线同时保留 `1d` 已确认日线和 `1d_est` 盘中估算日线（用当天 10 分钟线折算今日临时日线）。
- **信号判断**（`src/strategy.py`）：`K >= 80` 高位信号，`K <= 20` 低位信号。
- **防重复提醒**（`src/state.py`）：状态化去重——同一股票同一周期进入触发区间只提醒一次，回到正常区间后再次进入才重新提醒；另有 600 秒冷却期兜底。
- **通知渠道**（`src/notifier.py`）：双通道并行——①邮件：SMTP SSL 发送，当前配置 163 邮箱发件，同时发送到 QQ 邮箱和 163 邮箱；②微信：Pushplus 公众号推送，支持单 token 和多 token 群发到多个微信（`KDJ_PUSHPLUS_TOKEN` / `KDJ_PUSHPLUS_TOKENS`），渠道开关在 `alert.channels`。
- **监控循环**（`src/runner.py`）：盘中每 60 秒遍历所有股票 × 所有周期执行一轮；**仅在 A 股交易时段（9:30–11:30 / 13:00–15:00）拉取行情**，收盘后保留 90 秒宽限期补齐 15:00 最后一根 K 线，非交易时段自动暂停不请求接口。
- **参数寻优**（`src/optimizer.py`）：添加股票后自动后台扫描 buy∈{5..25}×sell∈{75..95} 共 25 组阈值组合，按「总收益优先 + 交易次数≥8 + 最大回撤≥-45%」约束选出最优参数，持久化到 `best_params.json`。
- **回测引擎**（`src/backtest.py`）：机械策略——K < buy 买入、K > sell 卖出，信号次根 K 线开盘价成交。
- **API 接口**（`src/api.py`）：状态快照、按日期查提醒、增删股票、切换当前股票、手动触发一轮、回测接口（支持 auto 用最优参数）、寻优查询/触发。

### 前端（web/ 单页，局域网可访问）

- 查看/添加/删除/切换观察股票，改动实时同步后台，无需重启。
- 展示每只股票各周期最新 KDJ 值（高位红色、低位绿色高亮），其中 `1d_est` 表示当前 10 分钟走势折算到日线后的盘中估算 K 值。
- 纯 SVG 手绘 K 线图：红涨绿跌，`K > 80` 标红点、`K < 15` 标绿点；分钟线只展示当天。
- 今日提醒记录 + 历史提醒按日期查询（含邮件是否发送成功）。
- 回测面板：指定标的/周期/阈值跑回测，或使用自动寻优结果。
- 每 10 秒自动轮询刷新。

### 回测研究与交易准则（2026-07-24）

- 三个独立回测脚本：`backtest.py`（日线）、`backtest_combo.py`（底仓+机动仓组合）、`backtest_minute.py`（分钟级）。
- 基于沪深300 2010-01 ~ 2026-07（4020 个交易日）回测，沉淀 **TRADING_RULES.md v1.0**：
  - 底仓 50% 长期不动 + 机动仓 50% 按 KDJ 极端信号做波段。
  - 机动仓 K < 10 买入、K > 85 卖出，次日开盘执行，不设止损。
  - 实盘用沪深300ETF 替代指数。

### 当前监控配置（config.yaml）

| 项目 | 值 |
| --- | --- |
| 监控股票 | `000300`（沪深300）、`002179`（中航光电）、`001309`（德明利）、`300394`（天孚通信）、`600519`（贵州茅台） |
| K线周期 | 日线（1d）、10分钟（10m） |
| KDJ 参数 | n=9, m1=3, m2=3；阈值 80 / 20 |
| 轮询间隔 | 60 秒（仅交易时段） |
| 提醒方式 | 邮件（163 SMTP → QQ + 163 收件）+ 微信（Pushplus 公众号推送，支持多个 token） |
| Web 访问 | `0.0.0.0:8010`，局域网访问 |

### 运行方式

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
# 创建 .env 写入 KDJ_EMAIL_PASSWORD / KDJ_PUSHPLUS_TOKEN / KDJ_PUSHPLUS_TOKENS
python main.py    # 临时手动运行；监听 8010 端口
```

长期后台运行推荐 systemd 用户服务：

```bash
chmod +x scripts/*.sh
./scripts/install-systemd-user.sh
sudo loginctl enable-linger "$USER"   # 只需执行一次，保证退出 SSH 后服务仍运行
```

**敏感信息管理**：SMTP 授权码和 Pushplus token 通过环境变量 `KDJ_EMAIL_PASSWORD`、`KDJ_PUSHPLUS_TOKEN` / `KDJ_PUSHPLUS_TOKENS` 注入（写在 `.env` 中），`config.yaml` 里对应字段留空。优先级：环境变量 > config.yaml。systemd 启动脚本会自动加载 `.env`；如果临时手动运行，命令必须带 `source .env &&`，否则两个通知渠道都会跳过（日志有 warning）。

日志在 `logs/`：`app.log`（运行日志，5MB×3 轮转）、`alerts.log`（提醒记录）、`server.log`（旧 nohup 输出）；systemd stdout/stderr 用 `journalctl --user -u kdj-alert.service -f` 查看。

## 三、明确不做的（第一版边界）

- 不自动下单、不做高频交易、不做资金管理。
- 不接短信服务（后期再扩展）。

## 四、待办 / 后续方向

1. **部署加固**：已补充 systemd 用户服务方案（`deploy/systemd/kdj-alert.service`、`scripts/start-kdj-alert.sh`、`scripts/install-systemd-user.sh`、`DEPLOYMENT.md`），用于长期后台运行和异常自动重启。
2. **策略增强**：
   - K/D 金叉、死叉提醒。
   - J 值极端区间提醒。
   - 多周期共振判断（如日线低位 + 分钟金叉）。
   - 价格突破均线、成交量放大等扩展条件。
3. **标的扩展**：更多股票、港股/美股市场支持。
4. **短信提醒**：后期按短信渠道选型接入。

## 五、关键约定（继续开发时遵守）

- 提醒策略保持"进入区间提醒一次、连续区间不重复"的状态化去重逻辑。
- 不默认加入自动下单或短信功能，除非用户明确要求。
- 每次重要修改沉淀到本文档「修改记录」一节，避免上下文丢失。
- `config.yaml`、`.env`（含 SMTP 授权码、Pushplus token）、`best_params.json`、`cache/`、`logs/` 不提交 git；仓库只提交 `config.example.yaml` 模板。
- 密钥一律走环境变量（`KDJ_EMAIL_PASSWORD`、`KDJ_PUSHPLUS_TOKEN` / `KDJ_PUSHPLUS_TOKENS`，写在 `.env`），不写进任何配置文件；systemd 启动脚本会自动加载 `.env`，临时手动运行时命令必须 `source .env &&` 前缀。

## 六、修改记录

### 2026-07-27

- **新增交易时段感知**（`src/runner.py`）：监控循环只在工作日 9:30–11:30 / 13:00–15:00 拉取行情，收盘后保留 90 秒宽限期确保 15:00 最后一根 K 线入库；非交易时段每分钟只打一条 `paused (market closed)` 日志，不请求任何行情接口。修复收盘后仍持续爬取东方财富/新浪数据的问题。
- **踩坑记录**：修改代码后必须重启进程才生效——15:40 启动的进程加载的是旧代码，16:05 的修复直到 17:32 重启后才生效，期间收盘后白跑了 100 多轮无效请求。
- **新增启动补抓逻辑**（`src/runner.py`）：非交易时段启动且内存状态为空时，先补跑一轮数据再进入暂停。修复收盘后/周末重启导致 Web 页面无任何数据的问题（K线/最新KDJ/提醒记录均为纯内存状态，不落盘）。
- **新增微信推送通道**（`src/notifier.py`）：接入 Pushplus 公众号推送，与邮件并行发送；`alert.channels` 增加 `pushplus` 开关，token 通过环境变量配置（不入库）；前端提醒记录增加微信发送状态展示。
- **新增参数自动寻优**（`src/optimizer.py` + `/api/best-params`、`/api/optimize/{symbol}`）：添加股票后自动后台扫描 25 组 (buy,sell) 阈值，按总收益（带交易次数/回撤约束）选出最优并持久化到 `best_params.json`；已完成 5 只标的的首次寻优。
- **新增服务内回测接口**（`/api/backtest`，`src/backtest.py`）：支持指定阈值或使用 auto 最优参数；未寻优标的返回 202 并自动启动后台寻优。
- **前端新增回测面板**（`web/app.js`、`web/index.html`）：可在页面直接跑回测、查看最优参数。
- **监控列表扩展**：新增 `002179`、`001309`、`300394`、`600519`；监控周期精简为 1d + 10m。
- **邮件发件改为 163 邮箱**（原 QQ 邮箱方案替换）。
- **安全整改**：`config.yaml`（含 SMTP 授权码、Pushplus token）从 git 移除并加入 `.gitignore`，改用 `config.example.yaml` 模板提交；`best_params.json`、`cache/` 等运行产物一并排除。
- **新增 systemd 用户服务部署方案**：补充 `deploy/systemd/kdj-alert.service`、`scripts/start-kdj-alert.sh`、`scripts/install-systemd-user.sh` 和 `DEPLOYMENT.md`，替代长期依赖 `nohup` 的运行方式；服务异常退出后 `Restart=always` 自动重启，启动脚本自动加载 `.env`，并支持 `journalctl --user -u kdj-alert.service -f` 查看日志。
- **服务器兼容性修复**：`requirements.txt` 固定 `numpy<2`、`pandas<2.2`，避免 Python 3.9 + Anaconda 环境下 NumPy 2 与 `numexpr` / `bottleneck` 二进制不兼容；同时将代码中的 `str | None` / `dict | None` 等 Python 3.10 类型语法改为 `typing.Optional`，保证 Python 3.9 可启动。
- **Pushplus 多微信扩展**：新增 `PUSHPLUS.md` 记录 Pushplus 开通流程与多微信 token 配置；`src/notifier.py` 支持 `KDJ_PUSHPLUS_TOKENS=token1,token2` 多 token 合并去重发送，兼容旧的 `KDJ_PUSHPLUS_TOKEN` 单 token。

### 2026-07-24

- 新增沪深300 历史回测研究：三个独立回测脚本（日线 / 底仓+机动仓组合 / 分钟级）。
- 基于 2010-01 ~ 2026-07 回测结果制定 **TRADING_RULES.md 交易准则 v1.0**：底仓 50% + 机动仓 50%，机动仓 K<10 买、K>85 卖，次日开盘执行，不设止损。
- 新增 `src/backtest.py` 回测引擎、`cache/` 日线数据缓存。

### 2026-06-24

- 初始化项目方案，明确项目定位：后台KDJ盯盘提醒，不自动下单。
- 明确核心触发规则：K值 `>= 80` 或 `<= 20` 时提醒。
- 完成第一版项目代码：FastAPI后端、AkShare行情获取、KDJ计算、阈值判断、邮件提醒、冷却控制、局域网前端页面。
- 新增 `config.yaml`、`requirements.txt`、`main.py`、`src/` 后端模块和 `web/` 前端文件。
- 前端访问端口调整为 `8010`，局域网访问地址为 `http://10.122.86.70:8010`。
- 新增前端价格K线图区域：展示当前观察股票各周期K线，并在对应K线上用红点标记 `K > 80`、绿点标记 `K < 15`。
- 新增新浪行情备用数据源：当 AkShare/东方财富分钟K线接口连接失败时，自动回退到新浪接口拉取分钟K线。
- 前端K线图只展示当天分钟K线；第一根5分钟K线标注为 `09:30-09:35` 区间，避免误解。
- 后端新增K线序列缓存，接口 `/api/status` 返回最近K线OHLC和KDJ数据供前端绘图。
- 为提醒增加状态化去重策略：进入高位/低位区间后只提醒一次，回到正常区间后再次进入才重新提醒。
- 调整提醒记录展示：前端默认只展示当天提醒，历史提醒可按日期查询。
