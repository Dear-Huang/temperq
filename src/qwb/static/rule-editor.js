document.querySelectorAll('.rule-row').forEach(row => {
  const op = row.querySelector('select[name$="_operator"]');
  const value = row.querySelector('input[name$="_value"]');
  const required = row.querySelector('input[name$="_required"]');
  const sync = (changed = false) => {
    const active = ['mean_above_mean', 'return_above', 'above_mean_ratio'].includes(op.value);
    value.closest('label').hidden = !active;
    if (!active) value.value = '0';
    if (changed && op.value === 'mean_above_mean') value.value = '60';
    if (changed && op.value === 'above_mean_ratio') value.value = '1.5';
    value.step = op.value === 'mean_above_mean' ? '1' : 'any';
    value.closest('label').firstChild.textContent = ({mean_above_mean: '长均线窗口', return_above: '涨幅阈值（小数）', above_mean_ratio: '均值倍数'})[op.value] || '附加数值';
    const continuous = ['return', 'low_volatility'].includes(op.value);
    required.disabled = continuous;
    if (continuous) required.checked = false;
  };
  op.addEventListener('change', () => sync(true));
  sync();
});
document.querySelector('form[action="/playbooks/setup"]')?.addEventListener('submit', event => {
  if (event.submitter?.value === 'preview') {
    event.submitter.textContent = '正在检验…';
    event.submitter.setAttribute('aria-busy', 'true');
  }
});
