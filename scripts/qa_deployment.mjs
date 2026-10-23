// Operate an actual isolated paper account; no mocked financial/API responses.
import {createRequire} from 'node:module';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
const require = createRequire(new URL('../website/package.json', import.meta.url));
const {chromium, expect} = require('@playwright/test');
const base = process.env.QWB_DEPLOYMENT_QA_URL || 'http://127.0.0.1:8788';
const prior = JSON.parse(await readFile('artifacts/experience-qa/acceptance.json', 'utf8'));
const strategy = new URL(prior.strategyURL).pathname;
const out = path.resolve('artifacts/deployment-qa');
await mkdir(out, {recursive: true});
const browser = await chromium.launch({channel: 'chrome', headless: true});
const context = await browser.newContext({viewport: {width: 1600, height: 1050}});
const page = await context.newPage();
page.setDefaultTimeout(30000);page.setDefaultNavigationTimeout(60000);
const errors = [], captures = [];
page.on('pageerror', error => errors.push(error.message));
async function capture(name) {
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => scrollTo(0, 0));
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)) throw Error('Horizontal overflow: '+name);
  await page.screenshot({path: path.join(out, name+'.png'), fullPage: true});captures.push(name);
}
try {
  await page.goto(base+strategy);
  await page.getByRole('link', {name: '打开部署看板', exact: true}).click();
  await expect(page).toHaveURL(base+strategy+'/dashboard');
  const source = await (await page.request.get(base+strategy+'/dashboard/snapshot.json')).json();
  const before = await (await page.request.get(base+strategy+'/records.csv')).text();
  expect(source.historical_demo).toBe(true);
  await expect(page.locator('#desk-scope')).toContainText('历史演示');
  await expect(page.locator('#metric-equity')).toHaveText(Number(source.metrics.equity).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2}));
  await expect(page.locator('[data-window]:visible')).toHaveCount(6);
  await expect(page.locator('#desk-equity-chart svg')).toBeVisible();
  await capture('01-overview-1600');
  await page.getByRole('button', {name: '专业多窗', exact: true}).click();
  await expect(page.locator('[data-window]:visible')).toHaveCount(9);
  await expect(page.locator('#desk-cost-total')).not.toHaveText('—');
  await page.getByRole('button', {name:'全屏',exact:true}).click();
  await expect.poll(()=>page.evaluate(()=>Boolean(document.fullscreenElement))).toBe(true);
  await page.getByRole('button', {name:'退出全屏',exact:true}).click();
  await expect.poll(()=>page.evaluate(()=>Boolean(document.fullscreenElement))).toBe(false);
  await capture('02-professional-1600');
  await page.locator('[data-range="20"]').click();
  expect((await page.locator('#desk-equity-chart polyline').getAttribute('points')).split(' ')).toHaveLength(Math.min(20, source.series.length));
  await page.locator('#desk-equity-chart svg').focus();
  await page.keyboard.press('End');
  await expect(page.locator('#desk-chart-readout')).toContainText(source.metrics.last_day);
  await expect(page.locator('#desk-crosshair')).toHaveAttribute('visibility', 'visible');
  await page.locator('[data-range="all"]').click();
  await page.locator('#desk-holding-search').fill('NO_MATCH');
  await expect(page.locator('#desk-holdings-table')).toContainText('没有匹配');
  await page.locator('#desk-holding-search').fill('');
  await page.getByRole('button', {name: '自定义窗口', exact: true}).click();
  await page.locator('#desk-window-checks input[value="fills"]').uncheck();
  await expect(page.locator('[data-window="fills"]')).toBeHidden();
  await page.reload();
  await expect(page.locator('[data-window="fills"]')).toBeHidden();
  await expect(page.locator('button[data-layout="professional"]')).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', {name: '自定义窗口', exact: true}).click();
  await page.locator('#desk-window-checks input[value="fills"]').check();
  await page.getByRole('button', {name: '自定义窗口', exact: true}).click();
  // Real network failure: retain last good data instead of erasing or faking it.
  const equity = await page.locator('#metric-equity').innerText();
  await context.setOffline(true);
  await page.getByRole('button', {name: '刷新看板', exact: true}).click();
  await expect(page.locator('#desk-connection')).toContainText('上次成功读取');
  await expect(page.locator('#metric-equity')).toHaveText(equity);
  await capture('03-disconnected-retains-records');
  await context.setOffline(false);
  await page.getByRole('button', {name: '刷新看板', exact: true}).click();
  await expect(page.locator('#desk-connection')).toBeHidden();
  const after = await (await page.request.get(base+strategy+'/records.csv')).text();
  expect(after).toBe(before);
  for (const width of [1920, 768, 390]) {
    await page.setViewportSize({width, height: width === 390 ? 844 : 1080});
    if (width === 390) await expect.poll(() => page.locator('#desk-equity-chart svg text').first().evaluate(text => text.getBoundingClientRect().height)).toBeGreaterThan(8);
    await capture(`professional-${width}`);
  }
  await page.setViewportSize({width: 1440, height: 1000});
  await page.getByRole('button', {name: '开启每日检查', exact: true}).click();
  await expect(page.locator('#desk-auto-state')).toHaveText('每日检查已开启');
  await page.getByRole('button', {name: '关闭每日检查', exact: true}).click();
  await expect(page.locator('#desk-auto-state')).toHaveText('每日检查已关闭');
  await page.getByRole('button', {name: '检查行情并记账', exact: true}).click();
  await page.waitForURL(/\/job\//);
  await expect(page.locator('.state.succeeded')).toBeVisible({timeout: 90000});
  await page.getByRole('link', {name: '返回部署看板', exact: true}).click();
  await expect(page).toHaveURL(base+strategy+'/dashboard');
  expect(await (await page.request.get(base+strategy+'/records.csv')).text()).toBe(before);
  // A new, unstarted policy opens an honest empty terminal, never the old curve.
  await page.getByRole('link', {name: '修改并建立新版本 ↗', exact: true}).click();
  await page.locator('input[name="name"]').fill('部署验收 · 等待启用');
  await page.getByRole('button', {name: '冻结设置，进入检验', exact: true}).click();
  await page.waitForURL(/\/strategies\/[0-9a-f]{32}\/[0-9a-f]{32}$/);
  await page.goto(page.url()+'/dashboard');
  await expect(page.locator('#metric-equity')).toHaveText('—');
  await expect(page.locator('#desk-tick')).toBeDisabled();
  await expect(page.locator('#desk-equity-chart')).toContainText('不会用回测曲线填充');
  await capture('07-not-started-empty');
  expect(errors).toEqual([]);
  const result = {status:'PASS', strategy:base+strategy+'/dashboard', scope:'existing isolated synthetic paper ledger', mockedResponses:0,
    captures, pageErrors:errors, checks:['entry_from_successful_strategy','ledger_metrics','demo_scope_preserved','overview_and_professional',
      'fullscreen','time_range','keyboard_crosshair','holdings_search','window_preferences_survive_reload','offline_retains_last_record',
      'refresh_does_not_write_ledger','responsive_390_768_1600_1920','automation_opt_in_toggle','actual_tick_job_and_return',
      'retry_idempotence','unstarted_account_has_no_fake_curve'], limitation:'Software/UI acceptance only; not a live account or an external usability study.'};
  await writeFile(path.join(out,'acceptance.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
} catch(error) {
  await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});
  await writeFile(path.join(out,'failure.html'),await page.content());
  console.error(page.url(),await page.locator('main').innerText().catch(()=>''));throw error;
} finally {await browser.close();}
