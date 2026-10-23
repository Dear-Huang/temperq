import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const require = createRequire(new URL("../website/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const out = path.resolve("artifacts/temperq-ui-qa");
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true,
  args: ["--enable-webgl", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const results = [];
const errors = [];
const page = await browser.newPage();
page.on("pageerror", e => errors.push(e.message));
const local = "http://127.0.0.1:8767";
const site = "http://127.0.0.1:8801";

async function capture(base, route, name, width, mode) {
  await page.setViewportSize({ width, height: width > 600 ? 1000 : 844 });
  await page.emulateMedia({ colorScheme: mode });
  const response = await page.goto(base + route);
  if (!response.ok()) throw Error(`${response.status()} ${route}`);
  await page.evaluate(() => document.fonts.ready);
  if (route === "/models/api_test_40_lgbm") {
    await page.getByRole("heading", { name: "模型参考清单" }).waitFor({ timeout: 120000 });
  }
  if (!(await page.title()).includes("TemperQ")) throw Error(`Missing brand title ${route}`);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
  if (overflow) throw Error(`Overflow ${width} ${mode} ${route}`);
  const brand = await page.locator(".brand").first().innerText();
  if (brand.trim() !== "TemperQ") throw Error(`Old brand ${brand}`);
  const file = `${width}-${mode}-${name}.png`;
  if (base === site && route === "/") {
    await page.locator(".model-grid").scrollIntoViewIfNeeded();
    await page.waitForTimeout(800);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await page.waitForTimeout(300);
  }
  await page.screenshot({ path: path.join(out, file), fullPage: true });
  results.push({ route, width, mode, file });
}

try {
  const routes = [["/", "training"], ["/models", "models"],
    ["/workflow/api_test_40_lgbm", "workflow"],
    ["/holdings", "holdings"], ["/holdings/lgbm_forward_20261023", "holdings-form"],
    ["/models/api_test_40_lgbm", "model"], ["/paper", "accounts"],
    ["/paper/lgbm_forward_20261023", "forward"],
    ["/paper/api_test_40_lgbm_replay", "replay"],
    ["/experiments", "experiments"], ["/audit", "audit"]];
  for (const width of [1440, 390, 360]) {
    for (const mode of ["light", "dark"]) {
      for (const [route, name] of routes) await capture(local, route, name, width, mode);
    }
  }
  const csv = await page.request.get(local + "/template.csv");
  if (!csv.ok() || !csv.headers()["content-disposition"].includes("temperq"))
    throw Error("Template download branding failed");
  const history = await page.request.get(local + "/paper/api_test_40_lgbm_replay/history.csv");
  if (!history.ok() || !(await history.text()).includes("historical_replay"))
    throw Error("History download failed");
  await page.goto(local + "/paper/api_test_40_lgbm_replay");
  if (await page.locator(".equity-chart polyline").count() !== 1) throw Error("Replay chart missing");
  await page.goto(local + "/");
  if (!await page.evaluate(() => document.fonts.check('16px "DM Sans"'))) throw Error("Font missing");
  await page.locator("#step3 details summary").click();
  await page.locator('input[name="model"][value="ridge"]').check();
  await page.locator(".pm-ridge").waitFor({ state: "visible" });
  await page.locator('input[name="model"][value="lightgbm"]').check();
  await page.locator(".pm-lightgbm").waitFor({ state: "visible" });

  for (const width of [1440, 390, 360]) {
    await capture(site, "/", "website", width, "light");
    await page.locator("canvas").waitFor();
    await page.waitForTimeout(1200);
    const pixels = await page.locator("canvas").evaluate(canvas => {
      const gl = canvas.getContext("webgl2") || canvas.getContext("webgl");
      const data = new Uint8Array(canvas.width * canvas.height * 4);
      gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, data);
      let colored = 0;
      for (let i = 0; i < data.length; i += 4)
        if (data[i + 3] > 100 && Math.max(...data.subarray(i, i + 3)) - Math.min(...data.subarray(i, i + 3)) > 25) colored++;
      return { colored, width: canvas.width, height: canvas.height };
    });
    if (pixels.colored < 1000) throw Error(`Blank scene ${JSON.stringify(pixels)}`);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await page.waitForTimeout(500);
    const frame1 = await page.screenshot();
    await page.waitForTimeout(1200);
    const frame2 = await page.screenshot();
    if (frame1.equals(frame2)) throw Error(`Scene does not move at ${width}`);
    await page.screenshot({ path: path.join(out, `${width}-website-hero.png`) });
    results.push({ websiteCanvas: pixels });
    await capture(site, "/community", "community", width, "light");
  }
  if (errors.length) throw Error(errors.join("\n"));
  await writeFile(path.join(out, "result.json"), JSON.stringify({ status: "PASS", results, errors }, null, 2));
  console.log(JSON.stringify({ status: "PASS", screenshots: results.filter(r => r.file).length, errors }));
} finally {
  await browser.close();
}
