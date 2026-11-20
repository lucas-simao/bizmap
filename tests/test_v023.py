"""v0.2.3 precision fixes — synthetic fixtures only (orders, users, tenants, documents, jobs, items)."""

from __future__ import annotations

import threading
from pathlib import Path

from bizmap.detect.go_lang import GoAnalyzer
from bizmap.detect.rules import load_rules
from bizmap.graph import GRAPH_VERSION, collection_id, function_id
from bizmap.http_match import match_http
from bizmap.scan import scan_path
from bizmap.sql_analyze import DYNAMIC_TABLE_HOLE, analyze_sql, has_dynamic_table_hole


def _write(tmp: Path, rel: str, source: str) -> None:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _go_mod(root: Path, name: str = "t") -> None:
    (root / "go.mod").write_text(f"module {name}\n\ngo 1.22\n", encoding="utf-8")


def _edge(graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def test_graph_version() -> None:
    assert GRAPH_VERSION == "0.4.1"


def test_go_group_prefix_same_var_two_funcs_does_not_hang(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "routes.go",
        """
package main

import "github.com/labstack/echo/v4"

func mountA(e *echo.Echo) {
    g := e.Group("/a")
    g.GET("/items", listA)
}

func mountB(e *echo.Echo) {
    g := e.Group("/b")
    g.GET("/items", listB)
}

func listA() {}
func listB() {}
""",
    )
    _write(
        tmp_path,
        "routes_test.go",
        """
package main

import "github.com/labstack/echo/v4"

func TestMountA(t *testing.T) {
    e := echo.New()
    g := e.Group("/ta")
    g.GET("/x", listA)
}

func TestMountB(t *testing.T) {
    e := echo.New()
    g := e.Group("/tb")
    g.GET("/y", listB)
}
""",
    )
    files = [tmp_path / "routes.go", tmp_path / "routes_test.go"]
    result: list = []
    err: list = []

    def run() -> None:
        try:
            result.append(GoAnalyzer(load_rules()).analyze(files, tmp_path))
        except Exception as exc:  # pragma: no cover
            err.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "Go Group prefix resolution hung"
    assert not err, err
    analysis = result[0]
    paths = {(r.method, r.path) for r in analysis.routes}
    assert ("GET", "/a/items") in paths
    assert ("GET", "/b/items") in paths
    assert ("GET", "/ta/x") in paths
    assert ("GET", "/tb/y") in paths


def test_go_group_passed_and_returned_keeps_prefix(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "main.go",
        """
package main

import "github.com/labstack/echo/v4"

func API(e *echo.Echo) *echo.Group {
    return e.Group("/api")
}

func mount(g *echo.Group) {
    g.GET("/items", listItems)
}

func orphan(g *echo.Group) {
    g.POST("/jobs", createJob)
}

func listItems() {}
func createJob() {}

func main() {
    e := echo.New()
    g := API(e)
    mount(g)
}
""",
    )
    analysis = GoAnalyzer(load_rules()).analyze([tmp_path / "main.go"], tmp_path)
    paths = {(r.method, r.path, r.path_unresolved) for r in analysis.routes}
    assert ("GET", "/api/items", False) in paths
    orphan = [r for r in analysis.routes if r.method == "POST"]
    assert orphan
    assert orphan[0].path_unresolved is True
    assert "{g}" in orphan[0].path or orphan[0].path.endswith("/jobs")
    assert orphan[0].path != "/jobs"


def test_insert_on_conflict_excluded_not_dynamic() -> None:
    sql = (
        "INSERT INTO items (id, status) VALUES ($1, $2) "
        "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, notes = items.notes"
    )
    assert has_dynamic_table_hole(sql) is False
    acc = analyze_sql(sql)
    tables = {a.table.lower() for a in acc}
    assert "unknown" not in tables
    assert "excluded" not in tables
    writes = {(a.table, a.column) for a in acc if a.kind == "write"}
    assert ("items", "id") in writes
    assert ("items", "status") in writes
    assert ("items", "notes") in writes


def test_insert_on_conflict_go_and_python(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE items (id text, status text, notes text);\n")
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "repo.go",
        """
package main

import (
    "context"
    "github.com/jackc/pgx/v5"
)

func UpsertItem(ctx context.Context, tx pgx.Tx) {
    _, _ = tx.Exec(ctx, `INSERT INTO items (id, status) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status`)
}

func DynUpdate(ctx context.Context, tx pgx.Tx, t string) {
    _, _ = tx.Exec(ctx, "UPDATE "+t+" SET status = $1")
}

func DynFormat(ctx context.Context, tx pgx.Tx, t string) {
    _, _ = tx.Exec(ctx, "EXECUTE format('UPDATE %I SET status = $1', t)")
}
""",
    )
    _write(
        tmp_path,
        "repo.py",
        '''
def upsert(conn):
    conn.execute("INSERT INTO items (id, status) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status")

def dyn(conn, t):
    conn.execute("UPDATE "+t+" SET status = %s")

def dyn_fmt(conn):
    conn.execute("EXECUTE format('UPDATE %I SET status = $1', table)")
''',
    )
    graph = scan_path(tmp_path)
    go_fn = function_id("main.UpsertItem")
    py_fn = function_id("repo.upsert")
    assert _edge(graph, "writes", go_fn, "column:items.status")
    assert _edge(graph, "writes", py_fn, "column:items.status")
    assert not any(
        e.from_id == go_fn and e.to_id == "table:unknown" for e in graph.edges.values()
    )
    assert not any(
        e.from_id == py_fn and e.to_id == "table:unknown" for e in graph.edges.values()
    )
    dyn_go = function_id("main.DynUpdate")
    dyn_py = function_id("repo.dyn")
    assert _edge(graph, "writes", dyn_go, "table:unknown")
    assert _edge(graph, "writes", dyn_py, "table:unknown")
    assert _edge(graph, "writes", function_id("main.DynFormat"), "table:unknown")
    assert _edge(graph, "writes", function_id("repo.dyn_fmt"), "table:unknown")


def test_select_for_update_of_alias_not_phantom() -> None:
    sql = "SELECT i.id, i.status FROM items i WHERE i.id = $1 FOR UPDATE OF i"
    acc = analyze_sql(sql)
    tables = {a.table.lower() for a in acc}
    assert "i" not in tables
    assert "items" in tables
    assert all(a.column != "i" for a in acc)
    acc2 = analyze_sql(sql, schema_columns={"items": ["id", "status"]}, known_tables={"items"})
    tables2 = {a.table.lower() for a in acc2}
    assert "items" in tables2
    assert "i" not in tables2


def test_dynamic_table_hole_helpers() -> None:
    assert DYNAMIC_TABLE_HOLE.search("UPDATE {t} SET status = $1")
    assert has_dynamic_table_hole("UPDATE {t} SET status = $1") is True
    assert has_dynamic_table_hole("UPDATE  SET status = $1") is True
    assert has_dynamic_table_hole("EXECUTE format('UPDATE %I SET status = $1', t)") is True
    assert (
        has_dynamic_table_hole(
            "INSERT INTO items (id) VALUES ($1) ON CONFLICT (id) DO UPDATE SET c = EXCLUDED.c"
        )
        is False
    )


def test_ts_client_calls_are_not_routes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "server.ts",
        """
import express from "express";
import axios from "axios";

const app = express();
const BASE = "http://catalog.internal";

app.post("/items", async (req, res) => {
  await supabase.from("items").insert({ id: 1 });
  res.json({ ok: true });
});

client.get("/path");
api.post("/y");
axios.get("/z");
fetch(`${BASE}/api/orders`, { method: "POST" });
fetch(`${base(id)}/x`);
const m = new Map();
m.get(key);
m.delete(key);
""",
    )
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
    assert ("POST", "/items") in http
    assert ("GET", "/path") not in http
    assert ("POST", "/y") not in http
    assert ("GET", "/z") not in http
    assert not any(p in {"/path", "/y", "/z", "{key}"} for _m, p in http)
    assert any(e.type == "calls_http" for e in graph.edges.values())
    ep = graph.get("entrypoint:http:POST:/items")
    assert ep is not None
    assert any(
        e.from_id == ep.id and e.type == "writes" and "items" in e.to_id
        for e in graph.edges.values()
    )


def test_http_unresolved_path_does_not_wildcard() -> None:
    routes = [
        ("entrypoint:http:GET:/users/{id}", "GET", "/users/{id}"),
        ("entrypoint:http:GET:/orders/{id}", "GET", "/orders/{id}"),
        ("entrypoint:http:GET:/jobs/{id}", "GET", "/jobs/{id}"),
        ("entrypoint:http:GET:/health", "GET", "/health"),
    ]
    assert match_http("GET", "{x}", routes) == []
    assert match_http("GET", "{a}/{b}", routes) == []
    assert match_http("GET", "{ENV}", routes) == []
    hits = match_http("GET", "/users/abc", routes)
    assert hits == ["entrypoint:http:GET:/users/{id}"]
    health = match_http("GET", "{WORKER_URL}/health", routes)
    assert health == ["entrypoint:http:GET:/health"]


def test_http_ternary_and_base_url(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "bizmap.yaml",
        "services:\n  catalog: CATALOG_URL\n",
    )
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.post("/api/orders")
def create_order():
    return {}

@app.post("/api/jobs")
def create_job():
    return {}
""",
    )
    _write(
        tmp_path,
        "client.py",
        """
import os
import httpx

PATH = "/api/orders" if flag else "/api/jobs"

def submit():
    httpx.post(os.environ["CATALOG_URL"] + PATH)

class Client:
    base_url = os.environ["CATALOG_URL"]
    def send(self):
        httpx.post(self.base_url + "/api/orders")
""",
    )
    graph = scan_path(tmp_path)
    submit = function_id("client.submit")
    send = function_id("client.Client.send")
    assert _edge(graph, "calls_http", submit, "entrypoint:http:POST:/api/orders")
    assert _edge(graph, "calls_http", submit, "entrypoint:http:POST:/api/jobs")
    assert _edge(graph, "calls_http", send, "entrypoint:http:POST:/api/orders")


def test_ts_handler_calls_same_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "schema.sql",
        "CREATE TABLE items (id text, status text);\n",
    )
    _write(
        tmp_path,
        "repo.ts",
        """
export function saveItem() {
  return supabase.from("items").insert({ id: "1" });
}
""",
    )
    _write(
        tmp_path,
        "server.ts",
        """
import express from "express";
import { saveItem } from "./repo";

const app = express();
app.post("/items", saveItem);
""",
    )
    graph = scan_path(tmp_path)
    ep = graph.get("entrypoint:http:POST:/items")
    assert ep is not None
    assert any(
        e.from_id == ep.id and e.type == "writes" and "items" in e.to_id
        for e in graph.edges.values()
    )


def test_firestore_dynamic_collection_unknown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.py",
        '''
from firebase_functions import https_fn

db = object()

@https_fn.on_request()
def ingest(req):
    name = req.args["col"]
    db.collection(name).document("1").set({"ok": True})
    db.collection("jobs").document("2").set({"ok": True})
''',
    )
    graph = scan_path(tmp_path)
    ingest = function_id("main.ingest")
    assert _edge(graph, "writes", ingest, collection_id("unknown"))
    assert _edge(graph, "writes", ingest, collection_id("jobs"))
    assert "table:unknown" not in graph.nodes or not any(
        e.from_id == ingest and e.to_id == "table:unknown" for e in graph.edges.values()
    )


def test_unresolved_interface_and_protocol(tmp_path: Path) -> None:
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "store.go",
        """
package main

type Store interface {
    Save(id string) error
}

func Handle(s Store) {
    _ = s.Save("1")
}
""",
    )
    _write(
        tmp_path,
        "svc.py",
        '''
from typing import Protocol

class Store(Protocol):
    def save(self, item: str) -> None: ...

def handle(store: Store) -> None:
    store.save("1")
''',
    )
    graph = scan_path(tmp_path)
    go_fn = graph.get(function_id("main.Handle"))
    py_fn = graph.get(function_id("svc.handle"))
    assert go_fn is not None
    assert py_fn is not None
    go_notes = go_fn.attrs.get("unresolved_calls") or []
    py_notes = py_fn.attrs.get("unresolved_calls") or []
    assert any("Save" in str(x) for x in go_notes)
    assert any("save" in str(x) for x in py_notes)


def test_sql_dict_injected_executor(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE items (id text, status text);\n")
    _write(
        tmp_path,
        "queries.py",
        '''
QUERIES = {
    "k": "SELECT id, status FROM items",
    "w": "INSERT INTO items (id, status) VALUES ('1', 'open')",
}
''',
    )
    _write(
        tmp_path,
        "repo.py",
        '''
from queries import QUERIES

def load(run_query):
    run_query(QUERIES["k"])

def persist(executor):
    executor(QUERIES["w"])
''',
    )
    graph = scan_path(tmp_path)
    load = function_id("repo.load")
    persist = function_id("repo.persist")
    assert any(e.from_id == load and e.type == "reads" and "items" in e.to_id for e in graph.edges.values())
    assert any(e.from_id == persist and e.type == "writes" and "items" in e.to_id for e in graph.edges.values())
