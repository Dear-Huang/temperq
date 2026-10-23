import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(new URL("../website/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const url = process.env.QWB_STRATEGY_QA_URL || "http://127.0.0.1:8770";
const out = path.resolve("artifacts/strategy-qa");
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [], captures = [], jobs = [];
page.on("pageerror", e => errors.push(e.message));
async function capture(name) {
  await page.evaluate(()=>scrollTo(0,0));
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1))
    throw Error(`Horizontal overflow: ${name}`);
  await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
  captures.push(name);
}
async function finishJob() {
  await page.waitForURL(/\/job\//);
  jobs.push(page.url());
  const deadline=Date.now()+180000;
  for (let i = 0; i < 100 && Date.now()<deadline; i++) {
    if (await page.locator(".state.succeeded").count()) break;
    if (await page.locator(".state.failed,.state.cancelled").count())
      throw Error(await page.locator("main").innerText());
    await page.waitForTimeout(1500);
    await page.reload({waitUntil:"domcontentloaded"});
  }
  if (!await page.locator(".state.succeeded").count()) throw Error("Job did not complete");
  await page.getByRole("link", { name: "返回策略与检验结果" }).click();
}
try {
  await page.goto(`${url}/strategies/new?source=baostock_onboarding_20261023`);
  await page.locator('select[name="dataset"]').selectOption("free_daily");
  await capture("signal-desktop");
  await page.getByRole("button", { name: "下一步：配置政策" }).click();
  await page.locator('input[name="name"]').fill("浏览器验收 · 分步调整");
  await page.locator('select[name="preset"]').selectOption("patient");
  await capture("policy-simple-desktop");
  await page.locator("#advanced summary").click();
  if (await page.locator('[data-field="target_volatility"]').isVisible())
    throw Error("Inactive fixed-mode volatility target is visible");
  await page.locator('select[name="mode"]').selectOption("ewma");
  if (!await page.locator('[data-field="target_volatility"]').isVisible())
    throw Error("Active EWMA target missing");
  await page.getByRole("button", { name: "恢复该预设" }).click();
  if (await page.locator('select[name="mode"]').inputValue() !== "fixed") throw Error("Preset reset failed");
  await page.locator('input[name="n_hold"]').fill("4");
  await page.locator('input[name="hold_until_rank"]').fill("6");
  await page.locator('input[name="max_weight"]').fill("0.3");
  await capture("policy-advanced-desktop");
  await page.getByRole("button", { name: "冻结设置，进入检验" }).click();
  await page.waitForURL(/\/strategies\/[0-9a-f]{32}\/[0-9a-f]{32}$/);
  const base = page.url();
  const config = await (await page.request.get(`${base}/config.json`)).json();
  if (config.resolved.policy.parameters.adjustment_speed !== .5 || config.resolved.policy.parameters.n_hold !== 4)
    throw Error("Frozen config differs from editor");
  if (!config.data_feed.replaceAll("\\", "/").endsWith("/data/free_daily")) throw Error("Bound static snapshot instead of feed");
  await page.locator('input[name="start"]').fill("2026-10-09");
  await page.locator('input[name="end"]').fill("2026-10-22");
  await page.getByRole("button", { name: "记录尝试并检验" }).click();
  await finishJob();
  if (await page.locator("svg polyline").count() !== 2) throw Error("Missing policy comparison");
  await capture("evidence-desktop");
  await page.getByRole("link", { name: "逐日决策与成交" }).click();
  await page.locator("main details summary").first().click();
  await capture("decisions-desktop");
  await page.goto(base);
  await page.getByText("高级：用历史演示运行流程", { exact: true }).click();
  await page.locator('input[name="historical_start"]').fill("2026-10-09");
  await page.getByRole("button", { name: "开启历史演示", exact: true }).click();
  await page.getByRole("button", { name: "检查新行情并记账" }).click();
  await finishJob();
  const before = await (await page.request.get(`${base}/records.csv`)).text();
  if (!before.includes("historical_replay") || before.includes("forward_paper")) throw Error("Historical records mislabeled");
  await page.getByRole("button", { name: "检查新行情并记账" }).click();
  await finishJob();
  const after = await (await page.request.get(`${base}/records.csv`)).text();
  if (after !== before) throw Error("Retry changed ledger");
  await capture("running-desktop");
  await page.getByRole("link", { name: "修改并建立新版本" }).click();
  await page.locator("#advanced summary").click();
  await page.locator('input[name="adjustment_speed"]').fill("0.8");
  await page.getByRole("button", { name: "冻结设置，进入检验" }).click();
  const next = page.url();
  if (next === base) throw Error("Version overwritten");
  await page.locator('input[name="start"]').fill("2026-10-09");
  await page.locator('input[name="end"]').fill("2026-10-22");
  await page.getByRole("button", { name: "记录尝试并检验" }).click();
  await finishJob();
  await page.getByRole("button", { name: "开启纸面观察", exact: true }).click();
  await page.getByRole("button", { name: "检查新行情并记账" }).click();
  await finishJob();
  const live = await (await page.request.get(`${next}/records.csv`)).text();
  if (live.includes("historical_replay")) throw Error("Forward account contains historical replay");
  if (!await page.getByText(/等待新行情|等待启用后的完整收盘数据|已记账至/).count()) throw Error("Missing daily operation status");
  await capture("forward-waiting-desktop");
  const old = await (await page.request.get(`${base}/config.json`)).json();
  if (JSON.stringify(old) !== JSON.stringify(config)) throw Error("Old version changed");
  for (const [width,height] of [[390,844],[768,1024]]) {
    await page.setViewportSize({ width,height });
    for (const [name,target] of [["index","/strategies"],["new","/strategies/new"],["evidence",base],["decisions",`${base}/decisions`]]) {
      await page.goto(target.startsWith("http") ? target : url+target);
      await capture(`${name}-${width}`);
    }
    await page.goto(`${base}/edit`);
    await page.locator("#advanced summary").click();
    await capture(`editor-${width}`);
  }
  if (errors.length) throw Error(errors.join("\n"));
  const acceptance = {status:"PASS", historical:base, forward:next, jobs, captures, errors,
    spec_hash:config.spec_hash, historical_sessions:before.trim().split("\n").length-1,
    limitation:"Real 40-stock short-window workflow test; no profitability evidence. Forward account waits for post-activation data."};
  await writeFile(path.join(out,"acceptance.json"),JSON.stringify(acceptance,null,2));
  console.log(JSON.stringify(acceptance));
} catch(error) {
  await writeFile(path.join(out,"failure.html"),await page.content());
  console.error(page.url(),await page.locator("main").innerText());
  throw error;
} finally { await browser.close(); }
