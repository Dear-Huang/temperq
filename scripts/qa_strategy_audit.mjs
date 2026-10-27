// This flow uses synthetic files in an isolated QA workspace only.
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
const require = createRequire(new URL('../website/package.json', import.meta.url));
const {chromium, expect} = require('@playwright/test');
const base = process.env.TEMPERQ_AUDIT_QA_URL || 'http://127.0.0.1:8852';
const output = path.resolve('artifacts/strategy-audit-qa');
await mkdir(output, {recursive: true});
const browser = await chromium.launch({headless: true, channel: 'chrome'});
const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
const errors = [];
page.on('pageerror', error => errors.push(error.message));
async function shot(name) {
  await page.evaluate(() => window.scrollTo({top:0, left:0, behavior:'instant'}));
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1)).toBe(true);
  await expect.poll(() => page.locator('.nav').evaluate(e => e.scrollWidth <= e.clientWidth+1)).toBe(true);
  await page.screenshot({path: path.join(output,name+'.png'), fullPage: true});
}
const tables = {
  'trades.csv': '日期,时间,证券代码,买/卖,数量,成交价,成交金额,交易费用,commission_basis,commission_evidence\n2024-01-03,09:30,600000.SH,卖出,100,11,1100,6.1,fill,synthetic contract\n2024-01-02,09:30,600000.SH,买入,100,10,1000,5,fill,synthetic contract\n,09:30,000001.SZ,买入,100,20,2000,5,fill,synthetic contract\n',
  'holdings.csv': '日期,证券代码,数量,持仓均价,收盘价,持仓市值\n2024-01-03,000001.SZ,100,20,21,2100\n,,,总资产: 10183.9,现金: 8083.9,2100\n2024-01-02,600000.SH,100,10,10,1000\n,000001.SZ,100,20,20,2000\n,,,总资产: 9990,现金: 6990,3000\n',
  'strategy.py': 'def initialize(context):\n    context.stock_count = 2\n',
  'quotes.csv': 'date,code,quoted_spread_bp,sample_count,measurement_scope\n2024-01-02,600000.SH,10,100,daily_quote_mean\n2024-01-02,000001.SZ,5,100,daily_quote_mean\n2024-01-03,600000.SH,9.2,100,daily_quote_mean\n',
};
try {
  await page.goto(base+'/strategy-audits/new');
  await expect(page.getByRole('heading', {name: '策略导入', exact: true})).toBeVisible();
  await shot('01-import-desktop');
  for (const width of [390,320]) {
    await page.setViewportSize({width, height: 844});
    await shot('02-import-mobile-'+width);
  }
  await page.locator('input[name="name"]').fill('合成策略 · 双股生命周期');
  await page.locator('input[name="files"]').setInputFiles(Object.entries(tables).map(([name,text]) => ({name, mimeType:'text/plain', buffer: Buffer.from(text)})));
  await page.locator('input[name="opening_empty"]').check();
  await page.locator('input[name="opening_cash"]').fill('10000');
  await page.getByRole('button', {name: '导入并核对材料', exact:true}).click();
  await expect(page.getByRole('heading', {name: '01 · 材料核对', exact:true})).toBeVisible();
  await shot('03-inspect-mobile');
  await page.locator('input[name="same_run"]').check();
  await page.getByRole('button', {name: '完成导入，进入策略治理', exact:true}).click();
  await page.getByRole('link', {name:'查看完整报告与可复算明细',exact:true}).click();
  await expect(page.getByText('账目已核对', {exact:false})).toBeVisible();
  await expect(page.locator('.audit-stage')).toHaveCount(15);
  await expect(page.getByRole('heading',{name:'价位成本：收益能否覆盖半档价差情景',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'实际持有期与收益覆盖',exact:true})).toBeVisible();
  await expect(page.locator('#tick-cost')).toContainText('匹配 3 笔');
  await expect(page.locator('#holding-age')).toContainText('右删失');
  await expect(page.getByRole('heading', {name:'费用核验与可计算建议',exact:true})).toBeVisible();
  await expect(page.locator('#risk-diagnostics')).toContainText('最大快照回撤');
  await expect(page.locator('main a[href="/train"],.nav a[href="/models"],.nav a[href="/start"]')).toHaveCount(0);
  await page.locator('#stage-constraint_tradeoffs > summary').click();
  await expect(page.locator('#stage-constraint_tradeoffs')).toContainText('50.00%');
  await page.locator('#stage-costs > summary').click();
  await expect(page.locator('#stage-costs')).toContainText('¥16.10');
  await expect(page.locator('#stage-costs')).toContainText('检查内容');
  await expect(page.locator('#stage-costs')).toContainText('本次范围');
  await page.getByRole('button', {name:'帮助与说明',exact:true}).click();
  await expect(page.locator('#workspace-help')).toBeVisible();
  await expect(page.locator('#workspace-help')).not.toContainText('训练');
  await page.getByRole('button', {name:'关闭帮助',exact:true}).click();
  const reportUrl = page.url();
  for (const width of [320,390,1440]) {
    await page.setViewportSize({width, height:width===1440 ? 1000 : 844});
    await shot('04-report-'+width);
  }
  const response = await page.request.get(reportUrl+'/evidence.json');
  expect(response.ok()).toBe(true);
  const evidence = await response.json();
  expect(Math.abs(evidence.result.modules.attribution.net_pnl-183.9)).toBeLessThan(.00001);
  expect(evidence.result.modules.accounting.exceptions).toEqual([]);
  expect(evidence.result.coverage).toHaveLength(15);
  expect(evidence.result.modules.fee_analysis.mismatch_count).toBe(1);
  expect(evidence.result.modules.risk_analysis.max_drawdown.contribution_gap).toBe(0);
  expect(evidence.result.modules.tick_analysis.all_exported_fills.tick_scenario_cost).toBe(1.5);
  expect(evidence.result.modules.tick_analysis.calibration.matched_fills).toBe(3);
  expect(evidence.result.modules.holding_analysis.coverage.net_bridge_gap).toBe(0);
  expect((await page.request.get(reportUrl+'/fees.csv')).ok()).toBe(true);
  expect((await page.request.get(reportUrl+'/risk.csv')).ok()).toBe(true);
  expect((await page.request.get(reportUrl+'/tick-costs.csv')).ok()).toBe(true);
  expect((await page.request.get(reportUrl+'/holding-lots.csv')).ok()).toBe(true);
  expect(evidence.result.coverage.find(c => c.id==='capacity').status).toBe('not_implemented');
  await page.getByRole('link', {name:'带入期末持仓，准备账户方案',exact:true}).click();
  await expect(page.getByText('已带入历史期末持仓和现金', {exact:false})).toBeVisible();
  expect(await page.locator('#holdings_csv').inputValue()).toContain('000001.SZ,100,0');
  await page.setViewportSize({width:390,height:844});
  await shot('05-account-handoff-mobile');
  await page.locator('input[name="observed_at"]').fill('2024-01-03T15:30');
  await page.locator('input[name="signal_available_at"]').fill('2024-01-03T15:00');
  await page.locator('input[name="execution_date"]').fill('2024-01-04');
  await page.locator('#signals').fill('code,value\n000001.SZ,0.4\n600000.SH,0.4');
  await page.locator('#quotes_csv').fill('code,price,status\n000001.SZ,21,conditional\n600000.SH,11,conditional');
  await page.locator('input[name="max_weight"]').fill('50');
  await page.locator('input[name="turnover_cap"]').fill('100');
  await page.locator('input[name="account_clear"]').check();
  await page.getByRole('button', {name:'计算诊断与调整方案',exact:true}).click();
  await expect(page.getByRole('heading',{name:'推荐调整',exact:true})).toBeVisible();
  await shot('06-plan-mobile');
  expect(errors).toEqual([]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({passed:true,reportUrl,errors},null,2));
  console.log(JSON.stringify({passed:true,reportUrl,output}));
} finally {
  await browser.close();
}
