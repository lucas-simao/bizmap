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
        "@@ -10,4 +10,5 @@ func A() {\n"
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
        imp = compute_impact(graph, _diff("web/src/clients/shop.ts", line))
        return {
            t.rsplit(":", 1)[-1]
            for f in imp.functions
            for e in graph.edges.values()
            if e.type == "calls_http" and e.from_id == f.function
            for t in [e.to_id]
        }, [f.function for f in imp.functions]

    got7, fns7 = routes(7)
    assert fns7 == ["function:src.clients.shop.createShopClient.stock"], fns7
    assert got7 == {"/inventory/:id"}, got7
    _got, fns3 = routes(3)
    assert fns3 == ["function:src.clients.shop.createShopClient"], fns3
    # the factory still reaches every member's route through `calls`
    factory = "function:src.clients.shop.createShopClient"
    members = {e.to_id for e in graph.edges.values() if e.type == "calls" and e.from_id == factory}
    assert {m.rsplit(".", 1)[-1] for m in members} >= {"order", "stock", "addUser"}, members
