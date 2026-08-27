---
name: trade-memory
description: Preserve and recall the user's trade state, executed orders, and T+1 guidance rules for this KDJ monitoring project. Use before giving stock operation advice, after the user reports a trade, or when context may be lost.
---

# Trade Memory Skill

Use this skill whenever the user asks for trading guidance, reports a buy/sell, asks why a T+1 message was missing/wrong, or asks to preserve context.

## Required workflow

1. Read persistent memory index:
   - `/data/kongdejing/.claude/projects/-data-kongdejing-workspace-kdj-quantitative-metrics/memory/MEMORY.md`
   - Then read both indexed files:
     - `canonical_trading_context.md`
     - `user_trading_preferences.md`

2. Read live project state before advising:
   - `/data/kongdejing/workspace/kdj/quantitative-metrics/config.yaml`
   - Treat UI/manual trade records in `trade_plan.positions.*.trade_history` and `last_report` as the source of truth, but verify with the user's newest message.
   - Beware YAML numeric-vs-string stock codes: match codes by `str(key) == code`.
   - `canonical_trading_context.md` is the authoritative concise summary. Do not search deleted historical memory files for superseded prices, names, positions, or plans.
   - When the service is available, read `/api/decision-plan?symbol=<code>` or build the same plan with `src.decision_engine`. Its deterministic action, lots, price constraints, T+1 state, and cancellation conditions are authoritative; the LLM may explain or flag a conflict but must not replace them.

3. Summarize in this fixed order:
   - 今日/最近成交
   - 当前真实持仓
   - 待补回/待处理仓位
   - T+1限制：今天买入的下一交易日才能卖；当天不能卖；下一交易日可卖的还包括此前持有的老仓
   - 技术指标/趋势模式
   - 明日/当前机械执行价位

4. When user reports a trade:
   - Confirm whether it is a new trade or correction of an earlier UI record if ambiguous.
   - If unambiguous and the user expects code/state to reflect it, update `config.yaml` or use the app API if appropriate.
   - Update `canonical_trading_context.md` only when the trade changes the durable current state. Do not create one file per trade or preserve erroneous input/correction history in memory.
   - If an entry is corrected, replace it with the final confirmed fact. Do not retain the wrong name, wrong price, or a narrative of the correction.

5. Do not generate template advice from close/KDJ alone. Actual trade history and current position must come first.

## Current important rules

- User prefers mechanical, steady rules with drawdown control, not emotional chasing.
- User has about 500k total capital and previously earmarked about 200k for this method, mainly around 中航光电.
- The 20%-30% annual-return aspiration applies to the 中航光电 strategy, not the user's full 500k account. Report both the roughly 200k strategy-sleeve return including idle cash and the deployed-position return; do not mix denominators.
- As of 2026-08-27 the verified 中航光电 position is 9 core lots and 0 separately purchased tactical lots after the user sold one old core lot at 34.99. One core lot is pending buyback; its 1.8% profit-buyback target is 34.36, and no new reverse-T sale is allowed first. The pre-sale user-confirmed brokerage cost was 33.65; the replayed post-sale breakeven is about 33.5067 and should not be presented as a newly user-confirmed broker display. Reserve 20% of the 10-lot target as sell-first reverse-T capacity, but sell at most 1 old lot per signal/day and keep at least 8 core lots. The accepted long-term ceiling is 40 core + 10 tactical lots with at least 15% sleeve cash, expanded gradually through 15/20/30/40 core stages; it is not an immediate buy instruction.
- As of 2026-08-27 烽火通信 sold its one lot at 42.00 and holds zero shares. A 40.00 buy-one-lot limit order is open but unfilled; never count it as a position or T+1-locked purchase until the user confirms execution.
- Protective higher-price buyback is disabled for 中航光电 by explicit user choice. If a reverse-T sell keeps rising, allow one fewer lot rather than buying back above the sell price; retain only the 1.8%-lower profit buyback or a later independently valid formal buy signal.
- During the prior 5-lot stage, 中航光电 completed two successful sell-first reverse-T cycles from the confirmed trades: sells at 35.94 and 36.48, buybacks at 35.93 and 35.40. Combined gross spread profit was 109 RMB; four one-lot executions cost 20 RMB, for about 89 RMB net. Treat this as positive live evidence, but not enough to enlarge the per-signal size beyond 1 lot.
- Trading fee: every buy or sell execution costs 5 RMB per lot. Record `fee = lots * 5` and subtract both sell-side and buy-side fees when calculating net T profit. Example: 36.48 sell / 35.93 buyback for 1 lot earns gross 55 RMB, fees 10 RMB, net about 45 RMB.
- If a prior T+1 suggestion was missing, inspect code/config and explain the concrete reason; do not silently skip configured stocks.
- Do not trust aggregate `cost`, `base_lots_remaining`, or `t_lots_held` when they conflict with confirmed trades. Recompute from the append-only history plus the verified opening position, and surface the inconsistency before advice.
- Treat deterministic position/T+1 checks as mandatory. LLM-generated text is advisory and must not override confirmed trades, per-symbol strategy scope, or A-share settlement rules.
- The deterministic decision engine is now the primary plan. It requires confirmed daily data plus ledger, fundamental, drawdown, capital, stage, oversold, and rebound gates. Separately purchased tactical inventory remains disabled. The current sell-first reverse-T execution rule is intentionally simple: when price is about 1.8% above the prior formal close and the 10-minute K turns down from above 80, sell at most one sellable old lot. The old MA20/MA60 uptrend filter remains in code behind `trend_filter_enabled: false` and must not affect or appear in current execution guidance unless the user later explicitly enables it. Use the recorded actual sell price for buyback; the configured profit-buyback target is also about 1.8% lower, rounded down to the one-cent tick. The user considers roughly 1.5%-2.0% acceptable and does not want repeated micro-optimization inside that range; keep 1.8% as the stable mechanical value unless later evidence supports a material change.
- Use persisted strategy performance high water and drawdown when available. The first walk-forward report did not pass the shadow-validation threshold, so it must not auto-change live parameters or enable tactical trading.
- The staged capital report also blocks 10→15 lots: fold stability, matured 30-day shadow samples, shadow quality, and the fundamental gate have not passed. The current oversold-event method produced only low-single-digit full-sleeve annualized returns in historical OOS stage curves; never enlarge lots merely to force the 20%-30% aspiration.
- Memory must contain only final confirmed values. Runtime/config inconsistencies should be resolved from trade replay and reported as a current validation issue, without copying superseded wrong values into persistent memory.
