"""Bugs found on the public demo repos (bizmap-demo-api / bizmap-demo-web):

1. a route with one exact handler was reported "handler is ambiguous";
2. a call to a route the scanned API does not have was `target_not_scanned`;
4. the same route registered twice (issue #148) kept one registration silently.

The impact / render tests use hand-built graphs; `test_scan_*` need the parsers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bizmap.graph import Graph
from bizmap.http_match import route_not_found, scanned_repo_names
from bizmap.impact import compute_impact, render_markdown
from test_action_comment import _diff, _ep, _fn

PING_GO = """package main

import "github.com/labstack/echo/v4"

type H struct{}

func (h *H) Ping(c echo.Context) error { return c.String(200, "pong") }

func main() { e := echo.New(); h := &H{}; e.GET("/ping", h.Ping); e.Start(":8080") }
"""


def _risk(md: str) -> str:
    m = re.search(r"<!-- bizmap-risk: (\w+) -->", md)
    assert m, md
    return m.group(1)


# ── 1. one candidate is not "ambiguous" ──


def _route_graph(**ep_attrs) -> Graph:
    g = Graph()
    g.attrs["repo_roots"] = {"api": "."}
    ep = _ep(g, "", "http", "GET:/ping", "main.go", 9, method="GET", path="/ping", **ep_attrs)
    fn = _fn(g, "", "main.H.Ping", "main.go", 7, 7)
    g.add_edge("triggers", ep, fn)
    return g


def test_single_candidate_is_not_ambiguous() -> None:
    g = _route_graph(candidates=["main.H.Ping"], confidence="exact")  # what 0.14.1 wrote
    md = render_markdown(g, compute_impact(g, _diff(("main.go", 7))))
    assert "ambiguous" not in md, md


def test_several_candidates_are_ambiguous() -> None:
    g = _route_graph(candidates=["main.H.Ping", "main.V.Ping"], confidence="unknown")
    md = render_markdown(g, compute_impact(g, _diff(("main.go", 7))))
    assert "`GET /ping`: handler is ambiguous (2 candidates)" in md, md


def test_unknown_handler_is_said_as_such() -> None:
    g = _route_graph(confidence="unknown")
    md = render_markdown(g, compute_impact(g, _diff(("main.go", 7))))
    assert "handler not resolved" in md and "ambiguous" not in md, md


# ── 2. route_not_found ──


def test_route_not_found_only_for_a_scanned_service() -> None:
    assert route_not_found("target_not_scanned", "api", {}, {"api", "web"}) == "route_not_found"
    assert route_not_found("target_not_scanned", "billing", {}, {"api", "web"}) == "target_not_scanned"
    assert route_not_found("target_not_scanned", None, {}, {"api"}) == "target_not_scanned"
    assert route_not_found("base_unknown", "api", {}, {"api"}) == "base_unknown"
    g = Graph()
    g.attrs["repos"] = ["api", "web"]
    g.add_node("web/function:src.a.ping", "function", repo="web")
    assert scanned_repo_names(g) == {"api", "web"}


def test_route_not_found_is_a_medium_risk_in_the_comment() -> None:
    g = Graph()
    g.attrs["repo_roots"] = {"web": "web"}
    fn = _fn(g, "web", "src.a.health", "src/a.ts", 4, 4, unresolved_http=[
        {"method": "GET", "url": "{VITE_API_URL}/health", "reason": "route_not_found", "service": "api"}])
    md = render_markdown(g, compute_impact(g, _diff(("web/src/a.ts", 4))))
    assert _risk(md) in ("medium", "high"), md
    assert "`src.a.health` calls `GET {VITE_API_URL}/health` — no matching route in `api`" in md or \
        "calls `GET {VITE_API_URL}/health` — no matching route in `api`" in md, md
    assert "which `api` does not serve (no matching route)" in md, md
    assert fn


# ── 4. duplicate routes ──


DUP_A = """package main

import "github.com/labstack/echo/v4"

func pingA(c echo.Context) error { return c.String(200, "a") }

func RegisterA(e *echo.Echo) { e.GET("/ping", pingA) }
"""
DUP_B = """package main

import "github.com/labstack/echo/v4"

func pingB(c echo.Context) error { return c.String(200, "b") }

func RegisterB(e *echo.Echo) { e.GET("/ping", pingB) }
"""


def test_duplicate_route_note_in_the_comment() -> None:
    g = _route_graph(registrations=[
        {"file": "a.go", "line": 7, "handler": "main.pingA"}, {"file": "b.go", "line": 7, "handler": "main.pingB"}],
        candidates=["main.pingA", "main.pingB"])
    md = render_markdown(g, compute_impact(g, _diff(("main.go", 7))))
    assert "`GET /ping`: registered 2 times (a.go:7, b.go:7)" in md, md
    assert "ambiguous" not in md


# ── whole scans (need the parsers) ──


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def test_scan_exact_route_has_no_candidates(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    from bizmap.scan import scan_path

    _write(tmp_path, {"go.mod": "module example.com/api\n\ngo 1.22\n", "main.go": PING_GO})
    g = scan_path(tmp_path)
    ep = g.nodes["entrypoint:http:GET:/ping"]
    assert "candidates" not in ep.attrs, ep.attrs
    md = render_markdown(g, compute_impact(g, _diff(("main.go", 7))))
    assert "ambiguous" not in md, md


def test_scan_duplicate_route_keeps_every_registration(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    from bizmap.scan import scan_path

    _write(tmp_path, {"go.mod": "module example.com/api\n\ngo 1.22\n", "a.go": DUP_A, "b.go": DUP_B,
                      "main.go": "package main\n\nimport \"github.com/labstack/echo/v4\"\n\n"
                                 "func main() { e := echo.New(); RegisterA(e); RegisterB(e) }\n"})
    g = scan_path(tmp_path)
    ep = g.nodes["entrypoint:http:GET:/ping"]
    regs = ep.attrs.get("registrations")
    assert [(r["file"], r["handler"]) for r in regs] == [("a.go", "main.pingA"), ("b.go", "main.pingB")], ep.attrs
    targets = {e.to_id for e in g.edges.values() if e.type == "triggers" and e.from_id == ep.id}
    assert {"function:main.pingA", "function:main.pingB"} <= targets
    assert any(w.startswith("duplicate_route: GET /ping is registered 2 times") for w in g.attrs["warnings"])
    data = g.to_dict()
    assert data["duplicate_routes"][0]["route"] == "entrypoint:http:GET:/ping"


def test_scan_workspace_route_not_found(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_path

    _write(tmp_path, {
        "bizmap.yaml": "repos: [api, web]\nservices:\n  api: { env: VITE_API_URL }\n",
        "api/bizmap.yaml": "name: api\n",
        "web/bizmap.yaml": "name: web\n",
        "api/go.mod": "module example.com/api\n\ngo 1.22\n",
        "api/main.go": PING_GO,
        "web/package.json": '{"name": "web", "dependencies": {"axios": "^1.6.0"}}\n',
        "web/src/a.ts": (
            "import axios from 'axios';\n"
            "const api = axios.create({ baseURL: import.meta.env.VITE_API_URL });\n"
            "export async function ping() { return api.get('/ping'); }\n"
            "export async function health() { return api.get('/health'); }\n"
        ),
    })
    g = scan_path(tmp_path)
    data = g.to_dict()
    items = {(u.get("from"), u.get("reason"), u.get("service")) for u in data.get("unresolved_http") or []}
    assert ("web/function:src.a.health", "route_not_found", "api") in items, items
    linked = {(e["from"], e["to"]) for e in data["edges"] if e["type"] == "calls_http"}
    assert ("web/function:src.a.ping", "api/entrypoint:http:GET:/ping") in linked
    assert not [u for u in data.get("unresolved_http") or [] if u.get("reason") == "target_not_scanned"]
