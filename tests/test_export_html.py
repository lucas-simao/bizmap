"""`bizmap export --format html`: the offline "book" (services → domains → routes → data)."""

from __future__ import annotations

import importlib.util
import json
import random
import re
import time
from pathlib import Path

import pytest

from bizmap.export import ExportError, export_graph
from bizmap.export.book import build_book, path_params, screen_group
from bizmap.export.domains import (
    OTHER,
    ROUTES,
    group_entrypoints,
    parse_domain_rules,
    path_segments,
)
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



def big_graph(seed: int = 11) -> dict:
    """A graph shaped like a 16-repo --discover (~108k nodes / ~640k edges):
    one very large API, a second API with 1,100+ routes, a front end calling
    them (and thousands of calls that match no route), small libraries; most
    data edges are derived, as in a real scan. All names are invented."""
    rnd = random.Random(seed)
    nodes: list[dict] = []
    edges: list[dict] = []
    tables = 830
    for t in range(tables):
        nodes.append(_n(f"table:tbl_{t}", "table", name=f"tbl_{t}"))
        for c in range(8):
            nodes.append(_n(f"column:tbl_{t}.col_{c}", "column", table=f"tbl_{t}", name=f"col_{c}", data_type="TEXT"))
    words = ["account", "project", "task", "invoice", "report", "user", "team", "asset", "order", "file",
             "message", "event", "plan", "audit", "export", "import", "search", "setting", "role", "token"]
    # (name, functions, routes, extension)
    services = [("core", 51000, 305, "go"), ("portal", 20000, 8, "tsx"), ("hub", 15400, 1154, "py"),
                ("ledger", 2850, 71, "go"), ("console", 3300, 0, "ts"), ("relay", 950, 2, "py"),
                ("ingest", 1500, 1, "py"), ("kit", 1040, 0, "ts")] + [
               (f"lib{k}", n, 0, "py") for k, n in enumerate([630, 470, 465, 220, 85, 75, 58, 24])]
    route_ids: dict[str, list[str]] = {}
    why = "Explains what this function does and why, as a comment above it would. " * 4
    for name, n_fn, n_routes, ext in services:
        fids = []
        for f in range(n_fn):
            area = words[f % len(words)]
            folder = ("src/pages" if ext == "tsx" else "internal") + f"/{area}/{['handlers', 'service', 'store'][f % 3]}"
            fid = f"{name}/function:{area}.{['handlers', 'service', 'store'][f % 3]}.{area.capitalize()}Fn{f}"
            attrs = {"evidence": {"file": f"{folder}/{area}_{f % 37}.{ext}", "line": 1 + f % 400}}
            if f % 5 < 2:
                attrs["why"] = why
            nodes.append(_n(fid, "function", name=f"Fn{f}", **attrs))
            fids.append(fid)
        rids = []
        for r in range(n_routes):
            area = words[(r * 7) % len(words)] if r % 4 else words[r % 3]
            depth = "" if r % 3 else f"/{words[(r // 3) % len(words)]}s"
            path = f"/api/v1/{area}s{depth}/:id/{['list', 'detail', 'update', 'items'][r % 4]}{r}"
            rid = f"{name}/entrypoint:http:{['GET', 'POST', 'PUT', 'DELETE'][r % 4]}:{path}"
            nodes.append(_n(rid, "entrypoint", kind="http", method=["GET", "POST", "PUT", "DELETE"][r % 4], path=path,
                            evidence={"file": "router." + ext, "line": r + 1}))
            edges.append(_e("triggers", rid, fids[(r * 13) % n_fn]))
            for _ in range(3):
                edges.append(_e("reads", rid, f"table:tbl_{rnd.randrange(tables)}", "inferred", derived="call"))
            rids.append(rid)
        route_ids[name] = rids
        for f in range(n_fn):
            # feature clusters of ~40 functions calling forward inside the
            # cluster, plus a few calls into shared helpers (the first 200)
            base = f - f % 40
            for _k in range(1 if f % 3 else 2):
                if f >= 200 and rnd.random() < 0.08:
                    g = rnd.randrange(200)
                elif f >= 200 or f % 40 < 30:
                    g = f + 1 + rnd.randrange(max(1, min(12, base + 39 - f))) if f < base + 39 else -1
                else:
                    g = -1
                if 0 <= g < n_fn and g != f:
                    edges.append(_e("calls", fids[f], fids[g]))
            if f % 3 == 2 and ext != "tsx":
                t = rnd.randrange(tables)
                kind = "writes" if f % 2 else "reads"
                for c in range(4):
                    edges.append(_e(kind, fids[f], f"column:tbl_{t}.col_{c}", ops=["UPDATE"] if kind == "writes" else []))
            for _ in range(4):
                edges.append(_e(rnd.choice(["reads", "writes"]), fids[f], f"table:tbl_{rnd.randrange(tables)}",
                                "inferred", derived="call"))
    unresolved = []
    apis = [s for s, _f, r, _e in services if r > 50]
    for name, n_fn, _r, ext in services:
        if ext not in ("tsx", "ts", "py") or name.startswith("lib"):
            continue
        for k in range(min(n_fn, 600)):
            src = f"{name}/function:{words[(k * 3) % len(words)]}." + ["handlers", "service", "store"][(k * 3) % 3] \
                  + f".{words[(k * 3) % len(words)].capitalize()}Fn{k * 3}"
            if k % 3 == 0:
                unresolved.append({"from": src, "method": "GET", "url": f"/api/v1/unknown/{k}", "keys": [],
                                   "reason": ["target_not_scanned", "path_dynamic", "base_unknown"][k % 3]})
            else:
                dst = rnd.choice(apis)
                if dst != name:
                    edges.append(_e("calls_http", src, rnd.choice(route_ids[dst]), "inferred"))
    unresolved += [{"from": f"portal/function:account.handlers.AccountFn{k * 60}", "method": "POST",
                    "url": f"/api/v2/x/{k}", "keys": [], "reason": "target_not_scanned"} for k in range(3200)]
    uniq = {e["id"]: e for e in edges}
    return {"version": "0.5.1", "generated_at": "2026-11-20T00:00:00Z", "nodes": nodes, "edges": list(uniq.values()),
            "unresolved_http": unresolved}


def _blocks(html: str) -> dict:
    out = {}
    for m in re.finditer(r'<script id="bm-([\w-]+)" type="application/json">(.*?)</script>', html, re.S):
        out[m.group(1)] = json.loads(m.group(2))
    assert "index" in out
    return out


def _book(data: dict, **kw) -> tuple[dict, dict]:
    return build_book(data, assign_services(data), **kw)


def _demo() -> dict:
    spec = importlib.util.spec_from_file_location("export_demo_graph", ROOT / "scripts" / "export_demo_graph.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build()


# ── domains ─────────────────────────────────────────────────────────────────


def test_path_segments_skip_generic_parts_and_params() -> None:
    assert path_segments("/api/v2/Projects/:id/tasks/{taskId}") == ["projects", "tasks"]
    assert path_segments("/rest/public/<int:pk>/x") == ["x"]
    assert path_params("/p/:id/t/{taskId}/u/<int:pk>/[slug]") == ["id", "taskId", "pk", "slug"]


def _routes(paths: list[str]) -> list[tuple[int, dict, str]]:
    return [(i, {"kind": "http", "method": "GET", "path": p}, f"h/{i}.go") for i, p in enumerate(paths)]


def test_routes_group_by_first_useful_segment_and_small_groups_go_to_other() -> None:
    paths = [f"/api/v1/projects/{k}" for k in range(6)] + [f"/api/v1/users/{k}" for k in range(5)] + \
            ["/api/v1/health", "/api/v1/metrics", "/api/v1/projects"]
    got = group_entrypoints(_routes(paths), "api")
    names = {i: n for i, (n, _r) in got.items()}
    assert {names[i] for i in range(6)} == {"/projects"} and names[13] == "/projects"
    assert {names[i] for i in range(6, 11)} == {"/users"}
    assert names[11] == OTHER and names[12] == OTHER


def test_a_dominant_group_is_split_by_the_next_segment() -> None:
    paths = [f"/admin/{area}/{k}" for area in ("orders", "users", "teams") for k in range(15)] + \
            [f"/reports/{k}" for k in range(5)]
    names = {i: n for i, (n, _r) in group_entrypoints(_routes(paths), "api").items()}
    # /admin is on 90% of the routes: skipped like a shared prefix
    assert names[0] == "/orders" and names[15] == "/users" and names[30] == "/teams"
    assert names[45] == "/reports"
    # a large group under a dominant prefix is split by the following segments
    paths = [f"/svc/alpha/{area}/{k}" for area in ("runs", "jobs") for k in range(50)] + \
            [f"/svc/beta/{k}" for k in range(10)] + [f"/health{k}" for k in range(3)]
    names = {n for n, _r in group_entrypoints(_routes(paths), "api").values()}
    assert {"/alpha/runs", "/alpha/jobs", "/beta"} <= names


def test_shared_prefix_is_skipped_and_small_services_get_one_domain() -> None:
    paths = [f"/billing/{a}/{k}" for a in ("invoices", "refunds") for k in range(8)]
    names = {n for n, _r in group_entrypoints(_routes(paths), "b").values()}
    assert names == {"/invoices", "/refunds"}
    names = {n for n, _r in group_entrypoints(_routes(["/a", "/b", "/c"]), "b").values()}
    assert names == {ROUTES}


def test_yaml_domains_and_folder_grouping_and_jobs_by_kind() -> None:
    rules = parse_domain_rules({"api": {"people": ["/users", "folder:internal/team/"]}})
    routes = _routes([f"/users/{k}" for k in range(20)]) + [(99, {"kind": "http", "path": "/teams/1"}, "internal/team/t.go"),
                                                             (100, {"kind": "scheduled_job", "symbol": "nightly"}, "jobs.py")]
    got = group_entrypoints(routes, "api", rules)
    assert got[0] == ("people", "yaml") and got[99] == ("people", "yaml")
    assert got[100] == ("jobs", "kind")
    by_folder = group_entrypoints(
        [(i, {"kind": "http", "path": f"/x/{i}"}, f"internal/{'a' if i < 10 else 'b'}/h.go") for i in range(20)], "api", by="folder")
    assert {n for n, _r in by_folder.values()} == {"/a", "/b"}


# ── book ────────────────────────────────────────────────────────────────────


def test_demo_book_levels_screens_and_unmatched_calls() -> None:
    index, chunks = _book(_demo())
    kinds = {s["name"]: s["kind"] for s in index["services"]}
    assert kinds == {"api": "api", "billing": "api", "catalog": "api", "shipping": "api", "web": "web", "worker": "worker"}
    api = next(s for s in index["services"] if s["name"] == "api")
    assert [index["domains"][d]["name"] for d in api["domains"]] == ["/cart", "/orders", "/projects", "/users", "other"]
    names = chunks["names"]["l"]
    projects = next(d for d in index["domains"] if d["name"] == "/projects")
    chunk = chunks[projects["chunk"]]
    archive = next(r for r in chunk["routes"] if r["p"].endswith("/archive"))
    assert archive["m"] == "POST" and archive["pp"] == ["id"]
    # the route's tables come from the scan's reach; its call tree is in the chunk
    assert {names[t] for t, _rw in archive["t"]} == {"projects", "audit_log"}
    handler = chunk["fns"][str(archive["h"][0])]
    assert handler[0] == "handlers.Projects.Archive" and handler[2].startswith("Archives a project")
    tree = {names[c] for c in handler[3]}
    assert {"service.Projects.Archive", "validate.Projects.Archive", "audit.Log"} <= tree
    # consumers: the web page that calls it
    assert [[names[c], g] for c, g in archive["c"]] == [["src.pages.projects.ProjectBoard.archive", 0]]
    assert [f for _s, f, _g in archive["sc"]] == ["src/pages/projects/ProjectBoard.tsx"] and archive["nh"] == 0
    # web screens, with the calls that matched no route
    web = next(s for s in index["services"] if s["kind"] == "web")
    assert web["screens"] == 11 and web["unmatched"] == 3
    screens = chunks[web["screen_chunk"]]["screens"]
    board = next(s for s in screens if s["f"].endswith("ProjectBoard.tsx"))
    assert board["g"] == "src/pages/projects" and board["u"] == 1 and len(board["r"]) == 5
    clients = chunks[web["screen_chunk"]]["clients"]
    reasons = sorted(u[2] for c in clients for f in c["fns"] for u in chunks[web["screen_chunk"]]["fns"][str(f)][8])
    assert reasons == ["base_unknown", "path_dynamic", "target_not_scanned"]


def test_own_data_access_only_and_tables_chunk() -> None:
    index, chunks = _book(_demo())
    names = chunks["names"]["l"]
    for ch in chunks.values():
        for rec in (ch.get("fns") or {}).values():
            for t, _rw, _cols, _ops in rec[6]:
                assert names[t] in {"orders", "order_items", "users", "projects", "tasks", "invoices", "payments",
                                    "products", "inventory", "audit_log", "shipments"}
    orders = next(t for t in index["tables"] if index["tableNames"][str(t["i"])] == "orders")
    info = chunks[orders["chunk"]]["tables"][str(orders["i"])]
    users = {names[u[0]] for u in info["users"]}
    # only the functions that touch it themselves (handlers reach it through them)
    assert "repository.Orders.Create" in users and "handlers.Orders.Create" not in users
    assert [c[0] for c in info["cols"]][:2] == ["created_at", "id"]
    assert orders["db"] == "shop" and orders["users"]


def test_every_node_has_a_home_chunk_and_search_names() -> None:
    data = _demo()
    index, chunks = _book(data)
    nm = chunks["names"]
    assert len(nm["l"]) == len(data["nodes"]) == len(nm["t"]) == len(nm["s"]) == len(nm["h"])
    homes = {index["chunks"][h] for h, t in zip(nm["h"], nm["t"], strict=True) if t in (0, 1, 2)}
    assert all(h in chunks for h in homes)
    assert all(h >= 0 for h, t in zip(nm["h"], nm["t"], strict=True) if t in (0, 1, 2, 3))


def test_signature_fields_are_shown_when_the_graph_has_them() -> None:
    data = _demo()
    for n in data["nodes"]:
        if n["id"] == "api/function:handlers.Projects.Archive":
            n["params"] = [{"name": "ctx", "type": "context.Context"}, {"name": "id", "type": "string"}]
            n["returns"] = "error"
    index, chunks = _book(data)
    rec = next(ch["fns"][k] for ch in chunks.values() for k in (ch.get("fns") or {})
               if ch["fns"][k][0] == "handlers.Projects.Archive")
    assert rec[9] == {"params": data_params(), "returns": "error"}


def data_params() -> list[dict]:
    return [{"name": "ctx", "type": "context.Context"}, {"name": "id", "type": "string"}]


def test_screen_group_prefers_ui_folders() -> None:
    assert screen_group("web/src/pages/orders/List.tsx") == "web/src/pages/orders"
    assert screen_group("src/app/settings/page.tsx") == "src/app/settings"
    assert screen_group("src/lib/api.ts") == "src/lib"


# ── html ────────────────────────────────────────────────────────────────────


def test_html_is_self_contained_and_offline() -> None:
    html = export_graph(_demo(), "html")
    assert html.startswith("<!DOCTYPE html>")
    assert not re.search(r'<(script|link|img)[^>]+(src|href)=', html)
    assert "Cytoscape Consortium" in html  # MIT notice kept with the inlined lib
    blocks = _blocks(html)
    assert set(blocks) == {"index", "names", *blocks["index"]["chunks"]}
    assert blocks["index"]["lang"] == "en"


def test_html_lang_title_and_script_safety() -> None:
    data = _demo()
    for n in data["nodes"]:
        if n["id"] == "api/function:handlers.Orders.Create":
            n["why"] = "</script><script>alert(1)</script> <!-- x"
    html = export_graph(data, "html", lang="pt-BR")
    assert '<html lang="pt-BR">' in html and "<title>BizMap — 6 serviços</title>" in html
    assert "alert(1)</script>" not in html
    blocks = _blocks(html)
    assert any("alert(1)" in rec[2] for b in blocks.values() for rec in (b.get("fns") or {}).values())
    assert "<title>BizMap — 6 services</title>" in export_graph(data, "html")
    one = {"repo": "shop", "nodes": [_n("function:a", "function")], "edges": []}
    assert "<title>BizMap — shop</title>" in export_graph(one, "html")


def test_output_is_deterministic() -> None:
    assert export_graph(_demo(), "html") == export_graph(_demo(), "html")


def test_llm_format_is_not_available_yet() -> None:
    with pytest.raises(ExportError, match="not available yet"):
        export_graph(_demo(), "llm")
    with pytest.raises(ExportError):
        export_graph({"nodes": 1}, "html")


def test_example_graph_is_the_demo() -> None:
    """examples/export/shop.graph.json is generated by scripts/export_demo_graph.py."""
    stored = json.loads((ROOT / "examples" / "export" / "shop.graph.json").read_text(encoding="utf-8"))
    assert stored == json.loads(json.dumps(_demo()))


# ── scale ───────────────────────────────────────────────────────────────────


@pytest.mark.perf
def test_export_book_on_a_16_repo_sized_graph(tmp_path: Path) -> None:
    data = big_graph()
    n, m = len(data["nodes"]), len(data["edges"])
    assert 100_000 <= n <= 115_000 and 600_000 <= m <= 680_000, (n, m)
    start = time.perf_counter()
    html = export_graph(data, "html")
    took = time.perf_counter() - start
    out = tmp_path / "big.html"
    out.write_text(html, encoding="utf-8")
    blocks = _blocks(html)
    index_bytes = len(json.dumps(blocks["index"]))
    largest = max(len(json.dumps(v)) for k, v in blocks.items() if k not in ("index", "names"))
    print(f"\nexport book: {n} nodes / {m} edges -> {out.stat().st_size / 1e6:.1f} MB in {took:.1f}s; "
          f"index {index_bytes / 1e3:.0f} KB, largest chunk {largest / 1e6:.2f} MB, {len(blocks)} blocks")
    assert took < 30, took
    assert index_bytes < 1_000_000  # parsed when the page opens
    assert largest < 4_000_000  # parsed when its level opens
    assert out.stat().st_size < 60_000_000


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
    assert res.exit_code != 0 and "not available yet" in res.output
    res = CliRunner().invoke(main, ["export", "--graph", str(tmp_path / "missing.json")])
    assert res.exit_code != 0 and "bizmap scan" in res.output


def test_export_scanned_checkout_example() -> None:
    """The bundled 3-service example, scanned for real (CI has the scanners)."""
    pytest.importorskip("sqlglot")
    from bizmap.scan import scan_path

    data = scan_path(ROOT / "examples" / "checkout").to_dict()
    index = _blocks(export_graph(data, "html"))["index"]
    names = [s["name"] for s in index["services"]]
    assert names == ["api", "billing", "web"]
    links = {(names[a], names[b]) for a, b, _n in index["connections"]}
    assert {("web", "api"), ("api", "billing"), ("billing", "api")} <= links


def test_export_services_from_bizmap_yaml(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    from bizmap.export.services import load_export_config, load_service_split

    cfg = tmp_path / "bizmap.yaml"
    cfg.write_text("name: mono\nexport:\n  services:\n    orders: services/orders/\n")
    assert load_service_split(cfg) == [(None, "services/orders/", "orders")]
    cfg.write_text("export:\n  domains:\n    api:\n      projects: [/projects, 'folder:internal/projects/']\n"
                   "  domains_by: folder\n")
    conf = load_export_config(cfg)
    assert conf["domains_by"] == "folder"
    assert conf["domains"] == {"api": [("folder", "internal/projects/", "projects"), ("path", "/projects", "projects")]}
    cfg.write_text("export:\n  domains_by: tree\n")
    with pytest.raises(ExportConfigError, match="domains_by"):
        load_export_config(cfg)
    cfg.write_text("export:\n  colors: red\n")
    with pytest.raises(ExportConfigError, match="unknown export key"):
        load_service_split(cfg)
    cfg.write_text("name: mono\n")
    assert load_service_split(cfg) == []


# ── services without routes (round 3) ───────────────────────────────────────


def vite_graph() -> dict:
    """Two repos: an API with GET /orders/:id and a Vite front end made of .ts
    files only (no pages/, no .tsx) that fetches it."""
    return {
        "version": "0.5.1", "generated_at": "x", "repos": ["api", "web"],
        "nodes": [
            _n("api/entrypoint:http:GET:/orders/:id", "entrypoint", kind="http", method="GET", path="/orders/:id",
               evidence={"file": "main.go", "line": 3}),
            _n("api/function:main.getOrder", "function", name="getOrder", evidence={"file": "main.go", "line": 9}),
            _n("table:orders", "table", name="orders"),
            _n("web/function:src.api.orders.getOrder", "function", name="getOrder",
               evidence={"file": "src/api/orders.ts", "line": 1}),
            _n("web/function:src.main.boot", "function", name="boot", evidence={"file": "src/main.ts", "line": 1}),
        ],
        "edges": [
            _e("triggers", "api/entrypoint:http:GET:/orders/:id", "api/function:main.getOrder"),
            _e("reads", "api/function:main.getOrder", "table:orders"),
            _e("calls_http", "web/function:src.api.orders.getOrder", "api/entrypoint:http:GET:/orders/:id", "inferred"),
            _e("calls", "web/function:src.main.boot", "web/function:src.api.orders.getOrder"),
        ],
    }


def test_ts_only_client_without_routes_is_a_front_end_with_screens() -> None:
    index, chunks = _book(vite_graph())
    web = next(s for s in index["services"] if s["name"] == "web")
    assert web["kind"] == "web" and web["screens"] == 1
    screen = chunks[web["screen_chunk"]]["screens"][0]
    assert screen["f"] == "src/api/orders.ts" and screen["g"] == "src/api"
    assert [[chunks["names"]["l"][r], g] for r, g in screen["r"]] == [["GET /orders/:id", 0]]
    # its code is listed by folder too, so its page is never empty
    assert [f[0] for f in web["folders"]] == ["src", "src/api"]


def test_library_page_lists_folders_and_the_tables_it_counts() -> None:
    index, chunks = _book(big_graph_small())
    for row in index["services"]:
        assert row["tables"] == len(row["data"]) or row["tables"] > len(row["data"]) == 400
        if not row["domains"] and row["kind"] != "web":
            assert row["folders"], row["name"]
            total = sum(f[1] for f in row["folders"])
            assert total == row["functions"]
            for f in row["folders"]:
                items = [i for cid in f[5] for i in chunks[cid]["items"]]
                assert len(items) == f[1]
                assert all(chunks["names"]["h"][i] >= 0 for i in items)


def big_graph_small() -> dict:
    data = _demo()
    lib = [_n(f"kit/function:util.Fn{k}", "function", name=f"Fn{k}",
              evidence={"file": f"pkg/{'db' if k % 2 else 'text'}/f{k}.py", "line": k + 1}) for k in range(20)]
    data["nodes"] += lib
    data["edges"] += [_e("reads", f"kit/function:util.Fn{k}", "table:orders") for k in range(1, 20, 2)]
    data["edges"] += [_e("writes", f"kit/function:util.Fn{k}", "table:users", "inferred", derived="call")
                      for k in range(0, 20, 2)]
    return data


def test_library_table_count_is_its_own_access() -> None:
    index, _chunks = _book(big_graph_small())
    kit = next(s for s in index["services"] if s["name"] == "kit")
    assert kit["kind"] == "lib" and kit["tables"] == 1  # derived edges do not count
    assert [f[0] for f in kit["folders"]] == ["pkg/db", "pkg/text"]
    assert kit["folders"][0][1:4] == [10, 0, 1]


def react_graph() -> dict:
    """A React front end: pages call hooks, hooks call the API client, the
    client calls the API; tests and generated clients call it too."""
    g = vite_graph()
    g["nodes"] += [
        _n("web/function:src.pages.Orders.OrderPage", "function", evidence={"file": "src/pages/Orders.tsx", "line": 1}),
        _n("web/function:src.hooks.useOrder", "function", evidence={"file": "src/hooks/useOrder.ts", "line": 1}),
        _n("web/function:src.api.__tests__.orders.test.module", "function",
           evidence={"file": "src/api/__tests__/orders.test.ts", "line": 1}),
        _n("api/function:contract.generated.client.module", "function",
           evidence={"file": "contract/generated/client.ts", "line": 1}),
        _n("web/function:src.api.users.getUser", "function", evidence={"file": "src/api/users.ts", "line": 1}),
    ]
    g["edges"] += [
        _e("calls", "web/function:src.pages.Orders.OrderPage", "web/function:src.hooks.useOrder"),
        _e("calls", "web/function:src.hooks.useOrder", "web/function:src.api.orders.getOrder"),
        _e("calls_http", "web/function:src.api.__tests__.orders.test.module", "api/entrypoint:http:GET:/orders/:id", "heuristic"),
        _e("calls_http", "api/function:contract.generated.client.module", "api/entrypoint:http:GET:/orders/:id", "inferred"),
        _e("calls_http", "web/function:src.api.users.getUser", "api/entrypoint:http:GET:/orders/:id", "heuristic"),
    ]
    return g


def test_screens_are_ui_files_reaching_the_api_client_and_tests_are_left_out() -> None:
    index, chunks = _book(react_graph())
    names = chunks["names"]["l"]
    web = next(s for s in index["services"] if s["name"] == "web")
    wc = chunks[web["screen_chunk"]]
    # the page reaches the API through a hook and the client
    assert [(s["f"], [names[r] for r, _g in s["r"]]) for s in wc["screens"]] == [("src/pages/Orders.tsx", ["GET /orders/:id"])]
    assert [names[f] for f in wc["screens"][0]["via"]] == ["src.api.orders.getOrder"]
    # the client layer: both client files, the test file is not there; users.ts has no screen
    clients = {c["f"]: c for c in wc["clients"]}
    assert set(clients) == {"src/api/orders.ts", "src/api/users.ts"}
    assert clients["src/api/orders.ts"]["ui"] == ["src/pages/Orders.tsx"] and clients["src/api/users.ts"]["ui"] == []
    assert clients["src/api/users.ts"]["r"][0][1] == 1  # heuristic match: "likely"
    assert web["clients"] == 2 and web["clients_linked"] == 1 and web["hidden_calls"] == 1
    # the route: consumers without tests or generated code, screens listed
    api_dom = next(d for d in index["domains"] if index["services"][d["svc"]]["name"] == "api")
    route = chunks[api_dom["chunk"]]["routes"][0]
    assert sorted(names[c] for c, _g in route["c"]) == ["src.api.orders.getOrder", "src.api.users.getUser"]
    assert route["nh"] == 2 and [[f, g] for _s, f, g in route["sc"]] == [["src/pages/Orders.tsx", 0]]


def test_module_level_functions_read_as_modules_and_unmatched_calls_on_any_service() -> None:
    g = react_graph()
    g["unresolved_http"] = [{"from": "api/function:main.getOrder", "method": "GET", "url": "/billing/x",
                             "keys": [], "reason": "target_not_scanned"}]
    index, chunks = _book(g)
    assert "src.api.__tests__.orders.test (module)" in chunks["names"]["l"]
    api = next(s for s in index["services"] if s["name"] == "api")
    assert api["kind"] == "api" and api["unmatched"] == 1 and api["screen_chunk"]


# ── API docs: OpenAPI / Swagger files and swag comments ─────────────────────


def _demo_spec() -> dict:
    spec = importlib.util.spec_from_file_location("export_demo_graph", ROOT / "scripts" / "export_demo_graph.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_openapi()


def _doc_of(index: dict, chunks: dict, method: str, path: str) -> dict:
    for d in index["domains"]:
        for r in chunks[d["chunk"]]["routes"]:
            if r["m"] == method and r["p"] == path:
                return r
    raise AssertionError((method, path))


def test_openapi_3_file_gives_params_body_and_response_json() -> None:
    from bizmap.export.openapi import SchemaTable, norm_path, operations_from_spec

    assert norm_path("/api/v1/projects/:id/") == norm_path("/api/v1/projects/{projectId}") == "/api/v1/projects/{}"
    table = SchemaTable()
    ops = {(o["m"], o["p"]): o for o in operations_from_spec(_demo_spec(), table)}
    post = ops[("POST", "/api/v1/orders")]  # servers[0] base path prepended
    assert post["sum"] == "Place an order" and post["tags"] == ["orders"]
    assert post["body"] == {"r": "CreateOrderRequest"}
    fields = {f[0]: f for f in table.named["CreateOrderRequest"]["f"]}
    assert fields["items"][1] == {"t": "array", "i": {"r": "OrderItem"}} and fields["items"][2] == 1
    assert table.named["Order"]["f"][1][1]["e"] == ["pending", "paid", "cancelled"]
    archive = ops[("POST", "/api/v1/projects/{}/archive")]
    assert archive["params"] == [["id", "path", "string", 1, "Project ID"]]
    assert [r[0] for r in archive["resp"]] == ["200", "404"]


def test_swagger_2_file_with_base_path_and_definitions() -> None:
    from bizmap.export.openapi import SchemaTable, operations_from_spec

    spec = {
        "swagger": "2.0", "basePath": "/api/v1",
        "paths": {"/users/{id}": {"patch": {
            "summary": "Rename a user", "tags": ["users"],
            "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"},
                           {"name": "body", "in": "body", "schema": {"$ref": "#/definitions/models.Rename"}}],
            "responses": {"200": {"description": "OK", "schema": {"$ref": "#/definitions/models.User"}}}}}},
        "definitions": {"models.Rename": {"type": "object", "properties": {"name": {"type": "string"}}},
                        "models.User": {"type": "object", "properties": {"id": {"type": "string"}}}},
    }
    table = SchemaTable()
    op = operations_from_spec(spec, table)[0]
    assert (op["m"], op["p"]) == ("PATCH", "/api/v1/users/{}")
    assert op["body"] == {"r": "models.Rename"} and op["resp"][0][2] == {"r": "models.User"}
    assert table.named["models.Rename"] == {"t": "object", "f": [["name", {"t": "string"}, 0, ""]]}


def test_swag_comments_joined_on_one_line() -> None:
    from bizmap.export.openapi import parse_swag, plain_summary

    why = ('ArchiveProject handles POST /projects/:id/archive @Summary Archive a project @Description Keeps tasks. '
           '@Tags projects @Accept json @Param id path string true "Project ID" @Param body body dto.Archive true "Why" '
           '@Success 200 {object} models.Project @Failure 404 {object} map[string]interface{} "Not found" '
           '@Router /projects/{id}/archive [post]')
    op = parse_swag(why)
    assert op["sum"] == "Archive a project" and op["desc"] == "Keeps tasks." and op["tags"] == ["projects"]
    assert op["params"] == [["id", "path", "string", 1, "Project ID"]]
    assert op["body"] == {"r": "dto.Archive", "n": "dto.Archive"}
    assert op["resp"] == [["200", "", {"r": "models.Project", "n": "models.Project"}], ["404", "Not found", {"t": "object"}]]
    assert op["router"] == ["POST", "/projects/{id}/archive"]
    assert parse_swag("Just a comment.") is None
    assert plain_summary("Lists the projects of a client. Paginated.") == "Lists the projects of a client."


def test_book_uses_spec_then_swag_then_comment_and_groups_by_tag() -> None:
    index, chunks = _book(_demo(), specs=[("api", _demo_spec())])
    assert index["docs"]["spec"] == 7 and index["docs"]["swag"] == 2 and index["docs"]["schemas"]
    archive = _doc_of(index, chunks, "POST", "/api/v1/projects/:id/archive")
    assert archive["doc"]["src"] == "spec" and archive["doc"]["sum"] == "Archive a project"
    assert archive["doc"]["body"]["t"] == "object"
    invoice = _doc_of(index, chunks, "POST", "/invoices")
    assert invoice["doc"]["src"] == "swag" and invoice["doc"]["body"] == {"r": "billing.InvoiceRequest", "n": "billing.InvoiceRequest"}
    assert "Order" in chunks["s0"]["schemas"]
    # domains: the API's tags; untagged /orders routes join the `orders` tag
    api = next(s for s in index["services"] if s["name"] == "api")
    doms = {index["domains"][d]["name"]: index["domains"][d] for d in api["domains"]}
    assert doms["orders"]["rule"] == "tag" and doms["orders"]["routes"] == 5
    assert doms["projects"]["routes"] == 7
    billing = next(s for s in index["services"] if s["name"] == "billing")
    assert [index["domains"][d]["name"] for d in billing["domains"]][0] == "invoices"


def test_export_finds_the_spec_next_to_each_repo(tmp_path: Path) -> None:
    from bizmap.export.openapi import find_specs

    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "openapi.json").write_text(json.dumps(_demo_spec()))
    assert find_specs(tmp_path, ["api", "web"]) == {"api": tmp_path / "api" / "openapi.json"}
    html = export_graph(_demo(), "html", root=tmp_path)
    assert _blocks(html)["index"]["docs"]["spec"] == 7
    html = export_graph(_demo(), "html", openapi=[f"api={tmp_path / 'api' / 'openapi.json'}"])
    assert _blocks(html)["index"]["docs"]["spec"] == 7
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(ExportError, match="no `paths`"):
        export_graph(_demo(), "html", openapi=[str(tmp_path / "bad.json")])


def test_example_spec_is_the_demo_spec() -> None:
    stored = json.loads((ROOT / "examples" / "export" / "api" / "openapi.json").read_text(encoding="utf-8"))
    assert stored == json.loads(json.dumps(_demo_spec()))


# ── round: noise, self-calls, likely screens, counts ────────────────────────


def noisy_graph() -> dict:
    g = react_graph()
    g["nodes"] += [
        _n("web/function:src.mocks.handlers.module", "function", evidence={"file": "src/mocks/handlers.ts", "line": 1}),
        _n("web/function:cypress.e2e.orders.module", "function", evidence={"file": "cypress/e2e/orders.cy.ts", "line": 1}),
        _n("web/function:src.test-utils.render.module", "function", evidence={"file": "src/test-utils/render.tsx", "line": 1}),
        # an SDK generated into the API repo, outside any `generated/` folder, that no route reaches
        _n("api/function:sdk.src.OrdersApi.getOrder", "function", evidence={"file": "sdk/src/OrdersApi.ts", "line": 3}),
        # a page that only reaches the route through a likely (heuristic) link
        _n("web/function:src.pages.Users.UsersPage", "function", evidence={"file": "src/pages/Users.tsx", "line": 1}),
    ]
    g["edges"] += [
        _e("calls_http", "web/function:src.mocks.handlers.module", "api/entrypoint:http:GET:/orders/:id", "heuristic"),
        _e("calls_http", "web/function:cypress.e2e.orders.module", "api/entrypoint:http:GET:/orders/:id", "heuristic"),
        _e("calls_http", "web/function:src.test-utils.render.module", "api/entrypoint:http:GET:/orders/:id", "heuristic"),
        _e("calls_http", "api/function:sdk.src.OrdersApi.getOrder", "api/entrypoint:http:GET:/orders/:id", "inferred"),
        _e("calls", "web/function:src.pages.Users.UsersPage", "web/function:src.api.users.getUser"),
    ]
    return g


def test_mocks_e2e_test_utils_and_unreached_self_calls_are_noise() -> None:
    index, chunks = _book(noisy_graph())
    names = chunks["names"]["l"]
    api_dom = next(d for d in index["domains"] if index["services"][d["svc"]]["name"] == "api")
    route = chunks[api_dom["chunk"]]["routes"][0]
    assert sorted(names[c] for c, _g in route["c"]) == ["src.api.orders.getOrder", "src.api.users.getUser"]
    assert route["nh"] == 6 and route["nc"] == 2
    api = next(s for s in index["services"] if s["name"] == "api")
    # the API does not "call itself" through its SDK
    assert api.get("clients", 0) == 0 and api["hidden_calls"] == 2  # generated client + SDK
    svc = [s["name"] for s in index["services"]]
    assert [[svc[a], svc[b], n] for a, b, n in index["connections"]] == [["web", "api", 2]]


def test_a_screen_reached_only_through_a_likely_link_is_likely() -> None:
    index, chunks = _book(noisy_graph())
    api_dom = next(d for d in index["domains"] if index["services"][d["svc"]]["name"] == "api")
    route = chunks[api_dom["chunk"]]["routes"][0]
    assert sorted([f, g] for _s, f, g in route["sc"]) == [["src/pages/Orders.tsx", 0], ["src/pages/Users.tsx", 1]]


def test_ts_only_front_counts_its_files_as_linked_screens() -> None:
    index, _chunks = _book(vite_graph())
    web = next(s for s in index["services"] if s["name"] == "web")
    assert web["has_ui"] is False and web["clients_linked"] == web["clients"] == 1
