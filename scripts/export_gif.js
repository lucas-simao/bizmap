const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const [file, outDir] = [process.argv[2], process.argv[3]];
  fs.rmSync(outDir, { recursive: true, force: true }); fs.mkdirSync(outDir);
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 });
  let k = 0;
  const snap = async (ms) => { const end = Date.now() + ms; do { await page.screenshot({ path: `${outDir}/f${String(k++).padStart(4, '0')}.png` }); await page.waitForTimeout(90); } while (Date.now() < end); };
  await page.goto('file://' + file);
  await page.waitForSelector('body[data-ready="1"]');
  await snap(1800);
  // click the api service node where it is drawn
  const pos = await page.evaluate(() => { const n = window.__bizmap.cy.getElementById('s' + window.__bizmap.data.services.findIndex(s => s.name === 'api')); const p = n.renderedPosition(); const r = document.getElementById('cy').getBoundingClientRect(); return { x: p.x + r.left, y: p.y + r.top }; });
  await page.mouse.move(pos.x, pos.y, { steps: 8 });
  await page.mouse.click(pos.x, pos.y);
  await snap(2600);
  await page.click('#q');
  for (const ch of 'orders') { await page.keyboard.type(ch); await snap(120); }
  await snap(1200);
  await page.keyboard.press('Enter');
  await snap(2400);
  await page.click('#tf');
  for (const ch of 'orders') { await page.keyboard.type(ch); await snap(100); }
  await page.keyboard.press('Enter');
  await snap(3500);
  await browser.close();
  console.log(k, 'frames');
})();
