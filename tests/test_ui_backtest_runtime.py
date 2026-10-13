import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is required")
class UiBacktestRuntimeTests(unittest.TestCase):
    def test_current_range_old_trade_and_stale_response_are_distinct(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const nodes = {};
const node = id => nodes[id] ||= {innerHTML: '', textContent: '', value: '', style: {}, addEventListener() {}};
const summary = {start: '2010-01-04', end: '2026-10-12', bars: 4018,
  total_return: 1.7, bench_return: .8, max_drawdown: -.4, bench_max_drawdown: -.3,
  round_trips: 1, win_rate: 1, avg_return: .1, best_return: .1, worst_return: .1};
const data = {symbol: '600498', timeframe: '1d', summary, params: {buy: 10, sell: 80},
  params_auto: true, data_source: 'test',
  freshness: {freshness: 'current', expected_end: '2026-10-12', checked_at: '2026-10-13T10:30:00'},
  trades: [{time: '2026-08-13', side: 'sell'}],
  round_trips: [{buy_time: '2026-07-08', buy_price: 30, sell_time: '2026-08-13', sell_price: 33, return_pct: .1}],
  curve: [{time: '2010-01-04', equity: 1, bench: 1}, {time: '2026-10-12', equity: 2.7, bench: 1.8}]};
let reply = data, fetches = [];
const context = {document: {getElementById: node, querySelectorAll: () => []}, URLSearchParams,
 localStorage: {getItem() {return null;}}, alert: msg => {throw new Error(msg);},
 fetch: async (url, options) => {fetches.push({url, options}); return {ok: true, status: 200, json: async () => reply};}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/app.js', 'utf8').split('\ninitializeBandPeriods();')[0], context);
(async () => {
 node('bt-symbol').value='600498'; node('bt-timeframe').value='1d';
 node('bt-buy').value='10'; node('bt-sell').value='80';
 await context.runBacktest(false);
 assert.equal(nodes['bt-result'].style.display, 'block');
 assert.match(nodes['bt-summary'].innerHTML, /本次回测区间 2010-01-04 ~ 2026-10-12/);
 assert.match(nodes['bt-trades'].innerHTML, /数据已计算至 2026-10-12/);
 assert.match(nodes['bt-trades'].innerHTML, /最后一次模拟成交：2026-08-13/);
 assert.match(nodes['bt-curve'].innerHTML, /2026-10-12/);
 assert.equal(fetches[0].options.cache, 'no-store');
 reply={...data, freshness: {...data.freshness, expected_end: '2026-10-13'}};
 await context.runBacktest(false);
 assert.equal(nodes['bt-result'].style.display, 'none');
 assert.match(nodes['bt-status'].textContent, /不展示过期结果/);
 reply={best: {buy:10, sell:80, range:'2010-01-04~2026-07-30', optimized_at:'2026-07-30 09:49:26'}};
 await context.refreshBestInfo();
 assert.doesNotMatch(nodes['bt-best-info'].textContent, /2026-07-30|收益|回撤/);
 assert.match(nodes['bt-best-info'].textContent, /最新完整K线/);
})().catch(error => {console.error(error); process.exitCode=1;});
'''
        result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
