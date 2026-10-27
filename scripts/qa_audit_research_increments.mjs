// Synthetic-only acceptance: order fee basis -> report -> blocked target repair.
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
const require=createRequire(new URL('../website/package.json',import.meta.url));
const {chromium,expect}=require('@playwright/test');
const base=process.env.TEMPERQ_INPUT_QA_URL||'http://127.0.0.1:8863';
const output=path.resolve('artifacts/audit-research-increments-qa');
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,channel:'chrome'});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('response',r=>{if(r.status()>=500)errors.push(r.url()+' '+r.status());});
async function shot(name){
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
}
try{
  await page.goto(base+'/strategy-audits/new');
  await page.locator('[name=name]').fill('合成验收 · 一笔委托两次成交');
  const trades='date,time,code,side,quantity,price,fee,order_id,fill_id,commission_basis,commission_evidence,order_complete\n'+
    '2024-01-02,09:30,600000.SH,buy,1000,10,6,SYN-A1,SYN-F1,order,synthetic contract,true\n'+
    '2024-01-02,09:31,600000.SH,buy,1000,10,0,SYN-A1,SYN-F2,order,synthetic contract,true\n';
  await page.locator('[name=files]').setInputFiles([
    {name:'strategy.py',mimeType:'text/plain',buffer:Buffer.from('# synthetic\nx=1\n')},
    {name:'fills.csv',mimeType:'text/csv',buffer:Buffer.from(trades)}]);
  await page.getByRole('button',{name:'导入并核对材料',exact:true}).click();
  await expect(page.locator('main')).toContainText('委托归属与最低佣金单位');
  await page.locator('[name=same_run]').check();
  await page.getByRole('button',{name:'完成导入，进入策略治理',exact:true}).click();
  await page.getByRole('link',{name:'查看完整报告与可复算明细',exact:true}).click();
  const feeUrl=page.url();
  await expect(page.getByRole('heading',{name:'按订单核对最低佣金',exact:true})).toBeVisible();
  await expect(page.locator('main')).toContainText('修正记账口径，不是交易节省');
  await expect(page.locator('main')).toContainText('0 个差额超过');
  const csv=await (await page.request.get(feeUrl+'/fee-units.csv')).text();
  expect(csv).toContain('SYN-A1');
  expect(csv).toContain('confirmed');
  expect(csv.trim().split('\n')).toHaveLength(2);
  await shot('01-order-fee-desktop');
  await page.setViewportSize({width:390,height:844});
  await shot('02-order-fee-mobile');
  await page.goto(base+'/review/new?example=1');
  await page.locator('[name=account_clear]').check();
  await page.getByRole('button',{name:'计算诊断与调整方案',exact:true}).click();
  const repairUrl=page.url();
  await expect(page.getByRole('heading',{name:'目标受阻后，还能调整多少',exact:true})).toBeVisible();
  await expect(page.locator('#target-repair')).toContainText('股票偏离下界');
  await expect(page.locator('main')).toContainText('只做朝向原目标的调整');
  const evidence=await (await page.request.get(repairUrl+'/evidence.json')).json();
  const result=evidence.result;
  const subset=result.candidates.find(c=>c.id==='target_only');
  expect(subset.feasible).toBe(true);
  expect(subset.violations).toHaveLength(0);
  expect(subset.rows.find(r=>r.code==='000858.SZ').delta).toBe(0);
  expect(subset.rows.find(r=>r.code==='000001.SZ').delta).toBeGreaterThanOrEqual(0);
  for(const width of [320,390,1440]){
    await page.setViewportSize({width,height:width===1440?1000:844});
    await shot('03-target-repair-'+width);
  }
  expect(errors).toEqual([]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({passed:true,feeUrl,repairUrl,errors},null,2));
  console.log(JSON.stringify({passed:true,feeUrl,repairUrl,output}));
}catch(error){
  await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
  throw error;
}finally{await browser.close();}
