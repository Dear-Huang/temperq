// Exercise the actual upload/mapping/save routes, without mocked responses.
import {createRequire} from 'node:module';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const require=createRequire(new URL('../website/package.json',import.meta.url));
const {chromium,expect}=require('@playwright/test');
const base=process.env.QWB_EXPERIENCE_QA_URL||'http://127.0.0.1:8792';
const out=path.resolve('artifacts/csv-mapping-qa');await mkdir(out,{recursive:true});
const browser=await chromium.launch({channel:'chrome',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
page.setDefaultTimeout(30000);
const errors=[];page.on('pageerror',e=>errors.push(e.message));
async function upload(name,raw,append=false){
  await page.goto(base+'/data'+(append?'?append='+name:''));
  if(!append)await page.locator('input[name="name"]').fill(name);
  await page.locator('input[name="file"]').setInputFiles({name:'sample.csv',mimeType:'text/csv',buffer:Buffer.from(raw)});
  await page.getByRole('button',{name:'上传并对应列'}).click();
}
async function capture(name){
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});
}
try{
  await page.goto(base+'/start');
  await page.getByRole('button',{name:'选择模型',exact:true}).click();
  const project=new URL(page.url()).searchParams.get('project');
  const trained=await page.locator('select[name="source"] option').evaluateAll(options=>options.find(o=>!o.value.startsWith('starter_')&&!o.value.startsWith('external_')).value);
  const chooser=page.url();
  await page.goto(base+'/train');
  await page.locator('.training-jobs > summary').click();
  await page.locator('.training-jobs a').filter({hasText:trained}).first().click();
  await expect(page.getByRole('link',{name:'下一步：设置规则 →',exact:true})).toHaveAttribute('href',/\/projects\/[0-9a-f]{32}\/rules$/);
  await page.goto(chooser);
  await page.locator('select[name="source"]').selectOption(trained);
  await page.locator('select[name="dataset"]').selectOption('qa_synthetic');
  await page.getByRole('button',{name:'下一步',exact:true}).click();
  await expect(page.getByRole('heading',{name:'模型检验',exact:true})).toBeVisible();
  await expect(page.locator('main')).not.toContainText('年化净超额');
  await page.getByRole('link',{name:'下一步：设置规则 →',exact:true}).click();
  await page.locator('.experience-flow li:first-child a').click();
  await expect(page.getByRole('heading',{name:'模型检验',exact:true})).toBeVisible();
  await page.getByRole('link',{name:'下一步：设置规则 →',exact:true}).click();
  await page.goto(base+'/start');
  await page.locator(`.experience-project[href="/projects/${project}"]`).click();
  await expect(page).toHaveURL(base+'/projects/'+project+'/rules');
  await expect(page.getByRole('heading',{name:'账户规则',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'保存并继续',exact:true})).toBeEnabled();
  await capture('00-resume-rules');
  await upload('missing_prices','code,date,成交量\n000001,2020-01-02,100\n');
  await page.locator('select[name="amount_unit"]').selectOption('thousand');
  await page.getByRole('button',{name:'检查数据',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('还缺：开盘价、最高价、最低价、收盘价、成交额');
  await expect(page.locator('select[name="amount_unit"]')).toHaveValue('thousand');
  await capture('01-missing-prices-desktop');
  await page.setViewportSize({width:390,height:844});await capture('02-missing-prices-mobile');
  await page.setViewportSize({width:1440,height:1000});
  const rows=['证券,交易日,开,最高价,最低价,收,千元额,复权,是否st'];
  const calendar=['date,is_open'];let count=0;
  for(let d=new Date('2020-01-01T00:00:00Z');count<800;d.setUTCDate(d.getUTCDate()+1)){
    const date=d.toISOString().slice(0,10),open=![0,6].includes(d.getUTCDay());
    calendar.push(`${date},${open?1:0}`);
    if(open){count++;for(const code of ['000001','000002','000003'])rows.push(`${code},${date},10,11,9,10,7000,1,0`);}
  }
  const name='mapped_'+Date.now();
  await upload(name,rows.join('\n'));
  for(const [i,value] of ['code','date','open','high','low','close','amount','adj_factor','is_st'].entries())
    await page.locator(`select[name="column_${i}"]`).selectOption(value);
  await page.locator('select[name="amount_unit"]').selectOption('thousand');
  await page.getByText('交易日历（规则检验需要）',{exact:true}).click();
  await page.locator('input[name="calendar"]').setInputFiles({name:'calendar.csv',mimeType:'text/csv',buffer:Buffer.from(calendar.join('\n'))});
  await page.getByRole('button',{name:'检查数据',exact:true}).click();
  await expect(page.getByRole('heading',{name:'检查完成'})).toBeVisible();
  await page.locator('select[name="amount_unit"]').selectOption('yuan');
  await expect(page.locator('#csv-confirmation')).toBeHidden();
  await page.locator('select[name="amount_unit"]').selectOption('thousand');
  await page.getByRole('button',{name:'检查数据',exact:true}).click();
  await expect(page.locator('#csv-confirmation')).toBeVisible();
  await capture('03-custom-mapping-checked');
  await page.setViewportSize({width:390,height:844});await capture('04-custom-mapping-mobile');
  await page.getByRole('button',{name:'保存并继续'}).click();
  await expect(page.locator(`input[name="dataset"][value="${name}"]`)).toBeChecked();
  await upload(name,rows.join('\n'),true);
  await expect(page.locator('select[name="amount_unit"]')).toHaveValue('thousand');
  await expect(page.locator('select[name="column_6"]')).toHaveValue('amount');
  expect(errors).toEqual([]);
  const result={status:'PASS',mockedResponses:0,checks:['training_completion_direct_rules','resume_rules_draft','back_to_selected_model','signal_and_account_evidence_separated','missing_fields_and_exits','mapping_and_unit_retained','custom_headers','calendar_validation','changed_settings_require_recheck','explicit_save','continue_training','reuse_source_mapping','responsive_390_1440'],pageErrors:errors};
  await writeFile(path.join(out,'acceptance.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
}catch(error){await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});throw error;}
finally{await browser.close();}
