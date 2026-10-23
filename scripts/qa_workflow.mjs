import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";

const require = createRequire(new URL("../website/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const out = "artifacts/temperq-workflow-qa";
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const base = "http://127.0.0.1:8767";
const failures = [];
page.on("pageerror", error => failures.push(error.message));
try {
  let jobUrl = null;
  if (process.argv[2]) {
    if (!/^[a-f0-9]{32}$/.test(process.argv[2])) throw Error("Invalid evaluation ID");
    await page.goto(base + "/evaluations/" + process.argv[2]);
  } else {
  await page.goto(base + "/workflow/api_test_40_lgbm");
  const form = page.locator('form[action$="/bench"]');
  if (!await form.count()) throw Error("Benchmark action missing");
  await form.getByRole("button", { name: "跑基准", exact: true }).click();
  await page.waitForURL(/\/job\//);
  jobUrl = page.url();
  console.log(`Benchmark submitted: ${jobUrl}`);
  await page.getByRole("link", { name: "查看评价结果", exact: true }).waitFor({ timeout: 600000 });
  await page.getByRole("link", { name: "查看评价结果", exact: true }).click();
  await page.waitForURL(/\/evaluations\//);
  }
  const evaluationUrl = page.url();
  if (!await page.getByRole("link", { name: "下一步：纸面交易", exact: true }).count())
    throw Error("Paper next step missing");
  await page.screenshot({ path: `${out}/evaluation.png`, fullPage: true });
  await page.getByRole("link", { name: "下一步：纸面交易", exact: true }).click();
  await page.getByRole("heading", { name: "创建纸面账户", exact: true }).waitFor();
  if (!await page.locator('input[name="automatic"]:checked').count()) throw Error("Automation default missing");
  await page.screenshot({ path: `${out}/paper-setup.png`, fullPage: true });
  // Existing accounts test navigation without creating another forward-performance series.
  await page.goto(base + "/paper/lgbm_forward_20261023");
  await page.getByRole("link", { name: "输入真实仓位 · 模拟建议", exact: true }).click();
  await page.waitForURL(/\/holdings\//);
  await page.locator('input[name="cash"]').fill("100000");
  await page.locator('input[name="execution_day"]').fill("2026-10-23");
  await page.locator('input[name="supported_only"]').check();
  await page.getByRole("button", { name: "模拟模型建议", exact: true }).click({ timeout: 300000 });
  await page.waitForURL(/\/advice\//, { timeout: 300000 });
  await page.getByRole("heading", { name: "模拟持仓建议", exact: true }).waitFor();
  const adviceUrl = page.url();
  await page.screenshot({ path: `${out}/holdings-advice.png`, fullPage: true });
  const csv = await page.request.get(adviceUrl + "/orders.csv");
  if (!csv.ok() || !(await csv.text()).includes("protocol_hash")) throw Error("Advice CSV failed");
  if (failures.length) throw Error(failures.join("\n"));
  const result = { status: "PASS", jobUrl, evaluationUrl, adviceUrl, failures };
  await writeFile(`${out}/result.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
