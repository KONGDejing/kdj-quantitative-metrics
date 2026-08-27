# KDJ 量化盯盘与交易计划辅助系统

这是一个面向 A 股的个人盯盘工具。它在交易时段抓取日线和分钟线、计算 KDJ、发送阈值提醒，并提供持仓流水、收盘总结、次日操作指引和回测研究。

> 系统只提供提醒和决策辅助，不连接券商、不自动下单，也不构成投资建议。

## 当前能力

- 交易时段监控：按已核验的A股交易日历，在9:30–11:30、13:00–15:00每60秒轮询一次，15:00后保留90秒宽限期。
- 行情降级：AkShare/东方财富不可用时自动切换新浪接口；长历史回测可继续使用本地缓存。
- KDJ 展示：计算 KDJ(9,3,3)，同时展示日线、10 分钟线和用当日 10 分钟线折算的 `1d_est`；中航光电10分钟图逐根标注K值，悬停或点按显示该时点的精确价格与K/D/J。
- 个股阈值：优先读取 `best_params.json` 中的个股寻优结果，没有结果时使用全局阈值。
- 通知：邮件和 Pushplus 微信双通道；盘中极值提醒、14:50 收盘前总结、15:15 后次日操作指引。
- 可信交易账本：Web 录入成交时区分核心仓/T仓，逐笔重算持仓、平均买入成本、保本成本、已实现收益和 T+1 可卖手数。
- 确定性交易计划：正式日线信号依次通过账本、基本面、回撤、资金、阶段仓位和止跌确认闸门，输出动作、最多手数、价位和取消条件。
- 简单冲高反T：较前收冲高约1.8%且10分钟K从80以上拐头时，单次卖1手可卖老仓；盈利回补也按实际卖价下方约1.8%。总额度20%、至少保留8手，MA趋势过滤代码保留但当前关闭。
- 策略净值：按完整20万元资金池回放每日现金、市值、高水位、当前回撤和历史最大回撤，同日重复运行不会重复累计。
- 影子评分：按正式信号日冻结原始计划，在5/10/20/30/60个交易日后评价增量动作；低开限价等日线无法判断的路径不强行假设成交。
- 样本外校准：扩展窗口 walk-forward 每折只用过去数据选择确认参数，再到后续年份检验，并按上升、下降、震荡和高波动价格状态分组；研究结果不会自动覆盖实盘配置。
- 分阶段资金曲线：固定当前规则比较10/15/20/30/40手在20万元资金池中的样本外收益、现金约束和回撤，并用明确闸门决定是否允许晋级。
- 运行状态持久化：提醒历史、区间去重、冷却时间和每日通知任务按通道持久化，重启不会重复发送已成功的通道。
- 安全写入：所有POST/PATCH/DELETE接口要求本机生成的写入令牌；成交支持先校验整本账本再替换/删除，错误值不进入审计历史。
- Web 面板：观察列表、K线/KDJ、确定性计划、净值/影子/阶段研究、成交录入与纠错、KDJ回测和价格波段分析。
- 研究工具：KDJ 参数网格寻优、服务内回测、沪深300独立研究脚本、中航光电价格区间分析。

当前私人配置观察 `000300`（沪深300）、`002179`（中航光电）和 `600498`（烽火通信）。观察列表以本机 `config.yaml` 为准，可在 Web 页面增删。

## 系统边界

项目现在包含三层能力：

1. **监控层**：行情 → KDJ → 页面状态与提醒。
2. **研究层**：历史数据 → 回测/寻优 → 阈值和波段参考。
3. **交易计划层**：人工成交 → 可信账本 → 确定性决策引擎 → T+1/次日指引；LLM只做只读复核。

交易计划层仍属于辅助功能。真实持仓以用户确认、`opening` 起始仓位和 `trade_history` 逐笔重放结果为准；`base_lots`、`cost` 等字段只是兼容汇总。确定性账本优先于 LLM 文本。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
# 创建 .env，写入通知凭据
python main.py
```

首次执行成交录入、纠错或其他写入操作前，在服务器查看一次写入令牌，并粘贴到页面顶部；令牌只保存在当前浏览器：

```bash
./scripts/show-write-token.sh
```

访问地址由 `config.yaml` 的 `web.host`/`web.port` 决定，不能假定固定端口：

```text
http://<服务器IP>:<web.port>
```

长期运行推荐 systemd 用户服务：

```bash
chmod +x scripts/*.sh
./scripts/install-systemd-user.sh
sudo loginctl enable-linger "$USER"
```

修改代码或配置后必须重启：

```bash
systemctl --user restart kdj-alert.service
```

完整运维说明见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 配置要点

基础模板见 `config.example.yaml`，主要字段如下：

```yaml
poll_interval_seconds: 60
symbols:
  - code: '000300'
    name: 沪深300
timeframes: [1d, 10m]
kdj:
  n: 9
  m1: 3
  m2: 3
  upper: 80
  lower: 20
alert:
  cooldown_seconds: 600
  channels: [email, pushplus]
trade_plan:
  positions:
    '002179':
      opening:
        as_of: '2026-01-01'
        core_lots: 5
        t_lots: 0
        cost_per_share: 34
      fee_per_lot: 5
      shadow_tracking_enabled: true
      shadow_horizons: [5, 10, 20, 30, 60]
      trade_history: []
use_llm_advice: false
web:
  host: 0.0.0.0
  port: 8010
```

通知密钥只通过 `.env`/环境变量注入：

```bash
export KDJ_EMAIL_PASSWORD=SMTP授权码
export KDJ_PUSHPLUS_TOKEN=单个微信Token
export KDJ_PUSHPLUS_TOKENS=tokenA,tokenB
```

可选 LLM 指引还可通过环境变量配置 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL` 和 `ANTHROPIC_MODEL`，并把 `use_llm_advice` 设为 `true`。启用后，持仓摘要、成交流水和行情上下文会发送到所配置的外部模型服务；LLM 输出仍缺少确定性业务校验，详见项目总纲。

`.env`、`config.yaml`、`best_params.json`、`cache/`、`logs/`、`runtime/` 都是本地私有数据或运行产物，不应提交到 Git。

## 核心运行链路

```text
交易时段
  → 拉取每只股票的 1d / 10m K线
  → 计算 KDJ
  → 写入内存状态供 Web 展示
  → 用 10m 聚合当日临时日线 1d_est
  → 按个股阈值检查 1d_est，进入新区间时提醒

14:50 后
  → 发送收盘前 KDJ 总结

15:15 后
  → 直接读取当日最后一轮内存行情
  → 读取已配置持仓与成交流水
  → 生成并发送次日 T+1 操作指引
```

注意：当前阈值即时提醒只针对 `1d_est`；它是盘中估算，不等于正式日线收盘信号。次日计划从 15:15 起尝试，只有所有持仓标的都取得当天正式日线才发送；数据未就绪时持续重试，绝不使用上一交易日日线。正式交易仍应以收盘确认数据、真实持仓和交易规则为准。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/status` | 当前观察列表、KDJ、K线、提醒和交易计划快照 |
| GET | `/api/alerts?date=YYYY-MM-DD` | 查询持久化提醒记录 |
| GET | `/api/auth/status`、`/api/runtime-status` | 查询写保护、任务持久化和交易日历状态 |
| POST/PATCH/DELETE | `/api/symbols`、`/api/symbols/{code}` | 增删观察标的或纠正名称 |
| POST | `/api/current-symbol` | 切换图表标的 |
| POST | `/api/run-once` | 手动拉取一轮 |
| POST | `/api/trade-report` | 录入人工成交 |
| GET/POST | `/api/trades`、`/api/trade-corrections` | 查询成交ID；校验后替换/删除错误成交 |
| GET | `/api/trade-ledger?symbol=002179&as_of=YYYY-MM-DD` | 查询逐笔重放后的持仓、成本和 T+1 状态 |
| GET | `/api/decision-plan?symbol=002179` | 查询正式日线驱动的确定性结构化交易计划 |
| GET | `/api/performance?symbol=002179` | 查询策略资金净值、高水位与回撤历史 |
| GET | `/api/shadow-decisions?symbol=002179` | 查询真实影子计划、执行判定和分周期评分 |
| GET/POST | `/api/research/walk-forward`、`/api/research/walk-forward/{symbol}` | 查询/重新生成滚动样本外研究 |
| GET/POST | `/api/research/stage-capital`、`/api/research/stage-capital/{symbol}` | 查询/刷新分阶段资金曲线与晋级闸门 |
| GET | `/api/backtest` | KDJ 策略回测 |
| GET/POST | `/api/best-params`、`/api/optimize/{symbol}` | 查询/触发阈值寻优 |
| GET | `/api/band-analysis/optimal` | 搜索价格波段 B/S 组合 |
| GET | `/api/band-analysis/detail` | 查看指定 B/S 的模拟明细 |

所有写接口必须携带`X-API-Key`。令牌优先读取`KDJ_API_WRITE_TOKEN`，未配置时自动生成到权限为600的`runtime/api_write_token`；不要把令牌提交到Git或发到聊天记录。

## 项目结构

```text
main.py                     FastAPI/uvicorn 入口
src/
  api.py                    Web 与 REST API
  runner.py                 监控、提醒、收盘总结、次日指引调度
  data_provider.py          实时/回测行情与缓存降级
  kdj.py / strategy.py      KDJ 计算与信号判断
  state.py                  内存状态、观察列表、成交流水写入
  trade_ledger.py           成交重放、双成本口径、仓位分类与T+1校验
  decision_engine.py        信号确认、资金/风险闸门和结构化机械计划
  performance_store.py      每日策略净值、高水位和最大回撤持久化
  shadow_tracker.py         正式计划留痕、到期评分和无前视价格状态分类
  stage_research.py         10/15/20/30/40手样本外资金曲线和阶段晋级检查
  walk_forward.py           确认规则的扩展窗口样本外事件研究
  runtime_state.py          提醒、冷却、每日任务和纠错元数据持久化
  trading_calendar.py       A股交易日与休市日判定
  auth.py                   本地写入令牌生成和校验
  notifier.py               邮件与 Pushplus
  llm_advisor.py            可选 LLM 次日建议客户端
  backtest.py / optimizer.py
  band_analysis.py
web/                        原生 HTML/CSS/JavaScript 单页
scripts/ + deploy/systemd/  启动与守护服务
backtest*.py                独立研究脚本
```

## 文档导航

- [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md)：当前架构、真实状态、已知限制和开发约定。
- [TRADING_RULES.md](TRADING_RULES.md)：沪深300 KDJ 研究准则，不是所有个股的通用规则。
- [DEPLOYMENT.md](DEPLOYMENT.md)：安装、启动、日志和排障。
- [PUSHPLUS.md](PUSHPLUS.md)：微信通知配置。

## 当前已知限制

- KDJ/K线序列仍保存在内存，重启后由启动首轮行情重新填充；提醒、冷却和每日发送状态已经持久化。
- 内置官方休市日历当前覆盖2026年；跨到新年份前必须补充交易所年度安排，未知年份默认停止定时交易任务。
- 写接口已有令牌保护，但查询接口仍会展示持仓与策略数据，且当前没有HTTPS，仍只适合可信局域网。
- LLM 只在确定性主计划发送后另行复核；失败或超时不会阻止主计划，也不能修改动作、手数与价位。
- 样本外事件和影子记录仍较少，不能据此保证收益或自动放大仓位；原KDJ阈值寻优仍存在样本内过拟合风险。
- 已覆盖账本、决策、净值、盘中日线过滤、影子评分、阶段资金曲线、任务持久化、成交纠错、交易日历和写接口鉴权；外部行情/通知服务的真实网络故障仍需运行中监控。

重要修改统一记录到 [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md)；README 只保留稳定的使用入口和当前能力。
