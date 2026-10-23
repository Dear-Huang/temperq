import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(new URL("../website/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const url = process.env.QWB_DAILY_QA_URL || "http://127.0.0.1:8767";
const account = process.env.QWB_DAILY_QA_ACCOUNT || "lgbm_forward_20261023";
const out = path.resolve("artifacts/paper-operation-qa");
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", error => errors.push(error.message));
const screenshots = [];
try {
  for (const width of [1440, 390, 360]) {
    await page.setViewportSize({ width, height: 900 });
    for (const route of ["/paper", `/paper/${account}`]) {
      const response = await page.goto(url + route);
      if (!response.ok()) throw Error(`${response.status()} ${route}`);
      if (route !== "/paper") {
        await page.getByText("最近每日任务：", { exact: false }).waitFor();
        await page.getByText("已核验行情截至", { exact: false }).waitFor();
        const csv = await page.request.get(url + route + "/history.csv");
        if (!csv.ok()) throw Error("History download failed");
      }
      if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1))
        throw Error(`Horizontal overflow at ${width}: ${route}`);
      const name = `${width}-${route === "/paper" ? "accounts" : "operation"}`;
      await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
      screenshots.push(name);
    }
  }
  if (errors.length) throw Error(errors.join("\n"));
  const result = { status: "PASS", screenshots, errors };
  await writeFile(path.join(out, "result.json"), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
}
