let writeAccessReady = false;

function setWriteAccess(ready, message) {
  writeAccessReady = Boolean(ready);
  document.querySelectorAll("[data-write-action]").forEach((button) => {
    button.disabled = !writeAccessReady;
    button.title = writeAccessReady ? "" : "请先在页面顶部保存有效写入令牌";
  });
  const status = document.getElementById("write-token-status");
  if (status && message) status.textContent = message;
}

function requireWriteAccess(statusElement) {
  if (writeAccessReady) return true;
  const message = "请先在页面顶部填写写入令牌并点击“保存并校验”；服务器可运行 ./scripts/show-write-token.sh 获取。";
  if (statusElement) statusElement.textContent = message;
  document.getElementById("write-token")?.focus();
  return false;
}

async function requestJson(url, options) {
  const method = String(options?.method || "GET").toUpperCase();
  const writeToken = localStorage.getItem("kdjWriteToken") || "";
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(method !== "GET" && writeToken ? { "X-API-Key": writeToken } : {}),
    },
    ...options,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      localStorage.removeItem("kdjWriteToken");
      setWriteAccess(false, "写入令牌缺失或已失效，请重新填写并校验。服务器可运行 ./scripts/show-write-token.sh 获取。");
    }
    throw new Error(data.detail || `请求失败: ${response.status}`);
  }
  return response.json();
}

async function saveWriteToken() {
  const input = document.getElementById("write-token");
  const status = document.getElementById("write-token-status");
  const token = input?.value.trim() || "";
  if (!token) {
    localStorage.removeItem("kdjWriteToken");
    setWriteAccess(false, "写入令牌已清除；成交录入和配置修改已禁用。查询与股票切换不受影响。");
    return;
  }
  localStorage.setItem("kdjWriteToken", token);
  if (status) status.textContent = "正在校验写入令牌...";
  try {
    await requestJson("/api/auth/verify", { method: "POST", body: "{}" });
    input.value = "";
    setWriteAccess(true, "写入令牌有效，成交录入和配置修改已启用。令牌只保存在当前浏览器。");
  } catch (err) {
    localStorage.removeItem("kdjWriteToken");
    setWriteAccess(false, `令牌无效，未保存：${err.message}`);
  }
}

async function initializeWriteAccess() {
  try {
    const auth = await requestJson("/api/auth/status");
    if (!auth.write_token_required) {
      document.getElementById("write-auth-row")?.setAttribute("hidden", "hidden");
      setWriteAccess(true, "");
      return;
    }
  } catch (err) {
    setWriteAccess(false, `读取写入权限失败：${err.message}`);
    return;
  }
  if (!localStorage.getItem("kdjWriteToken")) {
    setWriteAccess(false, "尚未配置写入令牌：查询和切换可用；成交录入、添加和删除已禁用。服务器可运行 ./scripts/show-write-token.sh 获取令牌。");
    return;
  }
  setWriteAccess(false, "正在校验浏览器中保存的写入令牌...");
  try {
    await requestJson("/api/auth/verify", { method: "POST", body: "{}" });
    setWriteAccess(true, "写入令牌有效，成交录入和配置修改已启用。");
  } catch (err) {
    setWriteAccess(false, `保存的令牌已失效：${err.message}`);
  }
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
      <button class="danger" data-write-action onclick="removeSymbol('${symbol.code}')" ${writeAccessReady ? "" : "disabled"}>删</button>
    </div>
  `).join("");
}

function kdjThresholds(item, data) {
  const best = item && item.best_thresholds ? item.best_thresholds : {};
  const config = data && data.config && data.config.kdj ? data.config.kdj : {};
  return {
    buy: Number(best.buy ?? config.lower ?? 20),
    sell: Number(best.sell ?? config.upper ?? 80),
    auto: Boolean(best.auto),
  };
}

function renderLatest(data) {
  const container = document.getElementById("latest");
  const latest = data.latest || {};
  const rows = [];

  for (const [symbol, timeframes] of Object.entries(latest)) {
    for (const [timeframe, item] of Object.entries(timeframes)) {
      const thresholds = kdjThresholds(item, data);
      const className = item.k >= thresholds.sell ? "high" : item.k <= thresholds.buy ? "low" : "";
      const zone = item.k >= thresholds.sell ? "超买" : item.k <= thresholds.buy ? "超卖" : "";
      const label = item.estimated ? `${timeframe} 盘中折算` : timeframe;
      const note = item.note ? `<div class="foot muted">${item.note}</div>` : "";
      const thresholdText = `阈值 K&lt;${thresholds.buy} / K&gt;${thresholds.sell}${thresholds.auto ? " · 个股最优" : " · 默认"}`;
      rows.push(`
        <div class="kdj-box ${item.estimated ? "estimated" : ""}">
          <h3>${item.name || symbol}<span class="tf">${label}</span></h3>
          <div class="close">${item.close}</div>
          <div class="kdj-vals">
            <span>K <b class="${className}">${item.k}</b></span>
            <span>D <b>${item.d}</b></span>
            <span>J <b>${item.j}</b></span>
            ${zone ? `<span class="${className}">${zone}</span>` : ""}
          </div>
          <div class="foot muted">K线 ${item.timestamp || "-"} · 更新 ${item.updated_at || "-"}</div>
          <div class="foot muted">${thresholdText}</div>
          ${note}
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

function renderChartHint(data, currentSymbol) {
  const hint = document.getElementById("chart-hint");
  if (!hint) return;
  const symbolLatest = ((data.latest || {})[currentSymbol] || {});
  const thresholdSource = symbolLatest["1d_est"] || symbolLatest["1d"] || Object.values(symbolLatest)[0] || {};
  const thresholds = kdjThresholds(thresholdSource, data);
  const name = thresholdSource.name || currentSymbol || "当前股票";
  const pointHint = currentSymbol === "002179" ? "；10分钟图每根K线上方显示K值，悬停或点按可查看精确价格。" : "。";
  hint.innerHTML = `${name}(${currentSymbol || "-"})：红点 = K≥${thresholds.sell} 超买/卖出阈值 · 绿点 = K≤${thresholds.buy} 超卖/买入阈值${thresholds.auto ? "（个股最优参数）" : "（默认参数，暂无个股寻优结果）"}${pointHint}`;
}

function chartPointReadout(point) {
  const value = (input, digits = 2) => Number.isFinite(Number(input)) ? Number(input).toFixed(digits) : "-";
  return `
    <span><b>时间</b>${formatCandleLabel(point, 0)}</span>
    <span><b>收盘</b>${value(point.close)}</span>
    <span><b>K</b>${value(point.k)}</span>
    <span class="chart-readout-detail"><b>开/高/低</b>${value(point.open)} / ${value(point.high)} / ${value(point.low)}</span>
    <span class="chart-readout-detail"><b>D/J</b>${value(point.d)} / ${value(point.j)}</span>
  `;
}

function bindChartPointReadouts(container) {
  container.querySelectorAll(".chart-box.has-point-readout").forEach((box) => {
    const readout = box.querySelector(".chart-point-readout");
    if (!readout) return;
    box.querySelectorAll(".candle-point").forEach((candle) => {
      const showPoint = () => {
        readout.innerHTML = `
          <span><b>时间</b>${candle.dataset.time || "-"}</span>
          <span><b>收盘</b>${candle.dataset.close || "-"}</span>
          <span><b>K</b>${candle.dataset.k || "-"}</span>
          <span class="chart-readout-detail"><b>开/高/低</b>${candle.dataset.open || "-"} / ${candle.dataset.high || "-"} / ${candle.dataset.low || "-"}</span>
          <span class="chart-readout-detail"><b>D/J</b>${candle.dataset.d || "-"} / ${candle.dataset.j || "-"}</span>
        `;
        box.querySelectorAll(".candle-point.active").forEach((item) => item.classList.remove("active"));
        candle.classList.add("active");
      };
      candle.addEventListener("pointerenter", showPoint);
      candle.addEventListener("pointerdown", showPoint);
      candle.addEventListener("focus", showPoint);
    });
  });
}

function renderCharts(data) {
  const container = document.getElementById("charts");
  const currentSymbol = data.current_symbol;
  renderChartHint(data, currentSymbol);
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
    const showPointValues = currentSymbol === "002179" && timeframe === "10m";
    const candles = points.map((point, index) => {
      const cx = x(index);
      const color = point.close >= point.open ? CHART.up : CHART.down;
      const top = Math.min(y(point.open), y(point.close));
      const bodyHeight = Math.max(Math.abs(y(point.open) - y(point.close)), 1);
      const label = formatCandleLabel(point, index);
      const pointAttributes = showPointValues
        ? `class="candle-point" tabindex="0" data-time="${label}" data-open="${Number(point.open).toFixed(2)}" data-high="${Number(point.high).toFixed(2)}" data-low="${Number(point.low).toFixed(2)}" data-close="${Number(point.close).toFixed(2)}" data-k="${Number(point.k).toFixed(2)}" data-d="${Number(point.d).toFixed(2)}" data-j="${Number(point.j).toFixed(2)}"`
        : "";
      return `
        <g ${pointAttributes}>
          <line x1="${cx}" y1="${y(point.high)}" x2="${cx}" y2="${y(point.low)}" stroke="${color}" />
          <rect x="${cx - candleWidth / 2}" y="${top}" width="${candleWidth}" height="${bodyHeight}" fill="${color}" opacity="0.8" />
          ${showPointValues ? `<rect class="candle-hit-area" x="${cx - Math.max(candleWidth, 18) / 2}" y="${padding}" width="${Math.max(candleWidth, 18)}" height="${plotHeight}" />` : ""}
          <title>${label} 开=${Number(point.open).toFixed(2)} 高=${Number(point.high).toFixed(2)} 低=${Number(point.low).toFixed(2)} 收=${Number(point.close).toFixed(2)} K=${Number(point.k).toFixed(2)} D=${Number(point.d).toFixed(2)} J=${Number(point.j).toFixed(2)}</title>
        </g>
      `;
    }).join("");
    const thresholdSource = ((data.latest || {})[currentSymbol] || {})[timeframe]
      || ((data.latest || {})[currentSymbol] || {})["1d_est"]
      || {};
    const thresholds = kdjThresholds(thresholdSource, data);
    const markers = points.map((point, index) => {
      const label = formatCandleLabel(point, index);
      if (point.k >= thresholds.sell) {
        return `<circle cx="${x(index)}" cy="${y(point.high) - 8}" r="4" fill="${CHART.up}" stroke="${CHART.grid}" stroke-width="1"><title>${label} K=${point.k} ≥ ${thresholds.sell} 收盘=${point.close}</title></circle>`;
      }
      if (point.k <= thresholds.buy) {
        return `<circle cx="${x(index)}" cy="${y(point.low) + 8}" r="4" fill="${CHART.down}" stroke="${CHART.grid}" stroke-width="1"><title>${label} K=${point.k} ≤ ${thresholds.buy} 收盘=${point.close}</title></circle>`;
      }
      return "";
    }).join("");
    const latest = points[points.length - 1];
    const first = points[0];
    const kLabels = showPointValues ? points.map((point, index) => {
      const labelY = Math.max(14, y(point.high) - 10);
      return `<text class="candle-k-label" x="${x(index)}" y="${labelY}" text-anchor="middle">K${Number(point.k).toFixed(1)}</text>`;
    }).join("") : "";

    rows.push(`
      <div class="chart-box ${showPointValues ? "has-point-readout" : ""}">
        <h3>${currentSymbol} ${timeframe} 价格K线</h3>
        ${showPointValues ? `<div class="chart-point-readout" aria-live="polite">${chartPointReadout(latest)}</div>` : ""}
        <svg viewBox="0 0 ${width} ${height}" role="img">
          ${gridLines}
          <text x="8" y="${padding + 4}" fill="${CHART.text}" font-size="11">${maxPrice.toFixed(2)}</text>
          <text x="8" y="${height - padding + 4}" fill="${CHART.text}" font-size="11">${minPrice.toFixed(2)}</text>
          <line x1="${padding}" y1="${padding}" x2="${padding}" y2="${height - padding}" stroke="${CHART.axis}" />
          <line x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}" stroke="${CHART.axis}" />
          ${candles}
          ${markers}
          ${kLabels}
          <text x="${padding}" y="${height - 12}" fill="${CHART.text}" font-size="11">${formatCandleLabel(first, 0).slice(11)}</text>
          <text x="${width - padding - 42}" y="${height - 12}" fill="${CHART.text}" font-size="11">${formatCandleLabel(latest, points.length - 1).slice(11)}</text>
        </svg>
        <p class="muted">范围：${formatCandleLabel(first, 0)} 至 ${formatCandleLabel(latest, points.length - 1)}；最新K=${latest.k}，阈值K&lt;${thresholds.buy}/K&gt;${thresholds.sell}${thresholds.auto ? "（个股最优）" : "（默认）"}，收盘=${latest.close}</p>
      </div>
    `);
  }

  container.innerHTML = rows.length ? rows.join("") : '<p class="muted">等待KDJ走势数据...</p>';
  bindChartPointReadouts(container);
}

function alertRowHtml(alert) {
  if (alert.type === "price_target") {
    const wechatText = alert.wechat_sent === undefined ? "未配置" : alert.wechat_sent ? "已发送" : "发送失败";
    return `
      <div class="alert-row">
        <div>
          <strong>${alert.name}(${alert.symbol}) ${alert.timeframe}</strong>
          <span class="alert-badge low">候选买入1手</span>
          <div class="muted">现价=${Number(alert.close).toFixed(2)}，计划价=${Number(alert.target_price).toFixed(2)}，提醒上沿=${Number(alert.trigger_price).toFixed(2)}，微信：${wechatText}</div>
        </div>
        <div class="muted">${alert.created_at}</div>
      </div>
    `;
  }
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

function decisionPct(value) {
  return value === null || value === undefined ? "-" : `${(Number(value) * 100).toFixed(2)}%`;
}

async function renderDecisionPlan(symbol) {
  const container = document.getElementById("decision-plan");
  if (!container || !symbol) return;
  try {
    const plan = await requestJson(`/api/decision-plan?symbol=${encodeURIComponent(symbol)}`);
    const d = plan.decision || {};
    const ledger = (plan.facts || {}).ledger || {};
    const perf = plan.performance || {};
    const market = plan.market || {};
    const reverseT = plan.reverse_t || {};
    const reverseDecision = reverseT.decision || {};
    const reverseRule = reverseT.rule || {};
    const reversePrice = reverseT.price_plan || {};
    const statusClass = d.action === "buy_core" ? "low" : d.action === "sell_tactical" ? "high" : "";
    const actionLabel = {
      hold: "持有 / 不操作",
      buy_core: "分批买入核心仓",
      sell_tactical: "卖出T仓",
      review: "人工复核",
      review_core_buyback: "检查待补回仓位",
      wait_buyback: "等待补回核心仓",
      buyback_core: "盈利补回核心仓",
      protective_buyback: "保护性补回核心仓",
      wait_limit_buy: "等待现有买入挂单",
    }[d.action] || d.action || "-";
    const price = plan.price_plan;
    const priceText = price && price.execution === "limit_zone"
      ? `${price.lower.toFixed(2)}—${price.upper.toFixed(2)}，高于${price.do_not_chase_above.toFixed(2)}不追`
      : price && price.execution === "next_session_open" ? "下一交易时段开盘" : "无有效挂单价位";
    const displayPriceText = price && price.execution === "existing_limit_order"
      ? `已有${price.price.toFixed(2)}元买入${price.lots}手挂单，等待成交，不重复下单`
      : price && price.profit_buyback !== undefined && price.protective_buyback === undefined
        ? `只在${price.profit_buyback.toFixed(2)}元盈利回补；上涨时不高价追回`
      : price && price.profit_buyback !== undefined
        ? `盈利补回${price.profit_buyback.toFixed(2)}元；若继续上冲至${price.protective_buyback.toFixed(2)}元，保护性补回`
      : priceText;
    const failed = (plan.gates || []).filter((g) => !g.passed).map((g) => `<li>${g.detail}</li>`).join("");
    const reverseActionLabel = {
      hold: "等待",
      sell_core_for_reverse_t: "冲高卖出老仓",
      wait_buyback: "等待回补",
      buyback_core: "盈利回补",
      protective_buyback: "保护性回补",
      review: "人工复核",
    }[reverseDecision.action] || reverseDecision.action || "-";
    const reversePriceText = reversePrice.sell_limit !== undefined && reversePrice.protective_buyback === undefined
      ? `卖出约${reversePrice.sell_limit.toFixed(2)}，只在${reversePrice.expected_buyback.toFixed(2)}盈利回补；上涨时不高价追回`
      : reversePrice.sell_limit !== undefined
      ? `卖出约${reversePrice.sell_limit.toFixed(2)}，按约${((reversePrice.target_gap_ratio ?? 0) * 100).toFixed(1)}%价差在${reversePrice.expected_buyback.toFixed(2)}回补，向上${reversePrice.protective_buyback.toFixed(2)}保护性补回`
      : reversePrice.profit_buyback !== undefined && reversePrice.protective_buyback === undefined
        ? `按约${((reversePrice.target_gap_ratio ?? 0) * 100).toFixed(1)}%价差只在${reversePrice.profit_buyback.toFixed(2)}盈利回补；上涨时不高价追回`
      : reversePrice.profit_buyback !== undefined
        ? `按约${((reversePrice.target_gap_ratio ?? 0) * 100).toFixed(1)}%价差在${reversePrice.profit_buyback.toFixed(2)}盈利回补，向上${reversePrice.protective_buyback.toFixed(2)}保护性补回`
        : "当前无反T执行价位";
    container.innerHTML = `
      <div class="decision-head">
        <div><span class="decision-label">${plan.symbol.name}(${plan.symbol.code})</span><span class="decision-date">正式日线 ${plan.signal_date}</span></div>
        <strong class="${statusClass}">${actionLabel} · 最多 ${d.max_lots ?? 0} 手</strong>
      </div>
      <div class="bt-summary decision-metrics">
        ${summaryCard("核心 / T仓", `${ledger.core_lots ?? 0} / ${ledger.t_lots ?? 0} 手`)}
        ${summaryCard("账本保本成本", ledger.breakeven_cost?.toFixed(3) ?? "-")}
        ${summaryCard("策略账户收益", decisionPct(perf.sleeve_return), (perf.sleeve_return ?? 0) >= 0 ? "up" : "down")}
        ${summaryCard("持仓收益", decisionPct(perf.deployed_position_return), (perf.deployed_position_return ?? 0) >= 0 ? "up" : "down")}
        ${summaryCard("资金部署", decisionPct(perf.deployed_ratio))}
        ${summaryCard("正式K / D", `${market.k?.toFixed(2) ?? "-"} / ${market.d?.toFixed(2) ?? "-"}`)}
        ${reverseT.enabled ? summaryCard("反T额度", `${reverseT.quota_lots ?? 0}手（单次1手）`) : ""}
      </div>
      <p><strong>机械结论：</strong>${d.summary || "-"}</p>
      <p><strong>执行价位：</strong>${displayPriceText}</p>
      <p><strong>T+1：</strong>当前可卖${plan.facts.t1.sellable_lots_now}手，锁定${plan.facts.t1.locked_lots_now}手；下一交易时段现有仓位最多可卖${plan.facts.t1.sellable_lots_next_session}手。</p>
      ${reverseT.enabled ? `
        <div class="decision-blockers">
          <strong>冲高反T</strong>
          <p>规则：${reverseRule.summary || "价格冲高且10分钟K从80以上拐头；不使用MA均线。"}</p>
          <p>结论：${reverseActionLabel}，最多${reverseDecision.max_lots ?? 0}手；${reverseDecision.summary || "-"}</p>
          <p>价位：${reversePriceText}</p>
          <p>纪律：最多动用总仓位20%，当前至少保留${reverseT.core_floor_lots ?? 0}手核心仓；盈利回补目标约${((reverseT.buyback_gap_ratio ?? 0) * 100).toFixed(1)}%。</p>
        </div>` : ""}
      ${failed ? `<div class="decision-blockers"><strong>未通过检查</strong><ul>${failed}</ul></div>` : ""}
    `;
  } catch (err) {
    container.innerHTML = `<p class="muted">${err.message}</p>`;
  }
}

async function renderStrategyValidation(symbol) {
  const container = document.getElementById("strategy-validation");
  if (!container || !symbol) return;
  const [performance, research, shadow, stageCapital, runtimeStatus, authStatus] = await Promise.all([
    requestJson(`/api/performance?symbol=${encodeURIComponent(symbol)}`).catch(() => null),
    requestJson(`/api/research/walk-forward?symbol=${encodeURIComponent(symbol)}`).catch(() => null),
    requestJson(`/api/shadow-decisions?symbol=${encodeURIComponent(symbol)}`).catch(() => null),
    requestJson(`/api/research/stage-capital?symbol=${encodeURIComponent(symbol)}`).catch(() => null),
    requestJson("/api/runtime-status").catch(() => null),
    requestJson("/api/auth/status").catch(() => null),
  ]);
  if (!performance && !research && !shadow && !stageCapital && !runtimeStatus) {
    container.innerHTML = '<p class="muted">该标的尚无策略净值或样本外报告。</p>';
    return;
  }
  const sections = [];
  if (performance) {
    const s = performance.summary || {};
    const latest = s.latest || {};
    sections.push(`
      <h3>真实策略资金轨迹</h3>
      <div class="bt-summary decision-metrics">
        ${summaryCard("最新策略净值", latest.equity?.toFixed(2) ?? "-")}
        ${summaryCard("策略账户收益", decisionPct(latest.sleeve_return), (latest.sleeve_return ?? 0) >= 0 ? "up" : "down")}
        ${summaryCard("当前回撤", decisionPct(s.current_drawdown), "down")}
        ${summaryCard("历史最大回撤", decisionPct(s.max_drawdown), "down")}
        ${summaryCard("高水位", s.high_water_equity?.toFixed(2) ?? "-")}
        ${summaryCard("净值快照", `${s.snapshot_count ?? 0} 日`)}
      </div>
      <p class="muted">区间 ${s.first_date || "-"} 至 ${s.last_date || "-"}；最大回撤发生日 ${s.max_drawdown_date || "-"}。</p>
    `);
  }
  if (research) {
    const current = research.current_rule_oos || {};
    const selected = research.selected_oos || {};
    const status = research.accepted_for_shadow_validation ? "达到影子验证门槛" : "未达到影子验证门槛";
    sections.push(`
      <h3>滚动样本外研究</h3>
      <div class="bt-summary decision-metrics">
        ${summaryCard("当前规则样本外事件", `${current.events ?? 0} 次`)}
        ${summaryCard("当前规则胜率", decisionPct(current.win_rate))}
        ${summaryCard("30日平均收益", decisionPct(current.avg_return), (current.avg_return ?? 0) >= 0 ? "up" : "down")}
        ${summaryCard("10%分位收益", decisionPct(current.p10_return), "down")}
        ${summaryCard("最差30日收益", decisionPct(current.worst_return), "down")}
        ${summaryCard("最差期间下探", decisionPct(current.worst_mae), "down")}
      </div>
      <p><strong>结论：</strong>${status}，不会自动修改实盘规则。滚动选择规则样本外${selected.events ?? 0}次，胜率${decisionPct(selected.win_rate)}，平均收益${decisionPct(selected.avg_return)}。</p>
      <p class="muted">当前规则状态分层：${Object.entries(research.current_rule_oos_by_regime || {}).map(([label, value]) => `${label} ${value.events}次/均值${decisionPct(value.avg_return)}`).join("；") || "暂无"}。</p>
      <p class="muted">数据 ${research.data_range}；${research.folds?.length ?? 0}个测试窗口；报告生成 ${research.generated_at}。</p>
    `);
  }
  if (shadow) {
    const s = shadow.summary || {};
    const latest = s.latest || {};
    const latestDecision = latest.plan?.decision || {};
    const horizons = Object.entries(s.by_horizon || {});
    sections.push(`
      <h3>真实影子决策评分</h3>
      <div class="bt-summary decision-metrics">
        ${summaryCard("留痕决策", `${s.records ?? 0} 次`)}
        ${summaryCard("等待到期", `${s.pending_records ?? 0} 次`)}
        ${summaryCard("最新信号日", latest.signal_date || "-")}
        ${summaryCard("最新动作", latestDecision.action || "-")}
        ${summaryCard("信号价格状态", latest.regime?.label || "-")}
        ${summaryCard("跟踪周期", horizons.length ? horizons.map(([h]) => `${h}日`).join("/") : "-")}
      </div>
      <p><strong>分周期：</strong>${horizons.map(([h, value]) => `${h}日 ${value.evaluated}次，正向率${decisionPct(value.favorable_rate)}`).join("；") || "尚未有到期样本"}。</p>
      <p class="muted">${s.sample_warning || "样本达到基础观察数量，但仍须结合回撤与不同状态复核。"}</p>
    `);
  }
  if (stageCapital) {
    const advancement = stageCapital.advancement || {};
    const stages = stageCapital.stages || [];
    const current = stages.find((item) => item.lots === stageCapital.current_stage_lots) || {};
    const next = stages.find((item) => item.lots === stageCapital.next_stage_lots) || {};
    sections.push(`
      <h3>分阶段资金曲线</h3>
      <div class="bt-summary decision-metrics">
        ${summaryCard("当前阶段", `${stageCapital.current_stage_lots ?? "-"}手`)}
        ${summaryCard("下一阶段", `${stageCapital.next_stage_lots ?? "-"}手`)}
        ${summaryCard("当前阶段曲线收益", decisionPct(current.total_return), (current.total_return ?? 0) >= 0 ? "up" : "down")}
        ${summaryCard("当前阶段最大回撤", decisionPct(current.max_drawdown), "down")}
        ${summaryCard("下一阶段曲线收益", decisionPct(next.total_return), (next.total_return ?? 0) >= 0 ? "up" : "down")}
        ${summaryCard("阶段结论", advancement.allowed ? "允许晋级" : "保持当前")}
      </div>
      <p><strong>晋级检查：</strong>${(advancement.gates || []).map((gate) => `${gate.passed ? "✓" : "✗"}${gate.detail}`).join("；") || "暂无"}。</p>
      <p class="muted">${stageCapital.method?.note || ""}；独立样本${stageCapital.method?.non_overlapping_events ?? 0}次，报告生成${stageCapital.generated_at || "-"}。</p>
    `);
  }
  if (runtimeStatus) {
    const calendar = runtimeStatus.calendar || {};
    sections.push(`
      <h3>运行保护</h3>
      <div class="bt-summary decision-metrics">
        ${summaryCard("今日交易日", calendar.is_session ? "是" : "否")}
        ${summaryCard("日历已核验", calendar.verified ? "是" : "否")}
        ${summaryCard("下一交易日", runtimeStatus.next_session || "未知")}
        ${summaryCard("持久化提醒", `${runtimeStatus.persisted_alerts ?? 0} 条`)}
        ${summaryCard("持久化任务", `${runtimeStatus.persisted_tasks ?? 0} 项`)}
        ${summaryCard("写入方式", authStatus?.write_token_required ? "令牌保护" : "可信内网直接录入")}
      </div>
      <p class="muted">日历状态：${calendar.reason || "-"}；成交纠错审计${runtimeStatus.corrections ?? 0}次（不保存被纠正的错误值）。</p>
    `);
  }
  container.innerHTML = sections.join("");
}

function renderTradeStatus(message, isError = false) {
  const el = document.getElementById("trade-status");
  if (!el) return;
  el.textContent = message || "";
  el.style.color = isError ? "var(--up)" : "var(--ink-3)";
}

async function submitTradeReport() {
  const code = document.getElementById("trade-code").value.trim();
  const side = document.getElementById("trade-side").value;
  const bucket = document.getElementById("trade-bucket").value;
  const lots = Number(document.getElementById("trade-lots").value);
  const price = document.getElementById("trade-price").value;
  const note = document.getElementById("trade-note").value.trim();

  if (!requireWriteAccess(document.getElementById("trade-status"))) return;

  if (!code) {
    renderTradeStatus("请输入股票代码", true);
    return;
  }
  if (!Number.isFinite(lots) || lots <= 0) {
    renderTradeStatus("请输入有效手数", true);
    return;
  }
  if (!price || Number.isNaN(Number(price)) || Number(price) <= 0) {
    renderTradeStatus("成交价是账本必填项，请输入有效成交价", true);
    return;
  }

  renderTradeStatus("正在提交仓位记录...");
  try {
    const data = await requestJson("/api/trade-report", {
      method: "POST",
      body: JSON.stringify({
        code,
        side,
        bucket,
        lots,
        price: price ? Number(price) : null,
        note: note || null,
      }),
    });
    const pos = data.position || {};
    renderTradeStatus(
      `已记录：${code} ${side === "buy" ? "买入" : "卖出"} ${lots} 手，当前核心仓 ${pos.base_lots_remaining ?? "-"} 手，T仓 ${pos.t_lots_held ?? "-"} 手，可卖 ${pos.ledger?.sellable_lots_today ?? "-"} 手，保本成本 ${pos.ledger?.breakeven_cost ?? "-"}。`
    );
    document.getElementById("trade-price").value = "";
    document.getElementById("trade-note").value = "";
    await refresh();
  } catch (err) {
    renderTradeStatus(`提交失败：${err.message}`, true);
  }
}

async function loadTradesForCorrection() {
  const code = document.getElementById("correction-code").value.trim();
  const status = document.getElementById("correction-status");
  if (!code) return;
  try {
    const data = await requestJson(`/api/trades?symbol=${encodeURIComponent(code)}`);
    const select = document.getElementById("correction-trade-id");
    select.innerHTML = '<option value="">选择成交</option>' + (data.trades || []).slice().reverse().map((trade) =>
      `<option value="${trade.id}">${trade.reported_at} ${trade.side === "buy" ? "买" : "卖"}${trade.lots}手 @ ${trade.price} [${trade.id}]</option>`
    ).join("");
    status.textContent = `已加载${data.trades?.length || 0}笔成交。`;
  } catch (err) {
    status.textContent = err.message;
  }
}

async function submitTradeCorrection(deleteTrade = false) {
  const code = document.getElementById("correction-code").value.trim();
  const tradeId = document.getElementById("correction-trade-id").value;
  const status = document.getElementById("correction-status");
  if (!requireWriteAccess(status)) return;
  if (!code || !tradeId) {
    status.textContent = "请先加载并选择一笔成交。";
    return;
  }
  const replacement = {};
  const price = document.getElementById("correction-price").value;
  const lots = document.getElementById("correction-lots").value;
  const bucket = document.getElementById("correction-bucket").value;
  if (price) replacement.price = Number(price);
  if (lots) replacement.lots = Number(lots);
  if (bucket) replacement.bucket = bucket;
  if (!deleteTrade && !Object.keys(replacement).length) {
    status.textContent = "请填写至少一个最终正确值。";
    return;
  }
  const message = deleteTrade ? "确认删除这条错误成交记录？" : "确认用填写的最终值替换该成交？";
  if (!confirm(message)) return;
  try {
    const result = await requestJson("/api/trade-corrections", {
      method: "POST",
      body: JSON.stringify({ code, trade_id: tradeId, replacement: deleteTrade ? null : replacement, delete: deleteTrade, confirm: true }),
    });
    status.textContent = `已完成，当前核心仓${result.position?.ledger?.core_lots ?? "-"}手，成本${result.position?.ledger?.breakeven_cost ?? "-"}。`;
    document.getElementById("correction-price").value = "";
    document.getElementById("correction-lots").value = "";
    document.getElementById("correction-bucket").value = "";
    await loadTradesForCorrection();
    await refresh();
  } catch (err) {
    status.textContent = `纠正失败：${err.message}`;
  }
}

async function loadAlertHistory() {
  const date = document.getElementById("alert-date").value;
  const container = document.getElementById("alert-history");
  if (!date) {
    container.innerHTML = '<p class="muted">请选择要查询的日期</p>';
    return;
  }
  try {
    const data = await requestJson(`/api/alerts?date=${encodeURIComponent(date)}`);
    renderAlertList(container, data.alerts || [], `${date} 无提醒记录`);
  } catch (err) {
    container.innerHTML = `<p class="muted">查询失败：${err.message}</p>`;
  }
}

async function refresh() {
  try {
    const data = await requestJson("/api/status");
    renderSymbols(data);
    renderLatest(data);
    renderCharts(data);
    renderAlerts(data);
    await renderDecisionPlan(data.current_symbol);
    await renderStrategyValidation(data.current_symbol);
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
  const status = document.getElementById("symbol-status");
  if (!requireWriteAccess(status)) return;
  if (!code) {
    if (status) status.textContent = "请输入股票代码";
    return;
  }
  try {
    await requestJson("/api/symbols", {
      method: "POST",
      body: JSON.stringify({ code, name: name || null }),
    });
    document.getElementById("symbol-code").value = "";
    document.getElementById("symbol-name").value = "";
    if (status) status.textContent = `已添加并切换到 ${name || code}(${code})`;
    await refresh();
  } catch (err) {
    if (status) status.textContent = `添加失败：${err.message}`;
  }
}

async function switchSymbol(code) {
  const status = document.getElementById("symbol-status");
  try {
    await requestJson("/api/current-symbol", {
      method: "POST",
      body: JSON.stringify({ code }),
    });
    if (status) status.textContent = `已切换到 ${code}`;
    await refresh();
  } catch (err) {
    if (status) status.textContent = `切换失败：${err.message}`;
  }
}

async function removeSymbol(code) {
  const status = document.getElementById("symbol-status");
  if (!requireWriteAccess(status)) return;
  if (!confirm(`确认删除 ${code}？`)) return;
  try {
    await requestJson(`/api/symbols/${code}`, { method: "DELETE" });
    if (status) status.textContent = `已删除 ${code}`;
    await refresh();
  } catch (err) {
    if (status) status.textContent = `删除失败：${err.message}`;
  }
}

document.getElementById("add-symbol").addEventListener("click", addSymbol);
document.getElementById("save-write-token").addEventListener("click", saveWriteToken);
document.getElementById("load-alert-history").addEventListener("click", loadAlertHistory);
document.getElementById("trade-submit").addEventListener("click", submitTradeReport);
document.getElementById("load-trades").addEventListener("click", loadTradesForCorrection);
document.getElementById("apply-correction").addEventListener("click", () => submitTradeCorrection(false));
document.getElementById("delete-trade").addEventListener("click", () => submitTradeCorrection(true));

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
  } catch (err) { info.textContent = `读取最优参数失败：${err.message}`; }
}

document.getElementById("bt-run").addEventListener("click", () => runBacktest(false));
document.getElementById("bt-auto").addEventListener("click", () => runBacktest(true));
document.getElementById("bt-symbol").addEventListener("input", refreshBestInfo);
document.getElementById("bt-symbol").addEventListener("change", refreshBestInfo);

// ---------- 波段买卖分析 ----------

const BAND_SYMBOL = "002179";
const BAND_PERIODS = {};
let bandCurrentStart = "";

function bandPeriodLabel(start) {
  return BAND_PERIODS[start] || start;
}

async function initializeBandPeriods() {
  const container = document.getElementById("band-periods");
  const status = document.getElementById("band-status");
  try {
    const data = await requestJson(`/api/band-analysis/periods?symbol=${BAND_SYMBOL}`);
    const periods = data.periods || [];
    if (!periods.length) throw new Error("没有足够的历史数据");
    periods.forEach((period) => { BAND_PERIODS[period.start_date] = period.label; });
    container.innerHTML = '<span style="line-height:36px;color:var(--text-muted)">时间段:</span>'
      + periods.map((period, index) => `<button class="band-period-btn secondary ${index === 0 ? "active" : ""}" data-start="${period.start_date}">${period.label}</button>`).join("");
    bandCurrentStart = periods[0].start_date;
    container.querySelectorAll(".band-period-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        container.querySelectorAll(".band-period-btn").forEach((item) => item.classList.remove("active"));
        btn.classList.add("active");
        bandCurrentStart = btn.dataset.start;
        status.textContent = `已选择「${bandPeriodLabel(bandCurrentStart)}」，点击"查询最优参数"或输入B/S后点"分析"`;
      });
    });
    status.textContent = `实际历史 ${data.earliest_date} 至 ${data.latest_date}；当前默认「${periods[0].label}」`;
  } catch (err) {
    container.innerHTML = '<span style="line-height:36px;color:var(--text-muted)">时间段:</span><span class="hint">无可用周期</span>';
    status.textContent = `读取历史范围失败：${err.message}`;
  }
}

async function fetchBandOptimal() {
  const status = document.getElementById("band-status");
  const info = document.getElementById("band-best-info");
  if (!bandCurrentStart) {
    status.textContent = "可用历史尚未加载，请稍后重试";
    return;
  }
  status.textContent = `正在搜索「${bandPeriodLabel(bandCurrentStart)}」最优买卖价位...`;
  info.textContent = "";
  try {
    const params = new URLSearchParams({ symbol: BAND_SYMBOL, start_date: bandCurrentStart, top_n: 3 });
    const data = await requestJson(`/api/band-analysis/optimal?${params}`);
    status.textContent =
      `搜索完成（${data.search_time_s}秒），共 ${data.trading_days} 个交易日，价格区间 ${data.price_range}`;
    if (data.optimal.length > 0) {
      const best = data.optimal[0];
      document.getElementById("band-buy").value = best.B;
      document.getElementById("band-sell").value = best.S;
      info.innerHTML = `★ 最优: B=<b>${best.B}</b>  S=<b>${best.S}</b>（价差${best.spread_pct}%）
        往返 <b>${best.round_trips}</b> 次  |  收益率 <b style="color:var(--up)">${(best.return_rate*100).toFixed(1)}%</b>
        |  1万→<b>${best.final_value.toLocaleString()}</b> 元  |  年化 ${(best.annual_return*100).toFixed(1)}%
        |  买入持有 ${(data.buy_hold_return*100).toFixed(1)}%`;
    }
  } catch (err) {
    status.textContent = `查询失败：${err.message}`;
  }
}

async function runBandAnalysis() {
  const B = parseFloat(document.getElementById("band-buy").value);
  const S = parseFloat(document.getElementById("band-sell").value);
  const status = document.getElementById("band-status");
  const resultBox = document.getElementById("band-result");
  const info = document.getElementById("band-best-info");

  if (!bandCurrentStart) {
    status.textContent = "可用历史尚未加载，请稍后重试";
    return;
  }

  if (isNaN(B) || isNaN(S)) {
    alert("请输入有效的 B 和 S 数值");
    return;
  }
  if (B >= S) {
    alert("B（买入价）必须小于 S（卖出价）");
    return;
  }

  status.textContent = `正在模拟 B=${B} / S=${S}（${bandPeriodLabel(bandCurrentStart)}）...`;
  resultBox.style.display = "none";
  document.getElementById("band-run").disabled = true;
  document.getElementById("band-optimal-btn").disabled = true;

  try {
    const params = new URLSearchParams({
      symbol: BAND_SYMBOL, B: B, S: S, start_date: bandCurrentStart,
    });
    const data = await requestJson(`/api/band-analysis/detail?${params}`);
    renderBandSummary(data);
    renderBandTrades(data);
    status.textContent = "";
    resultBox.style.display = "block";
    info.textContent = "";
  } catch (err) {
    status.textContent = `分析失败：${err.message}`;
  } finally {
    document.getElementById("band-run").disabled = false;
    document.getElementById("band-optimal-btn").disabled = false;
  }
}

function renderBandSummary(data) {
  const cls = data.return_rate >= 0 ? "up" : "down";
  const bhCls = data.buy_hold_return >= 0 ? "up" : "down";
  document.getElementById("band-summary").innerHTML = `
    ${summaryCard("总收益率", pct(data.return_rate), cls)}
    ${summaryCard("买入持有", pct(data.buy_hold_return), bhCls)}
    ${summaryCard("年化收益", pct(data.annual_return), cls)}
    ${summaryCard("完整往返", `${data.round_trips} 次`)}
    ${summaryCard("每轮理论", `${((data.per_trip_mult-1)*100).toFixed(2)}%`)}
    ${summaryCard("理论复利", pct(data.theoretical_return), cls)}
    ${summaryCard("1万→终值", `${data.final_value.toLocaleString()} 元`)}
    ${summaryCard("1万→买入持有", `${(10000*(1+data.buy_hold_return)).toLocaleString()} 元`, "muted")}
    <p class="muted bt-meta">
      B=${data.B} S=${data.S} | ${data.start_date} ~ ${data.end_date} | 共 ${data.trading_days} 天
      ${data.final_holding ? ` | ⚠ 期末持仓，按收盘价 ${data.last_close} 清仓（非目标价${data.S}）` : ""}
    </p>
  `;
}

function renderBandTrades(data) {
  const container = document.getElementById("band-trades");
  if (!data.trades.length) {
    container.innerHTML = '<p class="muted">无交易记录</p>';
    return;
  }

  // 累计往返计数
  let tripIdx = 0;
  const rows = data.trades.map((t) => {
    let tripLabel = "";
    let cashDisplay = "";
    let sharesDisplay = "";

    if (t.direction === "buy") {
      tripLabel = `第${t.trip_num}轮`;
    } else if (t.direction === "sell") {
      tripIdx++;
      tripLabel = `第${tripIdx}轮`;
      cashDisplay = t.cash != null ? t.cash.toLocaleString() : "";
    } else if (t.direction === "close_out") {
      tripLabel = "期末清仓";
      cashDisplay = t.cash != null ? t.cash.toLocaleString() : "";
    }

    if (t.direction === "buy") {
      sharesDisplay = t.shares != null ? t.shares.toLocaleString() : "";
    }

    const dirLabel = { buy: "买入", sell: "卖出", close_out: "清仓" }[t.direction] || t.direction;
    const dirCls = { buy: "up", sell: "down", close_out: "muted" }[t.direction] || "";

    return `<tr>
      <td>${t.date}</td>
      <td class="${dirCls}">${dirLabel}</td>
      <td>${t.price}</td>
      <td>${cashDisplay}</td>
      <td>${sharesDisplay}</td>
      <td class="muted">${tripLabel}</td>
    </tr>`;
  }).join("");

  container.innerHTML = `
    <table class="bt-table">
      <thead><tr>
        <th>日期</th><th>方向</th><th>价格</th><th>现金(元)</th><th>持仓(股)</th><th>轮次</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

document.getElementById("band-run").addEventListener("click", runBandAnalysis);
document.getElementById("band-optimal-btn").addEventListener("click", fetchBandOptimal);

initializeBandPeriods();
initializeWriteAccess();
refresh();
setInterval(refresh, 10000);
