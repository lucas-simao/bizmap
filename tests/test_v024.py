"""v0.2.4 — chained routers, Deno serve, HTTP compose, injected callables.

Synthetic fixtures only (orders, users, tenants, documents, jobs, items).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.graph import GRAPH_VERSION, collection_id, function_id
from bizmap.scan import scan_path, scan_paths


def _write(tmp: Path, rel: str, source: str) -> None:
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _go_mod(root: Path, name: str = "t") -> None:
    (root / "go.mod").write_text(f"module {name}\n\ngo 1.22\n", encoding="utf-8")


def _edge(graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def _http_eps(graph) -> set[tuple[str, str]]:
    return {
        (n.attrs.get("method"), n.attrs.get("path"))
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    }


def test_graph_version() -> None:
    assert GRAPH_VERSION == "0.3.10"


def test_chained_hono_routes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.ts",
        """
import { Hono } from "hono";

const mw = async (c, next) => next();
const h = (c) => c.json({ ok: true });

export const app = new Hono().use('*', mw).get('/a', h).post('/b', h);
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/a") in http
    assert ("POST", "/b") in http
    assert not any(p in {"*", "/*"} for _m, p in http)


def test_chained_hono_typed_and_basepath(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.ts",
        """
import { Hono } from "hono";

type B = { token: string };
const h = (c) => c.json({ ok: true });

export const typed = new Hono<{Bindings: B}>();
typed.get('/c', h);

export const app = new Hono<{Bindings: B}>().use('*', mw).get('/a', h).post('/b', h);

const v1 = new Hono().basePath('/v1');
v1.get('/items', h);

const sub = new Hono();
sub.get('/x', h);
app.route('/api', sub);
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/a") in http
    assert ("POST", "/b") in http
    assert ("GET", "/c") in http
    assert ("GET", "/v1/items") in http
    assert ("GET", "/api/x") in http


def test_client_get_axios_map_not_routes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "server.ts",
        """
import express from "express";
import axios from "axios";

const app = express();
app.post("/items", async (req, res) => {
  res.json({ ok: true });
});

client.get("/path");
api.post("/y");
axios.get("/z");
const m = new Map();
m.get(key);
m.delete(key);
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("POST", "/items") in http
    assert ("GET", "/path") not in http
    assert ("POST", "/y") not in http
    assert ("GET", "/z") not in http
    assert not any(p in {"/path", "/y", "/z", "{key}"} for _m, p in http)


def test_deno_serve_calls_imported_helpers(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE items (id text, status text);\n")
    _write(
        tmp_path,
        "supabase/functions/ingest/db.ts",
        """
export async function saveItem() {
  return supabase.from("items").insert({ id: "1" });
}
export async function loadItems() {
  return sql.query("SELECT id, status FROM items");
}
""",
    )
    _write(
        tmp_path,
        "supabase/functions/ingest/index.ts",
        """
import { serve } from "https://deno.land/std/http/server.ts";
import { saveItem, loadItems } from "./db";

const withAuth = (fn) => fn;

serve(async (req) => {
  await saveItem();
  return new Response("ok");
});
""",
    )
    _write(
        tmp_path,
        "supabase/functions/jobs/index.ts",
        """
import { saveItem } from "../ingest/db";

Deno.serve(handler);

async function handler(req: Request) {
  await saveItem();
  return new Response("ok");
}
""",
    )
    _write(
        tmp_path,
        "supabase/functions/users/index.ts",
        """
import { loadItems } from "../ingest/db";

const withAuth = (fn) => fn;
serve(withAuth(async (req) => {
  await loadItems();
  return new Response("ok");
}));
""",
    )
    graph = scan_path(tmp_path)
    ingest = graph.get("entrypoint:http:POST:/functions/v1/ingest")
    jobs = graph.get("entrypoint:http:POST:/functions/v1/jobs")
    users = graph.get("entrypoint:http:POST:/functions/v1/users")
    assert ingest is not None
    assert jobs is not None
    assert users is not None
    assert any(
        e.from_id == ingest.id and e.type == "writes" and "items" in e.to_id
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == jobs.id and e.type == "writes" and "items" in e.to_id
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == users.id and e.type == "reads" and "items" in e.to_id
        for e in graph.edges.values()
    )


def test_go_http_newrequest_and_get(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "bizmap.yaml",
        "services:\n  catalog:\n    url: http://catalog.internal\n    env: X_URL\n",
    )
    _write(
        tmp_path,
        "api.go",
        """
package main

import "github.com/labstack/echo/v4"

func Mount(e *echo.Echo) {
    e.POST("/api/orders", create)
    e.POST("/jobs", enqueue)
    e.GET("/health", health)
}

func create() {}
func enqueue() {}
func health() {}
""",
    )
    _write(
        tmp_path,
        "client.go",
        """
package main

import (
    "fmt"
    "net/http"
    "os"
)

const Base = "http://catalog.internal"

func Send(ctx context.Context, client *http.Client, host string) {
    req, _ := http.NewRequest("POST", Base+"/api/orders", nil)
    client.Do(req)
    http.Get(os.Getenv("X_URL")+"/health")
    http.Post(fmt.Sprintf("%s/jobs", Base), "application/json", nil)
    req2, _ := http.NewRequestWithContext(ctx, http.MethodPost, Base+"/api/orders", nil)
    _ = req2
}
""",
    )
    graph = scan_path(tmp_path)
    send = function_id("main.Send")
    assert _edge(graph, "calls_http", send, "entrypoint:http:POST:/api/orders")
    assert _edge(graph, "calls_http", send, "entrypoint:http:GET:/health")
    assert _edge(graph, "calls_http", send, "entrypoint:http:POST:/jobs")


def test_ts_axios_create_and_wrapper(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "bizmap.yaml",
        "services:\n  catalog: http://catalog.internal\n",
    )
    _write(
        tmp_path,
        "api.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.get("/x")
def get_x():
    return {}

@app.post("/items")
def post_item():
    return {}
""",
    )
    _write(
        tmp_path,
        "client.ts",
        """
import axios from "axios";
import { ofetch } from "ofetch";
import ky from "ky";

const BASE = "http://catalog.internal";
const api = axios.create({baseURL: BASE});
const rest = ofetch.create({ baseURL: BASE });
const k = ky.create({ prefixUrl: BASE });

export function pull() {
  return api.get("/x");
}

export function request(path) {
  return fetch(BASE + path, { method: "POST" });
}

export function send() {
  request("/items");
  rest.get("/x");
  k.get("/x");
}
""",
    )
    graph = scan_path(tmp_path)
    pull = function_id("client.pull")
    send = function_id("client.send")
    assert _edge(graph, "calls_http", pull, "entrypoint:http:GET:/x")
    assert _edge(graph, "calls_http", send, "entrypoint:http:POST:/items")
    assert _edge(graph, "calls_http", send, "entrypoint:http:GET:/x")


def test_python_injected_callable_fields(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\nCREATE TABLE items (id text);\n")
    _write(
        tmp_path,
        "db.py",
        '''
def run_query(sql):
    conn = object()
    conn.execute(sql)
''',
    )
    _write(
        tmp_path,
        "svc.py",
        '''
from dataclasses import dataclass
from typing import Callable
from db import run_query

@dataclass
class Deps:
    run_query: Callable

class Service:
    def __init__(self, run_query):
        self.run_query = run_query

    def handle(self):
        self.run_query("SELECT id, status FROM orders")

def go(deps: Deps):
    deps.run_query("INSERT INTO items (id) VALUES ('1')")
''',
    )
    graph = scan_path(tmp_path)
    handle = function_id("svc.Service.handle")
    go = function_id("svc.go")
    assert any(e.from_id == handle and e.type == "reads" and "orders" in e.to_id for e in graph.edges.values())
    assert any(e.from_id == go and e.type == "writes" and "items" in e.to_id for e in graph.edges.values())
    assert _edge(graph, "calls", handle, function_id("db.run_query")) or _edge(
        graph, "calls", handle, function_id("svc.run_query")
    ) or any(
        e.type == "calls" and e.from_id == handle and "run_query" in e.to_id for e in graph.edges.values()
    )
    assert any(
        e.type == "calls" and e.from_id == go and "run_query" in e.to_id for e in graph.edges.values()
    )


def test_python_dynamic_firestore_unknown(tmp_path: Path) -> None:
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


def test_python_patch_logger_not_http_entrypoint(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import FastAPI
from unittest.mock import patch

app = FastAPI()

@app.get("/items")
def list_items():
    return []

@patch("x.x.logger")
def test_x():
    pass
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/items") in http
    assert ("PATCH", "/x.x.logger") not in http
    assert not any(p and "logger" in str(p) for _m, p in http)


def test_registry_merge_rematch_http(tmp_path: Path) -> None:
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
    return {"ok": True}
""",
    )
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
    joint = scan_paths([a, b])
    joint_http = [e for e in joint.edges.values() if e.type == "calls_http"]
    assert joint_http

    runner = CliRunner()
    ga = tmp_path / "a.json"
    gb = tmp_path / "b.json"
    merged_path = tmp_path / "merged.json"
    r1 = runner.invoke(main, ["scan", str(a), "--out", str(ga)])
    r2 = runner.invoke(main, ["scan", str(b), "--out", str(gb)])
    r3 = runner.invoke(main, ["registry", "merge", str(ga), str(gb), "--out", str(merged_path)])
    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output
    assert r3.exit_code == 0, r3.output
    data = json.loads(merged_path.read_text(encoding="utf-8"))
    assert data["version"] == "0.3.10"
    http_edges = [e for e in data["edges"] if e["type"] == "calls_http"]
    assert http_edges, data["edges"][:12]
    assert any("/api/orders" in (e.get("to") or "") for e in http_edges)


def test_multipath_config_keeps_repo_names(tmp_path: Path) -> None:
    a = tmp_path / "orders"
    b = tmp_path / "jobs"
    shared = tmp_path / "shared.yaml"
    shared.write_text(
        "name: platform\ndatabase: platform\nservices:\n  catalog: http://catalog.internal\n",
        encoding="utf-8",
    )
    _write(a, "bizmap.yaml", "name: orders-svc\n")
    _write(a, "app.py", "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/orders')\ndef n():\n    return {}\n")
    _write(b, "app.py", "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/jobs')\ndef n():\n    return {}\n")
    graph = scan_paths([a, b], config=shared)
    repos = graph.attrs.get("repos") or []
    assert "platform" not in repos
    assert "orders-svc" in repos
    assert "jobs" in repos
    assert len(set(repos)) == 2


def test_unresolved_http_persisted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "client.py",
        """
import httpx
import os

def ping():
    httpx.get(os.environ["MISSING_URL"] + "/health")
""",
    )
    runner = CliRunner()
    out = tmp_path / "g.json"
    result = runner.invoke(main, ["scan", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "unresolved HTTP" in result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    top = data.get("unresolved_http") or []
    assert top
    stderr_n = int(result.output.split("unresolved HTTP")[0].strip().split()[-1])
    assert stderr_n == len(top)
    fn = next(n for n in data["nodes"] if n["id"] == function_id("client.ping"))
    assert fn.get("unresolved_http") or fn.get("http_calls")
