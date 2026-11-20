"""Render a BizMap impact comment (Markdown) the way GitHub shows it, and screenshot it."""
import json, sys, asyncio
from playwright.async_api import async_playwright

md = open(sys.argv[1], encoding="utf-8").read()
out = sys.argv[2]
title = sys.argv[3] if len(sys.argv) > 3 else ""
html = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5/github-markdown-light.css">
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
 body{background:#fff;margin:0;padding:24px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif}
 .pr{max-width:1012px;margin:0 auto}
 .prtitle{font-size:24px;color:#1f2328;margin:0 0 4px}
 .prtitle span{color:#59636e;font-weight:300}
 .state{display:inline-block;background:#1f883d;color:#fff;border-radius:2em;padding:4px 12px;font-size:14px;font-weight:600;margin:6px 0 18px}
 .c{border:1px solid #d1d9e0;border-radius:6px}
 .h{background:#f6f8fa;border-bottom:1px solid #d1d9e0;border-radius:6px 6px 0 0;padding:8px 16px;font-size:14px;color:#59636e;display:flex;align-items:center;gap:8px}
 .h b{color:#1f2328}.bot{border:1px solid #d1d9e0;border-radius:2em;padding:0 7px;font-size:12px}
 .av{width:20px;height:20px;border-radius:50%;background:#1f2328;display:inline-block}
 .markdown-body{padding:16px;font-size:14px}
 .markdown-body pre.mermaid{background:#fff;text-align:center}
</style></head><body><div class="pr">
<div class="prtitle">__TITLE__</div><div class="state">Open</div>
<div class="c"><div class="h"><span class="av"></span><b>github-actions</b><span class="bot">bot</span> commented</div>
<div class="markdown-body" id="b"></div></div></div>
<script>
const md = __MD__;
const el = document.getElementById('b');
el.innerHTML = marked.parse(md.replace(/<!--[^>]*-->/g,''));
el.querySelectorAll('code.language-mermaid').forEach(c => { const p=c.parentElement; const d=document.createElement('pre'); d.className='mermaid'; d.textContent=c.textContent; p.replaceWith(d); });
mermaid.initialize({startOnLoad:false, theme:'default', flowchart:{useMaxWidth:true}});
mermaid.run({querySelector:'pre.mermaid'}).then(()=>{document.body.dataset.done='1'});
</script></body></html>"""
html = html.replace("__MD__", json.dumps(md)).replace("__TITLE__", title)
open("/tmp/comment.html", "w").write(html)

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1060, "height": 800}, device_scale_factor=2)
        await pg.goto("file:///tmp/comment.html")
        await pg.wait_for_selector("body[data-done='1']", timeout=60000)
        await pg.locator(".pr").screenshot(path=out)
        await b.close()
asyncio.run(main())
