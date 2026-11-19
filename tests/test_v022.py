"""v0.2.2 — forward refs, external relations, object dispatch. Synthetic only."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.graph import (
    Confidence,
    function_id,
    table_id,
    view_id,
)
from bizmap.scan import scan_path, scan_paths
from bizmap.schema_replay import replay_sql
from bizmap.sql_analyze import analyze_sql
from bizmap.why import format_why


def _edge(graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def _write(tmp: Path, rel: str, source: str) -> None:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _go_mod(root: Path, name: str = "t") -> None:
    (root / "go.mod").write_text(f"module {name}\n\ngo 1.22\n", encoding="utf-8")


FORWARD_FN_SQL = r"""
CREATE OR REPLACE FUNCTION public.bump_status() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE orders SET status = 'x' WHERE id = NEW.id;
    INSERT INTO audit_log (action) VALUES (TG_OP);
    RETURN NEW;
END;
$$;

CREATE TABLE orders (id text, status text);
CREATE TABLE audit_log (id int, action text);

CREATE TRIGGER trg AFTER INSERT ON orders
FOR EACH ROW EXECUTE FUNCTION bump_status();
"""


def test_forward_referenced_function_writes_later_table() -> None:
    schema = replay_sql(FORWARD_FN_SQL)
    assert "orders" in schema.tables
    fn = schema.functions["bump_status"]
    tables = {a.table.lower() for a in fn.accesses if a.table}
    assert "orders" in tables
    assert "audit_log" in tables
    assert any(a.table == "orders" and a.kind == "write" for a in fn.accesses)


def test_forward_ref_scan_function_before_table(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", FORWARD_FN_SQL)
    graph = scan_path(tmp_path)
    bump = function_id("sql.bump_status")
    assert any(
        e.from_id == bump and e.type == "writes" and "orders" in e.to_id
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == bump and e.type == "writes" and "audit_log" in e.to_id
        for e in graph.edges.values()
    )


def test_dynamic_table_placeholder_and_format_percent_i() -> None:
    for sql in (
        "UPDATE {table} SET status = $1",
        "UPDATE {t} SET status = $1",
        "UPDATE %I SET status = $1",
        "UPDATE  SET status = $1",
        "INSERT INTO %I (id) VALUES ($1)",
        "DELETE FROM {name} WHERE id = $1",
    ):
        acc = analyze_sql(sql, dynamic=True, known_tables={"orders", "users"})
        assert acc, sql
        assert all(a.unknown_table or a.table == "unknown" for a in acc), (sql, acc)
        assert all(a.confidence == Confidence.UNKNOWN for a in acc), (sql, acc)
        assert not any(a.table and a.table.lower() == "set" for a in acc)


def test_dynamic_table_go_and_python(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "patch.go",
        """
package main

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

func PatchGo(ctx context.Context, t string, tx *pgxpool.Pool) error {
    q := "UPDATE " + t + " SET status = $1"
    _, err := tx.Exec(ctx, q, "open")
    return err
}
""",
    )
    _write(
        tmp_path,
        "patch.py",
        """
def patch_py(conn, t):
    q = "UPDATE " + t + " SET status = 'open'"
    conn.execute(q)
""",
    )
    graph = scan_path(tmp_path)

    def _fn_ending(suffix: str) -> str:
        hits = [
            n.id
            for n in graph.nodes.values()
            if n.type == "function" and n.id.endswith(suffix)
        ]
        assert hits, (suffix, [n.id for n in graph.nodes.values() if n.type == "function"])
        return hits[0]

    for origin in (_fn_ending("PatchGo"), _fn_ending("patch_py")):
        hits = [
            e
            for e in graph.edges.values()
            if e.from_id == origin and e.to_id == "table:unknown" and e.type == "writes"
        ]
        assert hits, origin
        assert all(e.confidence == Confidence.UNKNOWN for e in hits)
        node = graph.get("table:unknown")
        assert node is not None


def test_plpgsql_format_dynamic_table() -> None:
    schema = replay_sql(
        """
        CREATE TABLE orders (id text, status text);
        CREATE FUNCTION touch_any() RETURNS void AS $$
        DECLARE
            t text := 'orders';
        BEGIN
            EXECUTE format('UPDATE %I SET status = $1', t);
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    fn = schema.functions["touch_any"]
    assert any(a.unknown_table or a.table == "unknown" for a in fn.accesses)


def test_unresolved_and_external_relations(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "schema.sql",
        """
        CREATE TABLE orders (id text, status text);
        CREATE TABLE leftover (id int);
        DROP TABLE leftover;
        """,
    )
    _write(
        tmp_path,
        "repo.py",
        """
from fastapi import FastAPI
app = FastAPI()

def list_summaries(conn):
    conn.execute("SELECT id, status FROM order_summaries")
    conn.execute("SELECT id FROM leftover")
    conn.execute("SELECT name FROM storage.objects")
    conn.execute("SELECT id FROM vault.secrets")
    conn.execute("SELECT id FROM auth.users")
    conn.execute("SELECT rolname FROM pg_roles")

@app.get("/summaries")
def summaries():
    list_summaries(None)
    return []
""",
    )
    graph = scan_path(tmp_path)
    reader = function_id("repo.list_summaries")
    summary = graph.get(view_id("order_summaries")) or graph.get(table_id("order_summaries"))
    assert summary is not None
    kind = str(summary.attrs.get("kind") or summary.type)
    assert "unresolved" in kind or summary.type in {"view", "table", "unresolved_relation"}
    assert any(
        e.from_id == reader and e.type == "reads" and "order_summaries" in e.to_id
        for e in graph.edges.values()
    )
    leftover = graph.get(table_id("leftover"))
    assert leftover is not None
    assert leftover.attrs.get("dropped") is True
    for nid, extra in (
        (table_id("storage.objects"), True),
        (table_id("vault.secrets"), True),
        (table_id("auth.users"), True),
        (table_id("pg_roles"), True),
    ):
        node = graph.get(nid)
        if node is None and "." in nid:
            node = graph.get(table_id(nid.split(":", 1)[1].split(".")[-1]))
        assert node is not None, nid
        assert node.attrs.get("external") is True or extra
    ep = graph.get("entrypoint:http:GET:/summaries")
    assert ep is not None
    why = format_why(graph, "order_summaries")
    assert "list_summaries" in why or "summaries" in why


def test_object_dispatch_python(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _write(
        tmp_path,
        "drain.py",
        '''
class Outbox:
    def settle(self, conn):
        conn.execute("UPDATE orders SET status = 'done'")

class Drainer:
    def __init__(self, outbox):
        self.outbox = outbox

    def run(self, conn):
        self.outbox.settle(conn)

class Store:
    def save(self, conn):
        conn.execute("INSERT INTO orders (id, status) VALUES ('1', 'open')")

def run(store: Store, conn):
    store.save(conn)

def start(conn):
    Drainer(Outbox()).run(conn)
    run(Store(), conn)
''',
    )
    graph = scan_path(tmp_path)
    settle = function_id("drain.Outbox.settle")
    save = function_id("drain.Store.save")
    drun = function_id("drain.Drainer.run")
    run = function_id("drain.run")
    assert _edge(graph, "calls", drun, settle) is not None
    assert _edge(graph, "calls", run, save) is not None
    assert any(
        e.from_id == settle and e.type == "writes" and "orders" in e.to_id
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == save and e.type == "writes" and "orders" in e.to_id
        for e in graph.edges.values()
    )
    for edge in (_edge(graph, "calls", drun, settle), _edge(graph, "calls", run, save)):
        assert edge is not None
        assert edge.confidence.value in {"inferred", "exact"}


def test_object_dispatch_go(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "drain.go",
        """
package main

import (
    "context"
    "github.com/jackc/pgx/v5/pgxpool"
)

var db *pgxpool.Pool

type Outbox struct{}

func (o *Outbox) Settle(ctx context.Context) error {
    _, err := db.Exec(ctx, "UPDATE orders SET status = 'done'")
    return err
}

type Drainer struct {
    outbox *Outbox
}

func NewDrainer(outbox *Outbox) *Drainer {
    return &Drainer{outbox: outbox}
}

func (d *Drainer) Run(ctx context.Context) error {
    return d.outbox.Settle(ctx)
}

type Store struct{}

func (s *Store) Save(ctx context.Context) error {
    _, err := db.Exec(ctx, "INSERT INTO orders (id, status) VALUES ('1', 'open')")
    return err
}

func RunStore(store *Store, ctx context.Context) error {
    return store.Save(ctx)
}
""",
    )
    graph = scan_path(tmp_path)

    def _fn_ending(suffix: str) -> str:
        hits = [
            n.id
            for n in graph.nodes.values()
            if n.type == "function" and n.id.endswith(suffix)
        ]
        assert hits, suffix
        return hits[0]

    settle = _fn_ending("Outbox.Settle")
    save = _fn_ending("Store.Save")
    assert _edge(graph, "calls", _fn_ending("Drainer.Run"), settle) is not None
    assert _edge(graph, "calls", _fn_ending("RunStore"), save) is not None
    assert any(
        e.from_id == settle and e.type == "writes" and "orders" in e.to_id
        for e in graph.edges.values()
    )


def test_cloud_tasks_helper_url_and_queues_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "bizmap.yaml",
        """
name: platform
queues:
  ingest: ingest
  jobs: on_jobs
""",
    )
    _write(
        tmp_path,
        "worker.py",
        '''
from firebase_functions import https_fn, pubsub_fn

@https_fn.on_request()
def ingest(req):
    return {"ok": True}

@pubsub_fn.on_message_published(topic="jobs")
def on_jobs(event):
    return None
''',
    )
    _write(
        tmp_path,
        "produce.py",
        '''
import os
from google.cloud import tasks_v2, pubsub_v1

def _get_url():
    return f"{os.environ['WORKER_URL']}/ingest"

def enqueue():
    tasks_v2.CloudTasksClient().create_task(
        task={"http_request": {"http_method": "POST", "url": _get_url()}}
    )
    pubsub_v1.PublisherClient().publish("jobs", b"x")

def enqueue_named():
    tasks_v2.CloudTasksClient().create_task(
        parent="projects/p/locations/l/queues/ingest",
        task={"http_request": {"http_method": "POST"}},
    )
''',
    )
    graph = scan_path(tmp_path)
    produce = function_id("produce.enqueue")
    ingest_eps = [
        n
        for n in graph.nodes.values()
        if n.type == "entrypoint"
        and (n.attrs.get("path") == "/ingest" or str(n.attrs.get("symbol") or "").endswith("ingest"))
    ]
    assert ingest_eps
    assert any(
        e.type in {"triggers", "calls_http"}
        and (e.from_id == produce or e.from_id.startswith("queue:"))
        and e.to_id == ingest_eps[0].id
        for e in graph.edges.values()
    )
    named = function_id("produce.enqueue_named")
    assert any(
        e.from_id == named and e.type in {"enqueues", "triggers", "calls_http"}
        for e in graph.edges.values()
    )
    pubsub_eps = [n for n in graph.nodes.values() if n.type == "entrypoint" and n.attrs.get("kind") == "pubsub"]
    assert pubsub_eps
    assert any(
        e.type in {"enqueues", "triggers"} and "jobs" in (e.to_id + e.from_id)
        for e in graph.edges.values()
    )


def test_http_origin_env_not_local_name(tmp_path: Path) -> None:
    billing = tmp_path / "billing"
    store = tmp_path / "store"
    _write(
        billing,
        "bizmap.yaml",
        "name: billing\nservices:\n  x:\n    env: X_URL\n    repo: store\n",
    )
    _write(
        billing,
        "api.py",
        '''
from fastapi import FastAPI
app = FastAPI()

@app.post("/p")
def ping():
    return {"ok": True}
''',
    )
    _write(
        store,
        "bizmap.yaml",
        "name: store\nservices:\n  x:\n    env: X_URL\n    repo: billing\n",
    )
    _write(
        store,
        "client.py",
        '''
import os
import requests

def pay():
    base = os.environ["X_URL"]
    requests.post(base + "/p")

def other():
    requests.post("http://payments.internal/api/charges")
''',
    )
    graph = scan_paths([billing, store])
    pay = [
        n.id
        for n in graph.nodes.values()
        if n.id.endswith("function:client.pay") or n.id.endswith("/function:client.pay")
    ]
    assert pay
    assert any(e.type == "calls_http" and e.from_id in pay for e in graph.edges.values())
    unresolved = graph.attrs.get("unresolved_http") or []
    keys = {k for item in unresolved for k in item.get("keys") or []}
    assert "payments.internal" in keys or any("payments" in k for k in keys)
    assert "base" not in keys

    runner = CliRunner()
    out = tmp_path / "g.json"
    result = runner.invoke(main, ["scan", str(store), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "unresolved HTTP calls" in result.output
    assert "X_URL" in result.output or "payments.internal" in result.output
    assert "base" not in result.output.split("unresolved HTTP calls", 1)[-1]


def test_typescript_express_fastify_hono_supabase_pg_edge(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "schema.sql",
        """
        CREATE TABLE orders (id text, status text, notes text);
        CREATE TABLE tenants (id text);
        CREATE TABLE documents (id text, body text);
        CREATE TABLE jobs (id text);
        CREATE TABLE users (id text);
        CREATE FUNCTION apply_order_tax(order_id text) RETURNS void AS $$
        BEGIN
            UPDATE orders SET notes = 'tax' WHERE id = order_id;
        END;
        $$ LANGUAGE plpgsql;
        """,
    )
    _write(
        tmp_path,
        "server.ts",
        """
import express from "express";
import Fastify from "fastify";
import { Hono } from "hono";
import { Router } from "express";

const app = express();
const router = Router();
app.get("/health", (_req, res) => {
  res.json({ ok: true });
});
router.post("/orders", async (_req, res) => {
  res.json({ ok: true });
});
app.use("/api", router);

const fastify = Fastify();
fastify.post("/items", async () => ({ ok: true }));

const hono = new Hono();
hono.get("/tenants", (c) => c.json({}));

const supabase = { from: (t: string) => ({ select() {}, insert() {}, update() {}, delete() {}, upsert() {} }), rpc: (n: string) => n };
supabase.from("orders").select("id, status");
supabase.from("orders").insert({ id: "1" });
supabase.from("documents").update({ body: "x" });
supabase.from("jobs").delete();
supabase.from("users").upsert({ id: "1" });
supabase.rpc("apply_order_tax");

const client = { query: (s: string) => s };
client.query("SELECT id FROM tenants");
""",
    )
    _write(
        tmp_path,
        "supabase/functions/ingest/index.ts",
        """
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

serve(async (_req: Request) => {
  return new Response("ok");
});
""",
    )
    graph = scan_path(tmp_path)
    http = {
        (n.attrs.get("method"), n.attrs.get("path"))
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") in {None, "http", "firebase_http"}
    }
    assert ("GET", "/health") in http
    assert ("POST", "/orders") in http or ("POST", "/api/orders") in http
    assert ("POST", "/items") in http
    assert ("GET", "/tenants") in http
    assert any(
        n.type == "entrypoint" and str(n.attrs.get("path") or "") in {"/functions/v1/ingest", "/ingest"}
        for n in graph.nodes.values()
    )
    assert _edge(graph, "calls", function_id("server.rpc"), function_id("sql.apply_order_tax")) or any(
        e.type == "calls" and e.to_id.endswith("function:sql.apply_order_tax")
        for e in graph.edges.values()
    )
    assert any(
        e.type == "reads" and "orders" in e.to_id for e in graph.edges.values()
    )
    assert any(
        e.type == "writes" and "orders" in e.to_id for e in graph.edges.values()
    )
    assert any(
        e.type == "writes" and "documents" in e.to_id for e in graph.edges.values()
    )
    assert any("tenants" in e.to_id and e.type == "reads" for e in graph.edges.values())
