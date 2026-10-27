// Real UI and account jobs against an isolated, explicitly synthetic workspace.
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';
const require=createRequire(new URL('../website/package.json',import.meta.url));
const {chromium,expect}=require('@playwright/test');
const base=process.env.QWB_RULE_QA_URL || 'http://127.0.0.1:8832';
const out=path.resolve('artifacts/rule-browser-qa');
await mkdir(out,{recursive:true});
for(let attempt=0;attempt<30;attempt++){
  try{if((await fetch(base+'/start')).ok)break;}catch{}
  if(attempt===29)throw Error('Synthetic QA server did not become ready');
  await new Promise(resolve=>setTimeout(resolve,1000));
}
const browser=await chromium.launch({channel:'chrome',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
page.setDefaultTimeout(60000);
page.setDefaultNavigationTimeout(120000);
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
async function shot(name){
  await page.evaluate(()=>document.fonts.ready);
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
  expect(overflow, name+' must fit').toBe(false);
  await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});
}
try{
  await page.goto(base+'/start');
  await shot('01-start-desktop');
  await page.getByRole('link',{name:'选择起步模板'}).click();
  await shot('02-templates-desktop');
  await page.locator('article').filter({has:page.getByRole('heading',{name:'低波稳健',exact:true})}).getByRole('button',{name:'选择',exact:true}).click();
  const project=new URL(page.url()).searchParams.get('project');
  expect(project).toMatch(/^[0-9a-f]{32}$/);
  await page.locator('[name="r0_window"]').fill('25');
  await page.getByRole('button',{name:'添加条件',exact:true}).click();
  expect(await page.locator('.rule-row').count()).toBe(3);
  await expect(page.locator('[name="r0_window"]')).toHaveValue('25');
  await page.getByRole('button',{name:'删除规则',exact:true}).last().click();
  expect(await page.locator('.rule-row').count()).toBe(2);
  await shot('03-editor-desktop');
  await page.setViewportSize({width:390,height:844});
  await shot('04-editor-mobile');
  await page.getByRole('button',{name:'保存并查看模型检验'}).click();
  await expect(page.getByRole('heading',{name:'模型检验',exact:true})).toBeVisible();
  await expect(page.getByText('规则来源与检验范围', {exact:true})).toBeVisible();
  await shot('05-model-result-mobile');
  await page.goto(base+'/start');
  await page.locator(`a[href="/projects/${project}"]`).click();
  await expect(page.getByRole('heading',{name:'模型检验',exact:true})).toBeVisible();
  const exported=await page.request.get(base+'/rules/'+project+'.json');
  const doc=await exported.json();
  expect(doc.rules[0].origin).toBe('user');
  expect(doc.rules[0].window).toBe(25);
  await page.getByRole('link',{name:'修改经验规则',exact:true}).click();
  await page.getByRole('button',{name:'更换模板 / 导入',exact:true}).click();
  await page.locator('details').filter({has:page.getByText('导入自己的规则卡',{exact:true})}).locator('summary').click();
  await page.locator('input[name="rules"]').setInputFiles({name:'rules.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(doc))});
  await page.getByRole('button',{name:'校验并预览',exact:true}).click();
  await expect(page.getByRole('heading',{name:'确认规则变更',exact:true})).toBeVisible();
  await shot('06-import-confirm-mobile');
  // Pending imports do not change the active draft.
  expect((await (await page.request.get(base+'/rules/'+project+'.json')).json()).rules[0].origin).toBe('user');
  await page.getByRole('button',{name:'确认并建立规则草稿',exact:true}).click();
  await page.getByRole('button',{name:'保存并查看模型检验'}).click();
  await page.getByRole('link',{name:'下一步：设置规则 →',exact:true}).click();
  await page.locator('input[name="name"]').fill('规则端到端演示');
  await page.locator('#advanced > summary').click();
  await page.locator('input[name="n_hold"]').fill('4');
  await page.locator('input[name="hold_until_rank"]').fill('6');
  await page.locator('input[name="max_weight"]').fill('0.3');
  await page.getByRole('button',{name:'保存并继续',exact:true}).click();
  const strategyURL=page.url();
  await page.locator('input[name="start"]').fill('2022-11-01');
  await page.locator('input[name="end"]').fill('2023-01-20');
  await page.getByRole('button',{name:'开始检验',exact:true}).click();
  await page.waitForURL(/\/job\//);
  const deadline=Date.now()+180000;
  while(Date.now()<deadline){
    if(await page.locator('.state.succeeded').count())break;
    if(await page.locator('.state.failed').count())throw Error(await page.locator('main').innerText());
    await page.waitForTimeout(1000);
    await page.reload();
  }
  await expect(page.locator('.state.succeeded')).toBeVisible();
  await page.goto(strategyURL);
  await shot('07-rule-result-mobile');
  const report=await (await page.request.get(strategyURL+'/scorecard.json')).json();
  expect(report.rules.every(r=>r.origin==='imported')).toBe(true);
  expect(report.historical_accounts.selected.sessions).toBeGreaterThan(0);
  expect(JSON.stringify(report)).not.toMatch(/data_feed|holdings|C:\\\\|D:\\\\/);
  const json=await (await page.request.get(strategyURL+'/rule-card.json')).json();
  expect(json.rules[0].window).toBe(25);
  await page.getByRole('link',{name:'下一步：部署纸面账户 →',exact:true}).click();
  await expect(page.getByRole('heading',{name:'部署纸面账户',exact:true})).toBeVisible();
  // Synthetic data must remain an explicitly labelled historical demonstration.
  await page.getByText('高级：用历史演示运行流程',{exact:true}).click();
  await page.locator('input[name="historical_start"]').fill('2023-01-09');
  await page.getByRole('button',{name:'开启历史演示',exact:true}).click();
  await expect(page.locator('#desk-status')).toBeVisible();
  await page.locator('#desk-tick').click();
  await page.waitForURL(/\/job\//);
  await expect(page.locator('.state.succeeded')).toBeVisible({timeout:90000});
  await page.getByRole('link',{name:'返回部署看板',exact:true}).click();
  await expect(page.locator('#desk-equity-chart svg')).toBeVisible();
  await shot('08-dashboard-mobile');
  await page.setViewportSize({width:1440,height:1000});
  await shot('09-dashboard-desktop');
  await page.goto(strategyURL);
  await page.getByRole('button',{name:'修改经验规则（新版本）',exact:true}).click();
  await expect(page.locator('[name="r0_window"]')).toHaveValue('25');
  await page.locator('[name="r0_window"]').fill('30');
  await page.getByRole('button',{name:'保存并查看模型检验'}).click();
  await expect(page.getByText(/与上一版配对排序差/)).toBeVisible();
  await expect(page.getByText('查看规则变更',{exact:true})).toBeVisible();
  await expect(page.getByText(/3 次规则检验/)).toBeVisible();
  const old=await page.request.get(strategyURL+'/rule-card.json');
  expect((await old.json()).rules[0].window).toBe(25);
  await shot('10-revision-result-desktop');
  expect(errors).toEqual([]);
  await writeFile(path.join(out,'result.json'),JSON.stringify({status:'passed',project,strategyURL,errors,scope:'synthetic_software_only'},null,2));
  console.log(JSON.stringify({status:'passed',project,strategyURL,screenshots:10,scope:'synthetic_software_only'}));
}catch(error){
  await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});
  console.error(page.url(),await page.locator('main').innerText().catch(()=>''));
  throw error;
}finally{await browser.close();}
