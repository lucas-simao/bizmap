"""`bizmap export --format html` (0.5.0): offline page, services first."""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from bizmap.export import ExportError, export_graph
from bizmap.export.html import build_payload, node_order
from bizmap.export.overview import build_overview
from bizmap.export.reach import store_from_dict, table_reach
from bizmap.export.services import ExportConfigError, assign_services, parse_service_split
from bizmap.mcp_server import tool_table_usage

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "src" / "bizmap" / "export"


# ── fixtures ────────────────────────────────────────────────────────────────


def _n(nid: str, ntype: str, **attrs) -> dict:
    return {"id": nid, "type": ntype, **attrs}


def _e(etype: str, a: str, b: str, conf: str = "exact", **attrs) -> dict:
    return {"id": f"{etype}:{a}->{b}", "type": etype, "from": a, "to": b, "confidence": conf, **attrs}


def shop_graph() -> dict:
    """Four fictional services sharing one database (web → api → billing,
    worker and admin also touch `orders`)."""
    nodes = [
        _n("table:orders", "table", name="orders"),
        _n("table:invoices", "table", name="invoices"),
        _n("column:orders.status", "column", table="orders", name="status"),
        _n("column:orders.id", "column", table="orders", name="id"),
        _n("column:invoices.status", "column", table="invoices", name="status"),
        _n("web/function:src.checkout.placeOrder", "function", name="placeOrder",
           evidence={"file": "src/checkout.ts", "line": 4}),
        _n("api/entrypoint:http:POST:/orders", "entrypoint", kind="http", method="POST", path="/orders",
           evidence={"file": "routes.go", "line": 10}),
        _n("api/function:main.Handler.Create", "function", name="Handler.Create",
           evidence={"file": "handler.go", "line": 20}, why="Creates an order."),
        _n("api/function:main.Repo.Insert", "function", name="Repo.Insert", evidence={"file": "repo.go", "line": 5}),
        _n("api/function:main.Billing.Charge", "function", name="Billing.Charge", evidence={"file": "billing.go", "line": 3}),
        _n("billing/entrypoint:http:POST:/invoices", "entrypoint", kind="http", method="POST", path="/invoices",
           evidence={"file": "app.py", "line": 8}),
        _n("billing/function:app.create_invoice", "function", name="create_invoice", evidence={"file": "app.py", "line": 9}),
        _n("worker/entrypoint:job:expire", "entrypoint", kind="scheduled_job", symbol="expire"),
        _n("worker/function:jobs.expire", "function", name="expire", evidence={"file": "jobs.py", "line": 1}),
        _n("admin/entrypoint:http:GET:/admin/orders", "entrypoint", kind="http", method="GET", path="/admin/orders"),
        _n("admin/function:admin.list_orders", "function", name="list_orders"),
        _n("admin/function:admin.charge", "function", name="charge"),
    ]
    edges = [
        _e("calls_http", "web/function:src.checkout.placeOrder", "api/entrypoint:http:POST:/orders", "inferred"),
        _e("triggers", "api/entrypoint:http:POST:/orders", "api/function:main.Handler.Create"),
        _e("calls", "api/function:main.Handler.Create", "api/function:main.Repo.Insert"),
        _e("calls", "api/function:main.Handler.Create", "api/function:main.Billing.Charge"),
        _e("writes", "api/function:main.Repo.Insert", "column:orders.status", ops=["INSERT"]),
        _e("writes", "api/function:main.Handler.Create", "table:orders", "inferred", derived="call"),
        _e("writes", "api/entrypoint:http:POST:/orders", "table:orders", "inferred", derived="call"),
        _e("calls_http", "api/function:main.Billing.Charge", "billing/entrypoint:http:POST:/invoices", "inferred"),
        _e("triggers", "billing/entrypoint:http:POST:/invoices", "billing/function:app.create_invoice"),
        _e("writes", "billing/function:app.create_invoice", "column:invoices.status"),
        _e("reads", "billing/function:app.create_invoice", "column:orders.id"),
        _e("triggers", "worker/entrypoint:job:expire", "worker/function:jobs.expire"),
        _e("writes", "worker/function:jobs.expire", "column:orders.status", "unknown"),
        _e("triggers", "admin/entrypoint:http:GET:/admin/orders", "admin/function:admin.list_orders"),
        _e("reads", "admin/function:admin.list_orders", "table:orders", "inferred"),
        _e("calls_http", "admin/function:admin.charge", "billing/entrypoint:http:POST:/invoices", "heuristic"),
        _e("calls_http", "web/function:src.checkout.placeOrder", "billing/entrypoint:http:POST:/invoices", "heuristic"),
    ]
    return {"version": "0.4.2", "generated_at": "2026-11-20T00:00:00Z", "nodes": nodes, "edges": edges,
            "repos": ["admin", "api", "billing", "web", "worker"], "database": "shop"}


def synthetic_graph(services: int = 30, routes: int = 300, functions: int = 1350,
                    tables: int = 600, cols: int = 8, seed: int = 7) -> dict:
    """A --discover-sized graph (defaults: ~55k nodes / ~87k edges)."""
    rnd = random.Random(seed)
    nodes: list[dict] = []
    edges: list[dict] = []
    for t in range(tables):
        nodes.append(_n(f"table:t{t}", "table", name=f"t{t}"))
        for c in range(cols):
            nodes.append(_n(f"column:t{t}.c{c}", "column", table=f"t{t}", name=f"c{c}"))
    svc_routes: list[list[str]] = []
    for s in range(services):
        repo = f"svc{s:02d}"
        rids = []
        fids = [f"{repo}/function:pkg{f % 40}.F{f}" for f in range(functions)]
        for f, fid in enumerate(fids):
            nodes.append(_n(fid, "function", name=f"F{f}", evidence={"file": f"pkg{f % 40}/f.go", "line": f + 1}))
        for r in range(routes):
            rid = f"{repo}/entrypoint:http:GET:/{repo}/r{r}"
            rids.append(rid)
            nodes.append(_n(rid, "entrypoint", kind="http", method="GET", path=f"/{repo}/r{r}",
                            evidence={"file": "routes.go", "line": r + 1}))
            edges.append(_e("triggers", rid, fids[r]))
        svc_routes.append(rids)
        # layered calls: handlers -> services -> repositories
        for f in range(functions):
            for _ in range((2 if f % 7 else 1) if f < functions - 200 else 0):
                g = rnd.randrange(max(f + 1, routes), functions) if f < functions - 1 else f
                if g != f:
                    edges.append(_e("calls", fids[f], fids[g]))
        for f in range(functions - 200, functions):
            t = rnd.randrange(tables)
            kind = "writes" if f % 3 == 0 else "reads"
            edges.append(_e(kind, fids[f], f"column:t{t}.c{rnd.randrange(cols)}"))
            edges.append(_e(kind, fids[f], f"table:t{rnd.randrange(tables)}", "inferred"))
    for s in range(services):
        repo = f"svc{s:02d}"
        for k in range(70):
            dst = rnd.randrange(services)
            if dst == s:
                continue
            src = f"{repo}/function:pkg{(routes + k) % 40}.F{routes + k}"
            edges.append(_e("calls_http", src, rnd.choice(svc_routes[dst]), "inferred"))
    uniq = {e["id"]: e for e in edges}
    return {"version": "0.4.2", "generated_at": "2026-11-20T00:00:00Z", "nodes": nodes,
            "edges": list(uniq.values())}


def _payload_blob(html: str) -> dict:
    m = re.search(r'<script id="bizmap-data" type="application/json">(.*?)</script>', html, re.S)
    assert m
    return json.loads(m.group(1))


# ── services / overview ─────────────────────────────────────────────────────


def test_services_are_repos_and_shared_tables_have_none() -> None:
    svc = assign_services(shop_graph())
    assert svc["api/function:main.Repo.Insert"] == "api"
    assert svc["web/function:src.checkout.placeOrder"] == "web"
    assert svc["table:orders"] is None and svc["column:orders.status"] is None


def test_single_repo_graph_is_one_service() -> None:
    data = {"repo": "shop", "nodes": [_n("function:a", "function"), _n("table:t", "table", name="t")], "edges": []}
    assert assign_services(data) == {"function:a": "shop", "table:t": None}


def test_yaml_split_monorepo_into_services() -> None:
    data = {"repo": "mono", "nodes": [
        _n("function:orders.a", "function", evidence={"file": "services/orders/a.py", "line": 1}),
        _n("function:billing.b", "function", evidence={"file": "services/billing/b.py", "line": 1}),
        _n("function:shared.c", "function", evidence={"file": "libs/c.py", "line": 1}),
    ], "edges": []}
    split = parse_service_split({"orders": "services/orders/", "billing": ["./services/billing/"]})
    svc = assign_services(data, split)
    assert svc == {"function:orders.a": "orders", "function:billing.b": "billing", "function:shared.c": "mono"}
    # repo-scoped prefix only applies to that repo
    scoped = parse_service_split({"x": "other:services/orders/"})
    assert assign_services(data, scoped)["function:orders.a"] == "mono"
    with pytest.raises(ExportConfigError):
        parse_service_split({"bad": 3})


def test_overview_connections_and_risks() -> None:
    data = shop_graph()
    ov = build_overview(data, assign_services(data))
    names = [s["name"] for s in ov["services"]]
    assert names == ["admin", "api", "billing", "web", "worker"]
    conns = {(c["from"], c["to"]): c["links"] for c in ov["connections"]}
    assert conns == {("web", "api"): 1, ("web", "billing"): 1, ("api", "billing"): 1, ("admin", "billing"): 1}
    risks = {(r["kind"], r["name"]): r for r in ov["risks"]}
    # billing is called by admin, api and web; orders is used by admin, api, billing, worker
    assert risks[("service", "billing")]["users"] == ["admin", "api", "web"]
    assert risks[("table", "orders")]["users"] == ["admin", "api", "billing", "worker"]
    assert ("table", "invoices") not in risks
    assert ov["risks"][0]["name"] == "orders" and all(r["top"] for r in ov["risks"])


# ── reach (Python twin) == MCP table_usage ──────────────────────────────────


def test_table_reach_matches_mcp_table_usage_across_services() -> None:
    data = shop_graph()
    store = store_from_dict(data)
    for table in ("table:orders", "table:invoices"):
        ours = table_reach(store, table)
        mcp = tool_table_usage(store, {"table": table.split(":", 1)[1], "limit": 100})
        assert sorted(ours["functions"]) == sorted(f["id"] for f in mcp["functions"])
        assert ours["entrypoints"] == {e["id"]: e["confidence"] for e in mcp["entrypoints"]}
    eps = table_reach(store, "table:orders")["entrypoints"]
    # the web front end has no entrypoint; the api route and worker job reach orders,
    # and billing's route reaches it through create_invoice (reads orders.id)
    assert "api/entrypoint:http:POST:/orders" in eps
    assert "billing/entrypoint:http:POST:/invoices" in eps
    assert eps["worker/entrypoint:job:expire"] == "unknown"


# ── html ────────────────────────────────────────────────────────────────────


def test_html_is_self_contained_and_offline() -> None:
    html = export_graph(shop_graph(), "html")
    assert html.startswith("<!DOCTYPE html>")
    assert "http://" not in html.split("<script", 1)[0]
    # no external resources: no src=/href= to the network, no CDN
    assert not re.search(r'<(script|link|img)[^>]+(src|href)=', html)
    assert "Cytoscape Consortium" in html  # MIT notice kept with the inlined lib
    assert html.count("</script>") == 4
    payload = _payload_blob(html)
    assert [s["name"] for s in payload["services"]] == ["admin", "api", "billing", "web", "worker"]
    assert payload["lang"] == "en"


def test_payload_groups_nodes_by_service_slice() -> None:
    data = shop_graph()
    svc = assign_services(data)
    payload = build_payload(data, svc)
    for s, row in enumerate(payload["services"]):
        assert all(n[1] == s for n in payload["nodes"][row["start"]:row["end"]])
        assert sum(1 for n in payload["nodes"] if n[1] == s) == row["end"] - row["start"]
    order = node_order(data, svc, [r["name"] for r in payload["services"]])
    assert len(order) == len(data["nodes"])


def test_html_lang_and_script_safety() -> None:
    data = shop_graph()
    data["nodes"][7]["why"] = "</script><script>alert(1)</script> <!-- x"
    html = export_graph(data, "html", lang="pt-BR")
    assert '<html lang="pt-BR">' in html
    assert "alert(1)</script>" not in html
    assert _payload_blob(html)["why"]
    assert any("alert(1)" in w for w in _payload_blob(html)["why"].values())


def test_llm_format_is_reserved_for_051() -> None:
    with pytest.raises(ExportError, match="0.5.1"):
        export_graph(shop_graph(), "llm")
    with pytest.raises(ExportError):
        export_graph({"nodes": 1}, "html")


def _node() -> str | None:
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="node not installed")
def test_browser_reach_js_matches_python(tmp_path: Path) -> None:
    """reach.js (what the page runs) == reach.py == MCP table_usage, for every table."""
    for data in (shop_graph(), synthetic_graph(services=4, routes=20, functions=120, tables=30, cols=3)):
        svc = assign_services(data)
        payload = build_payload(data, svc)
        order = node_order(data, svc, [r["name"] for r in payload["services"]])
        (tmp_path / "p.json").write_text(json.dumps(payload))
        script = tmp_path / "run.js"
        script.write_text(
            "const R = require(process.argv[2]); const D = require(process.argv[3]);\n"
            "const G = R.buildIndex(D); const out = {};\n"
            "D.nodes.forEach((n, i) => { if (n[0] !== 2) return; const r = R.tableReach(G, i);\n"
            "  out[i] = { f: [...r.functions.keys()].sort((a,b)=>a-b),\n"
            "    e: Object.fromEntries([...r.entrypoints].map(([k, v]) => [k, R.CONF[v - 1]])) }; });\n"
            "console.log(JSON.stringify(out));\n"
        )
        res = subprocess.run([_node(), str(script), str(EXPORT / "reach.js"), str(tmp_path / "p.json")],
                             capture_output=True, text=True, check=True)
        js = json.loads(res.stdout)
        store = store_from_dict(data)
        assert js
        for i, got in js.items():
            py = table_reach(store, order[int(i)])
            assert sorted(order[f] for f in got["f"]) == sorted(py["functions"])
            assert {order[int(k)]: v for k, v in got["e"].items()} == py["entrypoints"]


# ── scale ───────────────────────────────────────────────────────────────────


@pytest.mark.perf
def test_export_html_discover_sized_graph(tmp_path: Path) -> None:
    data = synthetic_graph()
    n, m = len(data["nodes"]), len(data["edges"])
    assert 52_000 <= n <= 58_000 and 82_000 <= m <= 92_000, (n, m)
    start = time.perf_counter()
    html = export_graph(data, "html")
    took = time.perf_counter() - start
    out = tmp_path / "big.html"
    out.write_text(html, encoding="utf-8")
    size = out.stat().st_size
    print(f"\nexport html: {n} nodes / {m} edges -> {size / 1e6:.1f} MB in {took:.2f}s")
    assert took < 10, took
    assert size < 12_000_000, size
    payload = _payload_blob(html)
    assert len(payload["nodes"]) == n
    assert len(payload["services"]) == 30


def test_cli_export_writes_html(tmp_path: Path) -> None:
    pytest.importorskip("sqlglot")  # bizmap.cli imports the scanners
    from click.testing import CliRunner

    from bizmap.cli import main

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(shop_graph()))
    out = tmp_path / "out" / "map.html"
    res = CliRunner().invoke(main, ["export", "--graph", str(graph), "--out", str(out), "--lang", "pt-BR"])
    assert res.exit_code == 0, res.output
    assert "wrote" in res.output and out.is_file()
    res = CliRunner().invoke(main, ["export", "--graph", str(graph), "--format", "llm"])
    assert res.exit_code != 0 and "0.5.1" in res.output
    res = CliRunner().invoke(main, ["export", "--graph", str(tmp_path / "missing.json")])
    assert res.exit_code != 0 and "bizmap scan" in res.output


def test_export_scanned_checkout_example() -> None:
    """The bundled 3-service example, scanned for real (CI has the scanners)."""
    pytest.importorskip("sqlglot")
    from bizmap.scan import scan_path

    data = scan_path(ROOT / "examples" / "checkout").to_dict()
    payload = _payload_blob(export_graph(data, "html"))
    assert [s["name"] for s in payload["services"]] == ["api", "billing", "web"]
    names = [s["name"] for s in payload["services"]]
    links = {(names[a], names[b]) for a, b, _n in payload["connections"]}
    assert {("web", "api"), ("api", "billing"), ("billing", "api")} <= links


def test_export_services_from_bizmap_yaml(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    from bizmap.export.services import load_service_split

    cfg = tmp_path / "bizmap.yaml"
    cfg.write_text("name: mono\nexport:\n  services:\n    orders: services/orders/\n")
    assert load_service_split(cfg) == [(None, "services/orders/", "orders")]
    cfg.write_text("export:\n  colors: red\n")
    with pytest.raises(ExportConfigError, match="unknown export key"):
        load_service_split(cfg)
    cfg.write_text("name: mono\n")
    assert load_service_split(cfg) == []
