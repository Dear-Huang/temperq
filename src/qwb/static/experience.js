// Progressive disclosure never disables submitted settings or changes their units.
(() => {
  const presetData = document.getElementById('policy-presets');
  if (!presetData) return;
  const presets = JSON.parse(presetData.textContent);
  const select = document.getElementById('policy-preset');
  const mode = document.getElementById('policy-mode');
  const costModel = document.getElementById('policy-cost-model');
  const editor = select.form;
  const fields = [...editor.querySelectorAll('[data-field]')];
  const input = name => editor.elements.namedItem(name);
  const percentKeys = new Set(['max_weight', 'exposure_cap', 'cash_floor', 'target_volatility', 'adjustment_speed']);
  const percent = value => `${Number((Number(value) * 100).toFixed(2))}%`;
  const number = value => Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  const labels = Object.fromEntries(fields.map(field => [field.dataset.field, field.childNodes[0].textContent.trim()]));
  let current = select.value;
  let pending = null;
  const dialog = document.getElementById('preset-change-dialog');
  const search = document.getElementById('policy-search');
  const capitalData = document.getElementById('policy-capital');
  const pinned = new Set(capitalData ? JSON.parse(capitalData.textContent).pinned : []);
  const derivedKeys = ['n_hold', 'hold_until_rank', 'max_weight'];
  const syncPins = () => { if (input('explicit_overrides')) input('explicit_overrides').value = JSON.stringify([...pinned]); };
  let capitalRequest = 0;
  const refreshCapital = async () => {
    if (!capitalData) return;
    const request = ++capitalRequest;
    const submit = editor.querySelector('button:not([type])');
    const note = document.getElementById('capital-default-note');
    submit.disabled = true;
    note.textContent = '正在根据现金和初始持仓计算默认值…';
    try {
      const response = await fetch('/strategies/defaults', { method: 'POST', body: new FormData(editor) });
      const result = await response.json();
      if (request !== capitalRequest) return;
      if (!response.ok) throw Error(result.error || '默认值预览失败');
      for (const preset of Object.values(presets)) {
        for (const key of derivedKeys) preset.parameters[key] = result.capital_defaults.parameters[key];
      }
      for (const key of derivedKeys) if (!pinned.has(key)) input(key).value = presets[current].parameters[key];
      note.textContent = `参考总权益 ${number(result.capital_defaults.equity)} 元，资金档位 ${result.capital_defaults.label}；默认 ${result.capital_defaults.n_hold} 只。手动改过的参数保留。流动性门槛仅为训练参考，不保证成交。`;
      submit.disabled = false;
      syncPins(); update();
    } catch (error) {
      if (request === capitalRequest) note.textContent = `默认值尚未确认：${error.message}。请修正现金或持仓后重试。`;
    }
  };

  const differences = () => {
    const baseline = presets[current];
    const changes = Object.keys(baseline.parameters).filter(key => Number(input(key).value) !== baseline.parameters[key]);
    if (mode.value !== baseline.mode) changes.push('mode');
    if (costModel && costModel.value !== baseline.cost_model) changes.push('cost_model');
    return changes;
  };
  const filter = () => {
    const query = search.value.trim().toLowerCase();
    for (const field of fields) {
      const targetValid = input('target_volatility').value !== '' && input('target_volatility').validity.valid
        && Number(input('target_volatility').value) > 0;
      // An invalid saved field must remain reachable even when its mode is off.
      const inactive = field.dataset.field === 'target_volatility' && mode.value === 'fixed' && targetValid;
      field.hidden = inactive || !(field.textContent + ' ' + field.dataset.field).toLowerCase().includes(query);
    }
    for (const group of editor.querySelectorAll('.policy-group')) {
      const available = !!group.querySelector('[data-field]:not([hidden])');
      group.hidden = !available;
      if (query && available) group.open = true;
    }
    const count = fields.filter(field => !field.hidden).length;
    document.getElementById('policy-search-result').textContent = query ? `找到 ${count} 项当前生效参数` : '按中文名称或参数名查找；隐藏不改变已填值。';
  };
  const update = () => {
    const changes = differences();
    const base = presets[current];
    document.getElementById('preset-help').textContent = base.description;
    const keys = ['cash', 'n_hold', 'hold_until_rank', 'max_weight', 'exposure_cap', 'cash_floor', 'adjustment_speed', 'target_volatility'];
    const valid = keys.every(key => input(key).value !== '' && input(key).validity.valid)
      && Number(input('hold_until_rank').value) >= Number(input('n_hold').value)
      && Number(input('max_weight').value) > 0 && Number(input('adjustment_speed').value) > 0
      && Number(input('target_volatility').value) > 0;
    const summary = document.getElementById('policy-summary-text');
    if (!valid) summary.textContent = Number(input('hold_until_rank').value) < Number(input('n_hold').value)
      ? '继续持有的排名上限不能小于持仓只数；请在高级设置中一并调整。'
      : '有设置尚未填完整或超出范围；单票上限与调整比例须大于零。修正后这里会更新，提交时仍会校验。';
    else {
      const cash = Number(input('cash').value);
      const capacity = Math.min(Number(input('exposure_cap').value), 1 - Number(input('cash_floor').value));
      const speed = Number(input('adjustment_speed').value);
      const risk = mode.value === 'fixed' ? '固定仓位' : `${mode.value === 'ewma' ? 'EWMA 波动目标' : 'ML 风险辅助（实验）'}，目标年化波动 ${percent(input('target_volatility').value)}，不是亏损上限`;
      summary.textContent = `以 ${number(cash)} 元可用现金起步，目标持有 ${input('n_hold').value} 只；单只目标最多 ${percent(input('max_weight').value)}，股票目标总仓位最多 ${percent(capacity)}。每 5 个交易日决策，${speed === 1 ? '直接靠近目标' : `每次靠近目标的 ${percent(speed)}`}，使用${risk}。`;
    }
    const list = document.getElementById('policy-change-list');
    list.replaceChildren();
    for (const key of changes) {
      const item = document.createElement('li');
      if (key === 'mode') item.textContent = `风险控制：${base.mode} → ${mode.value}`;
      else if (key === 'cost_model') item.textContent = `成本口径：${base.cost_model} → ${costModel.value}`;
      else {
        const value = input(key).value;
        const format = v => percentKeys.has(key) ? `${v}（${percent(v)}）` : v;
        item.textContent = `${labels[key]}：${format(base.parameters[key])} → ${format(value)}${key === 'target_volatility' && mode.value === 'fixed' ? '；当前不参与计算' : ''}`;
      }
      list.append(item);
    }
    document.getElementById('policy-change-count').textContent = changes.length ? `相对「${presets[current].name}」修改了 ${changes.length} 项` : `使用「${presets[current].name}」的默认参数`;
    for (const field of fields) {
      const key = field.dataset.field;
      field.dataset.modified = String(changes.includes(key));
      const equivalent = field.querySelector('.field-equivalent');
      if (equivalent) equivalent.textContent = input(key).value === '' ? '' : `${percent(input(key).value)} · 配置值仍为 ${input(key).value}`;
    }
    filter();
  };
  const load = name => {
    current = name;
    select.value = name;
    mode.value = presets[name].mode;
    if (costModel) costModel.value = presets[name].cost_model;
    for (const [key, value] of Object.entries(presets[name].parameters)) input(key).value = value;
    pinned.clear(); syncPins();
    update();
  };
  select.addEventListener('change', () => {
    const next = select.value;
    select.value = current;
    if (differences().length) { pending = next; dialog.showModal(); }
    else load(next);
  });
  document.getElementById('keep-policy').addEventListener('click', () => { pending = null; dialog.close(); select.focus(); });
  document.getElementById('replace-policy').addEventListener('click', () => { const next = pending; pending = null; dialog.close(); if (next) load(next); select.focus(); });
  dialog.addEventListener('cancel', () => { pending = null; });
  document.getElementById('reset-policy').addEventListener('click', () => load(current));
  search.addEventListener('input', filter);
  editor.addEventListener('input', event => {
    if (event.target.closest('[data-field]')) { pinned.add(event.target.name); syncPins(); }
    update();
  });
  input('cash').addEventListener('change', refreshCapital);
  input('holdings').addEventListener('change', refreshCapital);
  mode.addEventListener('change', () => { syncPins(); });
  editor.addEventListener('change', event => { if (event.target !== select) update(); });
  editor.addEventListener('invalid', event => {
    let parent = event.target.parentElement;
    while (parent && parent !== editor) { if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement; }
    search.value = ''; filter();
  }, true);
  syncPins(); update();
  refreshCapital();
})();
