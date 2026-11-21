// Frames for the `bizmap export` GIF (docs/img/export-html.gif). Needs `npm i playwright`.
//   cd examples/export && bizmap export --graph shop.graph.json --out /tmp/shop.html   (finds api/openapi.json)
//   node scripts/export_gif.js /tmp/shop.html /tmp/frames
//   ffmpeg -framerate 10 -i /tmp/frames/f%04d.png -vf "fps=8,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse" docs/img/export-html.gif
const { chromium } = require("playwright");
const fs = require("fs");

(async () => {
  const [file, outDir] = [process.argv[2], process.argv[3]];
  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(outDir);
  const browser = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {});
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 });
  let k = 0;
  const snap = async (ms) => {
    const end = Date.now() + ms;
    do {
      await page.screenshot({ path: `${outDir}/f${String(k++).padStart(4, "0")}.png` });
      await page.waitForTimeout(90);
    } while (Date.now() < end);
  };
  const click = async (selector) => {
    const el = page.locator(selector).first();
    const box = await el.boundingBox();
    await page.mouse.move(box.x + Math.min(40, box.width / 2), box.y + box.height / 2, { steps: 6 });
    await el.click();
  };
  await page.goto("file://" + file);
  await page.waitForSelector('body[data-ready="1"]');
  await snap(1800);
  await click('.card:has-text("api")');
  await snap(1500);
  await click('tr.row:has-text("orders")');
  await snap(1200);
  await click('.ep-head:has-text("Place an order")');
  await snap(2600);
  await click(".code-btn");
  await snap(1800);
  await click('nav#toc a.sub:has-text("projects")');
  await snap(800);
  await click('.ep-head:has-text("Archive a project")');
  await snap(2200);
  await click('nav#toc a:has-text("web")');
  await snap(1200);
  await click('nav#toc a.sub:has-text("Screens")');
  await snap(1800);
  await page.click("#q");
  for (const ch of "orders") { await page.keyboard.type(ch); await snap(110); }
  await snap(900);
  await page.keyboard.press("Enter");
  await snap(2200);
  await browser.close();
  console.log(k, "frames");
})();
