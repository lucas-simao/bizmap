"""Follow-ups of #155:

1. Firestore writes through a built document path: `${CONST}/${id}`,
   `'notes/' + id`, `CONST + '/' + id`, in a batch / transaction or straight
   on the instance (`db.doc(path).set/update/delete/create`).
2. The Go helper's template route (`POST /internal/jobs/{job.Name()}`) is
   dropped once the helper is expanded for a call site.
3. Firestore triggers fire only for the write calls that can fire them.

The text passes run without tree-sitter; `test_scan_*` need the parsers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bizmap import fsapply
from bizmap.detect.go_helpers import drop_template_routes
from bizmap.detect.js_firestore import scan_files
from bizmap.fsapply import link_firestore_triggers
from bizmap.graph import Confidence, Evidence, Graph
from bizmap.query import Index, who
from test_query_followups import _ts_functions

MARK_PAID = """import { getFirestore } from "firebase-admin/firestore";
import { onCall } from "firebase-functions/v2/https";
const db = getFirestore();
const ORDERS = 'orders';
const NOTES = "notes";
export async function markPaid(id: string) {
  const batch = db.batch();
  batch.update(db.doc(`${ORDERS}/${id}`), { status: 'paid' });
  batch.set(db.doc('notes/' + id), { text: 'paid' });
  await batch.commit();
}
export async function touchCustomer(id: string, line: string) {
  await db.doc(`customers/${id}`).set({ seen: true });
  await db.doc('customers/' + id).update({ visits: 1 });
  await db.doc(NOTES + '/' + id).delete();
  await db.doc(`${ORDERS}/${id}/items/${line}`).create({ qty: 1 });
  const snap = await db.doc(`customers/${id}`).get();
  await db.runTransaction(async (tx) => { tx.set(db.doc(`${ORDERS}/${id}`), { status: 'paid' }); });
}
export async function dynamicName(id: string, suffix: string) {
  await db.doc('orders' + suffix).set({});
  await db.doc(`${suffix}/${id}`).update({});
}
"""


def _accesses(text: str = MARK_PAID) -> list[tuple[str, str, str, str, str | None, list[str]]]:
    funcs, ends = _ts_functions("src/orders.ts", text)
    res = scan_files({"src/orders.ts": text}, funcs, ends, [])
    return sorted((a.symbol.rsplit(".", 1)[-1], a.kind, a.collection, a.path, a.method, a.fields) for a in res.accesses)


def test_built_document_paths_resolve_their_collection() -> None:
    got = _accesses()
    assert got == [
        ("dynamicName", "write", "unknown", "orders{}", "set", []),
        ("dynamicName", "write", "unknown", "{}", "update", []),
        ("markPaid", "write", "notes", "notes", "set", ["text"]),
        ("markPaid", "write", "orders", "orders", "update", ["status"]),
        ("touchCustomer", "read", "customers", "customers", None, []),
        ("touchCustomer", "write", "customers", "customers", "set", ["seen"]),
        ("touchCustomer", "write", "customers", "customers", "update", ["visits"]),
        ("touchCustomer", "write", "items", "orders/{}/items", "create", ["qty"]),
        ("touchCustomer", "write", "notes", "notes", "delete", []),
        ("touchCustomer", "write", "orders", "orders", "set", ["status"]),
    ]


def test_a_doc_ref_passed_to_a_batch_is_not_also_a_chain() -> None:
    # `batch.update(db.doc(…), …)`: one write (the batch's), not a second from `db.doc(`
    got = [a for a in _accesses() if a[0] == "markPaid"]
    assert len(got) == 2


# ── 3. trigger events ──


def _trigger_graph(ops: dict[str, list[str] | None]) -> Graph:
    g = Graph()
    g.add_node("collection:orders", "collection", name="orders")
    for event in ("created", "updated", "deleted", "written"):
        g.add_node(f"entrypoint:firestore:orders:{event}", "entrypoint", kind="firestore", collection="orders", event=event)
    for fn, methods in ops.items():
        g.add_node(f"function:{fn}", "function", name=fn)
        e = g.add_edge("writes", f"function:{fn}", "collection:orders", confidence=Confidence.EXACT,
                       evidence=Evidence("src/x.ts", 1, fn))
        for m in methods or [None]:
            fsapply.note_fs_op(e.attrs, m)
    return g


def _fired(g: Graph) -> set[tuple[str, str]]:
    return {(e.from_id.split(":", 1)[1], e.to_id.rsplit(":", 1)[1]) for e in g.edges.values() if e.type == "triggers"}


def test_update_does_not_fire_on_created() -> None:
    g = _trigger_graph({"upd": ["update"], "add": ["add"], "st": ["set"], "rm": ["delete"], "any": None})
    link_firestore_triggers(g)
    assert _fired(g) == {
        ("upd", "updated"), ("upd", "written"),
        ("add", "created"), ("add", "written"),
        ("st", "created"), ("st", "updated"), ("st", "written"),
        ("rm", "deleted"), ("rm", "written"),
        # unknown write call: every event stays possible
        ("any", "created"), ("any", "updated"), ("any", "deleted"), ("any", "written"),
    }


def test_derived_writes_carry_the_write_calls() -> None:
    g = Graph()
    g.add_node("function:h.handler", "function", name="h.handler")
    g.add_node("function:h.save", "function", name="h.save")
    g.add_node("entrypoint:http:POST:/orders/{id}", "entrypoint", kind="firebase_http", symbol="h.handler")
    g.add_edge("calls", "function:h.handler", "function:h.save")
    g.add_node("collection:orders", "collection", name="orders")
    e = g.add_edge("writes", "function:h.save", "collection:orders", confidence=Confidence.EXACT)
    fsapply.note_fs_op(e.attrs, "update")
    fsapply._propagate(g, [("writes", "function:h.save", "collection:orders")])
    derived = {x.from_id: x for x in g.edges.values() if x.type == "writes" and x.attrs.get("derived")}
    assert {k: (v.attrs.get("fs_ops"), v.attrs.get("fs_ops_any")) for k, v in derived.items()} == {
        "function:h.handler": (["update"], None),
        "entrypoint:http:POST:/orders/{id}": (["update"], None),
    }
    g.add_node("entrypoint:firestore:orders:created", "entrypoint", kind="firestore", collection="orders", event="created")
    link_firestore_triggers(g)
    assert not [x for x in g.edges.values() if x.type == "triggers" and x.to_id.endswith(":created")]


# ── 2. the Go helper's template route ──


def test_template_route_is_dropped_once_expanded() -> None:
    from bizmap.detect.go_helpers import HelperRoute

    g = Graph()
    tmpl = "entrypoint:http:POST:/internal/jobs/{job.Name()}"
    g.add_node(tmpl, "entrypoint", kind="http", method="POST", path="/internal/jobs/{job.Name()}",
               evidence={"file": "internal/jobs/mount.go", "line": 9})
    g.add_node("entrypoint:http:GET:/health", "entrypoint", kind="http", method="GET", path="/health",
               evidence={"file": "internal/jobs/mount.go", "line": 20})
    g.add_node("function:internal/jobs.mount.func1", "function")
    g.add_node("table:orders", "table", name="orders")
    g.add_edge("triggers", tmpl, "function:internal/jobs.mount.func1")
    g.add_edge("writes", tmpl, "table:orders", derived="call")
    real = "entrypoint:http:POST:/internal/jobs/reminders"
    g.add_node(real, "entrypoint", kind="http", method="POST", path="/internal/jobs/reminders", helper="internal/jobs.mount",
               evidence={"file": "cmd/api/main.go", "line": 30})
    routes = [HelperRoute("POST", "/internal/jobs/reminders", "cmd/api/main.go", 30, "internal/jobs.mount",
                          "internal/jobs", "Reminder", ["Run"], "internal/jobs/mount.go", 9)]
    drop_template_routes(g, routes)
    assert tmpl not in g.nodes and real in g.nodes and "entrypoint:http:GET:/health" in g.nodes
    assert not [e for e in g.edges.values() if tmpl in (e.from_id, e.to_id)]
    assert g.attrs["template_routes_dropped"] == [tmpl]


# ── whole scans (need the parsers) ──

JOBS_GO = {
    "go.mod": "module example.com/jobs-go\n\ngo 1.22\n",
    "migrations/001.sql": "CREATE TABLE orders (id text PRIMARY KEY, total numeric, reminded_at timestamptz);\n"
                          "CREATE TABLE carts (id text PRIMARY KEY);\n",
    "internal/jobs/mount.go": """package jobs

import (
	"context"
	"database/sql"
	"net/http"

	"github.com/labstack/echo/v4"
)

const Prefix = "/internal/jobs/"

type Job interface {
	Name() string
	Run(ctx context.Context, db *sql.DB) error
}

func handler(db *sql.DB, job Job) echo.HandlerFunc {
	return func(c echo.Context) error {
		if err := job.Run(c.Request().Context(), db); err != nil {
			return err
		}
		return c.NoContent(http.StatusNoContent)
	}
}

func Mount(e *echo.Echo, db *sql.DB, job Job) {
	e.POST(Prefix+job.Name(), handler(db, job))
}
""",
    "internal/jobs/reminder.go": """package jobs

import (
	"context"
	"database/sql"
)

type Reminder struct{}

func NewReminder() *Reminder { return &Reminder{} }

func (r *Reminder) Name() string { return "reminders" }

func (r *Reminder) Run(ctx context.Context, db *sql.DB) error {
	_, err := db.ExecContext(ctx, "UPDATE orders SET reminded_at = now() WHERE reminded_at IS NULL")
	return err
}
""",
    "internal/jobs/cleanup.go": """package jobs

import (
	"context"
	"database/sql"
)

type Cleanup struct{}

func (c *Cleanup) Name() string { return "cleanup" }

func (c *Cleanup) Run(ctx context.Context, db *sql.DB) error {
	_, err := db.ExecContext(ctx, "DELETE FROM carts WHERE id = ''")
	return err
}
""",
    "cmd/api/main.go": """package main

import (
	"database/sql"

	"github.com/labstack/echo/v4"

	"example.com/jobs-go/internal/jobs"
)

func main() {
	e := echo.New()
	var db *sql.DB
	j := jobs.NewReminder()
	jobs.Mount(e, db, j)
	jobs.Mount(e, db, &jobs.Cleanup{})
	_ = e.Start(":8080")
}
""",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def test_scan_go_template_route_gone_and_one_write_per_route(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    from bizmap.scan import scan_path

    _write(tmp_path, JOBS_GO)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=False)
    g = scan_path(tmp_path)
    https = sorted(n.attrs.get("path") for n in g.nodes.values() if n.type == "entrypoint" and n.attrs.get("kind") == "http")
    assert https == ["/internal/jobs/cleanup", "/internal/jobs/reminders"], https
    out = tmp_path / "bizmap.graph.json"
    out.write_text(json.dumps(g.to_dict()), encoding="utf-8")
    r = who(Index.load(out), "table:orders", "writes")
    entries = [e["entrypoint"]["label"] for row in r["results"] for e in row["entrypoints"]]
    assert entries == ["POST /internal/jobs/reminders"], r["results"]


SHOP_FUNCTIONS = {
    "package.json": '{"name": "shop-functions", "main": "lib/index.js", "dependencies": '
                    '{"firebase-admin": "^12.0.0", "firebase-functions": "^5.0.0"}}\n',
    "src/index.ts": "export * as orders from './orders';\n",
    "src/orders.ts": """import { getFirestore } from "firebase-admin/firestore";
import { onCall, onRequest } from "firebase-functions/v2/https";
import { onDocumentCreated, onDocumentUpdated } from "firebase-functions/v2/firestore";

const db = getFirestore();
const ORDERS = 'orders';

export const markPaid = onCall(async (req) => {
  const id = req.data.id as string;
  const batch = db.batch();
  batch.update(db.doc(`${ORDERS}/${id}`), { status: 'paid' });
  batch.set(db.doc('notes/' + id), { text: 'paid' });
  await batch.commit();
});

export const touch = onRequest(async (req, res) => {
  await db.collection('orders').doc(String(req.query.id)).update({ touched: true });
  res.sendStatus(204);
});

export const onOrderCreated = onDocumentCreated('orders/{id}', async (event) => {});
export const onOrderUpdated = onDocumentUpdated('orders/{id}', async (event) => {});
""",
}


def test_scan_firebase_functions_doc_paths_and_trigger_events(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_path

    _write(tmp_path, SHOP_FUNCTIONS)
    g = scan_path(tmp_path)
    writes = {(e.from_id, e.to_id) for e in g.edges.values() if e.type == "writes" and not e.attrs.get("derived")}
    assert ("function:src.orders.markPaid", "collection:orders") in writes, sorted(writes)
    assert ("function:src.orders.markPaid", "collection:notes") in writes
    assert not any(t == "collection:unknown" for _f, t in writes)
    out = tmp_path / "bizmap.graph.json"
    out.write_text(json.dumps(g.to_dict()), encoding="utf-8")
    r = who(Index.load(out), "collection:orders", "writes")
    assert "markPaid" in {row["function"]["label"].rsplit(".", 1)[-1] for row in r["results"]}
    assert not r["unresolved"]
    # `.update()` fires onDocumentUpdated, never onDocumentCreated
    fired = {(e.from_id, e.to_id) for e in g.edges.values() if e.type == "triggers" and e.to_id.startswith("entrypoint:firestore:")}
    created = [f for f, t in fired if t == "entrypoint:firestore:orders:created"]
    updated = [f for f, t in fired if t == "entrypoint:firestore:orders:updated"]
    assert created == [] or all("markPaid" not in f and "touch" not in f for f in created), created
    assert any("touch" in f for f in updated) and any("markPaid" in f for f in updated), fired
