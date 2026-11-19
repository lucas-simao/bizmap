"""v0.1.3 — interface dispatch, dynamic SQL, alias phantoms. Synthetic only."""

from __future__ import annotations

import json
import time
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.graph import function_id, table_id
from bizmap.scan import scan_path
from bizmap.sql_analyze import _fallback_table_accesses, analyze_sql, text_phantom_aliases


def _go_mod(root: Path, name: str = "t") -> None:
    (root / "go.mod").write_text(f"module {name}\n\ngo 1.22\n", encoding="utf-8")


def _edge(graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def _write_schema(root: Path, extra: str = "") -> None:
    (root / "schema.sql").write_text(
        "CREATE TABLE notifications (body text);\n"
        "CREATE TABLE items (id int, notes text);\n"
        "CREATE TABLE a (x int);\n"
        "CREATE TABLE b (id int);\n" + extra,
        encoding="utf-8",
    )


def test_text_phantom_aliases_lateral_and_derived() -> None:
    sql = """
SELECT l.x, r.notes
FROM items i
LEFT JOIN LATERAL (SELECT i.id AS x) l ON true
LEFT JOIN LATERAL (SELECT i.notes AS notes) r ON true
"""
    phantoms = text_phantom_aliases(sql)
    assert "l" in phantoms
    assert "r" in phantoms
    derived = "SELECT r.n FROM (SELECT n FROM notes) r"
    assert "r" in text_phantom_aliases(derived)


def test_lateral_and_derived_aliases_are_not_tables() -> None:
    schema = {"items": ["id", "notes"], "appointments": ["id", "notes", "status"]}
    for sql in (
        "SELECT l.x FROM items i LEFT JOIN LATERAL (SELECT i.id AS x) l ON true",
        "SELECT r.notes FROM (SELECT notes FROM appointments) r",
        "SELECT l.x, r.y FROM items i LEFT JOIN LATERAL (SELECT i.id AS x) l ON true "
        "LEFT JOIN LATERAL (SELECT i.id AS y) r ON true",
    ):
        acc = analyze_sql(sql, schema_columns=schema, known_tables=set(schema))
        tables = {a.table.lower() for a in acc}
        assert "l" not in tables, sql
        assert "r" not in tables, sql
        assert not any(a.column and a.table.lower() in {"l", "r"} for a in acc), sql


def test_fallback_skips_lateral_aliases() -> None:
    sql = "SELECT l.x FROM items i LEFT JOIN LATERAL (SELECT i.id AS x) l ON true"
    acc = _fallback_table_accesses(sql, True, known_tables={"items"}, phantoms={"l"})
    tables = {a.table.lower() for a in acc}
    assert "l" not in tables
    assert "items" in tables


def test_ddl_known_tables_only_when_schema_present(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    _write_schema(tmp_path)
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

func Run(ctx context.Context, db *pgxpool.Pool) {
    _, _ = db.Query(ctx, `SELECT l.x FROM items i LEFT JOIN LATERAL (SELECT i.id AS x) l ON true`)
    _, _ = db.Query(ctx, `SELECT r.notes FROM (SELECT notes FROM items) r`)
}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    table_names = {n.attrs.get("name") for n in graph.nodes.values() if n.type == "table"}
    assert "l" not in table_names
    assert "r" not in table_names
    assert not any(nid.startswith("column:l.") for nid in graph.nodes)
    assert not any(nid.startswith("column:r.") for nid in graph.nodes)
    assert "items" in table_names
    assert "ghost" not in table_names


def test_interface_field_dispatch_reaches_notifications(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    _write_schema(tmp_path)
    notify = tmp_path / "notify"
    notify.mkdir()
    (notify / "mailer.go").write_text(
        """
package notify

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

type Event struct{ Body string }

type Bell interface {
    Changed(e Event) error
}

type Mailer struct{ DB *pgxpool.Pool }

func (m *Mailer) Changed(e Event) error {
    _, err := m.DB.Exec(context.Background(), `INSERT INTO notifications (body) VALUES ($1)`, e.Body)
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

import (
    "context"
    "t/notify"
)

type Service struct {
    Bell notify.Bell
}

func New(bell notify.Bell) *Service {
    return &Service{Bell: bell}
}

func (s *Service) Run(ctx context.Context) error {
    return s.Bell.Changed(notify.Event{Body: "hi"})
}
""",
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "github.com/labstack/echo/v4"
    "t/notify"
    "t/svc"
)

type Handler struct{ Svc *svc.Service }

func (h *Handler) Ping(c echo.Context) error {
    return h.Svc.Run(c.Request().Context())
}

func main() {
    s := svc.New(&notify.Mailer{})
    h := &Handler{Svc: s}
    e := echo.New()
    e.POST("/ping", h.Ping)
}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    run = function_id("svc.Service.Run")
    changed = function_id("notify.Mailer.Changed")
    ping = function_id("main.Handler.Ping")
    call = _edge(graph, "calls", run, changed)
    assert call is not None
    assert call.confidence.value == "inferred"
    assert _edge(graph, "writes", changed, table_id("notifications")) is not None or any(
        e.from_id == changed and e.type == "writes" and "notifications" in e.to_id
        for e in graph.edges.values()
    )
    assert _edge(graph, "writes", run, table_id("notifications")) is not None or any(
        e.from_id == run and e.type == "writes" and "notifications" in e.to_id
        for e in graph.edges.values()
    )
    assert _edge(graph, "writes", ping, table_id("notifications")) is not None or any(
        e.from_id == ping and e.type == "writes" and "notifications" in e.to_id
        for e in graph.edges.values()
    )
    ep = graph.get("entrypoint:http:POST:/ping")
    assert ep is not None
    assert _edge(graph, "writes", ep.id, table_id("notifications")) is not None or any(
        e.from_id == ep.id and e.type == "writes" and "notifications" in e.to_id
        for e in graph.edges.values()
    )


def test_interface_param_and_multiple_implementers(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    _write_schema(
        tmp_path,
        extra="CREATE TABLE inbox (body text);\nCREATE TABLE outbox (body text);\n",
    )
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

type Bell interface {
    Changed(msg string) error
}

type Inbox struct{ DB *pgxpool.Pool }
func (i *Inbox) Changed(msg string) error {
    _, err := i.DB.Exec(context.Background(), `INSERT INTO inbox (body) VALUES ($1)`, msg)
    return err
}

type Outbox struct{ DB *pgxpool.Pool }
func (o *Outbox) Changed(msg string) error {
    _, err := o.DB.Exec(context.Background(), `INSERT INTO outbox (body) VALUES ($1)`, msg)
    return err
}

func ping(n Bell, msg string) error {
    return n.Changed(msg)
}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    ping = function_id("main.ping")
    inbox = function_id("main.Inbox.Changed")
    outbox = function_id("main.Outbox.Changed")
    e1 = _edge(graph, "calls", ping, inbox)
    e2 = _edge(graph, "calls", ping, outbox)
    assert e1 is not None and e2 is not None
    assert e1.confidence.value == "inferred"
    assert e2.confidence.value == "inferred"
    cands = set(e1.attrs.get("candidates") or []) | set(e2.attrs.get("candidates") or [])
    assert inbox.replace("function:", "") in cands or "Inbox.Changed" in str(cands)
    assert "Outbox.Changed" in str(cands)


def test_range_literal_slice_expands_sql(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    _write_schema(tmp_path)
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

func Sweep(ctx context.Context, tx *pgxpool.Pool) error {
    for _, q := range []string{
        "UPDATE a SET x = 1",
        "DELETE FROM b WHERE id = 1",
    } {
        if _, err := tx.Exec(ctx, q); err != nil {
            return err
        }
    }
    return nil
}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    sweep = function_id("main.Sweep")
    assert any(
        e.from_id == sweep and e.type == "writes" and (e.to_id == table_id("a") or e.to_id.startswith("column:a."))
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == sweep and e.type == "writes" and (e.to_id == table_id("b") or e.to_id.startswith("column:b."))
        for e in graph.edges.values()
    )
    assert not any(e.from_id == sweep and e.to_id == "table:unknown" for e in graph.edges.values())


def test_query_param_helper_attributes_sql_to_caller(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    _write_schema(tmp_path)
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

var db *pgxpool.Pool

func named(query string, args ...any) error {
    _, err := db.Exec(ctx, query, args...)
    return err
}

func exec(ctx context.Context, q string) error {
    _, err := db.Exec(ctx, q)
    return err
}

func Do(ctx context.Context) error {
    if err := named(`UPDATE a SET x = 1`); err != nil {
        return err
    }
    return exec(ctx, `DELETE FROM b WHERE id = 1`)
}
""",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    do = function_id("main.Do")
    assert any(
        e.from_id == do and e.type == "writes" and (e.to_id == table_id("a") or e.to_id.startswith("column:a."))
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == do and e.type == "writes" and (e.to_id == table_id("b") or e.to_id.startswith("column:b."))
        for e in graph.edges.values()
    )
    assert not any(e.from_id == do and e.to_id == "table:unknown" for e in graph.edges.values())
    named = function_id("main.named")
    assert not any(e.from_id == named and e.to_id == "table:unknown" for e in graph.edges.values())


def test_cli_exclude_and_profile(tmp_path: Path, capsys) -> None:
    app = tmp_path / "app"
    app.mkdir()
    _go_mod(app)
    (app / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    (app / "keep.go").write_text("package main\nfunc Keep() {}\n", encoding="utf-8")
    skip = app / "skipme"
    skip.mkdir()
    (skip / "seed.go").write_text("package skipme\nfunc Seed() {}\n", encoding="utf-8")
    (app / "bizmap.yaml").write_text("exclude:\n  - other\n", encoding="utf-8")
    out = tmp_path / "graph.json"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["scan", str(app), "--out", str(out), "--exclude", "skipme"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    ids = {n["id"] for n in data["nodes"]}
    assert "function:main.Keep" in ids
    assert not any("Seed" in i for i in ids)

    scan_path(app, exclude=["skipme"], profile=True)
    err = capsys.readouterr().err
    assert "bizmap profile:" in err
    assert "parse=" in err
    assert "resolve=" in err
    assert "sql=" in err
    assert "propagate=" in err


def test_scan_many_go_files_under_ten_seconds(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE items (id int);\nCREATE TABLE notes (body text);\n",
        encoding="utf-8",
    )
    for i in range(150):
        pkg = tmp_path / f"p{i:03d}"
        pkg.mkdir()
        (pkg / "f.go").write_text(
            f"""
package p{i:03d}

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

type Repo struct{{ DB *pgxpool.Pool }}

func (r *Repo) Load(ctx context.Context) error {{
    _, err := r.DB.Query(ctx, `SELECT id FROM items`)
    return err
}}
""",
            encoding="utf-8",
        )
    t0 = time.perf_counter()
    graph = scan_path(tmp_path)
    elapsed = time.perf_counter() - t0
    assert elapsed < 10.0, elapsed
    assert any(n.id.startswith("function:p000.Repo.Load") or "p000.Repo.Load" in n.id for n in graph.nodes.values())
