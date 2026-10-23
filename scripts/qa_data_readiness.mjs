import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
const require = createRequire(new URL("../website/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const out = "artifacts/temperq-data-guidance-qa";
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", e => errors.push(e.message));
try {
  for (const width of [1440, 390, 360]) {
    for (const colorScheme of ["light", "dark"]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.emulateMedia({ colorScheme });
      await page.goto("http://127.0.0.1:8767/");
      await page.locator('input[name="dataset"][value="_trial"]').check();
      await page.locator('#data-check').getByText('流程演示', { exact: true }).waitFor();
      const real = page.locator('input[name="dataset"]:not([value="_trial"])').first();
      if (await real.count()) {
        await real.check();
        await page.locator('#data-check').getByText('训练前数据提示', { exact: true }).waitFor();
        await page.locator('input[name="horizon"][value="20"]').check();
        await page.locator('#data-check').getByText(/每日纸面交易只支持 5 日/).waitFor();
        await page.locator('input[name="horizon"][value="5"]').check();
        await page.waitForFunction(() => !document.querySelector('#data-check').textContent.includes('每日纸面交易只支持 5 日'));
      }
      if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1))
        throw Error(`Overflow ${width} ${colorScheme}`);
      await page.screenshot({ path: `${out}/${width}-${colorScheme}.png`, fullPage: true });
    }
  }
  if (errors.length) throw Error(errors.join("\n"));
  const result = { status: "PASS", screenshots: 6, errors };
  await writeFile(`${out}/result.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
