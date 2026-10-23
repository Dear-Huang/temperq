import { createRequire } from "node:module";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../website/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const workspace = process.env.QWB_QA_WORKDIR;
if (!workspace) throw Error("Set QWB_QA_WORKDIR to the isolated synthetic demo workspace");
const demo = JSON.parse(await readFile(path.join(workspace, "DEMO.json"), "utf8"));
if (demo.scope !== "synthetic_workflow_only") throw Error("QA requires a synthetic workspace");
const url = process.env.QWB_WORKBENCH_URL || "http://127.0.0.1:8766";
const out = path.join(root, "artifacts", "workbench-qa");
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
page.on("response", (response) => {
  if (response.url().startsWith(url) && response.status() >= 400)
    errors.push(`${response.status()} ${response.url()}`);
});
const ids = demo.runs.map((item) => path.basename(item));
const evaluations = demo.evaluations.map((item) => path.basename(item));
const screenshots = [];
async function snapshot(name) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
  if (overflow) throw Error(`Horizontal overflow: ${name}`);
  await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
  screenshots.push(name);
}
try {
  for (let attempt = 0; attempt < 20; attempt++) {
    try {
      if ((await page.request.get(`${url}/experiments`, { timeout: 2000 })).ok()) break;
    } catch {}
    if (attempt === 19) throw Error("Workbench server did not become ready");
    await page.waitForTimeout(500);
  }
  await page.goto(`${url}/experiments`);
  await page.getByRole("link", { name: "demo_ridge", exact: true }).waitFor();
  await snapshot("desktop-history");
  await page.getByRole("link", { name: "demo_ridge", exact: true }).click();
  await page.getByRole("heading", { name: "demo_ridge", exact: true }).waitFor();
  await snapshot("desktop-version");
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "导出研究摘要", exact: true }).click();
  await (await downloadEvent).saveAs(path.join(out, "downloaded-evidence.zip"));
  await page.locator('select[name="protocol"]').selectOption("synthetic-development.yaml");
  await page.getByRole("button", { name: "运行 Bench", exact: true }).click();
  await page.locator(".state.succeeded").waitFor({ timeout: 120000 });
  await page.getByRole("link", { name: "查看评价结果", exact: true }).click();
  await page.getByRole("heading", { name: "QW-Bench 模型报告", exact: true }).waitFor();
  await snapshot("desktop-evaluation");
  await page.goto(`${url}/experiments/compare`);
  await page.locator('select[name="old"]').selectOption(evaluations[0]);
  await page.locator('select[name="new"]').selectOption(evaluations[1]);
  await page.getByRole("button", { name: "运行配对比较", exact: true }).click();
  await page.locator(".state.succeeded").waitFor({ timeout: 120000 });
  await page.getByRole("link", { name: "查看评价结果", exact: true }).click();
  await page.getByRole("heading", { name: "QW-Bench 预测结果比较", exact: true }).waitFor();
  await snapshot("desktop-comparison");
  for (const width of [390, 360]) {
    await page.setViewportSize({ width, height: 844 });
    for (const [name, route] of [["history", "/experiments"], ["version", `/experiments/${ids[1]}`],
      ["compare", "/experiments/compare"], ["evaluation", `/evaluations/${evaluations[1]}`]]) {
      await page.goto(url + route);
      await snapshot(`mobile-${width}-${name}`);
    }
  }
  if (errors.length) throw Error(errors.join("\n"));
  await writeFile(path.join(out, "result.json"), JSON.stringify({ status: "PASS", screenshots, errors }, null, 2));
  console.log(JSON.stringify({ status: "PASS", screenshots: screenshots.length, errors }));
} finally {
  await browser.close();
}
