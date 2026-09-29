"""Execute quote rendering in Node, not just source-string assertions."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is required for frontend runtime assertions")
class UiQuotesRuntimeTests(unittest.TestCase):
    def test_real_quote_and_kline_are_not_mixed(self):
        script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const nodes = {};
const node = id => nodes[id] ||= {innerHTML: '', textContent: '', addEventListener() {}};
const context = {document: {getElementById: node, querySelectorAll: () => []}};
vm.createContext(context);
// Load declarations/listeners but do not start HTTP polling or auth actions.
const source = fs.readFileSync('web/app.js', 'utf8').split('\ninitializeBandPeriods();')[0];
vm.runInContext(source, context);
const quote = {price: 32.96, previous_close: 33.27, change_ratio: -0.0093,
  quote_time: '2026-09-29 11:30:00', status_label: '午间收盘', stale: false};
const data = {symbols: [{code: '002179', name: '中航光电'}], quotes: {'002179': quote},
  market_session: {label: '午间休市'}, latest: {'002179': {
    '10m': {close: 33.03, k: 34.35, d: 20, j: 50, complete: false,
      timestamp: '2026-09-29 11:30:00', updated_at: '2026-09-29 11:28:15'}
  }}};
context.renderLatest(data);
assert.match(nodes.latest.innerHTML, /现价 <strong>32\.96<\/strong>/);
assert.match(nodes.latest.innerHTML, /-0\.93%/);
assert.match(nodes.latest.innerHTML, /K线价 33\.03/);
assert.match(nodes.latest.innerHTML, /形成中/);
assert.match(nodes.latest.innerHTML, /KDJ更新 2026-09-29 11:28:15/);
assert.match(context.liveStatusText(data), /午间休市 · 行情 2026-09-29 11:30:00/);
assert.doesNotMatch(context.liveStatusText(data), /实时监控中/);
data.quotes = {};
context.renderLatest(data);
assert.match(nodes.latest.innerHTML, /现价：等待实时报价/);
assert.doesNotMatch(nodes.latest.innerHTML, /现价 <strong>33\.03/);
assert.match(context.liveQuoteHtml({...quote, stale: true, status_label: '行情滞后'}), /参考价/);
assert.match(context.liveQuoteHtml({...quote, change_ratio: null}), /-0\.93%/);
assert.match(context.liveStatusText({...data, quote_monitor: {error: 'failed'}}), /报价拉取异常/);
'''
        result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
