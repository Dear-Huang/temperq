import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

const require = createRequire(new URL('../website/package.json', import.meta.url));
const { chromium } = require('@playwright/test');
const workspace = path.resolve(process.argv[2]);
const info = JSON.parse(await readFile(path.join(workspace, 'qa.json'), 'utf8'));
if (!info.synthetic_only) throw Error('Synthetic workspace required');
const output = path.join(workspace, 'screenshots');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
async function capture(name) {
  await page.screenshot({ path: path.join(output, name+'.png'), fullPage: true });
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1))
    throw Error(`Horizontal overflow: ${name}`);
}
try {
  await page.goto(info.url);
  await capture('editor-desktop');
  await page.locator('input[name=signals]').setInputFiles(info.signals);
  await page.locator('select[name=score_kind]').selectOption('expected_return');
  await page.locator('input[name=allow_between]').check();
  await page.locator('input[name=start]').fill('2026-10-09');
  await page.locator('input[name=end]').fill('2026-10-22');
  await page.getByText('高级设置', { exact: true }).click();
  await page.locator('input[name=extra_turnover_cap]').fill('2');
  await page.locator('input[name=calibration_id]').fill('synthetic-qa-v1');
  await page.locator('input[name=calibration_end_at]').fill('2026-10-01T15:30:00+08:00');
  await page.getByRole('button', { name: '比较调仓规则' }).click();
  await page.waitForURL(/\/timing\/[0-9a-f]{32}$/);
  await page.getByText('每日决定与原因', { exact: true }).click();
  if (!await page.getByText('满足额外调整授权', { exact: true }).count())
    throw Error('No authorized event found');
  await capture('result-desktop');
  await page.setViewportSize({ width: 390, height: 844 });
  await capture('result-mobile');
  await page.goto(info.url);
  await capture('editor-mobile');
  if (errors.length) throw Error(errors.join('\n'));
  await writeFile(path.join(output, 'result.json'), JSON.stringify({ passed: true, errors }, null, 2));
  console.log('Timing browser flow passed on desktop and mobile.');
} finally {
  await browser.close();
}
