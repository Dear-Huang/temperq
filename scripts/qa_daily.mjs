import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(new URL("../website/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const url = process.env.QWB_DAILY_QA_URL || "http://127.0.0.1:8768";
const run = process.env.QWB_DAILY_QA_RUN || "daily_demo_ridge";
const account = process.env.QWB_DAILY_QA_ACCOUNT || "synthetic_replay";
const executionDay = process.env.QWB_DAILY_QA_EXECUTION_DAY || "2023-01-23";
const out = path.resolve(process.env.QWB_DAILY_QA_OUT || "artifacts/daily-qa");
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("response", response => {
  if (response.url().startsWith(url) && response.status() >= 400)
    errors.push(`${response.status()} ${response.url()}`);
});
const screenshots = [];
async function capture(name) {
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1))
    throw Error(`Horizontal overflow: ${name}`);
  await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
  screenshots.push(name);
}
try {
  await page.goto(`${url}/models/${run}/account`);
  await page.locator('input[name="cash"]').fill("100000");
  await page.locator('input[name="execution_day"]').fill(executionDay);
  await page.locator('input[name="file"]').setInputFiles({
    name: "holdings.csv", mimeType: "text/csv",
    buffer: Buffer.from("code,quantity,sellable_quantity\n000001.SZ,135,135\n")
  });
  await page.locator('input[name="supported_only"]').check();
  await capture("desktop-account");
  await page.getByRole("button", { name: "生成账户建议", exact: true }).click();
  await page.waitForURL(/\/advice\//);
  const adviceURL = page.url();
  await capture("desktop-advice");
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载建议 CSV", exact: true }).click();
  await (await download).saveAs(path.join(out, "account-advice.csv"));
  const protocol = await page.request.get(adviceURL + "/protocol.json");
  if (!protocol.ok() || !(await protocol.json()).protocol_hash) throw Error("Missing frozen configuration");
  for (const width of [1440, 390, 360]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [name, route] of [["account", `/models/${run}/account`],
      ["advice", adviceURL.slice(url.length)], ["ledger", `/paper/${account}`],
      ["data", "/paper/data"]]) {
      await page.goto(url + route);
      await capture(`${width}-${name}`);
    }
  }
  if (errors.length) throw Error(errors.join("\n"));
  await writeFile(path.join(out, "result.json"), JSON.stringify({ status: "PASS", screenshots, errors }, null, 2));
  console.log(JSON.stringify({ status: "PASS", screenshots: screenshots.length, errors }));
} finally {
  await browser.close();
}
