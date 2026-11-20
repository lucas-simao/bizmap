"""v0.2 — Python stacks, Firestore, queues, registry. Synthetic only."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.graph import Graph, collection_id, column_id, function_id, table_id
from bizmap.scan import scan_path, scan_paths
from bizmap.schema_replay import replay_sql
from bizmap.sql_analyze import analyze_sql
from bizmap.why import format_why


def _edge(graph: Graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def _write(tmp: Path, rel: str, source: str) -> None:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_pg_catalog_and_information_schema_are_external() -> None:
    for sql in (
        "SELECT * FROM pg_catalog.pg_class",
        "SELECT column_name FROM information_schema.columns",
        "SELECT rolname FROM pg_roles",
    ):
        acc = analyze_sql(sql)
        assert acc, sql
        assert all(a.external for a in acc), (sql, acc)


def test_create_policy_and_plpgsql_body() -> None:
    schema = replay_sql(
        """
        CREATE TABLE orders (id text, tenant_id text, status text);
        CREATE TABLE audit_log (id int, action text);
        CREATE OR REPLACE FUNCTION bump_status() RETURNS trigger AS $$
        BEGIN
            UPDATE orders SET status = 'x' WHERE id = NEW.id;
            INSERT INTO audit_log (action) VALUES (TG_OP);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg AFTER INSERT ON orders
        FOR EACH ROW EXECUTE FUNCTION bump_status();
        ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
        CREATE POLICY orders_select ON orders
            FOR SELECT TO app_user
            USING (tenant_id = current_setting('app.tenant_id', true));
        """
    )
    assert schema.tables["orders"].rls
    names = {p.name for p in schema.tables["orders"].policies}
    assert "orders_select" in names
    fn = schema.functions["bump_status"]
    assert any(a.table == "orders" and a.kind == "write" and a.column == "status" for a in fn.accesses)
    assert any(a.table == "audit_log" and a.kind == "write" for a in fn.accesses)
    assert schema.triggers["trg"].function == "bump_status"


def test_sqlalchemy_orm_and_alembic(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "models.py",
        """
from sqlalchemy import Column, Integer, String, Table, MetaData, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(primary_key=True)
    email: Mapped[str]

metadata = MetaData()
orders = Table(
    "orders",
    metadata,
    Column("id", String, primary_key=True),
    Column("status", String),
    Column("user_id", String, ForeignKey("users.id")),
)
""",
    )
    _write(
        tmp_path,
        "alembic/versions/0001_init.py",
        """
revision = "0001"
down_revision = None
import sqlalchemy as sa
from alembic import op

def upgrade():
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String()),
    )
    op.add_column("users", sa.Column("tenant_id", sa.String()))
""",
    )
    _write(
        tmp_path,
        "alembic/versions/0002_notes.py",
        """
revision = "0002"
down_revision = "0001"
import sqlalchemy as sa
from alembic import op

def upgrade():
    op.add_column("orders", sa.Column("notes", sa.Text()))
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("tmp", sa.Integer()))
        batch.drop_column("tmp")
    op.execute("CREATE TABLE leftover (id int)")
    op.drop_table("leftover")
""",
    )
    _write(
        tmp_path,
        "repo.py",
        """
from sqlalchemy import insert, select, update, delete, text
from sqlalchemy.orm import Session
from models import User, orders

def write_all(session: Session):
    session.add(User(id="1", email="a@b.c"))
    session.execute(insert(orders).values(id="o", status="open"))
    session.execute(update(orders).values(status="paid"))
    session.execute(delete(orders))
    session.execute(select(orders.c.status))
    session.execute(text("SELECT email FROM users"))
""",
    )
    graph = scan_path(tmp_path)
    assert "table:users" in graph.nodes
    assert "table:orders" in graph.nodes
    assert "table:tenants" in graph.nodes
    assert column_id("orders", "notes") in graph.nodes
    assert "table:leftover" not in graph.nodes
    writer = function_id("repo.write_all")
    assert _edge(graph, "writes", writer, table_id("users")) or any(
        e.from_id == writer and e.type == "writes" and "users" in e.to_id for e in graph.edges.values()
    )
    assert any(
        e.from_id == writer and e.type == "writes" and e.to_id == column_id("orders", "status")
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == writer and e.type == "reads" and e.to_id == column_id("orders", "status")
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == writer and e.type == "reads" and "users" in e.to_id for e in graph.edges.values()
    )
    upgrade = graph.get(function_id("alembic.versions.0001_init.upgrade"))
    assert upgrade is not None
    assert upgrade.attrs.get("role") == "schema"
    why = format_why(graph, "orders.status")
    assert "defined/altered by" in why or "writers:" in why
    assert "0002_notes.upgrade" in why or "0001_init.upgrade" in why or "write_all" in why
    # schema definers should not be listed as runtime writers
    runtime_block = why.split("defined/altered by")[0]
    assert "0002_notes.upgrade" not in runtime_block


def test_python_route_constants_and_unresolved(tmp_path: Path) -> None:
    _write(tmp_path, "paths.py", 'PREFIX = "/api"\nITEMS = "/items"\n')
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import APIRouter, FastAPI
from .paths import PREFIX, ITEMS

app = FastAPI()
router = APIRouter(prefix=PREFIX)

@router.get(ITEMS)
def list_items():
    return []

@router.get(PREFIX + "/concat")
def concat():
    return []

@app.get(f"{PREFIX}/fmt")
def fmt():
    return []

from otherpkg import Missing

@app.get(Missing + "/setup")
def setup():
    return []

app.include_router(router)
""",
    )
    graph = scan_path(tmp_path)
    http = {
        (n.attrs.get("method"), n.attrs.get("path")): n
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    }
    assert ("GET", "/api/items") in http
    assert ("GET", "/api/concat") in http or ("GET", "/api/api/concat") in http
    assert ("GET", "/api/fmt") in http
    unresolved = [n for n in http.values() if n.attrs.get("unresolved")]
    assert unresolved
    for node in unresolved:
        path = str(node.attrs.get("path"))
        assert "setup" in path
        assert path != "/setup"
        assert node.attrs.get("confidence") == "unknown"


def test_firebase_collections_and_triggers(tmp_path: Path) -> None:
    _write(tmp_path, "names.py", 'JOBS = "jobs"\n')
    _write(
        tmp_path,
        "main.py",
        """
from firebase_functions import firestore_fn, https_fn
from .names import JOBS

db = object()

@https_fn.on_request()
def ingest(req):
    db.collection(JOBS).document("1").set({"status": "queued"})

@firestore_fn.on_document_created(document="jobs/{id}")
def on_created(event):
    db.collection("notifications").add({"ok": True})
""",
    )
    graph = scan_path(tmp_path)
    assert collection_id("jobs") in graph.nodes
    assert collection_id("notifications") in graph.nodes
    ingest = function_id("main.ingest")
    assert _edge(graph, "writes", ingest, collection_id("jobs"))
    created = [n for n in graph.nodes.values() if n.attrs.get("kind") == "firestore"]
    assert created
    # writer of jobs fires the firestore entrypoint
    assert any(
        e.type == "triggers" and e.from_id == ingest and e.to_id == created[0].id
        for e in graph.edges.values()
    )


def test_queues_http_and_worker(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tasks.py",
        """
from celery import Celery
app = Celery("demo")

@app.task
def refresh():
    return 1

def produce():
    refresh.delay()
    from google.cloud import tasks_v2
    tasks_v2.CloudTasksClient().create_task(
        task={"http_request": {"http_method": "POST", "url": "http://worker.internal/ingest"}}
    )
""",
    )
    _write(
        tmp_path,
        "svc.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.post("/ingest")
def ingest():
    return {"ok": True}

def run():
    while True:
        ingest()

if __name__ == "__main__":
    run()
""",
    )
    graph = scan_path(tmp_path)
    assert any(n.type == "queue" and n.attrs.get("kind") == "celery" for n in graph.nodes.values())
    assert any(n.type == "entrypoint" and n.attrs.get("kind") == "worker" for n in graph.nodes.values())
    produce = function_id("tasks.produce")
    assert any(e.type == "enqueues" and e.from_id == produce for e in graph.edges.values())
    assert any(e.type == "calls_http" and e.from_id == produce for e in graph.edges.values())


def test_supabase_rpc_links_sql_function(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "schema.sql",
        """
        CREATE TABLE orders (id text, notes text);
        CREATE FUNCTION apply_order_tax(order_id text) RETURNS void AS $$
        BEGIN
            UPDATE orders SET notes = 'tax' WHERE id = order_id;
        END;
        $$ LANGUAGE plpgsql;
        """,
    )
    _write(
        tmp_path,
        "rpc.py",
        """
def call_tax(supabase):
    supabase.rpc("apply_order_tax")
""",
    )
    graph = scan_path(tmp_path)
    assert _edge(graph, "calls", function_id("rpc.call_tax"), function_id("sql.apply_order_tax"))
    assert any(
        e.from_id == function_id("sql.apply_order_tax") and "orders" in e.to_id
        for e in graph.edges.values()
    )


def test_cross_repo_http_and_registry(tmp_path: Path) -> None:
    a = tmp_path / "catalog"
    b = tmp_path / "store"
    _write(a, "bizmap.yaml", "name: catalog\ndatabase: platform\nservices:\n  catalog: http://catalog.internal\n")
    _write(b, "bizmap.yaml", "name: store\ndatabase: platform\nservices:\n  catalog: http://catalog.internal\n")
    _write(
        a,
        "api.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.post("/api/orders")
def create():
    from sqlalchemy import text
    conn = type("C", (), {"execute": lambda self, s: None})()
    conn.execute("INSERT INTO orders (id, status) VALUES ('1', 'open')")
""",
    )
    _write(a, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _write(
        b,
        "client.py",
        """
import httpx
CATALOG = "http://catalog.internal"

def submit():
    httpx.post(CATALOG + "/api/orders")
""",
    )
    _write(b, "report.py", 'def read_orders(conn):\n    conn.execute("SELECT status FROM orders")\n')
    graph = scan_paths([a, b])
    assert graph.database == "platform"
    # shared table id
    assert "table:orders" in graph.nodes
    why = format_why(graph, "orders.status")
    assert "create" in why
    assert "read_orders" in why
    http_edges = [e for e in graph.edges.values() if e.type == "calls_http"]
    assert http_edges, list(graph.edges)[:20]


def test_pyplatform_example(pyplatform_root: Path) -> None:
    graph = scan_path(pyplatform_root)
    assert "table:orders" in graph.nodes
    assert column_id("orders", "status") in graph.nodes or column_id("orders", "notes") in graph.nodes
    assert collection_id("jobs") in graph.nodes
    http_paths = {
        n.attrs.get("path")
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    }
    assert any(p == "/api/orders" or str(p).endswith("/api/orders") for p in http_paths)
    assert any(n.attrs.get("unresolved") for n in graph.nodes.values() if n.type == "entrypoint")
    why = format_why(graph, "orders.status")
    assert "writers:" in why
    jobs_why = format_why(graph, "jobs")
    assert "collection:" in jobs_why or "jobs" in jobs_why


def test_express_routes_fetch_and_rpc(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "schema.sql",
        """
        CREATE TABLE orders (id text, notes text);
        CREATE FUNCTION apply_order_tax(order_id text) RETURNS void AS $$
        BEGIN
            UPDATE orders SET notes = 'tax' WHERE id = order_id;
        END;
        $$ LANGUAGE plpgsql;
        """,
    )
    _write(
        tmp_path,
        "server.js",
        """
const express = require("express");
const app = express();
app.get("/health", (_req, res) => res.json({ ok: true }));
app.post("/proxy/orders", async (_req, res) => {
  await fetch("http://catalog.internal/api/orders", { method: "POST" });
});
function tax() {
  supabase.rpc("apply_order_tax");
}
""",
    )
    _write(tmp_path, "bizmap.yaml", "name: web\nservices:\n  catalog: http://catalog.internal\n")
    _write(
        tmp_path,
        "api.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.post("/api/orders")
def create():
    return {"ok": True}
""",
    )
    graph = scan_path(tmp_path)
    http = {
        (n.attrs.get("method"), n.attrs.get("path"))
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    }
    assert ("GET", "/health") in http
    assert ("POST", "/proxy/orders") in http
    assert ("POST", "/api/orders") in http
    assert _edge(graph, "calls", function_id("server.rpc"), function_id("sql.apply_order_tax"))
    assert any(e.type == "calls_http" for e in graph.edges.values())


def test_cli_scan_multi_and_registry_merge(pyplatform_root: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    catalog = pyplatform_root / "catalog"
    storefront = pyplatform_root / "storefront"
    ga = tmp_path / "a.json"
    gb = tmp_path / "b.json"
    merged = tmp_path / "merged.json"
    r1 = runner.invoke(main, ["scan", str(catalog), "--out", str(ga)])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(main, ["scan", str(storefront), "--out", str(gb)])
    assert r2.exit_code == 0, r2.output
    r3 = runner.invoke(main, ["registry", "merge", str(ga), str(gb), "--out", str(merged)])
    assert r3.exit_code == 0, r3.output
    data = json.loads(merged.read_text(encoding="utf-8"))
    assert data["version"] == "0.3.10"
    why = runner.invoke(main, ["why", "orders.status", "--graph", str(merged)])
    assert why.exit_code == 0, why.output
    assert "writers:" in why.output
