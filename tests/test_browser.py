"""The exported HTML opened in a real browser (offline, `file://`): every
page renders without errors, network or the error panel.

Run with `pytest -m browser`. Skipped when Playwright or a Chromium is not
installed. In CI: `pip install playwright && playwright install chromium`."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

sync_api = pytest.importorskip("playwright.sync_api")


def _launch(p):
    """Playwright's own Chromium, else a system Chrome (BIZMAP_CHROME or on PATH)."""
    try:
        return p.chromium.launch()
    except Exception:
        exe = os.environ.get("BIZMAP_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
        if not exe:
            pytest.skip("no Chromium for Playwright")
        return p.chromium.launch(executable_path=exe, args=["--no-sandbox"])


class Walk:
    """Opens the page once and visits hashes, collecting every error."""

    def __init__(self, page) -> None:
        self.page = page
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}") if m.type in {"error", "warning"} else None)
        page.on("request", lambda r: None if r.url.startswith(("file:", "data:", "blob:", "about:")) else self.errors.append(f"network: {r.url}"))

    def go(self, hash_: str) -> str:
        self.page.evaluate("h => { location.hash = h; }", hash_)
        self.page.wait_for_timeout(150)
        risk = self.page.query_selector("#read p.risk")
        if risk is not None:
            self.errors.append(f"{hash_}: {risk.inner_text()}")
        return self.page.inner_text("#read")


def walk_export(html: Path) -> tuple[Walk, dict]:
    """Open ``html`` and visit every kind of page; returns the walk and what it saw."""
    seen: dict = {}
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        walk = Walk(page)
        page.goto(html.resolve().as_uri())
        page.wait_for_selector('body[data-ready="1"]')
        ix = page.evaluate("() => window.__bizmap.index")
        seen["#/"] = walk.go("#/")
        for i, sv in enumerate(ix["services"]):
            seen[f"#/s/{i}"] = walk.go(f"#/s/{i}")
            if sv.get("screen_chunk"):
                seen[f"#/w/{i}"] = walk.go(f"#/w/{i}")
                seen[f"#/u/{i}"] = walk.go(f"#/u/{i}")
            if sv.get("uses_chunk"):
                seen[f"#/x/{i}"] = walk.go(f"#/x/{i}")
                seen[f"x-rows/{sv['name']}"] = page.evaluate(
                    """() => Array.from(document.querySelectorAll('#read .ep.use')).map(e => ({
                        head: e.querySelector('.ep-head .m').innerText + ' ' + e.querySelector('.ep-path').innerText.trim(),
                        who: e.querySelector('.ep-who').innerText.replace(/\\s+/g, ' ').trim() }))"""
                )
        if ix["domains"]:
            seen["#/d/0"] = walk.go("#/d/0")
            route = page.evaluate("() => window.__bizmap.chunk(window.__bizmap.index.domains[0].chunk).routes[0].i")
            seen["#/r"] = walk.go(f"#/r/{route}")
            seen["route-sig"] = page.evaluate(
                "() => Array.from(document.querySelectorAll('#read .sig, #detail .sig')).map(e => e.innerText)"
            )
            fn = page.evaluate("() => window.__bizmap.chunk(window.__bizmap.index.domains[0].chunk).routes[0].h[0]")
            if fn is not None:
                seen["#/f"] = walk.go(f"#/f/{fn}")
                seen["fn-sig"] = page.evaluate("() => (document.querySelector('#detail .sig') || {}).innerText || ''")
        seen["#/t"] = walk.go("#/t")
        table = page.evaluate("() => Object.keys(window.__bizmap.index.tableNames || {})[0]")
        if table is not None:
            seen["#/t/one"] = walk.go(f"#/t/{table}")
        seen["#/map"] = walk.go("#/map")
        seen["alerted"] = page.evaluate("() => window.__alerted || false")
        browser.close()
    return walk, seen


def test_export_pages_render_in_a_browser(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_typescript")  # the scan of the fictional repro
    from bizmap.export import export_graph
    from bizmap.scan import scan_paths
    from test_web_api_links import build

    roots = build(tmp_path)
    graph = scan_paths(roots, config=tmp_path / "bizmap.yaml", base=tmp_path)
    html = tmp_path / "out.html"
    out = export_graph(graph.to_dict(), "html")
    html.write_text(out if isinstance(out, str) else out[0], encoding="utf-8")
    walk, seen = walk_export(html)
    assert not walk.errors, walk.errors
    x = next(v for k, v in seen.items() if k.startswith("#/x/"))
    assert "API routes web uses" in x
    rows = seen["x-rows/web"]
    get_widgets = next(r for r in rows if r["head"] == "GET /api/v1/widgets")
    assert "Widgets" in get_widgets["who"], get_widgets
    delete = next(r for r in rows if r["head"] == "DELETE /api/v1/teams/:teamId/members/:userId")
    assert "called from" in delete["who"] and "screen" not in delete["who"].lower(), delete


def test_signatures_show_as_text_and_never_run(tmp_path: Path) -> None:
    """A signature with markup in its types is shown as text: the panel, the
    route page and the tree tooltip escape it, nothing runs."""
    from bizmap.export import export_graph
    from test_export_html import react_graph

    g = react_graph()
    fn = next(n for n in g["nodes"] if n["id"] == "web/function:src.api.orders.getOrder")
    fn["params"] = [{"name": "id", "type": "string"}, {"name": "opts", "type": '"<img src=x onerror=window.__alerted=1>"', "optional": True}]
    fn["returns"] = "`<script>window.__alerted=1</script>`"
    html = tmp_path / "out.html"
    out = export_graph(g, "html")
    html.write_text(out if isinstance(out, str) else out[0], encoding="utf-8")
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()
        walk = Walk(page)
        page.goto(html.resolve().as_uri())
        page.wait_for_selector('body[data-ready="1"]')
        fid = page.evaluate(
            """() => { const n = window.__bizmap.chunk('names').l;
                       return n.indexOf('src.api.orders.getOrder'); }"""
        )
        walk.go(f"#/f/{fid}")
        sig = page.inner_text("#detail .sig")
        tip = page.get_attribute("#ftree .node .mono", "title")
        alerted = page.evaluate("() => window.__alerted || false")
        browser.close()
    assert not walk.errors, walk.errors
    want = 'getOrder(id: string, opts?: "<img src=x onerror=window.__alerted=1>"): `<script>window.__alerted=1</script>`'
    assert sig == want and tip == want and alerted is False


def _open(p, html: Path):
    browser = _launch(p)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    walk = Walk(page)
    page.goto(html.resolve().as_uri())
    page.wait_for_selector('body[data-ready="1"]')
    return browser, page, walk


def _write(tmp_path: Path, data: dict) -> Path:
    from bizmap.export import export_graph

    html = tmp_path / "out.html"
    out = export_graph(data, "html")
    html.write_text(out if isinstance(out, str) else out[0], encoding="utf-8")
    return html


def test_flow_page_filters_and_opens_the_table(tmp_path: Path) -> None:
    from test_flow_page import repro_graph

    html = _write(tmp_path, repro_graph())
    with sync_api.sync_playwright() as p:
        browser, page, walk = _open(p, html)
        walk.go("#/")
        toc = page.inner_text("#toc")
        counter = page.get_attribute("#flow-counter", "href")
        walk.go("#/flow")
        rows = page.eval_on_selector_all("#flow-body tr.flowrow", "els => els.map(e => e.innerText.replace(/\\s+/g, ' ').trim())")
        quick = page.inner_text("#flow-quick")
        page.click('#flow-quick [data-quick="fspg"]')
        fspg = page.eval_on_selector_all("#flow-body tr.flowrow", "els => els.length")
        page.click('#flow-quick [data-quick=""]')
        shop = page.evaluate(
            "() => { const s = window.__bizmap.index.services.findIndex(x => x.name === 'shop-web'); return String(s); }"
        )
        page.select_option("#flow-repo", shop)
        by_shop = page.eval_on_selector_all("#flow-body tr.flowrow", "els => els.length")
        page.select_option("#flow-repo", "")
        page.fill("#flow-q", "nope-nothing")
        page.wait_for_timeout(300)
        by_text = page.eval_on_selector_all("#flow-body tr.flowrow", "els => els.length")
        page.fill("#flow-q", "mirror")
        page.wait_for_timeout(300)
        page.click("#flow-body tr.flowrow td:nth-child(4)")
        page.wait_for_timeout(200)
        landed = page.evaluate("() => location.hash")
        h1 = page.inner_text("#read h1")
        # search: kinds and storage in the label, flow badge first
        page.fill("#q", "orders")
        page.wait_for_timeout(300)
        results = page.eval_on_selector_all("#results .r", "els => els.map(e => e.innerText.replace(/\\s+/g, ' ').trim())")
        browser.close()
    assert not walk.errors, walk.errors
    assert "Data flow" in toc or "Fluxo de dados" in toc
    assert counter == "#/flow"
    assert rows and len(rows) == 1, rows
    assert "collection · Firestore" in rows[0] and "table · Postgres" in rows[0] and "orders-api" in rows[0], rows
    assert "heuristic" in rows[0] and "table level" in rows[0], rows
    assert "Firestore → Postgres" in quick
    assert fspg == 1 and by_shop == 0 and by_text == 0
    assert landed.startswith("#/t/") and h1.startswith("orders"), (landed, h1)
    assert len(results) == len(set(results)), results  # no two identical rows
    assert any(r.startswith("table · Postgres") and "flow 1" in r for r in results), results
    assert any(r.startswith("collection · Firestore") and "flow 1" in r and "orders-api" in r for r in results), results
    shop_row = next(i for i, r in enumerate(results) if "shop-web" in r)
    flow_rows = [i for i, r in enumerate(results) if "flow 1" in r]
    assert all(i < shop_row for i in flow_rows), results


def test_flow_page_empty_explains_and_big_graph_paginates(tmp_path: Path) -> None:
    from test_flow_page import repro_graph

    empty = repro_graph()
    empty["edges"] = [e for e in empty["edges"] if e["type"] != "feeds"]
    html = _write(tmp_path, empty)
    nodes, edges = [], []
    for k in range(600):
        nodes.append({"id": f"svc{k % 16}/table:t{k}", "type": "table", "name": f"t{k}", "engine": "postgres"})
        nodes.append({"id": f"svc{k % 16}/collection:c{k}", "type": "collection", "name": f"c{k}", "db": "firestore:default"})
    for k in range(49_000):
        nodes.append({"id": f"svc{k % 16}/function:pkg{k % 300}.F{k}", "type": "function", "name": f"F{k}",
                      "evidence": {"file": f"pkg{k % 300}/f{k}.go", "line": 1}})
    for k in range(500):
        edges.append({"id": f"feeds:{k}", "type": "feeds", "from": f"svc{k % 16}/collection:c{k}",
                      "to": f"svc{k % 16}/table:t{k}", "confidence": "heuristic",
                      "via": [{"path": [f"svc{k % 16}/function:pkg{k % 300}.F{k}"]}]})
    big = tmp_path / "big"
    big.mkdir()
    big_html = _write(big, {"nodes": nodes, "edges": edges})
    with sync_api.sync_playwright() as p:
        browser, page, walk = _open(p, html)
        empty_text = walk.go("#/flow")
        browser.close()
        browser, page, walk2 = _open(p, big_html)
        walk2.go("#/flow")
        first = page.eval_on_selector_all("#flow-body tr", "els => els.length")
        page.click("#flow-more-btn")
        second = page.eval_on_selector_all("#flow-body tr", "els => els.length")
        page.fill("#q", "F1")
        page.wait_for_timeout(400)
        hits = page.eval_on_selector_all("#results .r", "els => els.length")
        browser.close()
    assert not walk.errors and not walk2.errors, (walk.errors, walk2.errors)
    assert "No data flow found" in empty_text and "feeds" in empty_text
    assert first == 200 and second == 400
    assert 0 < hits <= 60
