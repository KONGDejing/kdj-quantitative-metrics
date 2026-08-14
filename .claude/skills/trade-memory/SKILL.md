---
name: trade-memory
description: Preserve and recall the user's trade state, executed orders, and T+1 guidance rules for this KDJ monitoring project. Use before giving stock operation advice, after the user reports a trade, or when context may be lost.
---

# Trade Memory Skill

Use this skill whenever the user asks for trading guidance, reports a buy/sell, asks why a T+1 message was missing/wrong, or asks to preserve context.

## Required workflow

1. Read persistent memory index:
   - `/data/kongdejing/.claude/projects/-data-kongdejing-workspace-kdj-quantitative-metrics/memory/MEMORY.md`
   - Then read relevant memory files, especially:
     - `trading_guidance_must_reflect_executed_trades.md`
     - `2026_08_14_trade_state_and_guidance.md`
     - `user_trading_preferences.md`

2. Read live project state before advising:
   - `/data/kongdejing/workspace/kdj/quantitative-metrics/config.yaml`
   - Treat UI/manual trade records in `trade_plan.positions.*.trade_history` and `last_report` as the source of truth, but verify with the user's newest message.
   - Beware YAML numeric-vs-string stock codes: match codes by `str(key) == code`.

3. Summarize in this fixed order:
   - 今日/最近成交
   - 当前真实持仓
   - 待补回/待处理仓位
   - T+1限制：今天买入的后天才能卖；明天可卖的是今天以前持有的老仓
   - 技术指标/趋势模式
   - 明日/当前机械执行价位

4. When user reports a trade:
   - Confirm whether it is a new trade or correction of an earlier UI record if ambiguous.
   - If unambiguous and the user expects code/state to reflect it, update `config.yaml` or use the app API if appropriate.
   - Persist important non-obvious state into the memory directory, one fact per memory file, and update `MEMORY.md`.

5. Do not generate template advice from close/KDJ alone. Actual trade history and current position must come first.

## Current important rules

- User prefers mechanical, steady rules with drawdown control, not emotional chasing.
- Funds are sufficient: user has about 200k available for this method, mainly around 中航光电.
- Capital expansion order must be: first use the current small position to validate the method; after successful T cycles and stable message/state tracking, restore/maintain the 5-lot experimental base; later raise base position step by step (5 → 8/10 → 12/15 → up to ~20 lots only in better/deeper opportunities). Do not jump directly to a large base.
- 2026-08-14 中航光电 completed one valid T cycle: sold 1 lot at 36.48 and bought it back at 35.93. Treat this as evidence that the method can work, but not yet enough to enlarge aggressively.
- Trading fee: every buy or sell execution costs 5 RMB per lot. Record `fee = lots * 5` and subtract both sell-side and buy-side fees when calculating net T profit. Example: 36.48 sell / 35.93 buyback for 1 lot earns gross 55 RMB, fees 10 RMB, net about 45 RMB.
- If a prior T+1 suggestion was missing, inspect code/config and explain the concrete reason; do not silently skip configured stocks.
