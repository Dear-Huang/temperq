// Synthetic exports only. The QA server must use its own isolated workspace.
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
const require = createRequire(new URL('../website/package.json', import.meta.url));
const {chromium, expect} = require('@playwright/test');
const base = process.env.TEMPERQ_INPUT_QA_URL || 'http://127.0.0.1:8863';
const output = path.resolve('artifacts/audit-input-a2-qa');
await mkdir(output, {recursive:true});
const browser = await chromium.launch({headless:true, channel:'chrome'});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('response', r => { if(r.status()>=500) errors.push(r.url()+' '+r.status()); });
async function screenshot(name) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1)).toBe(true);
  await expect.poll(() => page.locator('.nav').evaluate(e => e.scrollWidth <= e.clientWidth+1)).toBe(true);
  await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
}
async function upload(tables, name) {
  await page.goto(base+'/strategy-audits/new');
  await page.locator('[name=adapter]').selectOption('bigquant');
  await expect(page.locator('[data-template-kind=fills]')).toHaveAttribute('href',/adapter=bigquant$/);
  await expect(page.locator('[data-template-kind=snapshots]')).toBeHidden();
  await page.locator('[name=adapter]').selectOption('auto');
  await page.locator('[name=name]').fill(name);
  await page.locator('[name=files]').setInputFiles(Object.entries(tables).map(([name,text])=>({name,mimeType:'text/plain',buffer:Buffer.from(text)})));
  await page.getByRole('button',{name:'导入并核对材料',exact:true}).click();
  await expect(page.getByRole('heading',{name:'01 · 材料核对',exact:true})).toBeVisible();
}
const dates=[];
for(let d=new Date('2024-05-07T12:00:00Z');d<=new Date('2024-12-31T12:00:00Z');d.setUTCDate(d.getUTCDate()+1)) {
  if(![0,6].includes(d.getUTCDay())) dates.push(d.toISOString().slice(0,10));
}
const longHoldings='日期,证券代码,数量,持仓均价,收盘价,持仓市值\n'+dates.map(d=>`${d},600000.SH,100,10,10,1000\n,,,总资产: 9995,现金: 8995,1000`).join('\n')+'\n';
try {
  await page.goto(base+'/');
  await expect(page).toHaveURL(/strategy-governance/);
  await upload({
    'strategy.py': "capital_base=10000\nbuy_cost=.0003\nmin_cost=5\nstart_date='2023-01-01'\nend_date='2025-12-31'\n",
    'trades.csv': '日期,时间,证券代码,买/卖,数量,成交价,成交金额,交易费用\n2024-05-07,09:30,600000.SH,买入,100,10,1000,5\n',
    'holdings.csv':longHoldings,
    'logs.csv':'时间,级别,内容\n2024-05-07,INFO,synthetic run\n',
  }, '合成八个月策略');
  const identity=new URL(page.url()).pathname.split('/').at(-1);
  await expect(page.locator('#input-consistency')).toContainText('2025-12-31');
  await expect(page.locator('#audit-period')).toContainText('规则效果仅作描述');
  await page.locator('details.import-details').filter({has:page.locator('#audit-period')}).locator(':scope > summary').click();
  expect(await page.locator('[name=commission_bp]').inputValue()).toBe('3.0');
  await expect(page.locator('.audit-split')).toBeVisible();
  await screenshot('01-inspection-desktop');
  await page.locator('[name=same_run]').check();
  await page.getByRole('button',{name:'完成导入，进入策略治理',exact:true}).click();
  await expect(page).toHaveURL(new RegExp('strategy-governance\\?strategy='+identity));
  await expect(page.locator('#input-consistency')).toHaveCount(0);
  await expect(page.locator('input[type=file]')).toHaveCount(0);
  await expect(page.getByRole('heading',{name:'本次审计发现',exact:true})).toBeVisible();
  await expect(page.locator('header #current-strategy')).toHaveCount(0);
  await expect(page.locator('main #current-strategy')).toHaveValue(identity);
  await screenshot('01b-governance-desktop');
  await page.getByRole('link',{name:'查看完整报告与可复算明细',exact:true}).click();
  const reportUrl=page.url();
  const response=await page.request.get(reportUrl+'/evidence.json');
  const evidence=await response.json();
  const split=evidence.result.period_review.partition;
  expect(split.status).toBe('planned');
  expect(split.settings.validation_fraction).toBe(.25);
  expect(split.purged_dates).toHaveLength(20);
  expect(split.development_end < split.validation_start).toBe(true);
  expect(evidence.result.period_review.band).toBe('3m_to_1y');
  expect(evidence.result.confirmation.same_run).toBe(true);
  const originalDigest=evidence.result.result_digest;
  for(const width of [390,320]) {
    await page.setViewportSize({width,height:844});
    await screenshot('02-report-mobile-'+width);
  }
  for(const [label,route] of [['盈亏归因','strategy-attribution'],['效果验证','strategy-comparison'],['策略治理','strategy-governance']]) {
    await page.locator('.nav').getByRole('link',{name:label,exact:true}).click();
    await expect(page).toHaveURL(new RegExp(route+'\\?strategy='+identity));
    await expect(page.locator('#current-strategy')).toHaveValue(identity);
    await screenshot('03-'+route+'-mobile');
  }
  const reread=await (await page.request.get(reportUrl+'/evidence.json')).json();
  expect(reread.result.result_digest).toBe(originalDigest);
  await page.locator('.strategy-actions').getByRole('link',{name:'我的策略',exact:true}).click();
  const strategyRow=page.locator(`[data-strategy="${identity}"]`);
  for(const name of ['一键审计','一键归因','效果验证']) await expect(strategyRow.getByRole(name==='一键审计'?'button':'link',{name,exact:true})).toBeVisible();
  await screenshot('03b-my-strategies-mobile');
  await strategyRow.getByRole('button',{name:'一键审计',exact:true}).click();
  await expect(page).toHaveURL(new RegExp('strategy-governance\\?strategy='+identity));
  // A missing-input action opens the exact upload panel, retaining old files.
  await page.getByRole('link',{name:'材料管理 / 补充上传',exact:true}).click();
  // Optional materials are folded; open them before using the upload link.
  await page.locator('#optional-materials > summary').click();
  await page.locator('#input-consistency').getByRole('link',{name:'上传每日打分',exact:true}).click();
  const scorePanel=page.locator('#material-scores');
  await expect(scorePanel).toHaveAttribute('open','');
  const download=await Promise.all([page.waitForEvent('download'),scorePanel.getByRole('link',{name:'下载每日打分模板',exact:true}).click()]);
  expect(download[0].suggestedFilename()).toBe('scores-template.zip');
  await scorePanel.locator('[name=files]').setInputFiles({name:'scores.csv',mimeType:'text/csv',buffer:Buffer.from('date,code,score,available_at\n2024-05-07,600000.SH,0.8,2024-05-07T08:00:00+08:00\n')});
  await scorePanel.getByRole('button',{name:'上传并重新核对',exact:true}).click();
  const supplemented=new URL(page.url()).pathname.split('/').at(-1);
  expect(supplemented).not.toBe(identity);
  await expect(page.locator('#current-strategy')).toHaveValue(supplemented);
  await expect(page.locator('.review-meta').first()).toContainText(dates.length+' 个账户快照');
  await page.locator('[name=same_run]').check();
  await page.getByRole('button',{name:'完成导入，进入策略治理',exact:true}).click();
  await expect(page).toHaveURL(new RegExp('strategy-governance\\?strategy='+supplemented));
  const oldAfterSupplement=await (await page.request.get(reportUrl+'/evidence.json')).json();
  expect(oldAfterSupplement.result.result_digest).toBe(originalDigest);
  await page.locator('#current-strategy').selectOption('new');
  await expect(page).toHaveURL(/strategy-audits\/new/);
  await upload({
    'strategy.py':'capital_base=10000\n',
    'positions.csv':'日期,品种,标的,多空,数量,可用数量,收盘价/结算价,市值/价值,仓位占比\n2024-01-02,股票,合成甲(600000.XSHG),多,100股,0股,10,1000,10000,10%\n2024-01-02,,Cash,,,,,9000,0\n2024-01-03,股票,合成甲(600000.XSHG),多,200股,100股,11,2200,10100,22%\n2024-01-03,,Cash,,,,,7900,0\n',
  }, '合成聚宽净变动');
  await expect(page.locator('main')).toContainText('由持仓推算');
  await page.locator('[name=same_run]').check();
  await page.getByRole('button',{name:'完成导入，进入策略治理',exact:true}).click();
  await expect(page.getByRole('heading',{name:'本次审计发现',exact:true})).toBeVisible();
  await expect(page.locator('main')).toContainText('未知，不按0元处理');
  await screenshot('04-joinquant-partial-mobile');
  await page.goto(base+'/strategy-audits/new');
  await page.locator('[name=name]').fill('合成代码静态检查');
  await page.locator('details').filter({has:page.locator('[name=code_text]')}).locator('summary').click();
  await page.locator('[name=code_text]').fill('capital_base=1000000\n');
  await page.getByRole('button',{name:'导入并核对材料',exact:true}).click();
  await expect(page.locator('#audit-period')).toContainText('仅代码静态检查');
  await page.locator('[name=same_run]').check();
  await page.getByRole('button',{name:'完成导入，进入策略治理',exact:true}).click();
  await expect(page.locator('main')).toContainText('仅代码静态检查');
  await screenshot('05-code-only-mobile');
  await page.goto(base+'/review');
  await expect(page.getByRole('heading',{name:'策略与资源',exact:true})).toBeVisible();
  expect(errors).toEqual([]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({passed:true,reportUrl,split,errors},null,2));
  console.log(JSON.stringify({passed:true,reportUrl,output}));
} catch (error) {
  await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
  console.error(JSON.stringify({url:page.url(),title:await page.title(),errors}));
  throw error;
} finally {
  await browser.close();
}
