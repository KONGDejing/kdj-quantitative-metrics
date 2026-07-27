async function requestJson(url, options) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || `请求失败: ${response.status}`);
  }
  return response.json();
}

// ---------- 深色主题图表常量 ----------
const CHART = {
  up: "#e66767",      // 涨 / 超买标记
  down: "#3ec98a",    // 跌 / 超卖标记
  equity: "#3987e5",  // 策略净值线
  bench: "#898781",   // 基准线
  grid: "#2c2c2a",    // 网格线
  axis: "#383835",    // 轴线
  text: "#898781",    // 图表文字
};

function renderSymbols(data) {
  document.getElementById("current-symbol").textContent = data.current_symbol || "-";
  const container = document.getElementById("symbols");
  if (!data.symbols.length) {
    container.innerHTML = '<p class="muted">暂无观察股票</p>';
    return;
  }

  container.innerHTML = data.symbols.map((symbol) => `
    <div class="symbol-chip ${symbol.code === data.current_symbol ? "active" : ""}">
      <span class="chip-name">${symbol.name || symbol.code}</span>
      <span class="chip-code">${symbol.code}</span>
      <button class="secondary" onclick="switchSymbol('${symbol.code}')">切换</button>
      <button class="danger" onclick="removeSymbol('${symbol.code}')">删</button>
    </div>
  `).join("");
}

function renderLatest(data) {
  const container = document.getElementById("latest");
  const latest = data.latest || {};
  const rows = [];

  for (const [symbol, timeframes] of Object.entries(latest)) {
    for (const [timeframe, item] of Object.entries(timeframes)) {
      const className = item.k >= 80 ? "high" : item.k <= 20 ? "low" : "";
      const zone = item.k >= 80 ? "超买" : item.k <= 20 ? "超卖" : "";
      rows.push(`
        <div class="kdj-box">
          <h3>${item.name || symbol}<span class="tf">${timeframe}</span></h3>
          <div class="close">${item.close}</div>
          <div class="kdj-vals">
            <span>K <b class="${className}">${item.k}</b></span>
            <span>D <b>${item.d}</b></span>
            <span>J <b>${item.j}</b></span>
            ${zone ? `<span class="${className}">${zone}</span>` : ""}
          </div>
          <div class="foot muted">K线 ${item.timestamp || "-"} · 更新 ${item.updated_at || "-"}</div>
        </div>
      `);
    }
  }

  container.innerHTML = rows.length ? `<div class="grid">${rows.join("")}</div>` : '<p class="muted">等待首次行情数据...</p>';
}

function formatCandleLabel(point, index) {
  if (index === 0 && point.timestamp && point.timestamp.includes("09:35")) {
    return point.timestamp.replace("09:35:00", "09:30-09:35");
  }
  return point.timestamp || "-";
}

function renderCharts(data) {
  const container = document.getElementById("charts");
  const currentSymbol = data.current_symbol;
  const seriesByTimeframe = (data.series || {})[currentSymbol] || {};
  const rows = [];

  for (const [timeframe, points] of Object.entries(seriesByTimeframe)) {
    if (!points.length) continue;
    const width = 920;
    const height = 320;
    const padding = 40;
    const plotWidth = width - padding * 2;
    const plotHeight = height - padding * 2;
    const prices = points.flatMap((point) => [point.open, point.high, point.low, point.close]);
    const minPrice = Math.min(...prices);
    const maxPrice = Math.max(...prices);
    const priceRange = maxPrice - minPrice || 1;
    const x = (index) => padding + (points.length === 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth);
    const y = (price) => padding + (maxPrice - price) / priceRange * plotHeight;
    const candleWidth = Math.max(4, Math.min(14, plotWidth / points.length * 0.55));
    const gridLines = [0.25, 0.5, 0.75].map((f) => {
      const gy = padding + plotHeight * f;
      const gv = (maxPrice - priceRange * f).toFixed(2);
      return `<line x1="${padding}" y1="${gy}" x2="${width - padding}" y2="${gy}" stroke="${CHART.grid}" stroke-width="1" />
              <text x="8" y="${gy + 4}" fill="${CHART.text}" font-size="11">${gv}</text>`;
    }).join("");
    const candles = points.map((point, index) => {
      const cx = x(index);
      const color = point.close >= point.open ? CHART.up : CHART.down;
      const top = Math.min(y(point.open), y(point.close));
      const bodyHeight = Math.max(Math.abs(y(point.open) - y(point.close)), 1);
      return `
        <line x1="${cx}" y1="${y(point.high)}" x2="${cx}" y2="${y(point.low)}" stroke="${color}" />
        <rect x="${cx - candleWidth / 2}" y="${top}" width="${candleWidth}" height="${bodyHeight}" fill="${color}" opacity="0.8" />
      `;
    }).join("");
    const markers = points.map((point, index) => {
      const label = formatCandleLabel(point, index);
      if (point.k > 80) {
        return `<circle cx="${x(index)}" cy="${y(point.high) - 8}" r="4" fill="${CHART.up}" stroke="${CHART.grid}" stroke-width="1"><title>${label} K=${point.k} 收盘=${point.close}</title></circle>`;
      }
      if (point.k < 15) {
        return `<circle cx="${x(index)}" cy="${y(point.low) + 8}" r="4" fill="${CHART.down}" stroke="${CHART.grid}" stroke-width="1"><title>${label} K=${point.k} 收盘=${point.close}</title></circle>`;
      }
      return "";
    }).join("");
    const latest = points[points.length - 1];
    const first = points[0];

    rows.push(`
      <div class="chart-box">
        <h3>${currentSymbol} ${timeframe} 价格K线</h3>
        <svg viewBox="0 0 ${width} ${height}" role="img">
          ${gridLines}
          <text x="8" y="${padding + 4}" fill="${CHART.text}" font-size="11">${maxPrice.toFixed(2)}</text>
          <text x="8" y="${height - padding + 4}" fill="${CHART.text}" font-size="11">${minPrice.toFixed(2)}</text>
          <line x1="${padding}" y1="${padding}" x2="${padding}" y2="${height - padding}" stroke="${CHART.axis}" />
          <line x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}" stroke="${CHART.axis}" />
          ${candles}
          ${markers}
          <text x="${padding}" y="${height - 12}" fill="${CHART.text}" font-size="11">${formatCandleLabel(first, 0).slice(11)}</text>
          <text x="${width - padding - 42}" y="${height - 12}" fill="${CHART.text}" font-size="11">${formatCandleLabel(latest, points.length - 1).slice(11)}</text>
        </svg>
        <p class="muted">范围：${formatCandleLabel(first, 0)} 至 ${formatCandleLabel(latest, points.length - 1)}；最新K=${latest.k}，收盘=${latest.close}</p>
      </div>
    `);
  }

  container.innerHTML = rows.length ? rows.join("") : '<p class="muted">等待KDJ走势数据...</p>';
}

function alertRowHtml(alert) {
  const direction = alert.direction === "high" ? "K值高位" : "K值低位";
  const className = alert.direction === "high" ? "high" : "low";
  return `
    <div class="alert-row">
      <div>
        <strong>${alert.name}(${alert.symbol}) ${alert.timeframe}</strong>
        <span class="alert-badge ${className}">${direction}</span>
        <div class="muted">K=${Number(alert.k).toFixed(2)} D=${Number(alert.d).toFixed(2)} J=${Number(alert.j).toFixed(2)}，邮件：${alert.email_sent ? "已发送" : "未发送/未配置"}，微信：${alert.wechat_sent === undefined ? "未配置" : alert.wechat_sent ? "已发送" : "发送失败"}</div>
      </div>
      <div class="muted">${alert.created_at}</div>
    </div>
  `;
}

function renderAlertList(container, alerts, emptyText) {
  if (!alerts.length) {
    container.innerHTML = `<p class="muted">${emptyText}</p>`;
    return;
  }
  container.innerHTML = alerts.map(alertRowHtml).join("");
}

function renderAlerts(data) {
  renderAlertList(document.getElementById("alerts"), data.alerts || [], "今天暂无提醒记录");
}

async function loadAlertHistory() {
  const date = document.getElementById("alert-date").value;
  const container = document.getElementById("alert-history");
  if (!date) {
    container.innerHTML = '<p class="muted">请选择要查询的日期</p>';
    return;
  }
  const data = await requestJson(`/api/alerts?date=${encodeURIComponent(date)}`);
  renderAlertList(container, data.alerts || [], `${date} 无提醒记录`);
}

async function refresh() {
  try {
    const data = await requestJson("/api/status");
    renderSymbols(data);
    renderLatest(data);
    renderCharts(data);
    renderAlerts(data);
    const live = document.getElementById("live-status");
    if (live) live.textContent = `实时监控中 · ${new Date().toLocaleTimeString("zh-CN")}`;
  } catch {
    const live = document.getElementById("live-status");
    if (live) live.textContent = "连接中断，重试中...";
  }
}

async function addSymbol() {
  const code = document.getElementById("symbol-code").value.trim();
  const name = document.getElementById("symbol-name").value.trim();
  if (!code) {
    alert("请输入股票代码");
    return;
  }
  await requestJson("/api/symbols", {
    method: "POST",
    body: JSON.stringify({ code, name: name || null }),
  });
  document.getElementById("symbol-code").value = "";
  document.getElementById("symbol-name").value = "";
  await refresh();
}

async function switchSymbol(code) {
  await requestJson("/api/current-symbol", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  await refresh();
}

async function removeSymbol(code) {
  if (!confirm(`确认删除 ${code}？`)) return;
  await requestJson(`/api/symbols/${code}`, { method: "DELETE" });
  await refresh();
}

document.getElementById("add-symbol").addEventListener("click", addSymbol);
document.getElementById("load-alert-history").addEventListener("click", loadAlertHistory);

// ---------- 策略回测 ----------

const BT_TIMEFRAME_LABEL = { "1d": "日线", "15m": "15分钟", "30m": "30分钟", "60m": "60分钟" };

function pct(v, digits = 1) {
  return v === null || v === undefined ? "-" : `${(v * 100).toFixed(digits)}%`;
}

function summaryCard(label, value, cls) {
  return `<div class="bt-metric ${cls || ""}"><div class="bt-metric-value">${value}</div><div class="bt-metric-label">${label}</div></div>`;
}

function renderBacktestSummary(data) {
  const s = data.summary;
  const strategyCls = s.total_return >= 0 ? "up" : "down";
  const benchCls = s.bench_return >= 0 ? "up" : "down";
  document.getElementById("bt-summary").innerHTML = `
    ${summaryCard("策略总收益", pct(s.total_return), strategyCls)}
    ${summaryCard("买入持有", pct(s.bench_return), benchCls)}
    ${summaryCard("策略最大回撤", pct(s.max_drawdown), "down")}
    ${summaryCard("持有最大回撤", pct(s.bench_max_drawdown), "down")}
    ${summaryCard("完整往返", `${s.round_trips} 次`)}
    ${summaryCard("胜率", s.win_rate === null ? "-" : pct(s.win_rate, 0))}
    ${summaryCard("平均单次", pct(s.avg_return, 2))}
    ${summaryCard("最好/最差单次", `${pct(s.best_return, 2)} / ${pct(s.worst_return, 2)}`)}
    <p class="muted bt-meta">标的 ${data.symbol} | 周期 ${BT_TIMEFRAME_LABEL[data.timeframe]} |
      K&lt;${data.params.buy}买 / K&gt;${data.params.sell}卖${data.params_auto ? " ⭐自动寻优参数" : ""} | 区间 ${s.start.slice(0, 10)} ~ ${s.end.slice(0, 10)} |
      共 ${s.bars} 根K线 | 数据源 ${data.data_source || "-"}${s.open_position ? " | 当前持仓中" : ""}</p>
    ${data.warning ? `<p class="bt-warning bt-meta">⚠️ ${data.warning}</p>` : ""}
  `;
}

function renderBacktestCurve(data) {
  const curve = data.curve;
  if (!curve.length) return;
  const width = 920, height = 280, padding = 40;
  const plotWidth = width - padding * 2, plotHeight = height - padding * 2;
  const values = curve.flatMap((p) => [p.equity, p.bench]);
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const x = (i) => padding + (curve.length === 1 ? plotWidth / 2 : (i / (curve.length - 1)) * plotWidth);
  const y = (v) => padding + (max - v) / range * plotHeight;
  const path = (key) => curve.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  const y1 = y(1);
  const gridLines = [0.25, 0.5, 0.75].map((f) => {
    const gy = padding + plotHeight * f;
    const gv = (max - range * f).toFixed(2);
    return `<line x1="${padding}" y1="${gy}" x2="${width - padding}" y2="${gy}" stroke="${CHART.grid}" stroke-width="1" />
            <text x="8" y="${gy + 4}" fill="${CHART.text}" font-size="11">${gv}</text>`;
  }).join("");
  document.getElementById("bt-curve").innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img">
      ${gridLines}
      <text x="8" y="${padding + 4}" fill="${CHART.text}" font-size="11">${max.toFixed(2)}</text>
      <text x="8" y="${height - padding + 4}" fill="${CHART.text}" font-size="11">${min.toFixed(2)}</text>
      <line x1="${padding}" y1="${padding}" x2="${padding}" y2="${height - padding}" stroke="${CHART.axis}" />
      <line x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}" stroke="${CHART.axis}" />
      <line x1="${padding}" y1="${y1}" x2="${width - padding}" y2="${y1}" stroke="${CHART.bench}" stroke-dasharray="4 4" opacity="0.6" />
      <path d="${path("bench")}" fill="none" stroke="${CHART.bench}" stroke-width="1.5" />
      <path d="${path("equity")}" fill="none" stroke="${CHART.equity}" stroke-width="2" />
      <text x="${width - padding - 210}" y="${padding - 10}" font-size="12">
        <tspan fill="${CHART.equity}">— 策略净值</tspan><tspan fill="${CHART.bench}" dx="12">— 买入持有</tspan>
      </text>
      <text x="${padding}" y="${height - 12}" fill="${CHART.text}" font-size="11">${curve[0].time.slice(0, 10)}</text>
      <text x="${width - padding - 70}" y="${height - 12}" fill="${CHART.text}" font-size="11">${curve[curve.length - 1].time.slice(0, 10)}</text>
    </svg>
  `;
}

function renderBacktestTrades(data) {
  const container = document.getElementById("bt-trades");
  if (!data.round_trips.length) {
    container.innerHTML = '<p class="muted">回测区间内没有完整交易（信号未触发或仅触发买入）</p>';
    return;
  }
  const rows = data.round_trips.map((t, i) => {
    const cls = t.return_pct >= 0 ? "up" : "down";
    const ret = t.return_pct >= 0 ? `+${(t.return_pct * 100).toFixed(2)}%` : `${(t.return_pct * 100).toFixed(2)}%`;
    return `<tr>
      <td>${i + 1}</td>
      <td>${t.buy_time}</td><td>${t.buy_price}</td>
      <td>${t.sell_time || "持仓中"}</td><td>${t.sell_price || "-"}</td>
      <td class="${cls}">${ret}</td>
    </tr>`;
  }).join("");
  container.innerHTML = `
    <table class="bt-table">
      <thead><tr><th>#</th><th>买入时间</th><th>买入价</th><th>卖出时间</th><th>卖出价</th><th>盈亏</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

async function runBacktest(useAuto = false) {
  const symbol = document.getElementById("bt-symbol").value.trim();
  const timeframe = useAuto ? "1d" : document.getElementById("bt-timeframe").value;
  const buy = document.getElementById("bt-buy").value;
  const sell = document.getElementById("bt-sell").value;
  const status = document.getElementById("bt-status");
  const resultBox = document.getElementById("bt-result");
  if (!symbol) {
    alert("请输入股票/指数代码");
    return;
  }
  if (!useAuto && Number(buy) >= Number(sell)) {
    alert("买入阈值必须小于卖出阈值");
    return;
  }
  if (useAuto) {
    document.getElementById("bt-timeframe").value = "1d";
  }
  status.textContent = useAuto
    ? "回测中，使用该标的自动寻优的最优K值区间（日线）..."
    : "回测中，正在拉取行情数据（分钟线约几秒，日线约十几秒）...";
  resultBox.style.display = "none";
  document.getElementById("bt-run").disabled = true;
  document.getElementById("bt-auto").disabled = true;
  try {
    const params = new URLSearchParams({ symbol, timeframe, buy, sell, auto: useAuto });
    const resp = await fetch(`/api/backtest?${params}`);
    if (resp.status === 202) {
      const detail = (await resp.json()).detail;
      status.textContent = `⏳ ${detail}`;
      return;
    }
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    if (data.params_auto) {
      document.getElementById("bt-buy").value = data.params.buy;
      document.getElementById("bt-sell").value = data.params.sell;
    }
    renderBacktestSummary(data);
    renderBacktestCurve(data);
    renderBacktestTrades(data);
    status.textContent = "";
    resultBox.style.display = "block";
  } catch (err) {
    status.textContent = `回测失败：${err.message}`;
  } finally {
    document.getElementById("bt-run").disabled = false;
    document.getElementById("bt-auto").disabled = false;
  }
}

async function refreshBestInfo() {
  const symbol = document.getElementById("bt-symbol").value.trim();
  const info = document.getElementById("bt-best-info");
  if (!symbol) { info.textContent = ""; return; }
  try {
    const data = await requestJson(`/api/best-params?symbol=${encodeURIComponent(symbol)}`);
    if (data.optimizing) {
      info.textContent = "⏳ 最优参数寻优中...";
    } else if (data.best) {
      const b = data.best;
      info.textContent = `最优: K<${b.buy}买/K>${b.sell}卖 收益${(b.total_return * 100).toFixed(0)}% 回撤${(b.max_drawdown * 100).toFixed(0)}% (${b.range})`;
    } else {
      info.textContent = "该标的尚未寻优（添加自选或点⭐自动触发）";
    }
  } catch { info.textContent = ""; }
}

document.getElementById("bt-run").addEventListener("click", () => runBacktest(false));
document.getElementById("bt-auto").addEventListener("click", () => runBacktest(true));
document.getElementById("bt-symbol").addEventListener("input", refreshBestInfo);
document.getElementById("bt-symbol").addEventListener("change", refreshBestInfo);

refresh();
setInterval(refresh, 10000);
