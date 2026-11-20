"""`bizmap impact`: a diff mapped onto the graph (the PR-comment Action)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.impact import MARKER, compute_impact, parse_unified_diff, render_markdown
from bizmap.scan import scan_path

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = ROOT / "examples" / "checkout"
PREFIX = "examples/checkout"


def _diff(path: str, line: int, old: str = "old", new: str = "new") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -{line},1 +{line},1 @@\n-{old}\n+{new}\n"
    )


def test_parse_unified_diff_lines() -> None:
    text = (
        "diff --git a/x.go b/x.go\n--- a/x.go\n+++ b/x.go\n"
        "@@ -10,4 +10,4 @@ func A() {\n"
        " ctx\n"
        "+added\n"
        " ctx\n"
        "-gone\n"
        " ctx\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-a\n-b\n"
        "diff --git a/new.ts b/new.ts\n--- /dev/null\n+++ b/new.ts\n"
        "@@ -0,0 +1,2 @@\n+a\n+b\n"
    )
    got = parse_unified_diff(text)
    # +added is line 11; the deletion sits between new lines 12 and 13
    assert got == {"x.go": {11, 12, 13}, "new.ts": {1, 2}}


def test_functions_carry_end_lines_and_repo_roots() -> None:
    graph = scan_path(CHECKOUT)
    assert set(graph.attrs["repo_roots"].values()) == {f"{PREFIX}/{r}" for r in ("web", "api", "billing")}
    assert graph.attrs["repo_roots_base"] == "git"
    langs = {}
    for node in graph.nodes.values():
        if node.type != "function":
            continue
        lang = node.attrs.get("language")
        end = node.attrs.get("end_line")
        start = (node.attrs.get("evidence") or {}).get("line")
        if isinstance(end, int):
            assert end >= start, node.id
            langs[lang] = langs.get(lang, 0) + 1
    assert {"go", "python", "javascript"} <= set(langs), langs
    data = graph.to_dict()
    assert data["repo_roots"] == dict(sorted(graph.attrs["repo_roots"].items()))


def test_go_client_change_reaches_route_and_billing_table() -> None:
    graph = scan_path(CHECKOUT)
    impact = compute_impact(graph, _diff(f"{PREFIX}/api/billing.go", 27), prefix=PREFIX)
    names = [f.function for f in impact.functions]
    assert len(names) == 1 and names[0].endswith("CreateInvoice"), names
    fi = impact.functions[0]
    assert any(e.endswith("entrypoint:http:POST:/api/orders") for e in fi.entrypoints), fi.entrypoints
    assert any("invoices" in t for t in fi.writes), fi.writes
    assert impact.unmapped_files == []


def test_python_and_ts_changes() -> None:
    graph = scan_path(CHECKOUT)
    diff = _diff(f"{PREFIX}/billing/app.py", 35) + _diff(f"{PREFIX}/web/src/checkout.ts", 12)
    impact = compute_impact(graph, diff, prefix=PREFIX)
    by_name = {f.function.rsplit(".", 1)[-1]: f for f in impact.functions}
    assert set(by_name) == {"payment_webhook", "placeOrder"}, list(by_name)
    hook = by_name["payment_webhook"]
    assert any(e.endswith("entrypoint:http:POST:/webhooks/payment") for e in hook.entrypoints)
    assert any("invoices" in t for t in hook.writes), hook.writes
    # the webhook calls the api, whose handler writes orders
    assert any("orders" in t for t in hook.writes), hook.writes
    place = by_name["placeOrder"]
    assert any("orders" in t for t in place.writes), place.writes


def test_lines_outside_functions_and_unknown_files() -> None:
    graph = scan_path(CHECKOUT)
    diff = _diff(f"{PREFIX}/api/billing.go", 4) + _diff("docs/readme.md", 1)
    impact = compute_impact(graph, diff, prefix=PREFIX)
    assert impact.functions == []
    assert impact.unmapped_files == ["docs/readme.md"]
    md = render_markdown(graph, impact)
    assert md.startswith(MARKER) and "No mapped function" in md


def test_markdown_and_cli(tmp_path: Path) -> None:
    graph = scan_path(CHECKOUT)
    gpath = tmp_path / "g.json"
    gpath.write_text(json.dumps(graph.to_dict()), encoding="utf-8")
    dpath = tmp_path / "d.patch"
    dpath.write_text(_diff(f"{PREFIX}/api/billing.go", 27), encoding="utf-8")
    res = CliRunner().invoke(main, ["impact", "--graph", str(gpath), "--diff", str(dpath), "--prefix", PREFIX])
    assert res.exit_code == 0, res.output
    assert res.output.startswith(MARKER)
    assert "```mermaid" in res.output and "CreateInvoice" in res.output
    assert "POST /api/orders" in res.output
    assert "invoices (amount_cents, id, order_id, status)" in res.output
    res = CliRunner().invoke(
        main, ["impact", "--graph", str(gpath), "--diff", "-", "--prefix", PREFIX, "--json"],
        input=dpath.read_text(encoding="utf-8"),
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert len(data["functions"]) == 1 and data["writes"]


EXAMPLES = ["checkout", "clinic", "pyplatform", "shop"]


def _spanless(graph) -> list[str]:
    out = []
    for node in graph.nodes.values():
        if node.type != "function":
            continue
        ev = node.attrs.get("evidence") or {}
        if not ev.get("file"):
            continue
        start = node.attrs.get("start_line") or ev.get("line")
        end = node.attrs.get("end_line")
        if not (isinstance(end, int) and isinstance(start, int) and start <= end):
            out.append(f"{node.id} ({ev.get('file')}:{ev.get('line')})")
    return sorted(out)


def test_every_function_with_evidence_has_a_span() -> None:
    """Inline handlers, closures, module pseudo-functions and SQL functions
    included: a diff inside any of them must map to a node."""
    missing = {}
    for name in EXAMPLES:
        bad = _spanless(scan_path(ROOT / "examples" / name))
        if bad:
            missing[name] = bad
    assert missing == {}, missing


def _scan(tmp_path: Path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return scan_path(tmp_path)


def test_inline_handlers_and_closures_map(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "web/server.ts": (
                "import express from 'express';\n"          # 1
                "import { db } from './db';\n"               # 2
                "const router = express.Router();\n"         # 3
                "router.get('/v1/progress', async (req, res) => {\n"  # 4
                "  const rows = await db.query('SELECT id FROM progress');\n"  # 5
                "  res.json(rows);\n"                        # 6
                "});\n"                                      # 7
                "router.post(\n"                             # 8
                "  '/v1/progress',\n"                        # 9
                "  async (req, res) => {\n"                  # 10
                "    res.json({});\n"                        # 11
                "  },\n"                                     # 12
                ");\n"                                       # 13
                "export default router;\n"                   # 14
            ),
            "web/db.ts": "export const db = { query: async (q: string) => [] };\n",
            "svc/main.go": (
                "package main\n"                             # 1
                "\n"                                         # 2
                'import "github.com/labstack/echo/v4"\n'     # 3
                "\n"                                         # 4
                "func main() {\n"                            # 5
                "\te := echo.New()\n"                        # 6
                '\te.GET("/x", func(c echo.Context) error {\n'  # 7
                '\t\treturn c.String(200, "ok")\n'           # 8
                "\t})\n"                                     # 9
                '\te.Start(":8080")\n'                       # 10
                "}\n"                                        # 11
            ),
            "svc/go.mod": "module svc\n",
            "db/migrations/0001.sql": (
                "CREATE TABLE progress (id int primary key, n int);\n"  # 1
                "CREATE FUNCTION bump() RETURNS trigger AS $$\n"         # 2
                "BEGIN\n"                                                # 3
                "  UPDATE progress SET n = n + 1;\n"                     # 4
                "  RETURN NEW;\n"                                        # 5
                "END;\n"                                                 # 6
                "$$ LANGUAGE plpgsql;\n"                                 # 7
            ),
        },
    )
    assert _spanless(graph) == []

    def hit(path: str, line: int) -> list[str]:
        return [f.function for f in compute_impact(graph, _diff(path, line)).functions]

    assert any("GET /v1/progress" in f for f in hit("web/server.ts", 6)), hit("web/server.ts", 6)
    assert any("POST /v1/progress" in f for f in hit("web/server.ts", 11)), hit("web/server.ts", 11)
    go = hit("svc/main.go", 8)
    assert go and all("main" in f for f in go), go
    assert hit("db/migrations/0001.sql", 4) == ["function:sql.bump"], hit("db/migrations/0001.sql", 4)


def test_comment_stays_under_github_limit() -> None:
    """A huge diff: rows capped, quiet functions summarized, size < max_chars."""
    from bizmap.graph import Graph
    from bizmap.impact import FunctionImpact, Impact

    graph = Graph()
    fns = []
    for i in range(1300):
        graph.add_node(f"function:pkg.mod.fn_{i:04d}_" + "x" * 40, "function", name=f"pkg.mod.fn_{i:04d}_" + "x" * 40)
        writes = [f"column:t{i % 30}.c{j}" for j in range(20)] if i % 3 == 0 else []
        fns.append(FunctionImpact(function=f"function:pkg.mod.fn_{i:04d}_" + "x" * 40, file="a.py", lines=[1], writes=writes))
    impact = Impact(functions=fns, unmapped_files=[])
    md = render_markdown(graph, impact, details_url="https://example.invalid/run/1")
    assert len(md) < 60000, len(md)
    assert md.count("\n| `") <= 50
    assert "changed function(s) reach no route or table" in md
    assert "more function(s) with impact not shown" in md
    assert "https://example.invalid/run/1" in md
    tiny = render_markdown(graph, impact, max_chars=3000)
    assert len(tiny) <= 3000 and tiny.startswith(MARKER)


def test_quoted_non_ascii_paths() -> None:
    d = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -3,0 +4,1 @@\n+x\n'
    )
    assert parse_unified_diff(d) == {"café.py": {4}}


def test_registry_merge_keeps_roots(tmp_path: Path) -> None:
    """per-repo scans + `registry merge` map a diff like the joint scan."""
    runner = CliRunner()
    parts = []
    for repo in ("web", "api", "billing"):
        out = tmp_path / f"{repo}.json"
        assert runner.invoke(main, ["scan", str(CHECKOUT / repo), "--out", str(out)]).exit_code == 0
        parts.append(str(out))
    merged = tmp_path / "merged.json"
    res = runner.invoke(main, ["registry", "merge", *parts, "--out", str(merged)])
    assert res.exit_code == 0, res.output
    from bizmap.registry import graph_from_json

    g = graph_from_json(json.loads(merged.read_text(encoding="utf-8")))
    diff = _diff(f"{PREFIX}/api/billing.go", 27) + _diff(f"{PREFIX}/billing/app.py", 35)
    joint = compute_impact(scan_path(CHECKOUT), diff)
    via_merge = compute_impact(g, diff)
    assert [f.function for f in via_merge.functions] == [f.function for f in joint.functions]
    assert len(joint.functions) == 2


def test_hint_when_nothing_maps(tmp_path: Path) -> None:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    graph = scan_path(tmp_path / "svc")  # no git: roots relative to the scan path
    assert graph.attrs["repo_roots_base"] == "scan"
    miss = compute_impact(graph, _diff("svc/app.py", 2))
    assert miss.functions == [] and miss.hint and "--prefix" in miss.hint
    hit = compute_impact(graph, _diff("svc/app.py", 2), prefix="svc")
    assert len(hit.functions) == 1 and hit.hint is None


def test_factory_members_get_their_own_routes(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "api/server.ts": (
                "import express from 'express';\nconst app = express();\n"
                "app.get('/orders/:id', (req, res) => res.json({}));\n"
                "app.get('/inventory/:id', (req, res) => res.json({}));\n"
                "app.post('/settings/users', (req, res) => res.json({}));\n"
            ),
            "web/src/clients/shop.ts": (
                "import axios, { AxiosInstance } from 'axios';\n"            # 1
                "export function createShopClient(http: AxiosInstance) {\n"  # 2
                "  const unused = 1;\n"                                      # 3
                "  return {\n"                                               # 4
                "    order: (id: string) => http.get(`/orders/${id}`),\n"    # 5
                "    stock(id: string) {\n"                                  # 6
                "      return http.get(`/inventory/${id}`);\n"               # 7
                "    },\n"                                                   # 8
                "    addUser: () => http.post('/settings/users', {}),\n"     # 9
                "  };\n"                                                     # 10
                "}\n"                                                        # 11
            ),
        },
    )

    def routes(line: int) -> set[str]:
        p = "web/src/clients/shop.ts"
        added = f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -{line},0 +{line},1 @@\n+x\n"
        imp = compute_impact(graph, added)
        return {
            t.split(":", 3)[-1]
            for f in imp.functions
            for e in graph.edges.values()
            if e.type == "calls_http" and e.from_id == f.function
            for t in [e.to_id]
        }, [f.function for f in imp.functions]

    got5, fns5 = routes(5)
    assert len(fns5) == 1 and fns5[0].endswith("createShopClient.order"), fns5
    assert got5 == {"/orders/:id"}, got5
    got9, fns9 = routes(9)
    assert len(fns9) == 1 and fns9[0].endswith("createShopClient.addUser"), fns9
    assert got9 == {"/settings/users"}, got9
    # a method definition is its own function already: only its route
    got7, fns7 = routes(7)
    assert len(fns7) == 1 and got7 == {"/inventory/:id"}, (fns7, got7)
    _got, fns3 = routes(3)
    assert len(fns3) == 1 and fns3[0].endswith("createShopClient"), fns3
    # the factory still reaches its members' routes through `calls`
    factory = fns3[0]
    members = {e.to_id for e in graph.edges.values() if e.type == "calls" and e.from_id == factory}
    assert {m.rsplit(".", 1)[-1] for m in members} >= {"order", "addUser"}, members


FACTORY = (
    "import { AxiosInstance } from 'axios';\n"                 # 1
    "export function createShopClient(http: AxiosInstance) {\n"  # 2
    "  return {\n"                                               # 3
    "    order: (id: string) =>\n"                               # 4
    "      http.get(`/orders/${id}`),\n"                         # 5
    "    stock: (id: string) =>\n"                               # 6
    "      http.get(`/inventory/${id}`),\n"                      # 7
    "    addUser: () =>\n"                                       # 8
    "      http.post('/settings/users', {}),\n"                  # 9
    "  };\n"                                                     # 10
    "}\n"                                                        # 11
)


def _git_diff(tmp_path: Path, old: str, new: str) -> tuple[str, object]:
    import subprocess

    rel = "web/src/clients/shop.ts"
    for base, body in (("old", old), ("repo", new)):
        p = tmp_path / base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        ["git", "diff", "--no-index", "--unified=0", f"old/{rel}", f"repo/{rel}"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    return proc.stdout, scan_path(tmp_path / "repo")


def _members_hit(tmp_path: Path, new: str) -> list[str]:
    diff, graph = _git_diff(tmp_path, FACTORY, new)
    imp = compute_impact(graph, diff, prefix="repo")
    return sorted(f.function.rsplit(".", 1)[-1] for f in imp.functions)


def test_unified0_replace_first_line_of_member(tmp_path: Path) -> None:
    new = FACTORY.replace("    stock: (id: string) =>", "    stock: (sku: string) =>")
    assert _members_hit(tmp_path, new) == ["stock"]


def test_unified0_replace_last_line_of_member(tmp_path: Path) -> None:
    new = FACTORY.replace("`/inventory/${id}`", "`/inventory/${id}/v2`")
    assert _members_hit(tmp_path, new) == ["stock"]


def test_unified0_delete_last_member(tmp_path: Path) -> None:
    new = FACTORY.replace("    addUser: () =>\n      http.post('/settings/users', {}),\n", "")
    # the gap is between stock's last line and the object's closing line: the
    # factory owns it, not both remaining members
    hit = _members_hit(tmp_path, new)
    assert "order" not in hit and hit in (["createShopClient"], ["createShopClient", "stock"]), hit


def test_unified0_delete_first_member(tmp_path: Path) -> None:
    new = FACTORY.replace("    order: (id: string) =>\n      http.get(`/orders/${id}`),\n", "")
    hit = _members_hit(tmp_path, new)
    assert "stock" not in hit or hit == ["createShopClient", "stock"], hit
    assert "addUser" not in hit and "createShopClient" in hit, hit


def test_marker_survives_tiny_cap() -> None:
    from bizmap.graph import Graph
    from bizmap.impact import FunctionImpact, Impact

    graph = Graph()
    graph.add_node("function:a." + "x" * 500, "function", name="a." + "x" * 500)
    imp = Impact(functions=[FunctionImpact(function="function:a." + "x" * 500, file="a.py", lines=[1], writes=["table:t"])],
                 unmapped_files=[])
    md = render_markdown(graph, imp, max_chars=200)
    assert md.startswith(MARKER) and "### BizMap impact" in md and "truncated" in md


TWO_FACTORIES = {
    "api/server.ts": (
        "import express from 'express';\nconst app = express();\n"
        "app.get('/orders/:id', (req, res) => res.json({}));\n"
        "app.get('/inventory/:id', (req, res) => res.json({}));\n"
        "app.get('/settings/users/:id', (req, res) => res.json({}));\n"
    ),
    "web/c.ts": (
        "export function createAClient(http) {\n"                       # 1
        "  function build(id) {\n"                                      # 2
        "    return http.get(`/orders/${id}`);\n"                       # 3
        "  }\n"                                                         # 4
        "  return { one: (id) => build(id) };\n"                        # 5
        "}\n"                                                           # 6
        "export function createBClient(http) {\n"                       # 7
        "  function build(id) {\n"                                      # 8
        "    return http.get(`/inventory/${id}`);\n"                    # 9
        "  }\n"                                                         # 10
        "  const loadUser = (id) => http.get(`/settings/users/${id}`);\n"  # 11
        "  return {\n"                                                  # 12
        "    one: (id) => build(id),\n"                                 # 13
        "    addUser: (id) => loadUser(id),\n"                          # 14
        "    external: () => http.get('/vendor/ping'),\n"               # 15
        "  };\n"                                                        # 16
        "}\n"                                                           # 17
    ),
}


def _added(path: str, line: int) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{line},0 +{line},1 @@\n+x\n"


def _http_targets(graph, fid: str) -> set[str]:
    return {e.to_id.split(":", 3)[-1] for e in graph.edges.values() if e.type == "calls_http" and e.from_id == fid}


def test_nested_helpers_are_scoped_per_factory(tmp_path: Path) -> None:
    graph = _scan(tmp_path, TWO_FACTORIES)
    a_build, b_build = "function:web.c.createAClient.build", "function:web.c.createBClient.build"
    assert a_build in graph.nodes and b_build in graph.nodes, sorted(n for n in graph.nodes if "build" in n)
    assert _http_targets(graph, a_build) == {"/orders/:id"}
    assert _http_targets(graph, b_build) == {"/inventory/:id"}
    imp = compute_impact(graph, _added("web/c.ts", 9))
    assert [f.function for f in imp.functions] == [b_build]


def test_members_call_local_helpers(tmp_path: Path) -> None:
    graph = _scan(tmp_path, TWO_FACTORIES)
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    assert ("function:web.c.createBClient.one", "function:web.c.createBClient.build") in calls, calls
    assert ("function:web.c.createBClient.addUser", "function:web.c.createBClient.loadUser") in calls, calls
    assert ("function:web.c.createAClient.one", "function:web.c.createAClient.build") in calls, calls
    # the member reaches its helper's route through `calls`
    imp = compute_impact(graph, _added("web/c.ts", 14))
    assert [f.function for f in imp.functions] == ["function:web.c.createBClient.addUser"]
    graph_eps = imp.functions[0]
    reach = set()
    for e in graph.edges.values():
        if e.type == "calls" and e.from_id == graph_eps.function:
            reach |= _http_targets(graph, e.to_id)
    assert reach == {"/settings/users/:id"}, reach


def test_unresolved_http_sits_on_the_member(tmp_path: Path) -> None:
    graph = _scan(tmp_path, TWO_FACTORIES)
    ext = graph.nodes.get("function:web.c.createBClient.external")
    assert ext is not None and any("/vendor/ping" in str(u) for u in ext.attrs.get("unresolved_http") or []), (
        ext and ext.attrs
    )
    factory = graph.nodes["function:web.c.createBClient"]
    assert not any("/vendor/ping" in str(u) for u in factory.attrs.get("unresolved_http") or [])


def test_go_inline_closure_handler(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/go.mod": "module svc\n\ngo 1.22\n",
            "svc/schema.sql": "CREATE TABLE orders (id int primary key, status text);\n",
            "svc/main.go": (
                "package main\n"                                                   # 1
                "\n"                                                               # 2
                "import (\n"                                                       # 3
                '\t"database/sql"\n'                                               # 4
                '\t"github.com/labstack/echo/v4"\n'                                # 5
                ")\n"                                                              # 6
                "\n"                                                               # 7
                "func main() {\n"                                                  # 8
                "\tvar db *sql.DB\n"                                               # 9
                "\te := echo.New()\n"                                              # 10
                '\te.GET("/shipments", func(c echo.Context) error {\n'             # 11
                '\t\trows, _ := db.Query("SELECT id, status FROM orders")\n'       # 12
                "\t\tdefer rows.Close()\n"                                         # 13
                '\t\treturn c.String(200, "ok")\n'                                 # 14
                "\t})\n"                                                           # 15
                '\te.Start(":8080")\n'                                             # 16
                "}\n"                                                              # 17
            ),
        },
    )
    ep = "entrypoint:http:GET:/shipments"
    trig = [e.to_id for e in graph.edges.values() if e.type == "triggers" and e.from_id == ep]
    assert len(trig) == 1 and trig[0].endswith("main.func1"), trig
    closure = trig[0]
    reads = {e.to_id for e in graph.edges.values() if e.type == "reads" and e.from_id == closure}
    assert any("orders" in r for r in reads), reads
    main_reads = {e.to_id for e in graph.edges.values() if e.type == "reads" and e.from_id == "function:main.main"}
    assert not any("orders" in r for r in main_reads), main_reads
    # rolled up onto the route, like a named handler
    ep_reads = {e.to_id for e in graph.edges.values() if e.type == "reads" and e.from_id == ep}
    assert any(r.endswith("table:orders") for r in ep_reads), ep_reads
    imp = compute_impact(graph, _added("svc/main.go", 13))
    assert [f.function for f in imp.functions] == [closure]
    assert imp.functions[0].entrypoints == [ep]
    assert any("orders" in r for r in imp.functions[0].reads)


def test_no_hint_for_docs_only_diffs() -> None:
    graph = scan_path(CHECKOUT)
    imp = compute_impact(graph, _diff("README.md", 3) + _diff(".github/workflows/x.yml", 1))
    assert imp.functions == [] and imp.hint is None


def test_module_and_imported_function_calls(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/server.ts": (
                "import express from 'express';\n"
                "import { helperTop } from './lib';\n"
                "import fetchStock from './stock';\n"
                "const app = express();\n"
                "function local() { return 1; }\n"
                "function helperOuter() { local(); return helperTop(); }\n"
                "export function makeB(http) {\n"
                "  return { run: () => build(), inv: () => fetchStock() };\n"
                "}\n"
                "function build() { return 2; }\n"
                "app.get('/x', (req, res) => res.json(helperOuter()));\n"
            ),
            "svc/lib.ts": "import axios from 'axios';\nexport function helperTop() { return axios.get('http://svc/other'); }\n",
            "svc/stock.ts": "export default function fetchStock() { return 3; }\n",
        },
    )
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    assert ("function:svc.server.helperOuter", "function:svc.lib.helperTop") in calls, calls
    assert ("function:svc.server.helperOuter", "function:svc.server.local") in calls, calls
    assert ("function:svc.server.makeB.run", "function:svc.server.build") in calls, calls
    assert ("function:svc.server.makeB.inv", "function:svc.stock.fetchStock") in calls, calls
    # no edges to things that are not scanned functions
    assert not [c for c in calls if c[1].startswith("function:express")], calls


def test_method_members_are_called_by_the_factory(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "web/c.ts": (
                "export function createShopClient(http) {\n"
                "  return {\n"
                "    stock(sku) { return http.get(`/inventory/${sku}`); },\n"
                "    order: (id) => http.get(`/orders/${id}`),\n"
                "  };\n"
                "}\n"
            ),
        },
    )
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    f = "function:web.c.createShopClient"
    assert (f, f + ".stock") in calls and (f, f + ".order") in calls, calls


def test_literal_route_beats_param_route(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "api/server.ts": (
                "import express from 'express';\nconst app = express();\n"
                "app.get('/shops/points', (req, res) => res.json({}));\n"
                "app.get('/shops/:shop_id', (req, res) => res.json({}));\n"
            ),
            "web/c.ts": (
                "import axios from 'axios';\n"
                "export const points = () => axios.get('/shops/points');\n"
                "export const one = (id) => axios.get(`/shops/${id}`);\n"
                "export const other = () => axios.get('/shops/main');\n"
            ),
        },
    )
    assert _http_targets(graph, "function:web.c.points") == {"/shops/points"}
    assert _http_targets(graph, "function:web.c.one") == {"/shops/:shop_id"}
    assert _http_targets(graph, "function:web.c.other") == {"/shops/:shop_id"}


def _calls_from(graph, prefix: str) -> set[tuple[str, str]]:
    return {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls" and e.from_id.startswith(prefix)}


SHADOW_BASE = "function formatLabel(x) { return x }\nfunction pick() { return null }\n"


def _shadow_calls(tmp_path: Path, body: str) -> set[tuple[str, str]]:
    graph = _scan(tmp_path, {"svc/util.ts": SHADOW_BASE + body})
    return {c for c in _calls_from(graph, "function:svc.util.render") if c[1].endswith("formatLabel")}


def test_param_shadows_module_function(tmp_path: Path) -> None:
    assert _shadow_calls(tmp_path, "export function render(formatLabel) { return formatLabel(1) }\n") == set()


def test_let_shadows_module_function(tmp_path: Path) -> None:
    body = "export function render() { let formatLabel = pick(); return formatLabel(1) }\n"
    assert _shadow_calls(tmp_path, body) == set()


def test_var_in_block_shadows_module_function(tmp_path: Path) -> None:
    body = "export function render() { if (1) { var formatLabel = pick(); } return formatLabel(1) }\n"
    assert _shadow_calls(tmp_path, body) == set()


def test_arrow_param_shadows_module_function(tmp_path: Path) -> None:
    body = "export function render(list) { return list.map(formatLabel => formatLabel(1)) }\n"
    assert _shadow_calls(tmp_path, body) == set()


def test_destructured_param_shadows_module_function(tmp_path: Path) -> None:
    body = "export function render({ formatLabel }) { return formatLabel(1) }\n"
    assert _shadow_calls(tmp_path, body) == set()


def test_catch_param_shadows_module_function(tmp_path: Path) -> None:
    body = "export function render() { try { return 1 } catch (formatLabel) { return formatLabel(1) } }\n"
    assert _shadow_calls(tmp_path, body) == set()


def test_unshadowed_module_function_still_links(tmp_path: Path) -> None:
    body = "export function render(other) { return formatLabel(other) }\n"
    assert _shadow_calls(tmp_path, body) == {("function:svc.util.render", "function:svc.util.formatLabel")}


def test_imports_through_barrels_namespace_default_and_require(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/lib/top.ts": "export function top() { return 1 }\n",
            "svc/lib/other.ts": "export function other() { return 2 }\n",
            "svc/lib/index.ts": "export { top } from './top';\nexport * from './other';\n",
            "svc/handler.ts": "const handler = () => 3;\nexport default handler;\n",
            "svc/cjs.js": "function a() { return 4 }\nfunction b() { return 5 }\nmodule.exports = { a, b };\n",
            "svc/app.ts": (
                "import { top, other } from './lib';\n"
                "import * as ns from './lib/top';\n"
                "import h from './handler';\n"
                "const { a } = require('./cjs');\n"
                "export function useAll() { top(); other(); ns.top(); h(); a(); }\n"
            ),
        },
    )
    got = {t for f, t in _calls_from(graph, "function:svc.app.useAll")}
    assert got == {
        "function:svc.lib.top.top",
        "function:svc.lib.other.other",
        "function:svc.handler.handler",
        "function:svc.cjs.a",
    }, got


def test_go_closure_numbering_ignores_nested_literals(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/go.mod": "module svc\n\ngo 1.22\n",
            "svc/main.go": (
                "package main\n\n"
                'import "github.com/labstack/echo/v4"\n\n'
                "func main() {\n"
                "\te := echo.New()\n"
                '\te.GET("/a", func(c echo.Context) error { return nil })\n'
                '\te.GET("/b", func(c echo.Context) error {\n'
                "\t\tf := func() {}\n"
                "\t\tf()\n"
                "\t\treturn nil\n"
                "\t})\n"
                '\te.GET("/c", func(c echo.Context) error { return nil })\n'
                "}\n"
            ),
        },
    )

    def handler(path: str) -> str:
        ep = f"entrypoint:http:GET:{path}"
        return next(e.to_id for e in graph.edges.values() if e.type == "triggers" and e.from_id == ep)

    assert handler("/a").endswith("main.func1")
    assert handler("/b").endswith("main.func2")
    assert handler("/c").endswith("main.func3"), handler("/c")


def test_spans_post_passes_scale_linearly() -> None:
    """The graph post-passes index edges once (no per-node edge scans)."""
    import time

    from bizmap.graph import Evidence, Graph
    from bizmap.spans import _OutIndex, _table_hits

    graph = Graph()
    n = 4000
    for i in range(n):
        graph.add_node(f"function:f{i}", "function", name=f"f{i}")
    graph.add_node("table:orders", "table", name="orders")
    for i in range(n - 1):
        graph.add_edge("calls", f"function:f{i}", f"function:f{i + 1}", evidence=Evidence(file="a.go", line=i, symbol=f"f{i}"))
    graph.add_edge("reads", f"function:f{n - 1}", "table:orders")
    index = _OutIndex(graph)
    memo: dict = {}
    t0 = time.perf_counter()
    for start in range(0, n, 10):
        assert _table_hits(graph, index, f"function:f{start}", memo) == {("reads", "table:orders")}
    assert time.perf_counter() - t0 < 20


def test_table_hits_with_cycles() -> None:
    from bizmap.graph import Graph
    from bizmap.spans import _OutIndex, _table_hits

    g = Graph()
    for n in ("a", "b", "c", "d"):
        g.add_node(f"function:{n}", "function", name=n)
    g.add_node("table:t1", "table", name="t1")
    g.add_node("table:t2", "table", name="t2")
    g.add_edge("calls", "function:a", "function:b")
    g.add_edge("calls", "function:b", "function:c")
    g.add_edge("calls", "function:c", "function:a")  # cycle a→b→c→a
    g.add_edge("calls", "function:c", "function:d")
    g.add_edge("reads", "function:b", "table:t1")
    g.add_edge("writes", "function:d", "table:t2")
    idx, memo = _OutIndex(g), {}
    want = {("reads", "table:t1"), ("writes", "table:t2")}
    assert _table_hits(g, idx, "function:a", memo) == want
    assert _table_hits(g, idx, "function:c", memo) == want
    assert _table_hits(g, idx, "function:d", memo) == {("writes", "table:t2")}


def test_local_helper_in_anonymous_callback(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/v.ts": (
                "declare const schema: { refine(cb: (p: any, ctx: any) => void): void };\n"
                "schema.refine((p, ctx) => {\n"
                "  const report = (m: string) => ctx.add(m);\n"
                "  const checkFields = (who: string) => { if (p.a) report(who); };\n"
                "  checkFields('x');\n"
                "});\n"
            ),
        },
    )
    calls = {(e.from_id.rsplit(".", 1)[-1], e.to_id.rsplit(".", 1)[-1]) for e in graph.edges.values() if e.type == "calls"}
    assert ("checkFields", "report") in calls, calls


def test_local_helper_in_object_literal_method_with_try_catch(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/s.ts": (
                "declare function serveHttp(h: any): void;\n"
                "declare function makeStream(o: any): void;\n"
                "serveHttp(async (req: any) => {\n"
                "  makeStream({ async start(c: any) {\n"
                "    const push = (f: string) => { try { c.enqueue(f) } catch {} };\n"
                "    try { push('a') } catch (e) { push('err') }\n"
                "  } });\n"
                "});\n"
            ),
        },
    )
    calls = {(e.from_id.rsplit(".", 1)[-1], e.to_id.rsplit(".", 1)[-1]) for e in graph.edges.values() if e.type == "calls"}
    assert ("start", "push") in calls, calls


def test_local_non_function_binding_still_shadows(tmp_path: Path) -> None:
    body = "export function render(list) { const formatLabel = list[0]; return formatLabel(1) }\n"
    assert _shadow_calls(tmp_path, body) == set()


def test_ambiguous_export_star_is_not_linked(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/amb1.ts": "export function dup() { return 1 }\nexport function one() { return 1 }\n",
            "svc/amb2.ts": "export function dup() { return 2 }\n",
            "svc/index.ts": "export * from './amb1';\nexport * from './amb2';\n",
            "svc/app.ts": "import { dup, one } from './index';\nexport function use() { dup(); one(); }\n",
        },
    )
    got = {t for f, t in _calls_from(graph, "function:svc.app.use")}
    assert got == {"function:svc.amb1.one"}, got


def test_tsconfig_paths_relative_to_config_and_scoped(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "apps/web/tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}',
            "apps/web/src/lib/fmt.ts": "export function fmt() { return 1 }\n",
            "apps/web/src/page.ts": "import { fmt } from '@/lib/fmt';\nexport function page() { return fmt() }\n",
            "apps/admin/tsconfig.json": '{"extends": "./tsconfig.base.json"}',
            "apps/admin/tsconfig.base.json": '{"compilerOptions": {"paths": {"@/*": ["./app/*"]}}}',
            "apps/admin/app/lib/fmt.ts": "export function fmt() { return 2 }\n",
            "apps/admin/app/page.ts": "import { fmt } from '@/lib/fmt';\nexport function page() { return fmt() }\n",
            "apps/shop/vite.config.ts": (
                "import path from 'path';\nexport default { resolve: { alias: { '@': path.resolve(__dirname, './src') } } };\n"
            ),
            "apps/shop/src/util/fmt.ts": "export function fmt() { return 3 }\n",
            "apps/shop/src/page.ts": "import { fmt } from '@/util/fmt';\nexport function page() { return fmt() }\n",
        },
    )
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    assert ("function:apps.web.src.page.page", "function:apps.web.src.lib.fmt.fmt") in calls, calls
    assert ("function:apps.admin.app.page.page", "function:apps.admin.app.lib.fmt.fmt") in calls, calls
    assert ("function:apps.shop.src.page.page", "function:apps.shop.src.util.fmt.fmt") in calls, calls


def _use_calls(tmp_path: Path, files: dict[str, str], fn: str = "function:svc.use.u") -> set[str]:
    graph = _scan(tmp_path, files)
    return {t for f, t in _calls_from(graph, fn)}


def test_named_reexport_barrel_file(tmp_path: Path) -> None:
    got = _use_calls(
        tmp_path,
        {
            "svc/a.ts": "export function alpha() { return 1 }\n",
            "svc/index.ts": "export { alpha } from './a';\n",
            "svc/use.ts": "import { alpha } from './index';\nexport function u() { return alpha() }\n",
        },
    )
    assert got == {"function:svc.a.alpha"}, got


def test_named_reexport_directory_barrel(tmp_path: Path) -> None:
    got = _use_calls(
        tmp_path,
        {
            "svc/lib/top.ts": "export function top() { return 1 }\n",
            "svc/lib/index.ts": "export { top } from './top';\n",
            "svc/use.ts": "import { top } from './lib';\nexport function u() { return top() }\n",
        },
    )
    assert got == {"function:svc.lib.top.top"}, got


def test_renamed_reexport(tmp_path: Path) -> None:
    got = _use_calls(
        tmp_path,
        {
            "svc/a.ts": "export function alpha() { return 1 }\n",
            "svc/index.ts": "export { alpha as renamed } from './a';\n",
            "svc/use.ts": "import { renamed } from './index';\nexport function u() { return renamed() }\n",
        },
    )
    assert got == {"function:svc.a.alpha"}, got


def test_mixed_barrel_named_and_star(tmp_path: Path) -> None:
    got = _use_calls(
        tmp_path,
        {
            "svc/a.ts": "export function alpha() { return 1 }\n",
            "svc/b.ts": "export function gamma() { return 2 }\n",
            "svc/index.ts": "export { alpha } from './a';\nexport * from './b';\n",
            "svc/use.ts": "import { alpha, gamma } from './index';\nexport function u() { alpha(); gamma() }\n",
        },
    )
    assert got == {"function:svc.a.alpha", "function:svc.b.gamma"}, got


def test_vite_array_alias_and_multi_target_paths(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "web/vite.config.ts": (
                "import path from 'path';\n"
                "export default { resolve: { alias: [{ find: '@', replacement: path.resolve(__dirname, 'src') }] } };\n"
            ),
            "web/src/util/fmt.ts": "export function fmt() { return 1 }\n",
            "web/src/page.ts": "import { fmt } from '@/util/fmt';\nexport function page() { return fmt() }\n",
            "api/tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["src/*", "generated/*"]}}}',
            "api/src/own.ts": "export function own() { return 1 }\n",
            "api/generated/client.ts": "export function gen() { return 2 }\n",
            "api/src/use.ts": (
                "import { own } from '~/own';\nimport { gen } from '~/client';\n"
                "export function u() { own(); gen() }\n"
            ),
        },
    )
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    assert ("function:web.src.page.page", "function:web.src.util.fmt.fmt") in calls, calls
    assert ("function:api.src.use.u", "function:api.src.own.own") in calls, calls
    assert ("function:api.src.use.u", "function:api.generated.client.gen") in calls, calls


def test_const_function_expression_is_a_node(tmp_path: Path) -> None:
    got = _use_calls(
        tmp_path,
        {"svc/use.ts": "const fe = function (n: number) { return n };\nexport function u() { return fe(1) }\n"},
    )
    assert got == {"function:svc.use.fe"}, got


def test_function_expression_named_by_binding(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/use.ts": (
                "const topNamed = function topN() { return 1 };\n"
                "export function u() { return topNamed() }\n"
                "export function v() { return topN() }\n"
                "export function w() {\n"
                "  const inner = function innerN() { return 2 };\n"
                "  return inner();\n"
                "}\n"
                "export function x() { return innerN() }\n"
            ),
        },
    )
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    assert ("function:svc.use.u", "function:svc.use.topNamed") in calls, calls
    assert ("function:svc.use.w", "function:svc.use.w.inner") in calls, calls
    assert not [c for c in calls if c[0] in {"function:svc.use.v", "function:svc.use.x"}], calls
    assert "function:svc.use.topN" not in graph.nodes


def test_same_inner_name_function_expressions_are_two_nodes(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/use.ts": (
                "export function u() {\n"
                "  const a1 = function dupn() { return 1 };\n"
                "  const a2 = function dupn() { return 2 };\n"
                "  return a1() + a2();\n"
                "}\n"
            ),
        },
    )
    got = {t for f, t in _calls_from(graph, "function:svc.use.u")}
    assert got == {"function:svc.use.u.a1", "function:svc.use.u.a2"}, got


def test_generators_are_nodes(tmp_path: Path) -> None:
    got = _use_calls(
        tmp_path,
        {
            "svc/use.ts": (
                "function* gen() { yield 1 }\n"
                "const g2 = function* () { yield 2 };\n"
                "export function u() { gen(); g2() }\n"
            )
        },
    )
    assert got == {"function:svc.use.gen", "function:svc.use.g2"}, got


def test_vite_regex_find_and_root_string(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "a/vite.config.ts": (
                "import path from 'path';\n"
                "export default { resolve: { alias: [{ find: /^@\\//, replacement: path.resolve(__dirname, 'src') + '/' }] } };\n"
            ),
            "a/src/fmt.ts": "export function fmt() { return 1 }\n",
            "a/src/page.ts": "import { fmt } from '@/fmt';\nexport function page() { return fmt() }\n",
            "b/vite.config.ts": "export default { resolve: { alias: { '~': '/src' } } };\n",
            "b/src/fmt.ts": "export function fmt() { return 2 }\n",
            "b/src/page.ts": "import { fmt } from '~/fmt';\nexport function page() { return fmt() }\n",
        },
    )
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    assert ("function:a.src.page.page", "function:a.src.fmt.fmt") in calls, calls
    assert ("function:b.src.page.page", "function:b.src.fmt.fmt") in calls, calls


def test_base_tsconfig_only_reaches_extending_apps(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "tsconfig.base.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@shared/*": ["libs/shared/*"]}}}',
            "libs/shared/fmt.ts": "export function fmt() { return 1 }\n",
            "apps/web/tsconfig.json": '{"extends": "../../tsconfig.base.json"}',
            "apps/web/page.ts": "import { fmt } from '@shared/fmt';\nexport function page() { return fmt() }\n",
            "apps/solo/tsconfig.json": '{"compilerOptions": {}}',
            "apps/solo/page.ts": "import { fmt } from '@shared/fmt';\nexport function page() { return fmt() }\n",
        },
    )
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    assert ("function:apps.web.page.page", "function:libs.shared.fmt.fmt") in calls, calls
    assert not [c for c in calls if c[0] == "function:apps.solo.page.page"], calls


def test_js_ts_why_from_jsdoc_and_line_comments(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/schema.sql": "CREATE TABLE orders (id int primary key, status text);\n",
            "svc/orders.ts": (
                "import { db } from './db';\n"
                "/** Marks an order as paid. */\n"
                "export async function markPaid(id: number) {\n"
                "  await db.query('UPDATE orders SET status = $1 WHERE id = $2', ['paid', id]);\n"
                "}\n"
                "\n"
                "/**\n"
                " * Cancels an order.\n"
                " * Only pending orders can be cancelled.\n"
                " * @param id order id\n"
                " */\n"
                "export const cancel = async (id: number) => id;\n"
                "\n"
                "// Restocks the inventory\n"
                "// after a cancellation.\n"
                "const restock = function () { return 1 };\n"
                "\n"
                "// detached comment\n"
                "\n"
                "export function noDoc() { return 2 }\n"
                "\n"
                "export class Orders {\n"
                "  /** Lists open orders. */\n"
                "  list() { return [] }\n"
                "}\n"
            ),
            "svc/db.ts": "export const db = { query: async (q: string, a?: unknown[]) => [] };\n",
            "svc/server.ts": (
                "import express from 'express';\nimport { markPaid } from './orders';\n"
                "const app = express();\n"
                "app.post('/orders/:id/paid', async (req, res) => { await markPaid(1); res.json({}); });\n"
            ),
        },
    )

    def why(name: str) -> str | None:
        return graph.nodes[f"function:svc.orders.{name}"].attrs.get("why")

    assert why("markPaid") == "Marks an order as paid."
    assert why("cancel") == "Cancels an order. Only pending orders can be cancelled."
    assert why("restock") == "Restocks the inventory after a cancellation."
    assert why("noDoc") is None
    assert why("list") == "Lists open orders."
    out = tmp_path / "g.json"
    out.write_text(json.dumps(graph.to_dict()), encoding="utf-8")
    res = CliRunner().invoke(main, ["why", "orders.status", "--graph", str(out)])
    assert res.exit_code == 0, res.output
    assert "Marks an order as paid." in res.output, res.output


SHOP = {
    "shop/schema.sql": (
        "CREATE TABLE orders (\n"            # 1
        "  id int primary key,\n"            # 2
        "  status text,\n"                   # 3
        "  discount_pct numeric\n"           # 4
        ");\n"                               # 5
        "CREATE TABLE inventory (sku text primary key, qty int);\n"  # 6
        "ALTER TABLE orders ADD COLUMN note text;\n"                 # 7
    ),
    "shop/src/db.ts": "export const db = { query: async (q: string, a?: unknown[]) => [] };\n",
    "shop/src/orders.ts": (
        "import { db } from './db';\n"                                                       # 1
        "export async function cancelOrder(id: number) {\n"                                  # 2
        "  await db.query('UPDATE orders SET status = $1 WHERE id = $2', ['cancelled', id]);\n"  # 3
        "  await db.query('UPDATE inventory SET qty = qty + 1 WHERE sku = $1', ['x']);\n"   # 4
        "}\n"                                                                                # 5
    ),
    "shop/src/server.ts": (
        "import express from 'express';\n"                                                   # 1
        "import { cancelOrder } from './orders';\n"                                          # 2
        "const app = express();\n"                                                           # 3
        "app.post('/orders/:id/cancel', async (req, res) => {\n"                             # 4
        "  await cancelOrder(Number(req.params.id));\n"                                      # 5
        "  res.json({ ok: true });\n"                                                        # 6
        "});\n"                                                                              # 7
    ),
}


def test_pass_through_handler_is_folded_into_its_callee(tmp_path: Path) -> None:
    graph = _scan(tmp_path, SHOP)
    diff = _added("shop/src/orders.ts", 3) + _added("shop/src/server.ts", 6)
    imp = compute_impact(graph, diff)
    assert len(imp.functions) == 2, [f.function for f in imp.functions]  # JSON keeps both
    md = render_markdown(graph, imp)
    rows = [line for line in md.splitlines() if line.startswith("| `")]
    assert len(rows) == 1 and "cancelOrder" in rows[0] and "also changed" in rows[0], rows
    assert "writes **2** tables · reads **0** tables" in md or "writes **2** tables" in md, md


def test_schema_only_change_is_listed(tmp_path: Path) -> None:
    graph = _scan(tmp_path, SHOP)
    for rel, body in SHOP.items():  # repo_root = the checkout
        (tmp_path / rel).write_text(body, encoding="utf-8")
    diff = _added("shop/schema.sql", 3) + _added("shop/schema.sql", 4) + _added("shop/schema.sql", 7) + _added("README.md", 1)
    imp = compute_impact(graph, diff, repo_root=tmp_path)
    cols = sorted(graph.nodes[c.column].attrs["name"] for c in imp.schema_changes)
    assert cols == ["discount_pct", "note", "status"], cols
    status = next(c for c in imp.schema_changes if graph.nodes[c.column].attrs["name"] == "status")
    assert any(e.endswith("entrypoint:http:POST:/orders/:id/cancel") for e in status.entrypoints), status
    md = render_markdown(graph, imp)
    assert "Schema changes" in md and "orders.discount_pct" in md and "not used yet" in md, md
    assert "outside any function: `README.md`" in md, md


def test_header_names_units() -> None:
    graph = scan_path(CHECKOUT)
    md = render_markdown(graph, compute_impact(graph, _diff(f"{PREFIX}/api/billing.go", 27), prefix=PREFIX))
    assert "· writes **1** table · reads **" in md or "· writes **1** table ·" in md, md


def _why_of(tmp_path: Path, source: str, name: str) -> str | None:
    graph = _scan(tmp_path, {"svc/f.ts": source})
    return graph.nodes[f"function:svc.f.{name}"].attrs.get("why")


def test_js_why_ignores_trailing_comment_of_previous_line(tmp_path: Path) -> None:
    src = "const limit = 5; // max retries\nexport function afterTrailing(id: number) { return id }\n"
    assert _why_of(tmp_path, src, "afterTrailing") is None


def test_js_why_skips_tool_directives(tmp_path: Path) -> None:
    src = (
        "/** Ships an order. */\n"
        "// eslint-disable-next-line no-console\n"
        "export function ship() { return 1 }\n"
        "// TODO: remove after the migration\n"
        "export function legacy() { return 2 }\n"
        "// @ts-ignore\n"
        "// prettier-ignore\n"
        "export function raw() { return 3 }\n"
    )
    graph = _scan(tmp_path, {"svc/f.ts": src})
    why = {n: graph.nodes[f"function:svc.f.{n}"].attrs.get("why") for n in ("ship", "legacy", "raw")}
    assert why == {"ship": "Ships an order.", "legacy": None, "raw": None}, why


def test_js_why_skips_license_header(tmp_path: Path) -> None:
    src = (
        "/* Copyright (c) Example Shop Inc. All rights reserved. */\n"
        "export async function firstInFile() { return 1 }\n"
    )
    assert _why_of(tmp_path, src, "firstInFile") is None
    src2 = "/*! shop-ui v1.2.3 */\nexport function banner() { return 1 }\n"
    assert _why_of(tmp_path, src2, "banner") is None


def test_js_why_on_named_route_handler_and_overloads(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/orders.ts": (
                "/** Shows one order. */\n"
                "export function showOrder(req: any, res: any) { res.json({}) }\n"
                "/** Formats an id. */\n"
                "export function overloaded(id: number): number;\n"
                "export function overloaded(id: string): string;\n"
                "export function overloaded(id: any) { return id }\n"
            ),
            "svc/server.ts": (
                "import express from 'express';\nimport { showOrder } from './orders';\n"
                "const app = express();\napp.get('/orders/:id', showOrder);\n"
            ),
        },
    )
    ep = graph.nodes["entrypoint:http:GET:/orders/:id"]
    assert ep.attrs.get("why") == "Shows one order.", ep.attrs
    assert graph.nodes["function:svc.orders.overloaded"].attrs.get("why") == "Formats an id."


def test_inline_express_handler_owns_its_sql(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/schema.sql": "CREATE TABLE customers (id int primary key, email text);\n",
            "svc/server.ts": (
                "import express from 'express';\nimport { Pool } from 'pg';\n"   # 1-2
                "const pool = new Pool();\nconst app = express();\n"              # 3-4
                "app.post('/local/:id', async (req, res) => {\n"                  # 5
                "  await pool.query('UPDATE customers SET email = lower(email) WHERE id = $1', [req.params.id]);\n"  # 6
                "  res.end();\n"                                                  # 7
                "});\n"                                                           # 8
            ),
        },
    )
    ep = "entrypoint:http:POST:/local/:id"
    handler = next(e.to_id for e in graph.edges.values() if e.type == "triggers" and e.from_id == ep)
    writes = {e.to_id for e in graph.edges.values() if e.type == "writes" and e.from_id == handler}
    assert any("customers" in w for w in writes), (handler, writes)
    ep_writes = {e.to_id for e in graph.edges.values() if e.type == "writes" and e.from_id == ep}
    assert "table:customers" in ep_writes, ep_writes
    module_writes = {e.to_id for e in graph.edges.values() if e.type == "writes" and e.from_id == "function:svc.server.module"}
    assert not module_writes, module_writes
    imp = compute_impact(graph, _added("svc/server.ts", 7))
    assert [f.function for f in imp.functions] == [handler] and imp.functions[0].entrypoints == [ep]


def test_default_and_namespace_imported_route_handlers(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/schema.sql": "CREATE TABLE orders (id int primary key, status text);\n",
            "svc/handlers.ts": (
                "import { Pool } from 'pg';\nconst pool = new Pool();\n"
                "/** Lists orders. */\n"
                "export default async function listHandler(req: any, res: any) {\n"
                "  res.json(await pool.query('SELECT id, status FROM orders'));\n"
                "}\n"
                "/** Cancels an order. */\n"
                "export async function cancelHandler(req: any, res: any) {\n"
                "  await pool.query('UPDATE orders SET status = $1 WHERE id = $2', ['x', 1]);\n"
                "}\n"
            ),
            "svc/server.ts": (
                "import express from 'express';\n"
                "import listHandler, { cancelHandler } from './handlers';\n"
                "import * as h from './handlers';\n"
                "const app = express();\n"
                "app.post('/cancel/:id', cancelHandler);\n"
                "app.get('/orders', listHandler);\n"
                "app.get('/orders2', h.cancelHandler);\n"
            ),
        },
    )

    def handler(path: str, method: str = "GET") -> str:
        ep = f"entrypoint:http:{method}:{path}"
        return next(e.to_id for e in graph.edges.values() if e.type == "triggers" and e.from_id == ep)

    assert handler("/orders") == "function:svc.handlers.listHandler"
    assert handler("/orders2") == "function:svc.handlers.cancelHandler"
    assert handler("/cancel/:id", "POST") == "function:svc.handlers.cancelHandler"
    assert graph.nodes["entrypoint:http:GET:/orders"].attrs.get("why") == "Lists orders."
    assert "function:svc.handlers.default" not in graph.nodes
    assert "function:svc.server.h.cancelHandler" not in graph.nodes


def test_js_why_keeps_comments_that_mention_license(tmp_path: Path) -> None:
    src = (
        "// License checks for resellers happen here: only active licences may order.\n"
        "export async function licenseRule(id: number) { return id }\n"
    )
    assert _why_of(tmp_path, src, "licenseRule") == "License checks for resellers happen here: only active licences may order."


def _schema_md(tmp_path: Path, files: dict[str, str], diff: str):
    graph = _scan(tmp_path, files)
    imp = compute_impact(graph, diff, repo_root=tmp_path)
    return graph, imp, render_markdown(graph, imp)


def _replace(path: str, line: int, old: str, new: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{line} +{line} @@\n-{old}\n+{new}\n"


SCHEMA_APP = {
    "shop/schema.sql": (
        "CREATE TABLE customers (\n"            # 1
        "  id int primary key,\n"               # 2
        "  name text,\n"                        # 3
        "  email TEXT NOT NULL,\n"              # 4
        "  updated_at timestamptz\n"            # 5
        ");\n"                                  # 6
        "CREATE TABLE orders (id int primary key, status text);\n"  # 7
        "ALTER TABLE orders ADD COLUMN gift_note TEXT, ADD COLUMN gift_wrap BOOLEAN;\n"  # 8
        "ALTER TABLE public.\"customers\" DROP COLUMN updated_at;\n"  # 9
        "ALTER TABLE customers RENAME COLUMN name TO full_name;\n"   # 10
        "CREATE TABLE shipments (\n"            # 11
        "  id int primary key,\n"               # 12
        "  order_id int,\n"                     # 13
        "  carrier text\n"                      # 14
        ");\n"                                  # 15
    ),
    "shop/src/db.ts": "export const db = { query: async (q: string, a?: unknown[]) => [] };\n",
    "shop/src/customers.ts": (
        "import { db } from './db';\n"
        "export async function touch(id: number) {\n"
        "  await db.query('UPDATE customers SET updated_at = now(), name = $2 WHERE id = $1', [id, 'x']);\n"
        "}\n"
    ),
    "shop/src/server.ts": (
        "import express from 'express';\nimport { touch } from './customers';\n"
        "const app = express();\napp.post('/customers/:id/touch', async (req, res) => { await touch(1); res.end(); });\n"
    ),
}


def test_schema_ops_multi_alter_drop_rename_and_new_table(tmp_path: Path) -> None:
    diff = (
        _added("shop/schema.sql", 8) + _added("shop/schema.sql", 9) + _added("shop/schema.sql", 10)
        + "".join(_added("shop/schema.sql", n) for n in range(11, 16))
    )
    graph, imp, md = _schema_md(tmp_path, SCHEMA_APP, diff)
    got = sorted((c.op, c.table, c.name, c.detail) for c in imp.schema_changes)
    assert ("added", "orders", "gift_note", "") in got and ("added", "orders", "gift_wrap", "") in got, got
    assert ("dropped", "customers", "updated_at", "") in got, got
    assert ("renamed", "customers", "name", "full_name") in got, got
    assert ("new table", "shipments", "", "3 columns") in got, got
    assert sum(1 for c in imp.schema_changes if c.table == "shipments") == 1
    dropped = next(c for c in imp.schema_changes if c.op == "dropped")
    assert dropped.risky and any(e.endswith("POST:/customers/:id/touch") for e in dropped.entrypoints), dropped
    assert "⚠️ `customers.updated_at` **dropped** — still used" in md, md
    assert "renamed → `full_name`" in md and "new table `shipments` (3 columns)" in md, md


def test_rename_column_applies_to_schema(tmp_path: Path) -> None:
    graph = _scan(tmp_path, SCHEMA_APP)
    cols = graph.schema["tables"]["customers"]["columns"]
    names = set(cols) if isinstance(cols, dict) else {c["name"] for c in cols}
    assert "full_name" in names and "name" not in names and "updated_at" not in names, sorted(names)


def test_punctuation_only_line_is_not_a_change(tmp_path: Path) -> None:
    files = dict(SCHEMA_APP)
    files["shop/schema.sql"] = files["shop/schema.sql"].replace("  updated_at timestamptz\n", "  updated_at timestamptz,\n  phone text\n")
    diff = (
        "diff --git a/shop/schema.sql b/shop/schema.sql\n--- a/shop/schema.sql\n+++ b/shop/schema.sql\n"
        "@@ -5 +5,2 @@\n-  updated_at timestamptz\n+  updated_at timestamptz,\n+  phone text\n"
    )
    _graph, imp, _md = _schema_md(tmp_path, files, diff)
    assert [(c.op, c.name) for c in imp.schema_changes] == [("added", "phone")], imp.schema_changes


def test_sql_only_diff_has_no_roots_hint_and_missing_file_warns(tmp_path: Path) -> None:
    graph = _scan(tmp_path, SCHEMA_APP)
    imp = compute_impact(graph, _added("shop/schema.sql", 8), repo_root=tmp_path)
    assert imp.hint is None and imp.schema_changes
    imp2 = compute_impact(graph, _added("shop/schema.sql", 8), repo_root=tmp_path / "nowhere")
    assert imp2.schema_changes == [] and imp2.warnings and "not found under --repo-root" in imp2.warnings[0]
    assert "not found under --repo-root" in render_markdown(graph, imp2)


def test_header_counts_folded_rows(tmp_path: Path) -> None:
    graph = _scan(tmp_path, SHOP)
    md = render_markdown(graph, compute_impact(graph, _added("shop/src/orders.ts", 3) + _added("shop/src/server.ts", 6)))
    assert "**2** changed functions (1 folded into another row)" in md, md


def test_new_column_in_existing_create_table_is_added(tmp_path: Path) -> None:
    files = dict(SCHEMA_APP)
    files["shop/schema.sql"] = files["shop/schema.sql"].replace("  email TEXT NOT NULL,\n", "  email TEXT NOT NULL,\n  phone TEXT,\n")
    _g, imp, md = _schema_md(tmp_path, files, _added("shop/schema.sql", 5))
    assert [(c.op, c.name) for c in imp.schema_changes] == [("added", "phone")], imp.schema_changes
    # a real edit of an existing definition stays "changed"
    files2 = dict(SCHEMA_APP)
    files2["shop/schema.sql"] = files2["shop/schema.sql"].replace("  email TEXT NOT NULL,\n", "  email VARCHAR(320) NOT NULL,\n")
    _g2, imp2, _ = _schema_md(tmp_path, files2, _replace("shop/schema.sql", 4, "  email TEXT NOT NULL,", "  email VARCHAR(320) NOT NULL,"))
    assert [(c.op, c.name) for c in imp2.schema_changes] == [("changed", "email")], imp2.schema_changes


def test_handler_with_own_sql_is_not_folded(tmp_path: Path) -> None:
    files = dict(SHOP)
    files["shop/src/server.ts"] = (
        "import express from 'express';\nimport { Pool } from 'pg';\nimport { cancelOrder } from './orders';\n"  # 1-3
        "const pool = new Pool();\nconst app = express();\n"                                                      # 4-5
        "app.post('/orders/:id/cancel', async (req, res) => {\n"                                                  # 6
        "  await cancelOrder(1);\n"                                                                               # 7
        "  await pool.query(\"UPDATE orders SET status = 'cancelled' WHERE id = $1\", [1]);\n"                   # 8
        "  res.end();\n"                                                                                          # 9
        "});\n"                                                                                                   # 10
    )
    graph = _scan(tmp_path, files)
    md = render_markdown(graph, compute_impact(graph, _added("shop/src/orders.ts", 3) + _added("shop/src/server.ts", 9)))
    rows = [line for line in md.splitlines() if line.startswith("| `")]
    assert len(rows) == 2 and "folded" not in md, md


def test_inline_handler_has_column_edges_without_table_copies(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/schema.sql": "CREATE TABLE customers (id int primary key, email text);\n",
            "svc/server.ts": (
                "import express from 'express';\nimport { Pool } from 'pg';\n"
                "const pool = new Pool();\nconst app = express();\n"
                "app.post('/local/:id', async (req, res) => {\n"
                "  await pool.query('UPDATE customers SET email = lower(email) WHERE id = $1', [req.params.id]);\n"
                "  res.end();\n"
                "});\n"
            ),
        },
    )
    ep = "entrypoint:http:POST:/local/:id"
    handler = next(e.to_id for e in graph.edges.values() if e.type == "triggers" and e.from_id == ep)
    targets = {(e.type, e.to_id) for e in graph.edges.values() if e.from_id == handler and e.type in {"reads", "writes"}}
    assert ("writes", "column:customers.email") in targets, targets
    assert not [t for t in targets if t[1] == "table:customers"], targets


def test_inline_handlers_sharing_a_column_each_get_their_edges(tmp_path: Path) -> None:
    graph = _scan(
        tmp_path,
        {
            "svc/schema.sql": "CREATE TABLE orders (id int primary key, status text, note text);\n",
            "svc/server.ts": (
                "import express from 'express';\nimport { Pool } from 'pg';\n"
                "const app = express();\nconst pool = new Pool();\n"
                "app.post('/b/:id', async (req, res) => { await pool.query(\"UPDATE orders SET status = 'y' WHERE id = $1\", [1]); res.end(); });\n"
                "app.post('/c/:id', async (req, res) => { await pool.query(\"UPDATE orders SET status = 'z' WHERE id = $1\", [1]); res.end(); });\n"
                "app.post('/d/:id', async (req, res) => { await pool.query('SELECT status FROM orders WHERE id = $1', [1]); res.end(); });\n"
            ),
        },
    )

    def data(path: str) -> set[tuple[str, str]]:
        ep = f"entrypoint:http:POST:{path}"
        h = next(e.to_id for e in graph.edges.values() if e.type == "triggers" and e.from_id == ep)
        return {(e.type, e.to_id) for e in graph.edges.values() if e.from_id == h and e.type in {"reads", "writes"}}

    for path in ("/b/:id", "/c/:id"):
        got = data(path)
        assert ("writes", "column:orders.status") in got and ("reads", "column:orders.id") in got, (path, got)
    got = data("/d/:id")
    assert ("reads", "column:orders.status") in got and ("reads", "column:orders.id") in got, got
    for path in ("/b/:id", "/c/:id", "/d/:id"):
        ep = f"entrypoint:http:POST:{path}"
        assert any(e.from_id == ep and e.to_id == "table:orders" for e in graph.edges.values()), path
    assert not [e for e in graph.edges.values() if e.from_id == "function:svc.server.module" and e.type in {"reads", "writes"}]
