// Display preferences and read-only polling. Portfolio calculations stay in the ledger.
(() => {
  const shell = document.querySelector('.deployment-shell');
  if (!shell) return;
  let data = JSON.parse(document.getElementById('deployment-data').textContent);
  const $ = id => document.getElementById(id);
  const h = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const number = (value, digits = 2) => value == null ? '—' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const pct = (value, signed = false) => value == null ? '—' : `${signed && value > 0 ? '+' : ''}${number(value * 100)}%`;
  const money = (value, signed = false) => value == null ? '—' : `${signed && value > 0 ? '+' : ''}${number(value)}`;
  const tone = value => value == null || value === 0 ? '' : value > 0 ? 'desk-positive' : 'desk-negative';
  const scopeNames = {forward_paper: '按时纸面', late_reconstruction: '迟到重建', historical_replay: '历史演示'};
  const color = ['#4dd9ca', '#70a6ff', '#b2a0fb', '#e3b56d', '#ec92b3', '#83bd9d'];
  const windows = {equity: '账户净值', allocation: '资金分布', holdings: '持仓监视', decision: '最近决策', drawdown: '回撤与仓位', fills: '模拟成交', costs: '费用拆解', evidence: '记录质量', operations: '运行控制'};
  const overview = ['equity', 'allocation', 'holdings', 'decision', 'drawdown', 'operations'];
  const storageKey = `temperq.deployment.${data.sid}.${data.vid}`;
  let prefs = {layout: 'overview', range: 'all', visible: overview};
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (saved && ['overview', 'professional'].includes(saved.layout) && ['20', '60', 'all'].includes(saved.range) && Array.isArray(saved.visible)) {
      prefs = {...saved, visible: [...new Set([...saved.visible.filter(key => key in windows), 'operations'])]};
    }
  } catch { /* Display settings never prevent opening the account. */ }
  const save = () => { try { localStorage.setItem(storageKey, JSON.stringify(prefs)); } catch {} };
  const empty = message => `<div class="desk-empty"><span>NO RECORDS YET</span><p>${h(message)}</p></div>`;
  function table(headers, rows, message) {
    return rows.length ? `<table><thead><tr>${headers.map(v => `<th scope="col">${h(v)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table>` : empty(message);
  }
  function layout() {
    shell.dataset.layout = prefs.layout;
    shell.querySelectorAll('[data-layout][type="button"]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.layout === prefs.layout)));
    shell.querySelectorAll('[data-window]').forEach(panel => { panel.hidden = !prefs.visible.includes(panel.dataset.window); });
    $('desk-window-checks').innerHTML = Object.entries(windows).map(([key, label]) => `<label><input type="checkbox" value="${key}" ${prefs.visible.includes(key) ? 'checked' : ''} ${key === 'operations' ? 'disabled' : ''}>${label}${key === 'operations' ? ' · 常驻' : ''}</label>`).join('');
    shell.querySelectorAll('[data-range]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.range === prefs.range)));
  }
  function metric(id, text, value) { $(id).textContent = text; $(id).className = tone(value); }
  function summary() {
    const m = data.metrics, counts = data.scope_counts;
    $('desk-scope').textContent = data.historical_demo ? '历史演示 · 非前向' : data.runtime ? '纸面观察 · 非实盘' : '尚未部署';
    $('desk-scope').classList.toggle('desk-amber', data.historical_demo);
    metric('metric-equity', money(m.equity));
    metric('metric-daily', money(m.daily_pnl, true), m.daily_pnl);
    metric('metric-return', pct(m.total_return, true), m.total_return);
    metric('metric-drawdown', pct(m.max_drawdown), m.max_drawdown);
    metric('metric-exposure', pct(m.exposure));
    $('metric-date').textContent = m.last_day ? `最后记账 ${m.last_day}` : '等待第一条收盘记录';
    $('metric-daily-return').textContent = m.daily_return == null ? '至少两条收盘记录后显示' : `最近一日 ${pct(m.daily_return, true)} · 已扣已计费用`;
    $('metric-cash').textContent = m.cash == null ? '尚无现金记录' : `可用现金 ¥ ${money(m.cash)}`;
    $('metric-days').textContent = `${counts.forward_paper} / ${m.record_count}`;
    $('metric-evidence').textContent = `${m.return_intervals} 段日收益 · 尚未证明收益优势`;
    $('metric-return-note').textContent = counts.historical_replay ? '历史演示账户路径，非前向成绩' : counts.late_reconstruction ? `含 ${counts.late_reconstruction} 天迟到重建` : '首条记录至最后记账日';
    const states = {BLOCKED: '运行受阻，需要处理', WAITING_DATA: '等待完整行情', UP_TO_DATE: '检查完成，请核对最后记账日', NOT_STARTED: '先完成检验与启用'};
    $('desk-status').textContent = states[data.health.state] || '账户已启用，等待记录';
    $('desk-action').textContent = data.next_action;
    $('desk-health-message').textContent = data.health.message;
    $('desk-attention').dataset.state = data.health.state;
    $('desk-auto-state').textContent = data.runtime?.enabled ? '每日检查已开启' : '每日检查已关闭';
    $('desk-auto-button').textContent = data.runtime?.enabled ? '关闭每日检查' : '开启每日检查';
    $('desk-auto-value').value = data.runtime?.enabled ? 'no' : 'yes';
    $('desk-tick').disabled = !data.runtime || data.health.state === 'BLOCKED' && data.health.message.includes('实现已变化');
    $('desk-auto-button').disabled = $('desk-tick').disabled;
    $('desk-activation').hidden = Boolean(data.runtime);
    const checks = [ ['行情频率', '日线收盘 · 上海时区'], ['最近记账', m.last_day || '尚无记录'],
      ['最近检查', data.health.checked_at || '尚未检查'], ['启用日期', data.runtime?.start_date || '未启用'],
      ['政策', `${data.policy.preset} / ${data.policy.mode.toUpperCase()}`],
      ['信号', `${data.model.kind === 'external_scores' ? '外部预测' : '本地模型'} · ${data.model.version.replace('sha256:', '').slice(0, 10)}`] ];
    $('desk-operation-details').innerHTML = checks.map(([key, value]) => `<div><span>${h(key)}</span><b>${h(value)}</b></div>`).join('');
  }
  function holdings() {
    const query = $('desk-holding-search').value.trim().toUpperCase();
    const rows = data.holdings.filter(p => p.code.includes(query));
    $('desk-holdings-table').innerHTML = table(['股票代码', '估值价', '相邻估值变化', '持有 / 可卖', '市值 CNY', '实际权重'], rows.map(p => [
      `<b class="desk-code">${h(p.code)}</b>`, number(p.mark), `<span class="${tone(p.mark_change)}">${pct(p.mark_change, true)}</span>`,
      `${number(p.quantity, 0)} <span class="desk-muted">/ ${number(p.sellable, 0)}</span>`, money(p.value),
      `<span class="desk-weight-track"><i style="width:${Math.min(100, p.weight * 100)}%"></i></span>${pct(p.weight)}`
    ]), query ? '没有匹配的持仓代码。' : '目前没有已记录的股票持仓。目标不会提前显示为成交。');
  }
  function allocation() {
    if (data.metrics.equity == null) {
      $('desk-allocation-ring').innerHTML = empty('首份收盘记录后显示资金分布。');
      $('desk-allocation-list').innerHTML = ''; return;
    }
    const top = data.holdings.slice(0, 4).map(p => ({name: p.code, value: p.value, weight: p.weight}));
    const other = data.holdings.slice(4).reduce((sum, p) => sum + p.value, 0);
    if (other) top.push({name: '其他持仓', value: other, weight: other / data.metrics.equity});
    top.push({name: '可用现金', value: data.metrics.cash, weight: data.metrics.cash / data.metrics.equity, cash: true});
    let offset = 0;
    const circles = top.map((item, index) => {
      const arc = `<circle cx="100" cy="100" r="73" fill="none" stroke="${item.cash ? '#344353' : color[index % color.length]}" stroke-width="17" pathLength="100" stroke-dasharray="${item.weight * 100} ${100 - item.weight * 100}" stroke-dashoffset="${-offset}" transform="rotate(-90 100 100)"/>`;
      offset += item.weight * 100; return arc;
    }).join('');
    $('desk-allocation-ring').innerHTML = `<svg viewBox="0 0 200 200" role="img" aria-label="股票与现金分布"><circle cx="100" cy="100" r="73" fill="none" stroke="#1d2b38" stroke-width="17"/>${circles}<text x="100" y="92" text-anchor="middle" class="desk-ring-label">股票仓位</text><text x="100" y="119" text-anchor="middle" class="desk-ring-value">${pct(data.metrics.exposure)}</text></svg>`;
    $('desk-allocation-list').innerHTML = top.map((item, index) => `<div><i style="background:${item.cash ? '#55687e' : color[index % color.length]}"></i><span>${h(item.name)}</span><b>${pct(item.weight)}</b></div>`).join('');
  }
  function decisions() {
    if (!data.decision) {
      $('desk-decision-summary').innerHTML = empty('尚无已记录决策。完成启用后，按冻结政策产生目标。');
      $('desk-targets').innerHTML = ''; return;
    }
    const d = data.decision;
    $('desk-decision-summary').innerHTML = `<div class="desk-decision-date"><b>${h(d.day)}</b><span>目标仓位 <strong>${pct(d.exposure)}</strong></span></div><p class="desk-decision-execution">计划执行 ${h(d.execution_date)} · ${d.execution_recorded ? '执行日已有账户记录，成交见明细' : '尚无该执行日记录'}</p><p>${h(d.risk_reason)}</p><div class="desk-reason-tags">${d.reasons.map(reason => `<span>${h(reason)}</span>`).join('')}</div><div class="desk-target-legend"><span><i class="desk-key gray"></i>当前实际</span><span><i class="desk-key cyan"></i>最近目标</span></div>`;
    $('desk-targets').innerHTML = data.targets.slice(0, 10).map(row => `<div class="desk-target-row"><b>${h(row.code)}</b><div class="desk-target-track"><i class="desk-actual" style="width:${Math.max(0, Math.min(100, row.actual * 100))}%"></i><i class="desk-target" style="width:${Math.max(0, Math.min(100, row.target * 100))}%"></i></div><span>${pct(row.actual)} → ${pct(row.target)}</span></div>`).join('') + (data.targets.length > 10 ? '<p class="desk-panel-note">此处显示偏离最大的 10 项，全部目标见完整解释。</p>' : '');
  }
  function execution() {
    $('desk-fill-count').textContent = `共 ${data.fill_count} 条`;
    $('desk-fills-table').innerHTML = table(['日期 / 范围', '股票', '方向', '成交 / 请求', '费用', '结果'], data.fills.map(fill => [
      `${h(fill.day)}<small class="desk-cell-note">${scopeNames[fill.scope] || h(fill.scope)}</small>`, `<b class="desk-code">${h(fill.asset_id)}</b>`,
      `<span class="desk-side ${fill.side === 'BUY' ? 'buy' : 'sell'}">${fill.side === 'BUY' ? '买入' : '卖出'}</span>`,
      `${number(fill.quantity, 0)} / ${number(fill.requested_quantity, 0)}`, money(fill.total_cost),
      `<span class="${fill.unfilled_quantity ? 'desk-amber' : 'desk-muted'}">${h(fill.reason_text)}</span>`
    ]), '尚无模拟成交。未建仓或未到调仓日时，这是正常状态。');
    $('desk-cost-total').textContent = money(data.total_cost);
    const labels = {commission: '佣金（含最低佣金）', stamp_tax: '印花税', struct_spread: 'STRUCT 半价差估计', slippage: '额外滑点压力'};
    $('desk-cost-bars').innerHTML = Object.entries(labels).map(([key, label], index) => `<div class="desk-cost-row"><span>${label}</span><b>¥ ${money(data.costs[key])}</b><div><i style="width:${data.total_cost ? Math.min(100, data.costs[key] / data.total_cost * 100) : 0}%;background:${color[index]}"></i></div></div>`).join('');
    $('desk-cost-note').textContent = data.policy.cost_model === 'struct_v1' ? '按上月输入估计报价半价差，另计佣金税费与压力；不等于真实成交冲击。' : '本版本未计报价价差，成本不完整；保留原冻结口径。';
    $('desk-evidence-counts').innerHTML = Object.entries(scopeNames).map(([scope, label]) => `<div><strong>${data.scope_counts[scope]}</strong><span><i class="desk-key ${scope === 'forward_paper' ? 'cyan' : scope === 'late_reconstruction' ? 'amber' : 'gray'}"></i>${label}</span></div>`).join('');
    $('desk-evidence-timeline').innerHTML = data.series.slice(-90).map(row => `<span class="${row.scope}" title="${h(row.day)} · ${scopeNames[row.scope]}" aria-label="${h(row.day)} ${scopeNames[row.scope]}"></span>`).join('') || '<p class="desk-panel-note">记录将在启用后逐日积累。</p>';
  }
  function charts() {
    const series = prefs.range === 'all' ? data.series : data.series.slice(-Number(prefs.range));
    if (!series.length) {
      $('desk-equity-chart').innerHTML = empty('等待第一份收盘记录。这里不会用回测曲线填充。');
      $('desk-drawdown-chart').innerHTML = empty('记录后显示回撤与实际股票仓位。');
      $('desk-chart-note').textContent = '日频收盘路径 · 首条记录归一为 1 · 不含实时行情';
    } else {
      const plotWidth = $('desk-equity-chart').clientWidth || $('desk-drawdown-chart').clientWidth;
      const w = Math.max(320, Math.min(900, plotWidth - 24)), left = 56, right = 22, top = 24, bottom = 232;
      const x = i => left + (w - left - right) * i / Math.max(1, series.length - 1);
      const base = data.series[0].equity;
      const nav = series.map(row => row.equity / base);
      const min = Math.min(...nav), max = Math.max(...nav), pad = Math.max((max - min) * .15, .0005);
      const lo = min - pad, hi = max + pad, y = value => bottom - (value - lo) / (hi - lo) * (bottom - top);
      const points = nav.map((v, i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
      const area = `M ${x(0)} ${bottom} L ${nav.map((v, i) => `${x(i)} ${y(v)}`).join(' L ')} L ${x(nav.length - 1)} ${bottom} Z`;
      const grid = [0, 1, 2, 3, 4].map(i => {
        const value = lo + (hi - lo) * i / 4, yy = y(value);
        return `<line x1="${left}" x2="${w - right}" y1="${yy}" y2="${yy}" class="desk-grid-line"/><text x="${left - 10}" y="${yy + 4}" text-anchor="end">${number(value, hi - lo < .01 ? 4 : 3)}</text>`;
      }).join('');
      const dates = `<text x="${left}" y="260">${h(series[0].day)}</text><text x="${w - right}" y="260" text-anchor="end">${h(series.at(-1).day)}</text>`;
      const baseline = 1 >= lo && 1 <= hi ? `<line x1="${left}" x2="${w - right}" y1="${y(1)}" y2="${y(1)}" stroke="#536276" stroke-dasharray="4 5"/>` : '';
      $('desk-equity-chart').innerHTML = `<svg viewBox="0 0 ${w} 275" tabindex="0" role="img" aria-label="账户归一净值，方向键查看逐日记录"><defs><linearGradient id="desk-equity-fill" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#4dd9ca" stop-opacity=".22"/><stop offset="1" stop-color="#4dd9ca" stop-opacity="0"/></linearGradient></defs>${grid}${baseline}<path d="${area}" fill="url(#desk-equity-fill)"/><polyline points="${points}" fill="none" stroke="#4dd9ca" stroke-width="2.4" vector-effect="non-scaling-stroke"/>${series.length === 1 ? `<circle cx="${x(0)}" cy="${y(nav[0])}" r="4" fill="#4dd9ca"/>` : ''}<g id="desk-crosshair" visibility="hidden"><line y1="${top}" y2="${bottom}" stroke="#96a8bc" stroke-dasharray="4 4"/><circle r="4" fill="#4dd9ca" stroke="#101b27" stroke-width="2"/></g>${dates}</svg>`;
      const svg = $('desk-equity-chart').querySelector('svg'), crosshair = $('desk-crosshair');
      let cursor = series.length - 1;
      const inspect = index => {
        cursor = Math.min(series.length - 1, Math.max(0, index)); const row = series[cursor];
        crosshair.setAttribute('visibility', 'visible'); const line = crosshair.querySelector('line'), circle = crosshair.querySelector('circle');
        line.setAttribute('x1', x(cursor)); line.setAttribute('x2', x(cursor)); circle.setAttribute('cx', x(cursor)); circle.setAttribute('cy', y(nav[cursor]));
        $('desk-chart-readout').textContent = `${row.day} · 净值 ${number(nav[cursor], 4)} · ¥ ${money(row.equity)} · ${scopeNames[row.scope]}`;
      };
      svg.addEventListener('pointermove', event => { const rect = svg.getBoundingClientRect(); inspect(Math.round((((event.clientX - rect.left) / rect.width * w) - left) / (w - left - right) * (series.length - 1))); });
      svg.addEventListener('pointerleave', () => { crosshair.setAttribute('visibility', 'hidden'); });
      svg.addEventListener('keydown', event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); inspect(event.key === 'Home' ? 0 : event.key === 'End' ? series.length - 1 : cursor + (event.key === 'ArrowLeft' ? -1 : 1)); } });
      $('desk-chart-note').textContent = `${series[0].day} — ${series.at(-1).day} · ${series.length} 条记录 · 净值按全路径首条记录归一为 1${data.historical_demo ? ' · 全部为历史演示' : data.scope_counts.late_reconstruction ? ' · 账户路径含迟到重建，查看记录质量窗口' : ''}`;
      const maxDD = Math.max(.001, ...series.map(row => -row.drawdown));
      const dd = series.map((row, i) => `${x(i)},${29 + (-row.drawdown) / maxDD * 58}`).join(' ');
      const exposure = series.map((row, i) => `${x(i)},${175 - row.exposure * 48}`).join(' ');
      $('desk-drawdown-chart').innerHTML = `<svg viewBox="0 0 ${w} 210" role="img" aria-label="全路径高点回撤与实际股票仓位"><text x="${left}" y="17">回撤 · 相对全路径高点</text><text x="${w - right}" y="17" text-anchor="end">0 至 ${pct(-maxDD)}</text><line x1="${left}" x2="${w - right}" y1="29" y2="29" class="desk-grid-line"/><polyline points="${dd}" fill="none" stroke="#e3b56d" stroke-width="2"/><text x="${left}" y="117">实际股票仓位</text><text x="${w - right}" y="117" text-anchor="end">0 至 100%</text><line x1="${left}" x2="${w - right}" y1="175" y2="175" class="desk-grid-line"/><polyline points="${exposure}" fill="none" stroke="#70a6ff" stroke-width="2"/><text x="${left}" y="204">${h(series[0].day)}</text><text x="${w - right}" y="204" text-anchor="end">${h(series.at(-1).day)}</text></svg>`;
    }
    const p = data.policy.parameters;
    const riskNote = !data.metrics.record_count ? '尚无账户记录，暂不判断实际持仓是否达标。' : data.latest_violations.length ? `最近记录 ${data.latest_violations.length} 项实际约束偏离；详情见决策回放与运行记录。` : '最近记录无已报告约束偏离；约束不能保证受阻后持仓仍达标。';
    $('desk-risk-limits').innerHTML = `<div><span>总仓位上限</span><b>${pct(p.exposure_cap)}</b></div><div><span>单票上限</span><b>${pct(p.max_weight)}</b></div><div><span>最低现金</span><b>${pct(p.cash_floor)}</b></div><p class="${data.latest_violations.length ? 'desk-amber' : 'desk-muted'}">${riskNote}</p>`;
  }
  function render() { summary(); holdings(); allocation(); decisions(); execution(); charts(); layout(); }
  shell.querySelectorAll('button[data-layout]').forEach(button => button.addEventListener('click', () => { prefs.layout = button.dataset.layout; prefs.visible = prefs.layout === 'professional' ? Object.keys(windows) : overview; save(); layout(); }));
  shell.querySelectorAll('button[data-range]').forEach(button => button.addEventListener('click', () => { prefs.range = button.dataset.range; save(); charts(); layout(); }));
  $('desk-holding-search').addEventListener('input', holdings);
  $('desk-windows').addEventListener('click', () => { const box = $('desk-window-options'); box.hidden = !box.hidden; $('desk-windows').setAttribute('aria-expanded', String(!box.hidden)); });
  $('desk-window-checks').addEventListener('change', event => { const key = event.target.value; if (!(key in windows) || key === 'operations') return; prefs.visible = event.target.checked ? [...prefs.visible, key] : prefs.visible.filter(item => item !== key); save(); layout(); });
  $('desk-reset').addEventListener('click', () => { prefs = {layout: 'overview', range: 'all', visible: overview}; save(); render(); });
  $('desk-fullscreen').addEventListener('click', async () => { try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); } catch { $('desk-connection').hidden = false; $('desk-connection').textContent = '浏览器未允许全屏，可以使用 F11。'; } });
  document.addEventListener('fullscreenchange', () => { $('desk-fullscreen').textContent = document.fullscreenElement ? '退出全屏' : '全屏'; });
  let loading = false, timer;
  async function refresh() {
    if (loading || document.hidden) return;
    loading = true; $('desk-refresh').disabled = true;
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(`${shell.dataset.base}/dashboard/snapshot.json`, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const next = await response.json();
      // Keep cursor/focus stable during unchanged background reads.
      if (JSON.stringify({...data, generated_at: null}) !== JSON.stringify({...next, generated_at: null})) { data = next; render(); }
      $('desk-sync').textContent = `看板已同步 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`;
      $('desk-connection').hidden = true;
    } catch {
      $('desk-sync').textContent = '看板连接中断'; $('desk-connection').hidden = false;
      $('desk-connection').textContent = '暂时无法读取最新账本。当前仍显示上次成功读取的记录；不会清空曲线或伪装为已更新。';
    } finally { clearTimeout(timeout); loading = false; $('desk-refresh').disabled = false; }
  }
  function schedule() { clearInterval(timer); if (!document.hidden) timer = setInterval(refresh, 30000); }
  document.addEventListener('visibilitychange', () => { schedule(); if (!document.hidden) refresh(); });
  window.addEventListener('pagehide', () => clearInterval(timer));
  window.addEventListener('pageshow', schedule);
  $('desk-refresh').addEventListener('click', refresh);
  render(); $('desk-sync').textContent = '账本快照已载入 · 每 30 秒读取'; schedule();
  if ('ResizeObserver' in window) {
    let width = $('desk-equity-chart').clientWidth || $('desk-drawdown-chart').clientWidth;
    const observer = new ResizeObserver(() => { const next = $('desk-equity-chart').clientWidth || $('desk-drawdown-chart').clientWidth; if (next > 0 && next !== width) { width = next; charts(); } });
    observer.observe($('desk-equity-chart')); observer.observe($('desk-drawdown-chart'));
  }
})();
