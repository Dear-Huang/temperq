// Full synthetic flow: audit findings -> selected governance patch -> code download.
import {createRequire} from 'node:module';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const require=createRequire(new URL('../website/package.json',import.meta.url));
const {chromium,expect}=require('@playwright/test');
const base=process.env.TEMPERQ_INPUT_QA_URL||'http://127.0.0.1:8863';
const output=path.resolve('artifacts/audit-code-qa');
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,channel:'chrome'});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('response',r=>{if(r.status()>=500) errors.push(r.url()+' '+r.status())});
const source=`# Synthetic governance rewrite example
def initialize(context):
    context.max_cash_per_instrument = 0.5
    context.set_commission(PerOrder(buy_cost=0.0003, sell_cost=0.0013, min_cost=5))
def handle_data(context, data):
    ranked = context.data.sort_values('score', ascending=False)
    for instrument in ranked.instrument[:2]:
        cash = context.portfolio.cash / 2
        if cash > 0:
            context.order_value(instrument, cash)
    context.order_target(instrument, 0)
`;
async function shot(name){
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
}
try{
  const response=await page.request.get(base+'/strategy-audits/sample.zip');
  expect(response.ok()).toBe(true);
  await page.goto(base+'/strategy-audits/new');
  await page.locator('[name=name]').fill('合成策略 · 治理代码可改写');
  const tables={
    'strategy.py':source,
    'trades.csv':'日期,时间,证券代码,买/卖,数量,成交价,成交金额,交易费用,commission_basis,commission_evidence\n2024-01-02,09:30,600000.SH,买入,100,10,1000,5,fill,synthetic contract\n2024-01-03,09:30,600000.SH,卖出,100,11,1100,6.1,fill,synthetic contract\n',
    'holdings.csv':'日期,证券代码,数量,持仓均价,收盘价,持仓市值\n2024-01-02,600000.SH,100,10,10,1000\n,,,总资产: 9995,现金: 8995,1000\n2024-01-03,--,0,0,0,0\n,,,总资产: 10088.9,现金: 10088.9,0\n',
  };
  await page.locator('[name=files]').setInputFiles(Object.entries(tables).map(([name,text])=>({name,mimeType:'text/plain',buffer:Buffer.from(text)})));
  await page.getByRole('button',{name:'导入并核对材料',exact:true}).click();
  await page.locator('[name=max_weight]').fill('25');
  await page.locator('[name=same_run]').check();
  await page.getByRole('button',{name:'完成导入，进入策略治理',exact:true}).click();
  const governanceUrl=page.url();
  await expect(page.locator('header #current-strategy')).toHaveCount(0);
  await expect(page.locator('main #current-strategy')).toBeVisible();
  await expect(page.getByRole('heading',{name:'本次审计发现',exact:true})).toBeVisible();
  await expect(page.locator('#stage-signal_contract > summary')).toContainText('核对预测的含义');
  await expect(page.locator('#stage-signal_contract > summary')).toContainText('缺乏证据');
  await expect(page.locator('#stage-account_integrity > summary')).toContainText('基本通过');
  await expect(page.locator('#stage-costs > summary')).toContainText('需要调整');
  await expect(page.locator('#stage-costs > summary .verdict-adjust')).toBeVisible();
  await expect(page.locator('#coverage')).not.toContainText('基础测量');
  await page.locator('[name=minimum_buy_notional]').fill('5000');
  await shot('01-audit-actions-desktop');
  for(const width of [390,320]){
    await page.setViewportSize({width,height:844});
    await shot('02-audit-actions-'+width);
  }
  await page.getByRole('button',{name:'生成优化代码候选',exact:true}).click();
  await expect(page.getByRole('heading',{name:'优化代码候选',exact:true})).toBeVisible();
  await expect(page.locator('.code-diff')).toContainText('if cash >= 5000.0:');
  await expect(page.locator('.code-diff')).toContainText('context.max_cash_per_instrument = 0.25');
  await expect(page.locator('main')).toContainText('待原平台回测');
  const candidateUrl=page.url();
  const download=await Promise.all([page.waitForEvent('download'),page.getByRole('link',{name:'下载优化代码 .py',exact:true}).click()]);
  expect(download[0].suggestedFilename()).toBe('optimized_candidate.py');
  const code=await (await page.request.get(candidateUrl+'/optimized.py')).text();
  expect(code).toContain("ranked = context.data.sort_values('score', ascending=False)");
  expect(code).toContain('context.order_target(instrument, 0)');
  expect(code).toContain('buy_cost=0.0003, sell_cost=0.0013, min_cost=5');
  for(const width of [320,390,1440]){
    await page.setViewportSize({width,height:width===1440?1000:844});
    await shot('03-code-candidate-'+width);
  }
  await page.goto(governanceUrl);
  await expect(page.getByRole('heading',{name:'已生成的代码候选',exact:true})).toBeVisible();
  expect(errors).toEqual([]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({passed:true,governanceUrl,candidateUrl,errors},null,2));
  console.log(JSON.stringify({passed:true,governanceUrl,candidateUrl,output}));
}catch(error){
  await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
  console.error(JSON.stringify({url:page.url(),errors}));
  throw error;
}finally{await browser.close()}
