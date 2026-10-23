import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(new URL("../website/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const url = process.env.QWB_GOVERNANCE_QA_URL || "http://127.0.0.1:8770";
const out = path.resolve("artifacts/governance-qa");
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", error => errors.push(error.message));
const captures = [];
async function capture(name) {
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1))
    throw Error(`Horizontal overflow: ${name}`);
  await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
  captures.push(name);
}
try {
  await page.goto(`${url}/governance`);
  await page.locator('select[name="source"]').selectOption("baostock_onboarding_20261023");
  await page.locator('select[name="dataset"]').selectOption("free_daily");
  await page.locator('input[name="start"]').fill("2026-10-09");
  await page.locator('input[name="end"]').fill("2026-10-22");
  await page.locator('input[name="n_hold"]').fill("4");
  await page.locator('input[name="hold_until_rank"]').fill("6");
  await page.locator('input[name="max_weight"]').fill("30");
  await page.locator('input[name="target_volatility"]').fill("10");
  await capture("editor-desktop");
  await page.getByRole("button", { name: "记录本次尝试并运行对照" }).click();
  await page.waitForURL(/\/job\//);
  await page.getByRole("link", { name: "查看治理对照与决策回放" }).waitFor({ timeout: 180000 });
  await page.getByRole("link", { name: "查看治理对照与决策回放" }).click();
  await page.waitForURL(/\/governance\/[0-9a-f]{32}$/);
  const report = page.url();
  if (await page.locator("svg polyline").count() !== 5) throw Error("Missing equity curves");
  await capture("report-desktop");
  const result = await page.request.get(`${report}/result.json`);
  const payload = await result.json();
  if (payload.status !== "SUCCEEDED" || payload.metrics.ml.ml_warmup_decisions !== 0)
    throw Error("ML did not fit on mature real-data labels");
  if (!payload.diagnostics.ml_matched_fixed || !await page.getByText(/第五组使用整个区间/).count())
    throw Error("Missing post-hoc exposure matching disclosure");
  await page.getByRole("link", { name: "逐日决策与成交" }).click();
  await capture("decisions-desktop");
  for (const [width, height] of [[390, 844], [768, 1024]]) {
    await page.setViewportSize({ width, height });
    await page.goto(`${url}/governance`);
    await capture(`editor-${width}`);
    await page.goto(report);
    await capture(`report-${width}`);
    await page.goto(`${report}/decisions`);
    await capture(`decisions-${width}`);
  }
  if (errors.length) throw Error(errors.join("\n"));
  await writeFile(path.join(out, "acceptance.json"), JSON.stringify({
    status: "PASS", report, captures, elapsed_seconds: payload.elapsed_seconds,
    metrics: payload.metrics, errors,
  }, null, 2));
  console.log(JSON.stringify({ status: "PASS", report, captures, elapsed_seconds: payload.elapsed_seconds }));
} finally {
  await browser.close();
}
