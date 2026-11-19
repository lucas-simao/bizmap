"""v0.2.1 precision fixes — synthetic fixtures only (orders, users, tenants, documents, jobs)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.graph import Graph, collection_id, function_id, table_id, view_id
from bizmap.scan import scan_path
from bizmap.schema_replay import replay_sql
from bizmap.why import format_why


def _edge(graph: Graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def _write(tmp: Path, rel: str, source: str) -> None:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


PG_DUMP_SQL = r'''
CREATE TABLE IF NOT EXISTS "public"."orders" (
    "id" text NOT NULL,
    "status" text
);
CREATE TABLE IF NOT EXISTS "public"."audit_log" (
    "id" bigint NOT NULL,
    "action" text
);
CREATE TABLE IF NOT EXISTS "public"."documents" (
    "id" text NOT NULL,
    "body" text
);

CREATE OR REPLACE FUNCTION "public"."log_order_write"() RETURNS "pg_catalog"."trigger"
    LANGUAGE "plpgsql"
    AS $BODY$
DECLARE
    v_next bigint;
    n int;
BEGIN
    -- skip any leftover SELECT from the timestamp
    SELECT COALESCE(MAX(n), 0) + 1 INTO v_next FROM "public"."orders";
    IF FOUND THEN
        PERFORM pg_notify('orders', 'x');
    END IF;
    INSERT INTO "public"."audit_log" (action) VALUES (TG_OP);
    RETURN NEW;
END;
$BODY$;

CREATE OR REPLACE FUNCTION "public"."touch_documents"() RETURNS "pg_catalog"."trigger"
    LANGUAGE "plpgsql"
    AS $BODY$
BEGIN
    UPDATE "public"."documents" SET body = coalesce(body, '') WHERE id = NEW.id;
    RETURN NEW;
END;
$BODY$;

CREATE OR REPLACE TRIGGER "trg_orders_audit" AFTER INSERT OR UPDATE ON "public"."orders"
FOR EACH ROW
EXECUTE FUNCTION "public"."log_order_write"();

CREATE TRIGGER "trg_documents_touch" AFTER INSERT ON "public"."documents"
FOR EACH ROW
EXECUTE FUNCTION "public"."touch_documents"();
'''


JUNK_SQL = r'''
CREATE TABLE orders (id text, status text);
CREATE TABLE audit_log (id int, action text);
CREATE OR REPLACE FUNCTION bump() RETURNS trigger AS $$
DECLARE
    v_next bigint;
    found boolean;
    n int;
BEGIN
    -- SELECT * FROM any leftover the timestamp
    /* UPDATE perform SET found = 1 */
    SELECT COALESCE(MAX(n), 0) + 1 INTO v_next FROM orders;
    IF FOUND THEN
        PERFORM pg_notify('x', 'y');
        RAISE NOTICE 'if any';
    END IF;
    INSERT INTO audit_log (action) VALUES ('x');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
'''


def test_pg_dump_quoted_functions_and_triggers() -> None:
    schema = replay_sql(PG_DUMP_SQL)
    assert "log_order_write" in schema.functions
    assert "touch_documents" in schema.functions
    assert "trg_orders_audit" in schema.triggers
    assert "trg_documents_touch" in schema.triggers
    assert schema.triggers["trg_orders_audit"].function == "log_order_write"
    assert schema.triggers["trg_documents_touch"].function == "touch_documents"
    assert schema.triggers["trg_orders_audit"].table == "orders"
    log_fn = schema.functions["log_order_write"]
    assert any(a.table == "audit_log" and a.kind == "write" for a in log_fn.accesses)
    assert any(a.table == "orders" for a in log_fn.accesses)
    touch = schema.functions["touch_documents"]
    assert any(a.table == "documents" and a.kind == "write" for a in touch.accesses)


def test_plpgsql_junk_names_are_unresolved_not_nodes(tmp_path: Path) -> None:
    schema = replay_sql(JUNK_SQL)
    fn = schema.functions["bump"]
    tables = {a.table.lower() for a in fn.accesses if a.table}
    for junk in ("any", "perform", "found", "n", "if", "the", "timestamp", "v_next"):
        assert junk not in tables, tables
        assert junk not in {u.lower() for u in fn.unresolved}
    assert "orders" in tables
    assert "audit_log" in tables
    _write(tmp_path, "schema.sql", JUNK_SQL)
    graph = scan_path(tmp_path)
    for junk in ("any", "perform", "found", "if", "the", "timestamp", "v_next"):
        assert f"table:{junk}" not in graph.nodes
    assert "table:orders" in graph.nodes
    assert "table:audit_log" in graph.nodes
    sql_fn = graph.get(function_id("sql.bump"))
    assert sql_fn is not None
    unresolved = {str(x).lower() for x in (sql_fn.attrs.get("unresolved") or [])}
    for junk in ("any", "perform", "found", "if", "the", "timestamp"):
        assert junk not in unresolved


def test_sql_dicts_and_callable_injection(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _write(
        tmp_path,
        "queries.py",
        '''
QUERIES = {
    "list": "SELECT id, status FROM orders",
    "ins": "INSERT INTO orders (id, status) VALUES ('1', 'open')",
}
''',
    )
    _write(
        tmp_path,
        "repo.py",
        '''
from dataclasses import dataclass
from queries import QUERIES

def run_query(sql: str) -> None:
    pass

def load(key: str) -> None:
    run_query(QUERIES[key])

def load_one() -> None:
    run_query(QUERIES["list"])

def persist() -> None:
    run_query("UPDATE orders SET status = 'paid'")

def apply_sql(run_query):
    run_query("SELECT status FROM orders")

@dataclass
class Repo:
    save: object

def handle(repo: Repo) -> None:
    repo.save()

def entry() -> None:
    handle(Repo(save=persist))
    apply_sql(persist)
''',
    )
    _write(
        tmp_path,
        "app.py",
        '''
from fastapi import FastAPI
from repo import entry, load, load_one

app = FastAPI()

@app.post("/jobs")
def create_job():
    entry()
    load("ins")
    load_one()
    return {"ok": True}
''',
    )
    graph = scan_path(tmp_path)
    load = function_id("repo.load")
    load_one = function_id("repo.load_one")
    persist = function_id("repo.persist")
    handle = function_id("repo.handle")
    entry = function_id("repo.entry")
    apply_sql = function_id("repo.apply_sql")
    assert any(e.from_id == load and "orders" in e.to_id for e in graph.edges.values())
    assert any(e.from_id == load_one and e.type == "reads" and "orders" in e.to_id for e in graph.edges.values())
    assert any(e.from_id == apply_sql and e.type == "reads" and "orders" in e.to_id for e in graph.edges.values())
    assert _edge(graph, "calls", handle, persist) is not None
    assert _edge(graph, "calls", apply_sql, persist) is not None
    assert _edge(graph, "calls", entry, handle) is not None or any(
        e.type == "calls" and e.from_id == entry for e in graph.edges.values()
    )
    ep = graph.get("entrypoint:http:POST:/jobs")
    assert ep is not None
    assert any(
        e.from_id == ep.id and e.type in {"reads", "writes"} and "orders" in e.to_id
        for e in graph.edges.values()
    )


def test_views_derive_from_and_why_show_through(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "schema.sql",
        """
        CREATE TABLE orders (id text, status text, user_id text);
        CREATE TABLE users (id text, email text);
        CREATE VIEW order_summaries AS
            SELECT o.id, o.status, u.email
            FROM orders o JOIN users u ON u.id = o.user_id;
        CREATE MATERIALIZED VIEW open_orders AS
            SELECT id, status FROM orders WHERE status = 'open';
        """,
    )
    _write(
        tmp_path,
        "alembic/versions/0003_view.py",
        '''
revision = "0003"
down_revision = None
from alembic import op

def upgrade():
    op.execute("CREATE VIEW documents_v AS SELECT id, body FROM documents")
''',
    )
    _write(
        tmp_path,
        "repo.py",
        '''
def list_summaries(conn):
    conn.execute("SELECT id, status FROM order_summaries")
''',
    )
    graph = scan_path(tmp_path)
    assert "view:order_summaries" in graph.nodes
    assert graph.nodes["view:order_summaries"].attrs.get("kind") == "view"
    assert _edge(graph, "derives_from", view_id("order_summaries"), table_id("orders"))
    assert _edge(graph, "derives_from", view_id("order_summaries"), table_id("users"))
    assert "view:open_orders" in graph.nodes
    assert graph.nodes["view:open_orders"].attrs.get("materialized") is True
    reader = function_id("repo.list_summaries")
    assert any(
        e.from_id == reader and e.to_id in {view_id("order_summaries"), "column:order_summaries.status"}
        for e in graph.edges.values()
    )
    why_orders = format_why(graph, "orders.status")
    assert "list_summaries" in why_orders
    why_view = format_why(graph, "order_summaries")
    assert "derives from:" in why_view
    assert "orders" in why_view
    assert "view:documents_v" in graph.nodes
    assert _edge(graph, "derives_from", view_id("documents_v"), table_id("documents"))


def test_firestore_document_refs_transactions_batches(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.py",
        '''
from firebase_functions import https_fn

db = object()

@https_fn.on_request()
def ingest(req):
    col = db.collection("jobs")
    ref = col.document("1")
    ref.set({"status": "queued"})
    doc_ref = db.collection("documents").document("d1")
    doc_ref.update({"body": "x"})
    doc_ref.get()
    other = db.collection("tenants").document("t")
    other.delete()
    tx = object()
    tx.update(ref, {"status": "running"})
    batch = object()
    batch.set(doc_ref, {"body": "y"})
    batch.delete(other)
''',
    )
    graph = scan_path(tmp_path)
    ingest = function_id("main.ingest")
    assert _edge(graph, "writes", ingest, collection_id("jobs"))
    assert _edge(graph, "writes", ingest, collection_id("documents"))
    assert _edge(graph, "reads", ingest, collection_id("documents"))
    assert _edge(graph, "writes", ingest, collection_id("tenants"))


def test_queue_consumer_link_path_suffix_and_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "bizmap.yaml",
        """
name: platform
services:
  worker: WORKER_URL
queues:
  ingest: ingest
  jobs: on_jobs
""",
    )
    _write(
        tmp_path,
        "worker.py",
        '''
from firebase_functions import https_fn, pubsub_fn, tasks_fn
import os

@https_fn.on_request()
def ingest(req):
    return {"ok": True}

@tasks_fn.on_task_dispatched()
def handle_task(req):
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

def enqueue():
    base = os.environ["WORKER_URL"]
    tasks_v2.CloudTasksClient().create_task(
        task={"http_request": {"http_method": "POST", "url": base + "/ingest"}}
    )
    pubsub_v1.PublisherClient().publish("jobs", b"x")
''',
    )
    graph = scan_path(tmp_path)
    produce = function_id("produce.enqueue")
    assert any(e.type == "enqueues" and e.from_id == produce for e in graph.edges.values())
    ingest_eps = [
        n
        for n in graph.nodes.values()
        if n.type == "entrypoint" and (n.attrs.get("path") == "/ingest" or str(n.attrs.get("symbol") or "").endswith("ingest"))
    ]
    assert ingest_eps
    assert any(
        e.type == "triggers" and e.from_id.startswith("queue:") and e.to_id == ingest_eps[0].id
        for e in graph.edges.values()
    ) or any(
        e.type == "calls_http" and e.from_id == produce and e.to_id == ingest_eps[0].id
        for e in graph.edges.values()
    )
    pubsub_eps = [n for n in graph.nodes.values() if n.type == "entrypoint" and n.attrs.get("kind") == "pubsub"]
    assert pubsub_eps
    assert any(
        e.type in {"enqueues", "triggers"} and "jobs" in (e.to_id + e.from_id)
        for e in graph.edges.values()
    )


def test_http_env_base_and_unresolved_summary(tmp_path: Path) -> None:
    billing = tmp_path / "billing"
    store = tmp_path / "store"
    _write(billing, "bizmap.yaml", "name: billing\nservices:\n  billing:\n    url: http://billing.internal\n    env: BILLING_URL\n")
    _write(
        billing,
        "api.py",
        '''
from fastapi import FastAPI
app = FastAPI()

@app.post("/api/invoices")
def create_invoice():
    return {"ok": True}
''',
    )
    _write(store, "bizmap.yaml", "name: store\nservices:\n  billing:\n    url: http://billing.internal\n    env: BILLING_URL\n")
    _write(
        store,
        "client.py",
        '''
import os
import httpx

class settings:
    billing_url = None

def pay():
    httpx.post(os.environ["BILLING_URL"] + "/api/invoices")

def charge():
    httpx.post(settings.billing_url + "/api/invoices")

def unknown_host():
    httpx.post("http://payments.internal/api/charges")
''',
    )
    from bizmap.scan import scan_paths

    graph = scan_paths([billing, store])
    pay = [n.id for n in graph.nodes.values() if n.id.endswith("function:client.pay") or n.id.endswith("/function:client.pay")]
    assert pay
    assert any(e.type == "calls_http" and e.from_id in pay for e in graph.edges.values())
    unresolved = graph.attrs.get("unresolved_http") or []
    assert unresolved
    keys = {k for item in unresolved for k in item.get("keys") or []}
    assert "payments.internal" in keys or any("payments" in k for k in keys)

    runner = CliRunner()
    out = tmp_path / "g.json"
    result = runner.invoke(main, ["scan", str(store), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "unresolved HTTP calls" in result.output
    assert "payments.internal" in result.output or "BILLING_URL" in result.output
