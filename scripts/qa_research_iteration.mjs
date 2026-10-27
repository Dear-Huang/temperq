// Run only against a server created with qa_governance_review_server.py.
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
const require = createRequire(new URL('../website/package.json', import.meta.url));
const {chromium, expect} = require('@playwright/test');
const base = process.env.TEMPERQ_ITERATION_QA_URL || 'http://127.0.0.1:8876';
const output = path.resolve('artifacts/research-iteration-qa');
await mkdir(output, {recursive:true});
const browser = await chromium.launch({headless:true, channel:'chrome'});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('response', r => {if(r.status() >= 500) errors.push(r.url()+' '+r.status());});
async function shot(name, fullPage=true) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1)).toBe(true);
  const missing = await page.locator('main nav a[href^="#"]').evaluateAll(links =>
    links.map(a => a.getAttribute('href')).filter(href => !document.getElementById(href.slice(1))));
  expect(missing).toEqual([]);
  await page.screenshot({path:path.join(output,name+'.png'),fullPage});
}
try {
  await expect.poll(async () => {
    try {return (await fetch(base+'/strategy-governance')).ok;} catch {return false;}
  }, {timeout:20000}).toBe(true);
  await page.goto(base+'/');
  await expect(page.getByRole('heading',{name:'策略治理',exact:true})).toBeVisible();
  await expect(page.getByRole('navigation',{name:'策略处理流程'})).toBeVisible();
  await shot('01-empty-desktop');
  await page.setViewportSize({width:320,height:844});
  await shot('02-empty-mobile');
  await page.goto(base+'/review/new?example=1');
  await page.locator('[name=account_clear]').check();
  await page.getByRole('button',{name:'计算诊断与调整方案',exact:true}).click();
  await expect(page.locator('#tolerance-comparison tbody tr')).toHaveCount(3);
  const reviewUrl = page.url();
  const evidence = await (await page.request.get(reviewUrl+'/evidence.json')).json();
  const comparison = evidence.result.tolerance_comparison;
  expect(comparison.rows.map(r => r.additional_tolerance)).toEqual([0,.01,.02]);
  expect(comparison.rows.every(r => r.status === 'verified')).toBe(true);
  for(let index=1; index<comparison.rows.length; index++) {
    const narrow = comparison.rows[index-1], wide = comparison.rows[index];
    if(narrow.solver.optimal_within_tolerance && wide.solver.optimal_within_tolerance)
      expect(wide.estimated_cost).toBeLessThanOrEqual(narrow.estimated_cost+1e-5);
  }
  const csv = await (await page.request.get(reviewUrl+'/comparison.csv')).text();
  expect(csv).toContain('solver_gap');
  expect(csv.trim().split('\n')).toHaveLength(4);
  for(const width of [320,390,1440]) {
    await page.setViewportSize({width,height:width===1440?1000:844});
    await shot('03-review-'+width);
  }
  await shot('04-review-first-viewport',false);
  const save = page.locator('#tolerance-comparison form').first();
  const candidateId = await save.locator('[name=candidate_id]').inputValue();
  await save.getByRole('button').click();
  const planLink = await page.getByRole('link',{name:'下载已确认计划',exact:true}).getAttribute('href');
  const plan = await (await page.request.get(base+planLink)).text();
  const candidate = evidence.result.candidates.find(c => c.id === candidateId);
  expect(plan).toContain('proposed_quantity');
  for(const row of candidate.rows) expect(plan).toContain(row.code);
  const sources = {
    'strategy.py':'def initialize(context):\n    context.stock_count = 2\n',
    'trades.csv':'日期,时间,证券代码,买/卖,数量,成交价,成交金额,交易费用\n2024-01-03,09:30,600000.SH,卖出,100,11,1100,6.1\n2024-01-02,09:30,600000.SH,买入,100,10,1000,5\n,09:30,000001.SZ,买入,100,20,2000,5\n',
    'holdings.csv':'日期,证券代码,数量,持仓均价,收盘价,持仓市值\n2024-01-03,000001.SZ,100,20,21,2100\n,,,总资产: 10183.9,现金: 8083.9,2100\n2024-01-02,600000.SH,100,10,10,1000\n,000001.SZ,100,20,20,2000\n,,,总资产: 9990,现金: 6990,3000\n'
  };
  await page.goto(base+'/strategy-audits/new');
  await page.locator('[name=name]').fill('首期优化 · 合成验收');
  await page.locator('[name=files]').setInputFiles(Object.entries(sources).map(([name,body]) =>
    ({name,mimeType:'text/plain',buffer:Buffer.from(body)})));
  await page.locator('[name=opening_empty]').check();
  await page.locator('[name=opening_cash]').fill('10000');
  await page.getByRole('button',{name:'导入并核对材料',exact:true}).click();
  await page.locator('[name=same_run]').check();
  await page.getByRole('button',{name:'完成导入，进入策略治理',exact:true}).click();
  const governanceUrl = page.url();
  await expect(page.locator('.audit-workspace-metrics')).toContainText('已核对');
  await expect(page.locator('main .strategy-picker')).toHaveCount(1);
  for(const width of [320,390,1440]) {
    await page.setViewportSize({width,height:width===1440?1000:844});
    await shot('05-governance-'+width);
    if(width === 390) await shot('05-governance-mobile-first-viewport',false);
  }
  await shot('06-governance-first-viewport',false);
  const identity = new URL(governanceUrl).searchParams.get('strategy');
  for(const route of ['strategy-attribution','strategy-comparison','strategy-simulation']) {
    await page.goto(base+'/'+route+'?strategy='+identity);
    await expect(page.locator('main #current-strategy')).toHaveValue(identity);
    await page.setViewportSize({width:390,height:844});
    await shot('07-'+route);
  }
  await page.goto(governanceUrl);
  await page.emulateMedia({colorScheme:'dark'});
  await page.setViewportSize({width:1440,height:1000});
  await shot('08-governance-dark',false);
  expect(errors).toEqual([]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({passed:true,reviewUrl,governanceUrl,errors},null,2));
  console.log(JSON.stringify({passed:true,output,reviewUrl,governanceUrl}));
} catch(error) {
  await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
  throw error;
} finally {
  await browser.close();
}
