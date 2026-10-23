// Real local server and jobs, synthetic data only. No mocked API requests.
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
const require = createRequire(new URL('../website/package.json', import.meta.url));
const { chromium, expect } = require('@playwright/test');
const baseURL = process.env.QWB_EXPERIENCE_QA_URL || 'http://127.0.0.1:8784';
const out = path.resolve('artifacts/experience-qa');
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: process.env.HERO_QA_CHANNEL || 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [], captures = [];
page.on('pageerror', error => errors.push(error.message));
async function capture(name) {
  await page.evaluate(() => document.fonts.ready);
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)) throw Error(`Overflow: ${name}`);
  await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
  captures.push(name);
}
async function finishJob() {
  await page.waitForURL(/\/job\//);
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    if (await page.locator('.state.succeeded').count()) return;
    if (await page.locator('.state.failed,.state.cancelled').count()) throw Error(await page.locator('main').innerText());
    await page.waitForTimeout(1000);
    await page.reload({ waitUntil: 'domcontentloaded' });
  }
  throw Error('Job timed out');
}
let strategyURL;
try {
  await page.goto(`${baseURL}/start`);
  await expect(page.getByRole('heading', { name: '你的量化工作台' })).toBeVisible();
  await capture('01-workbench');
  await page.goto(`${baseURL}/strategies/new`);
  await expect(page.getByRole('button', { name: '下一步：配置政策' })).toBeDisabled();
  await expect(page.getByText('先准备兼容的五日模型', { exact: true })).toBeVisible();
  await capture('02-no-model-next-action');
  await page.goto(`${baseURL}/start`);
  await page.getByRole('link', { name: '选择数据，开始训练', exact: true }).click();
  await expect(page.locator('#training-scope')).not.toHaveAttribute('open');
  await expect(page.locator('input[name="horizon"]:checked')).toHaveValue('5');
  await page.locator('input[name="model"][value="ridge"]').check();
  const run = `experience_${Date.now()}`;
  await page.locator('input[name="name"][form="main"]').fill(run);
  await capture('03-train-simple');
  await page.getByRole('button', { name: '开始训练', exact: true }).click();
  await finishJob();
  await expect(page.getByRole('link', { name: '下一步：配置我的政策 →', exact: true })).toBeVisible();
  await capture('04-training-complete');
  await page.getByRole('link', { name: '下一步：配置我的政策 →', exact: true }).click();
  await page.getByRole('button', { name: '下一步：配置政策' }).click();
  await expect(page.locator('#advanced')).not.toHaveAttribute('open');
  await expect(page.locator('#policy-summary-text')).toContainText('每 5 个交易日决策');
  await page.locator('input[name="name"]').fill('体验验收 · 自己的纪律');
  await page.locator('#policy-preset').selectOption('patient');
  await expect(page.locator('#policy-summary-text')).toContainText('每次靠近目标的 50%');
  await capture('05-policy-simple');
  await page.locator('#advanced > summary').click();
  await expect(page.locator('[data-field="target_volatility"]')).toBeHidden();
  await page.locator('#policy-search').fill('n_hold');
  await expect(page.locator('[data-field="n_hold"]')).toBeVisible();
  await expect(page.locator('[data-field="max_weight"]')).toBeHidden();
  await page.locator('input[name="n_hold"]').fill('4');
  await page.locator('#policy-search').fill('');
  await page.locator('input[name="max_weight"]').fill('0.3');
  await page.locator('input[name="hold_until_rank"]').fill('6');
  await expect(page.locator('#policy-summary-text')).toContainText('目标持有 4 只');
  await expect(page.locator('#policy-change-count')).toContainText('修改了 3 项');
  await page.locator('#policy-change-count').click();
  await expect(page.locator('#policy-change-list')).toContainText('30%');
  await page.locator('#policy-preset').selectOption('risk');
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '保留当前微调', exact: true }).click();
  await expect(page.locator('#policy-preset')).toHaveValue('patient');
  await expect(page.locator('input[name="n_hold"]')).toHaveValue('4');
  await page.locator('#policy-mode').selectOption('ewma');
  await expect(page.locator('[data-field="target_volatility"]')).toBeVisible();
  await page.locator('#policy-mode').selectOption('fixed');
  await expect(page.locator('[data-field="target_volatility"]')).toBeHidden();
  await capture('06-policy-professional');
  const advanced = await page.locator('#advanced input').evaluateAll(inputs => inputs.map(input => [input.name, input.value]));
  await page.locator('#advanced > summary').click();
  expect(await page.locator('#advanced input').evaluateAll(inputs => inputs.map(input => [input.name, input.value]))).toEqual(advanced);
  await page.getByRole('button', { name: '冻结设置，进入检验' }).click();
  await page.waitForURL(/\/strategies\/[0-9a-f]{32}\/[0-9a-f]{32}$/);
  strategyURL = page.url();
  const frozen = await (await page.request.get(`${strategyURL}/config.json`)).json();
  expect(frozen.resolved.policy.parameters.n_hold).toBe(4);
  expect(frozen.resolved.policy.parameters.max_weight).toBe(.3);
  expect(frozen.resolved.policy.parameters.adjustment_speed).toBe(.5);
  expect(frozen.resolved.inactive).toContain('target_volatility');
  await expect(page.getByRole('heading', { name: '下一步：做一次历史检验' })).toBeVisible();
  await page.locator('input[name="start"]').fill('2022-12-01');
  await page.locator('input[name="end"]').fill('2022-12-30');
  await page.getByRole('button', { name: '记录尝试并检验' }).click();
  await finishJob();
  await page.getByRole('link', { name: '返回策略与检验结果' }).click();
  await expect(page.getByRole('heading', { name: '下一步：开启纸面观察' })).toBeVisible();
  await capture('07-history-and-next-step');
  await page.getByRole('button', { name: '开启纸面观察', exact: true }).click();
  await expect(page.locator('main')).toContainText('合成数据');
  // Never disguise synthetic history as forward. Use the explicit demo path.
  await page.goto(strategyURL);
  await page.getByText('高级：用历史演示运行流程', { exact: true }).click();
  await page.locator('input[name="historical_start"]').fill('2022-12-01');
  await page.getByRole('button', { name: '开启历史演示', exact: true }).click();
  await expect(page.getByRole('heading', { name: '日常只需看这里' })).toBeVisible();
  await expect(page.locator('#history')).not.toHaveAttribute('open');
  await page.getByRole('button', { name: '检查新行情并记账' }).click();
  await finishJob();
  await page.getByRole('link', { name: '返回策略与检验结果' }).click();
  const ledger = await (await page.request.get(`${strategyURL}/records.csv`)).text();
  expect(ledger).toContain('historical_replay');
  expect(ledger).not.toContain('forward_paper');
  await capture('08-running');
  await page.goto(`${baseURL}/start`);
  await expect(page.getByRole('heading', { name: '继续上次的工作' })).toBeVisible();
  await expect(page.locator('main')).toContainText('发现 1 个符合当前五日治理口径的模型');
  await expect(page.locator('.experience-project .tag')).toHaveText('历史演示');
  await capture('09-resume');
  for (const [width, height] of [[390, 844], [768, 1024]]) {
    await page.setViewportSize({ width, height });
    for (const [name, url] of [['home', `${baseURL}/start`], ['editor', `${strategyURL}/edit`], ['running', strategyURL]]) {
      await page.goto(url);
      if (name === 'editor') await page.locator('#advanced > summary').click();
      await capture(`${name}-${width}`);
    }
  }
  expect(errors).toEqual([]);
  const acceptance = { status: 'PASS', scope: 'synthetic software workflow only', mockedRequests: 0,
    actualTraining: 'Ridge / 40 synthetic stocks', strategyURL, captures, pageErrors: errors,
    checks: ['empty_state', 'training_to_policy', 'live_explanation', 'parameter_search', 'preset_discard_guard',
      'advanced_state_preserved', 'frozen_values_match', 'historical_evaluation', 'synthetic_forward_blocked',
      'historical_ledger', 'resume', 'responsive_390_768_1440'],
    limitation: 'No external user usability claim, market evidence, or live forward record.' };
  await writeFile(path.join(out, 'acceptance.json'), JSON.stringify(acceptance, null, 2));
  console.log(JSON.stringify(acceptance));
} catch (error) {
  await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {});
  await writeFile(path.join(out, 'failure.html'), await page.content());
  console.error(page.url(), await page.locator('main').innerText());
  throw error;
} finally { await browser.close(); }
