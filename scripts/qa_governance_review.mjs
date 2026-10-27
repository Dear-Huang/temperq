// Browser checks operate only on the isolated governance-review QA server.
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
const require = createRequire(new URL('../website/package.json', import.meta.url));
const {chromium, expect} = require('@playwright/test');
const base = process.env.TEMPERQ_REVIEW_QA_URL || 'http://127.0.0.1:8846';
const output = path.resolve(process.env.TEMPERQ_REVIEW_QA_OUTPUT || 'artifacts/governance-review-qa');
await mkdir(output, {recursive: true});
const browser = await chromium.launch({headless: true, channel: 'chrome'});
const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
const errors = [];
page.on('pageerror', error => errors.push(error.message));
async function shot(name) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: path.join(output, name+'.png'), fullPage: true});
}
try {
  await page.goto(base+'/');
  await expect(page.getByRole('heading', {name: '治理工作台', exact: true})).toBeVisible();
  await shot('01-home-desktop');
  await page.getByRole('link', {name: '载入合成示例', exact: true}).click();
  await expect(page.getByRole('heading', {name: '新建治理诊断', exact: true})).toBeVisible();
  await shot('02-input-desktop');
  await page.setViewportSize({width: 390, height: 844});
  await shot('03-input-mobile');
  await page.setViewportSize({width: 320, height: 740});
  await shot('03b-input-small-mobile');
  await page.setViewportSize({width: 390, height: 844});
  await page.locator('input[name="account_clear"]').check();
  await page.getByRole('button', {name: '计算诊断与调整方案'}).click();
  await expect(page.getByRole('heading', {name: '推荐调整', exact: true})).toBeVisible();
  await expect(page.getByRole('heading', {name: '逐环节诊断', exact: true})).toBeVisible();
  await shot('04-result-mobile');
  const resultUrl = page.url();
  await page.setViewportSize({width: 1440, height: 1000});
  await shot('05-result-desktop');
  await page.screenshot({path: path.join(output, 'governance-review-preview.png'), fullPage: false});
  await page.getByRole('button', {name: '确认并保存推荐方案', exact: true}).click();
  await expect(page.getByRole('link', {name: '下载已确认计划', exact: true})).toBeVisible();
  const link = await page.getByRole('link', {name: '下载已确认计划'}).getAttribute('href');
  const plan = await page.request.get(base+link);
  expect(plan.ok()).toBe(true);
  expect(await plan.text()).toContain('proposed_quantity');
  await shot('06-confirmed-desktop');
  await page.getByRole('link', {name: '修改输入再比较', exact: true}).click();
  await page.locator('input[name="max_weight"]').fill('10');
  await page.locator('#holdings_csv').fill('code,quantity,sellable_quantity\n600000.SH,4500,0\n000001.SZ,4400,0');
  await page.locator('input[name="account_clear"]').check();
  await page.getByRole('button', {name: '计算诊断与调整方案'}).click();
  await expect(page.getByRole('heading', {name: '先处理当前约束冲突', exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '确认并保存方案', exact: true})).toHaveCount(0);
  await page.setViewportSize({width: 390, height: 844});
  await shot('07-infeasible-mobile');
  await page.goto(base+'/review');
  await expect(page.locator('.review-history>a')).toHaveCount(2);
  await shot('08-history-mobile');
  expect(errors).toEqual([]);
  await writeFile(path.join(output,'result.json'), JSON.stringify({passed: true, errors, resultUrl, isolated: true}, null, 2));
  console.log(JSON.stringify({passed: true, output, resultUrl}));
} finally {
  await browser.close();
}
