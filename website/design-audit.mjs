import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
const browser=await chromium.launch({channel:'chrome',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
await mkdir('test-results/references',{recursive:true});
for(const [name,url]of [['awwwards','https://www.awwwards.com/websites/technology/'],['reactbits','https://reactbits.dev/'],['motionsites','https://motionsites.ai'],['producthunt','https://www.producthunt.com'],['linear','https://linear.app']]) {
  try {await page.goto(url,{waitUntil:'domcontentloaded',timeout:20000});await page.waitForTimeout(1800);await page.screenshot({path:`test-results/references/${name}.png`});console.log(name,await page.title());console.log((await page.locator('body').innerText()).slice(0,4000));} catch(e){console.log(name,e.message.slice(0,150));}
}
await browser.close();
