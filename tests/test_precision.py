"""v0.1.2 precision fixes — synthetic fixtures only."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.detect.go_lang import GoAnalyzer
from bizmap.detect.rules import load_rules, method_matches
from bizmap.graph import function_id, table_id
from bizmap.overrides import find_overrides, load_scan_config
from bizmap.scan import scan_path


def _go_mod(root: Path, name: str = "t") -> None:
    (root / "go.mod").write_text(f"module {name}\n\ngo 1.22\n", encoding="utf-8")


def _edge(graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def test_method_matches_is_case_sensitive() -> None:
    spec = {"in": ["GET", "POST", "OPTIONS", "Any", "Match"]}
    assert method_matches("GET", spec)
    assert method_matches("OPTIONS", spec)
    assert method_matches("Any", spec)
    assert not method_matches("options", spec)
    assert not method_matches("get", spec)


def test_lowercase_helper_is_not_a_route(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    (tmp_path / "main.go").write_text(
        """
package main

import "github.com/labstack/echo/v4"

type Handler struct{}

func (h *Handler) options(c echo.Context, allow string) {}

func (h *Handler) Version(c echo.Context) error { return nil }

func Wrap(next echo.HandlerFunc) echo.HandlerFunc { return next }

func main() {
    e := echo.New()
    h := Handler{}
    h.options(c, "GET")
    e.GET("/version", Wrap(h.Version))
}
""",
        encoding="utf-8",
    )
    analysis = GoAnalyzer(load_rules()).analyze([tmp_path / "main.go"], tmp_path)
    methods_paths = {(r.method, r.path) for r in analysis.routes}
    assert ("OPTIONS", "{c}") not in methods_paths
    assert not any(r.method == "OPTIONS" for r in analysis.routes)
    assert any(r.method == "GET" and r.path == "/version" for r in analysis.routes)
    version = next(r for r in analysis.routes if r.path == "/version")
    assert version.handler is not None
    assert version.handler.name == "Version"


def test_wrapped_handler_unwraps_to_inner(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    (tmp_path / "main.go").write_text(
        """
package main

import "github.com/labstack/echo/v4"

type Handler struct{}

func (h *Handler) Version(c echo.Context) error { return nil }

func wrap(next echo.HandlerFunc) echo.HandlerFunc { return next }

func mw(next echo.HandlerFunc) echo.HandlerFunc { return next }

func main() {
    e := echo.New()
    h := &Handler{}
    e.GET("/version", wrap(h.Version))
    e.GET("/ok", mw(h.Version))
}
""",
        encoding="utf-8",
    )
    analysis = GoAnalyzer(load_rules()).analyze([tmp_path / "main.go"], tmp_path)
    assert len(analysis.routes) == 2
    for route in analysis.routes:
        assert route.handler is not None
        assert route.handler.name == "Version"
        assert route.handler.receiver == "Handler"


def test_unresolved_const_sets_attribute(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    (tmp_path / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "github.com/labstack/echo/v4"
    otherpkg "t/notscanned"
)

func main() {
    e := echo.New()
    e.POST(otherpkg.PendingPath+"/setup", handleSetup)
}

func handleSetup() {}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    hits = [
        n
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    ]
    assert hits
    node = hits[0]
    assert "{otherpkg.PendingPath}" in (node.attrs.get("path") or "")
    assert node.attrs.get("unresolved") is True


def test_scanned_const_resolves_across_packages(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    (tmp_path / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    paths = tmp_path / "paths"
    paths.mkdir()
    (paths / "paths.go").write_text(
        'package paths\n\nconst PendingPath = "/pending"\n',
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "github.com/labstack/echo/v4"
    "t/paths"
)

func handleSetup() {}

func main() {
    e := echo.New()
    e.POST(paths.PendingPath+"/setup", handleSetup)
}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    node = graph.get("entrypoint:http:POST:/pending/setup")
    assert node is not None
    assert node.attrs.get("unresolved") is None


def test_fromless_select_does_not_propagate_unknown(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    origin = function_id("repositories/orders.Repo.BindTenant")
    assert origin in graph.nodes
    unknown_from_origin = [
        e
        for e in graph.edges.values()
        if e.from_id == origin and e.to_id == "table:unknown"
    ]
    assert unknown_from_origin == []
    create = function_id("controllers/orders.Handler.Create")
    assert _edge(graph, "reads", create, "table:unknown") is None
    assert _edge(graph, "writes", create, "table:unknown") is None
    ep = graph.get("entrypoint:http:POST:/api/appointments")
    assert ep is not None
    assert _edge(graph, "reads", ep.id, "table:unknown") is None
    assert _edge(graph, "writes", ep.id, "table:unknown") is None


def test_dynamic_unknown_stays_at_origin(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    origin = function_id("repositories/orders.Repo.PatchDynamic")
    assert any(
        e.from_id == origin and e.to_id == "table:unknown" for e in graph.edges.values()
    )
    # Not called from a handler, but callers still must not inherit unknown.
    for e in graph.edges.values():
        if e.to_id == "table:unknown" and e.type in {"reads", "writes"}:
            assert e.from_id == origin, e.from_id


def test_function_field_dispatch_notifier(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    create = function_id("services/orders.Service.Create")
    send = function_id("notify.Send")
    assert send in graph.nodes
    assert _edge(graph, "calls", create, send) is not None
    assert _edge(graph, "writes", send, table_id("notifications")) is not None or any(
        e.from_id == send and e.type == "writes" and "notifications" in e.to_id
        for e in graph.edges.values()
    )
    handler = function_id("controllers/orders.Handler.Create")
    assert _edge(graph, "writes", handler, table_id("notifications")) is not None or any(
        e.from_id == handler and e.type == "writes" and "notifications" in e.to_id
        for e in graph.edges.values()
    )


def test_function_field_tmp(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE notifications (body text);\n", encoding="utf-8"
    )
    notify = tmp_path / "notify"
    notify.mkdir()
    (notify / "send.go").write_text(
        """
package notify

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

func Send(ctx context.Context, msg string) error {
    _, err := (&pgxpool.Pool{}).Exec(ctx, `INSERT INTO notifications (body) VALUES ($1)`, msg)
    return err
}
""",
        encoding="utf-8",
    )
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "svc.go").write_text(
        """
package svc

import "context"

type Notifier func(ctx context.Context, msg string) error

type Service struct {
    ring Notifier
}

func New(ring Notifier) *Service {
    return &Service{ring: ring}
}

func (s *Service) Run(ctx context.Context) error {
    return s.ring(ctx, "hi")
}
""",
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "t/notify"
    "t/svc"
)

func main() {
    s := svc.New(notify.Send)
    _ = s.Run(nil)
}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    run = function_id("svc.Service.Run")
    send = function_id("notify.Send")
    assert _edge(graph, "calls", run, send) is not None
    assert any(
        e.from_id == run and e.type == "writes" and "notifications" in e.to_id
        for e in graph.edges.values()
    )


def test_qualnames_are_scan_root_relative(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    assert function_id("cmd/server.main") in graph.nodes
    assert function_id("main.main") not in graph.nodes
    assert function_id("controllers/orders.Handler.Create") in graph.nodes
    assert function_id("notify.Send") in graph.nodes
    # Unique package clause still uses the directory, never a second spelling.
    jobs = [n.id for n in graph.nodes.values() if n.id.startswith("function:jobs.")]
    assert jobs
    assert not any(n.id.startswith("function:clinic/jobs.") for n in graph.nodes.values())


def test_inferred_call_requires_unique_package(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE a (id int);\nCREATE TABLE b (id int);\n", encoding="utf-8"
    )
    (tmp_path / "pkg/a").mkdir(parents=True)
    (tmp_path / "pkg/b").mkdir(parents=True)
    (tmp_path / "pkg/a/a.go").write_text(
        """
package a

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

type Repo struct{ DB *pgxpool.Pool }

func (r *Repo) Load(ctx context.Context) error {
    _, err := r.DB.Query(ctx, `SELECT id FROM a`)
    return err
}
""",
        encoding="utf-8",
    )
    (tmp_path / "pkg/b/b.go").write_text(
        """
package b

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

type Repo struct{ DB *pgxpool.Pool }

func (r *Repo) Load(ctx context.Context) error {
    _, err := r.DB.Query(ctx, `SELECT id FROM b`)
    return err
}
""",
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        """
package main

func run(x mystery) {
    x.Load()
}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    run = function_id("main.run")
    # Unknown receiver must not infer both Load methods (or either uniquely).
    assert _edge(graph, "calls", run, function_id("pkg/a.Repo.Load")) is None
    assert _edge(graph, "calls", run, function_id("pkg/b.Repo.Load")) is None
    assert _edge(graph, "reads", run, table_id("a")) is None
    assert _edge(graph, "reads", run, table_id("b")) is None


def test_config_flag_and_git_toplevel(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "bizmap.yaml").write_text("exclude:\n  - skipme\n", encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir()
    _go_mod(app)
    (app / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    (app / "keep.go").write_text("package main\nfunc Keep() {}\n", encoding="utf-8")
    skip = app / "skipme"
    skip.mkdir()
    (skip / "seed.go").write_text("package skipme\nfunc Seed() {}\n", encoding="utf-8")

    cfg = load_scan_config(app)
    assert cfg.path is not None
    assert cfg.path == tmp_path / "bizmap.yaml"
    assert "skipme" in cfg.exclude

    graph = scan_path(app)
    files = [
        (n.attrs.get("evidence") or {}).get("file")
        for n in graph.nodes.values()
        if n.type == "function"
    ]
    assert any(f and "keep.go" in str(f) for f in files)
    assert not any(f and "skipme" in str(f) for f in files)

    other = tmp_path / "other.yaml"
    other.write_text("exclude:\n  - keep.go\n", encoding="utf-8")
    assert find_overrides(app, config=other) == other.resolve()
    graph2 = scan_path(app, config=other)
    files2 = [
        (n.attrs.get("evidence") or {}).get("file")
        for n in graph2.nodes.values()
        if n.type == "function"
    ]
    assert not any(f and str(f).endswith("keep.go") for f in files2)


def test_cli_config_option(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    _go_mod(app)
    (app / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    (app / "main.go").write_text("package main\nfunc Keep() {}\n", encoding="utf-8")
    (app / "skip.go").write_text("package main\nfunc Skip() {}\n", encoding="utf-8")
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("exclude:\n  - skip.go\n", encoding="utf-8")
    out = tmp_path / "graph.json"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scan", str(app), "--out", str(out), "--config", str(cfg)],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    ids = {n["id"] for n in data["nodes"]}
    assert "function:main.Keep" in ids
    assert "function:main.Skip" not in ids
