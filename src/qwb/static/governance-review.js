document.querySelector('.review-form')?.addEventListener('submit', (event) => {
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = '正在计算…';
});
window.addEventListener('pageshow', () => {
  const button = document.querySelector('.review-form button[type="submit"]');
  if (button) { button.disabled = false; button.textContent = '计算诊断与调整方案'; }
});
