"""The HTML start screen in a real browser (offline, `file://`): the question
first, cards, matrix, map, "Show details", and the page's answers compared
with the CLI's. Fictional repro: tests/fixtures/home_repro.py.

Run with `pytest -m browser` (Playwright + Chromium, or a system Chrome)."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

sync_api = pytest.importorskip("playwright.sync_api")

from bizmap.export import export_graph  # noqa: E402
from bizmap.query import Index, QueryError, trace, unresolved_footer, who  # noqa: E402
from fixtures.home_repro import build  # noqa: E402
from test_browser import Walk, _launch  # noqa: E402


def _export(tmp_path: Path, data: dict, lang: str = "pt-BR", name: str = "out.html") -> Path:
    html = tmp_path / name
    html.write_text(export_graph(data, "html", lang=lang), encoding="utf-8")
    return html


@pytest.fixture(scope="module")
def repro_html(tmp_path_factory) -> Path:
    return _export(tmp_path_factory.mktemp("home"), build())


class Page:
    def __init__(self, p, html: Path, hash_: str = "") -> None:
        self.browser = _launch(p)
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.walk = Walk(self.page)
        self.page.goto(html.resolve().as_uri() + hash_)
        self.page.wait_for_selector('body[data-ready="1"]')

    def go(self, hash_: str) -> str:
        return self.walk.go(hash_)

    def texts(self, sel: str) -> list[str]:
        return self.page.eval_on_selector_all(sel, "els => els.map(e => e.innerText.replace(/\\s+/g, ' ').trim())")

    def close(self) -> list[str]:
        self.browser.close()
        return self.walk.errors


def test_start_screen_opens_on_the_question(repro_html: Path) -> None:
    with sync_api.sync_playwright() as p:
        pg = Page(p, repro_html)
        strip = pg.texts("#strip .stat")
        focused = pg.page.evaluate("() => document.activeElement && document.activeElement.id")
        cards = pg.texts("#finds .fcard")
        matrix_rows = pg.texts("#mx-table tbody tr")
        heads = pg.texts("#mx-table thead th.col")
        errors = pg.close()
    assert not errors, errors
    assert strip[:6] == [
        "3 repos", "3 serviços", "2 tabelas / coleções Firestore 1 · Postgres 1", "4 pontos de entrada cron 1 · http 3",
        "1 fluxos entre bancos", "4 não resolvidas",
    ], strip
    assert focused == "hq"
    assert any("Escritor inesperado" in c and "billing-worker escreve orders.status (dono: orders-api)" in c for c in cards), cards
    assert any("orders é escrita por 2 serviços" in c for c in cards), cards
    assert any(c.startswith("Nomes montados em tempo de execução 3") and "shop-web 1" in c and "billing-worker 1" in c for c in cards), cards
    assert any(c.startswith("Funções órfãs 2") for c in cards), cards
    assert sorted(heads) == ["orders coleção · Firestore", "orders tabela · Postgres"], heads
    rows = {r.split(" ", 1)[0]: r for r in matrix_rows}
    assert set(rows) == {"orders-api", "shop-web", "billing-worker"}
    assert "LE" in rows["orders-api"] and "L ⚡" in rows["shop-web"] and "E ⚡" in rows["billing-worker"], rows


def test_who_writes_orders_status_in_seconds(repro_html: Path) -> None:
    """The usability check: focus is on the search, the suggestion names the
    column with its kind, Enter lists both writers with entrypoint and
    confidence above the fold."""
    with sync_api.sync_playwright() as p:
        pg = Page(p, repro_html)
        pg.page.keyboard.type("orders.status")
        pg.page.wait_for_selector("#hq-list .sg")
        first = pg.texts("#hq-list .sg")[0]
        pg.page.keyboard.press("Enter")
        pg.page.wait_for_selector("#ans .arow")
        landed = pg.page.evaluate("() => location.hash")
        rows = pg.texts("#ans .arow .chain.short")
        metas = pg.texts("#ans .arow .ameta")
        bottom = pg.page.evaluate("() => Math.max(...Array.from(document.querySelectorAll('#ans .arow')).map(e => e.getBoundingClientRect().bottom))")
        hidden_note = pg.texts("#ans .hidden-note")
        foot = pg.page.inner_text("#afoot")
        pg.page.click("#ans .arow button.via")
        full = pg.texts("#ans .arow.full .chain.long")
        pg.page.click('.atabs a[data-tab="t"]')
        pg.page.wait_for_selector("#ans .tsec")
        trace_text = pg.page.inner_text("#ans")
        errors = pg.close()
    assert not errors, errors
    assert first.startswith("orders.status") and "coluna · Postgres" in first, first
    assert landed.startswith("#/q/orders.status/w"), landed
    assert len(rows) == 2, rows
    assert rows[0].startswith("POST /pubsub/order-created → main.on_order_created → repo.mark_billed escreve orders.status inferida"), rows
    assert rows[1].startswith("PATCH /orders/:id/status → OrdersHandler.UpdateStatus → via 2 funções → Repo.storeStatus escreve orders.status exata"), rows
    assert "escritor inesperado" in metas[0] and "dono: orders-api" in metas[0] and "billing/repo.py:24" in metas[0], metas
    assert "internal/orders/repo.go:61" in metas[1] and "escritor inesperado" not in metas[1], metas
    assert bottom < 900  # no scrolling
    assert hidden_note and "1" in hidden_note[0]  # TestSetStatus, hidden by default
    assert foot.startswith("Pode mudar esta resposta:") and "repo.touch" in foot and "Repo.BulkFix" in foot, foot
    assert full == ["PATCH /orders/:id/status → OrdersHandler.UpdateStatus → Repo.SetStatus → Repo.applyStatus → Repo.storeStatus escreve orders.status exata"], full
    assert "disparada por order-created ← publicado por Publisher.OrderCreated [orders-api] ← POST /orders orders-api" in re.sub(r"\s+", " ", trace_text), trace_text


def test_ambiguous_name_lists_the_candidates(repro_html: Path) -> None:
    with sync_api.sync_playwright() as p:
        pg = Page(p, repro_html)
        pg.page.keyboard.type("orders")
        pg.page.wait_for_selector("#hq-list .sg")
        sugg = pg.texts("#hq-list .sg")
        pg.page.keyboard.press("Enter")
        pg.page.wait_for_selector("#cands li")
        cands = pg.texts("#cands li")
        pg.page.click("#cands li:nth-child(2) a")
        pg.page.wait_for_selector("#ans")
        h1 = pg.page.inner_text("#read h1")
        errors = pg.close()
    assert not errors, errors
    assert any("coleção · Firestore" in s for s in sugg) and any("tabela · Postgres" in s for s in sugg), sugg
    assert cands == [
        "orders coleção · Firestore orders-api, shop-web collection:orders",
        "orders tabela · Postgres billing-worker, orders-api table:orders",
    ], cands
    assert h1.startswith("orders") and "tabela · Postgres" in h1, h1


def test_each_card_opens_its_list(repro_html: Path) -> None:
    seen = {}
    with sync_api.sync_playwright() as p:
        pg = Page(p, repro_html)
        for kind in ("unexpected", "multi", "dynamic", "orphans", "mirrors"):
            pg.go("#/")
            pg.page.click(f'#finds .fcard[data-card="{kind}"] h3')
            pg.page.wait_for_timeout(250)
            seen[kind] = (pg.page.evaluate("() => location.hash"), pg.page.inner_text("#read"))
        pg.go("#/")
        pg.page.click('#strip .stat[data-stat="unres"]')
        pg.page.wait_for_timeout(250)
        seen["unres"] = (pg.page.evaluate("() => location.hash"), pg.page.inner_text("#read"))
        pg.go("#/")
        pg.page.click('#finds .fcard[data-card="unexpected"] li a')
        pg.page.wait_for_selector("#ans .arow")
        item = (pg.page.evaluate("() => location.hash"), pg.texts("#ans .arow .chain.short"))
        errors = pg.close()
    assert not errors, errors
    assert seen["unexpected"][0].startswith("#/find/unexpected") and "billing-worker escreve orders.status (dono: orders-api)" in seen["unexpected"][1]
    assert seen["multi"][0].startswith("#/find/multi") and "orders é escrita por 2 serviços" in seen["multi"][1]
    for fn in ("saveDoc", "repo.touch", "Repo.BulkFix"):
        assert fn in seen["dynamic"][1], seen["dynamic"]
    assert "Repo.CountByStatus" in seen["orphans"][1] and "TestSetStatus" not in seen["orphans"][1]
    assert seen["mirrors"][0].startswith("#/flow")
    assert seen["unres"][0].startswith("#/find/unresolved") and "{FRAUD_API_URL}/check" in seen["unres"][1]
    # the item opens the column's answer for that service only
    assert item[0].startswith("#/q/orders.status/w?svc=") and len(item[1]) == 1 and "repo.mark_billed" in item[1][0], item


def test_matrix_cell_opens_the_filtered_answer(repro_html: Path) -> None:
    with sync_api.sync_playwright() as p:
        pg = Page(p, repro_html)
        svc = pg.page.evaluate("() => window.__bizmap.index.services.findIndex(s => s.name === 'billing-worker')")
        pg.page.click(f'#mx-table td.c[data-ref="table:orders"][data-svc="{svc}"]')
        pg.page.wait_for_selector("#ans .arow")
        landed = pg.page.evaluate("() => location.hash")
        rows = pg.texts("#ans .arow .chain.short")
        bar = pg.page.inner_text(".filterbar")
        pg.go("#/")
        pg.page.click('#mx-table th.col[data-ref="table:orders"]')
        pg.page.wait_for_selector("#ans .arow")
        whole = pg.texts("#ans .arow .chain.short")
        pg.go("#/")
        pg.page.fill("#mx-q", "zzz")
        pg.page.wait_for_timeout(400)
        filtered = pg.page.eval_on_selector_all("#mx-table thead th.col", "els => els.length")
        errors = pg.close()
    assert not errors, errors
    assert landed == f"#/q/table%3Aorders/w?svc={svc}", landed
    assert len(rows) == 1 and "repo.mark_billed" in rows[0] and "billing-worker" in bar
    assert len(whole) == 3  # mark_billed, Job.Run (the mirror) and storeStatus
    assert filtered == 0


def test_map_is_one_or_two_hops_around_an_entity(repro_html: Path) -> None:
    with sync_api.sync_playwright() as p:
        pg = Page(p, repro_html)
        prompt = pg.go("#/map")
        has_input = pg.page.query_selector("#mq") is not None
        pg.go("#/map/e/table%3Aorders")
        one = pg.page.evaluate("() => window.__bizmap.mapNodes.slice().sort()")
        pg.page.click('#hops a[data-h="2"]')
        pg.page.wait_for_timeout(300)
        two = pg.page.evaluate("() => window.__bizmap.mapNodes.slice().sort()")
        pg.page.wait_for_selector("#map svg g.node")
        canvas = pg.page.query_selector("#map svg") is not None
        errors = pg.close()
    assert not errors, errors
    assert "nunca desenha o sistema inteiro" in prompt and has_input
    assert one == ["billing-worker|s", "orders-api|s", "orders|e", "orders|e"], one
    assert "order-created|q" in two and "shop-web|s" in two and "order-created|q" not in one
    assert canvas


def test_show_details_changes_what_shows_and_goes_to_the_url(repro_html: Path) -> None:
    with sync_api.sync_playwright() as p:
        pg = Page(p, repro_html, "#/q/orders.status/w")
        pg.page.wait_for_selector("#ans .arow")
        before = pg.texts("#ans .arow .chain.short")
        pg.page.click("#det summary")
        pg.page.check("#det-t")
        pg.page.wait_for_timeout(200)
        after = pg.texts("#ans .arow .chain.short")
        url = pg.page.evaluate("() => location.hash")
        pg.page.check("#det-i")
        pg.page.wait_for_timeout(200)
        full = pg.page.eval_on_selector_all("#ans .arow.full", "els => els.length")
        url2 = pg.page.evaluate("() => location.hash")
        # the state follows links and survives a reload from the URL
        pg.go("#/")
        kept = pg.page.evaluate("() => location.hash")
        orphans = pg.page.inner_text('#finds .fcard[data-card="orphans"] h3')
        errors = pg.close()
        pg2 = Page(p, repro_html, "#/q/orders.status/w?d=t")
        pg2.page.wait_for_selector("#ans .arow")
        reloaded = pg2.texts("#ans .arow .chain.short")
        checked = pg2.page.evaluate("() => document.getElementById('det-t').checked")
        errors += pg2.close()
    assert not errors, errors
    assert len(before) == 2 and len(after) == 3 and any("TestSetStatus" in r for r in after), after
    assert url.endswith("?d=t"), url
    assert full == 3 and url2.endswith("?d=it"), url2
    assert kept == "#/?d=it" and orphans.endswith("3"), (kept, orphans)
    assert len(reloaded) == 3 and checked


def _refs(ix: Index) -> list[str]:
    refs = sorted(ix.by_name) + sorted(ix.col_by_name)
    refs += ["table:orders", "collection:orders", "orders-api/table:orders", "column:orders.status", "orders", "nope", ""]
    refs += [n for n in ix.nodes if ix.kind(n) in ("table", "view", "collection", "column")]
    return refs


def _python_answers(ix: Index, refs: list[str], lang: str) -> dict:
    out = {}
    for r in refs:
        row = {}
        for name in ("writes", "reads", "trace"):
            try:
                res = trace(ix, r) if name == "trace" else who(ix, r, name)
                row[name] = res
                row["f" + name] = unresolved_footer(res, lang)
            except QueryError as e:
                row[name] = {"error": e.payload, "code": e.code}
        out[r] = row
    return out


_PAGE_ANSWERS = """refs => {
  const out = {};
  for (const r of refs) {
    const row = {};
    for (const a of ["writes", "reads", "trace"]) {
      try { const res = window.__bizmap.answer(r, a); row[a] = res; row["f" + a] = window.__bizmap.footer(res); }
      catch (e) { if (!e.payload) throw e; row[a] = { error: e.payload, code: e.code }; }
    }
    out[r] = row;
  }
  return JSON.parse(JSON.stringify(out));
}"""


def test_page_answers_equal_the_cli_engine(repro_html: Path) -> None:
    """Every table / collection / column, plus qualified and wrong names:
    the page's answer is the CLI's `bizmap.query/v1` JSON, footer included."""
    data = build()
    ix = Index.from_dict(data)
    refs = _refs(ix)
    want = _python_answers(ix, refs, "pt-BR")
    with sync_api.sync_playwright() as p:
        pg = Page(p, repro_html)
        got = pg.page.evaluate(_PAGE_ANSWERS, refs)
        errors = pg.close()
    assert not errors, errors
    for r in refs:
        assert got[r] == json.loads(json.dumps(want[r])), r


def test_page_answer_equals_cli_who_writes_json(repro_html: Path, tmp_path: Path) -> None:
    """`bizmap who-writes orders.status --format json` on the same graph."""
    pytest.importorskip("tree_sitter_go")  # the CLI module imports the scanners
    from click.testing import CliRunner

    from bizmap.cli import main

    graph = tmp_path / "bizmap.graph.json"
    graph.write_text(json.dumps(build()), encoding="utf-8")
    for cmd in ("who-writes", "who-reads"):
        res = CliRunner().invoke(main, [cmd, "orders.status", "--graph", str(graph), "--format", "json"])
        assert res.exit_code == 0, res.output
        with sync_api.sync_playwright() as p:
            pg = Page(p, repro_html)
            got = pg.page.evaluate("a => window.__bizmap.answer('orders.status', a)", "writes" if cmd == "who-writes" else "reads")
            errors = pg.close()
        assert not errors, errors
        assert got == json.loads(res.output), cmd


def test_big_graph_start_screen_is_bounded(tmp_path: Path) -> None:
    """Tens of thousands of nodes: the start screen renders without errors and
    draws a bounded amount (top columns, suggestions, the first page of an
    answer); a sample of answers still equals the CLI engine."""
    from fixtures.big_graph import build as big

    data = big(functions=36_000, tables=1_000)
    assert len(data["nodes"]) > 40_000
    html = _export(tmp_path, data, lang="en")
    ix = Index.from_dict(data)
    rnd = random.Random(3)
    refs = rnd.sample(sorted(ix.by_name), 6) + rnd.sample(sorted(ix.col_by_name), 6)
    want = _python_answers(ix, refs, "en")
    with sync_api.sync_playwright() as p:
        pg = Page(p, html)
        cols = pg.page.eval_on_selector_all("#mx-table thead th.col", "els => els.length")
        cells = pg.page.eval_on_selector_all("#mx-table td", "els => els.length")
        nodes = pg.page.evaluate("() => document.getElementsByTagName('*').length")
        pg.page.keyboard.type("t1")
        pg.page.wait_for_selector("#hq-list .sg")
        sugg = pg.page.eval_on_selector_all("#hq-list .sg", "els => els.length")
        busiest = max(ix.by_name, key=lambda n: sum(len(ix.inc.get(i, [])) for i in ix.by_name[n]))
        pg.go(f"#/q/{busiest}/r")
        rows = pg.page.eval_on_selector_all("#ans .arow", "els => els.length")
        got = pg.page.evaluate(_PAGE_ANSWERS, refs)
        pg.page.click("#mx-more") if pg.page.query_selector("#mx-more") else None
        errors = pg.close()
    assert not errors, errors
    assert cols == 30 and cells <= 30 * 16 + 16, (cols, cells)
    assert nodes < 6000, nodes
    assert 0 < sugg <= 12
    assert 0 < rows <= 100
    for r in refs:
        assert got[r] == json.loads(json.dumps(want[r])), r


def test_markup_from_the_code_never_runs(tmp_path: Path) -> None:
    data = build()
    bad = '<img src=x onerror="window.__alerted=1">'
    for n in data["nodes"]:
        if n["id"].endswith("Repo.storeStatus"):
            n["name"] = "storeStatus" + bad
        if n["id"] == "orders-api/table:orders":
            n["evidence"] = {"file": "migrations/" + bad + ".sql", "line": 1}
        if n.get("type") == "column" and n.get("name") == "status":
            n["name"] = "status" + bad
    html = _export(tmp_path, data)
    with sync_api.sync_playwright() as p:
        pg = Page(p, html)
        texts = []
        for h in ("#/", "#/find/unexpected", "#/find/orphans", "#/find/unresolved", "#/map/e/table%3Aorders?h=2&d=ict"):
            texts.append(pg.go(h))
        ref = pg.page.evaluate("() => window.__bizmap.chunk('qc').cols.find(c => c[1].indexOf('<img') >= 0)[0]")
        pg.go("#/q/" + pg.page.evaluate("r => encodeURIComponent(r)", ref) + "/w?d=ict")
        pg.page.keyboard.type("status")
        pg.page.wait_for_timeout(200)
        texts.append(pg.page.inner_text("#read"))
        alerted = pg.page.evaluate("() => window.__alerted || false")
        imgs = pg.page.eval_on_selector_all("#read img", "els => els.length")
        errors = pg.close()
    assert not errors, errors
    assert alerted is False and imgs == 0
    assert any("onerror" in t for t in texts)  # shown as text


def test_old_graph_opens(tmp_path: Path) -> None:
    """A graph from an older version (single repo, no repos list, engines,
    evidence or queues): the start screen and an answer still open."""
    data = {
        "nodes": [
            {"id": "function:pkg.Save", "type": "function"},
            {"id": "entrypoint:http:POST:/save", "type": "entrypoint", "kind": "http", "method": "POST", "path": "/save"},
            {"id": "table:orders", "type": "table"},
            {"id": "column:orders.status", "type": "column", "table": "orders", "name": "status"},
        ],
        "edges": [
            {"id": "t", "type": "triggers", "from": "entrypoint:http:POST:/save", "to": "function:pkg.Save"},
            {"id": "w", "type": "writes", "from": "function:pkg.Save", "to": "column:orders.status"},
        ],
    }
    html = _export(tmp_path, data, lang="en")
    with sync_api.sync_playwright() as p:
        pg = Page(p, html)
        home = pg.go("#/")
        answer = pg.go("#/q/orders.status/w")
        pg.go("#/q/orders.status/t")
        pg.go("#/map/e/orders")
        pg.go("#/find/orphans")
        errors = pg.close()
    assert not errors, errors
    assert "0 unresolved links" in home
    assert "POST /save → pkg.Save" in answer and "Nothing unresolved" in answer
