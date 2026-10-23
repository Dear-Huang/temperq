import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";

const browser = await chromium.launch({
  channel: "chrome",
  headless: true,
  args: [
    "--enable-webgl",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
  ],
});
const page = await browser.newPage({
  viewport: { width: 1440, height: 1000 },
  deviceScaleFactor: 1,
});
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("response", (response) => {
  if (
    response.url().startsWith("http://127.0.0.1:8800") &&
    response.status() >= 400
  )
    errors.push(`${response.status()} ${response.url()}`);
});
await mkdir("test-results", { recursive: true });
await page.goto("http://127.0.0.1:8800");
await page.locator("canvas").waitFor();
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(1500);
const pixels = await page.locator("canvas").evaluate((canvas) => {
  const gl = canvas.getContext("webgl2") || canvas.getContext("webgl");
  const pixels = new Uint8Array(
    gl.drawingBufferWidth * gl.drawingBufferHeight * 4,
  );
  gl.readPixels(
    0,
    0,
    gl.drawingBufferWidth,
    gl.drawingBufferHeight,
    gl.RGBA,
    gl.UNSIGNED_BYTE,
    pixels,
  );
  let opaque = 0,
    colored = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    if (pixels[i + 3] > 100) opaque++;
    if (
      pixels[i + 3] > 100 &&
      Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) -
        Math.min(pixels[i], pixels[i + 1], pixels[i + 2]) >
        25
    )
      colored++;
  }
  return { width: canvas.width, height: canvas.height, opaque, colored };
});
if (pixels.colored < 1000)
  throw Error(`Canvas blank: ${JSON.stringify(pixels)}`);
const frame1 = await page.locator("canvas").screenshot();
await page.waitForTimeout(650);
const frame2 = await page.locator("canvas").screenshot();
if (frame1.equals(frame2)) throw Error("Scene does not animate");
await page.getByRole("button", { name: "暂停动态场景" }).click();
await page.locator("canvas").screenshot({
  path: process.env.QWB_UPDATE_POSTER === "1" ? "public/hero-poster.png" : "test-results/hero-poster.png",
});
await page.locator('.model-grid').scrollIntoViewIfNeeded();
await page.waitForTimeout(700);
await page.evaluate(() => window.scrollTo({top:0,behavior:'instant'}));
await page.screenshot({ path: "test-results/desktop.png", fullPage: true });
await page.screenshot({ path: "test-results/desktop-hero.png" });
await page.getByRole("button", { name: "查看LightGBM · 截面排序" }).click();
await page.getByRole("dialog").waitFor();
await page
  .getByRole("dialog")
  .getByRole("tab", { name: "训练配方", exact: true })
  .click();
await page.getByText("run_cn_rank_5d.yaml", { exact: true }).waitFor();
await page.getByRole("tab", { name: "评估结果" }).click();
await page.screenshot({ path: "test-results/report.png" });
await page.keyboard.press("Escape");
await page.getByRole("tab", { name: "Kronos", exact: true }).click();
if ((await page.locator(".model-card").count()) !== 1)
  throw Error("Model filter failed");
await page.getByRole("tab", { name: "全部探索" }).click();
await page.getByRole("textbox", { name: "搜索模型" }).fill("no-match-1234");
await page.getByText("没有找到匹配的模型").waitFor();
await page.getByRole("button", { name: "清空搜索" }).click();
await page.getByRole("button", { name: "开始构建" }).click();
await page.getByRole("dialog").getByText("打开本地工作台").waitFor();
await page.keyboard.press("Escape");
await page.getByRole("button", { name: "登录", exact: true }).click();
await page.getByRole("link", { name: "进入本地开发账号" }).click();
await page.getByRole("link", { name: "local-dev · 退出" }).waitFor();
await page.goto('http://127.0.0.1:8800/?publish=model');
await page
  .getByRole("dialog")
  .getByText("Hugging Face 模型仓库", { exact: true })
  .waitFor();
await page.keyboard.press("Escape");
const checks = [];
for (const size of [
  { width: 1920, height: 1080 },
  { width: 390, height: 844 },
  { width: 360, height: 740 },
]) {
  await page.setViewportSize(size);
  await page.goto("http://127.0.0.1:8800");
  await page.locator("canvas").waitFor();
  await page.waitForTimeout(600);
  const overflow = await page.evaluate(() => ({
    width: innerWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  if (overflow.scroll > overflow.width)
    throw Error(`Horizontal overflow ${JSON.stringify(overflow)}`);
  const heading = await page.locator("h1").evaluate((el) => {
    const range = document.createRange();
    range.selectNodeContents(el.firstChild);
    return {
      textWidth: range.getBoundingClientRect().width,
      containerWidth: el.clientWidth,
    };
  });
  if (heading.textWidth > heading.containerWidth)
    throw Error(`Heading clipped: ${JSON.stringify(heading)}`);
  const framing = await page.locator("canvas").evaluate((canvas) => {
    const gl = canvas.getContext("webgl2") || canvas.getContext("webgl");
    const data = new Uint8Array(canvas.width * canvas.height * 4);
    gl.readPixels(
      0,
      0,
      canvas.width,
      canvas.height,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      data,
    );
    let min = canvas.width,
      max = 0,
      count = 0;
    for (let y = 0; y < canvas.height; y++)
      for (let x = 0; x < canvas.width; x++) {
        const i = (y * canvas.width + x) * 4;
        if (
          data[i + 3] > 150 &&
          Math.max(data[i], data[i + 1], data[i + 2]) -
            Math.min(data[i], data[i + 1], data[i + 2]) >
            30
        ) {
          min = Math.min(min, x);
          max = Math.max(max, x);
          count++;
        }
      }
    const bounds = canvas.getBoundingClientRect();
    return {
      left: bounds.left + (min / canvas.width) * bounds.width,
      right: bounds.left + (max / canvas.width) * bounds.width,
      count,
    };
  });
  if (framing.count < 1000 || framing.left < 0 || framing.right > size.width)
    throw Error(`Scene framing ${JSON.stringify({ size, framing })}`);
  await page.screenshot({
    path: `test-results/${size.width}.png`,
    fullPage: true,
  });
  if (size.width < 640)
    await page.screenshot({ path: `test-results/${size.width}-hero.png` });
  checks.push({ ...size, ...overflow, framing });
}
await page.getByRole("button", { name: "打开导航" }).click();
await page.getByRole("button", { name: "模型广场", exact: true }).click();
await page.getByRole("button", { name: "查看LightGBM · 截面排序" }).click();
await page.screenshot({ path: "test-results/mobile-report.png" });
await page.keyboard.press("Escape");
const reduced = await browser.newPage({
  viewport: { width: 390, height: 844 },
  reducedMotion: "reduce",
});
await reduced.goto("http://127.0.0.1:8800");
await reduced.getByRole("button", { name: "播放动态场景" }).waitFor();
await reduced.close();
const fallback = await browser.newPage({
  viewport: { width: 390, height: 844 },
});
await fallback.addInitScript(() => {
  const original = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, ...args) {
    return type.startsWith("webgl") ? null : original.call(this, type, ...args);
  };
});
await fallback.goto("http://127.0.0.1:8800");
await fallback.locator(".hero-scene img").waitFor();
const imageLoaded = await fallback
  .locator(".hero-scene img")
  .evaluate((img) => img.complete && img.naturalWidth > 0);
if (!imageLoaded) throw Error("Fallback asset missing");
await fallback.screenshot({ path: "test-results/fallback.png" });
await fallback.close();
await page.request.get("http://127.0.0.1:8800/logout");
await writeFile(
  "test-results/qa.json",
  JSON.stringify({ pixels, checks, errors }, null, 2),
);
await browser.close();
if (errors.length) throw Error(errors.join("\n"));
console.log(JSON.stringify({ pixels, checks, errors }, null, 2));
