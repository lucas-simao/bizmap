"""v0.2.5 — factory routes, HTTP client precision, SQL holes, cross-file mounts.

Synthetic fixtures only (orders, users, tenants, documents, jobs, items).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.graph import GRAPH_VERSION, collection_id, function_id, table_id
from bizmap.http_match import is_relative_client_url, match_http
from bizmap.scan import scan_path, scan_paths
from bizmap.sql_analyze import analyze_sql


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


def _http_urls(graph) -> set[tuple[str, str]]:
    urls: set[tuple[str, str]] = set()
    for node in graph.nodes.values():
        for item in node.attrs.get("http_calls") or []:
            urls.add((str(item.get("method") or "GET"), str(item.get("url") or "")))
        for note in node.attrs.get("unresolved_http") or []:
            if isinstance(note, dict) and note.get("url"):
                urls.add((str(note.get("method") or "GET"), str(note.get("url") or "")))
    for item in graph.attrs.get("unresolved_http") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.add((str(item.get("method") or "GET"), str(item.get("url") or "")))
    return urls


def test_graph_version() -> None:
    assert GRAPH_VERSION == "0.3.4"


def test_fastapi_factory_and_imported_app_routes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "factory.py",
        """
from fastapi import FastAPI, APIRouter

def create_service():
    app = FastAPI()
    return app

def make_router():
    return APIRouter()

app = FastAPI()
""",
    )
    _write(
        tmp_path,
        "main.py",
        """
from factory import create_service, app as imported_app

app = create_service()

@app.get("/items")
def list_items():
    return []

@app.api_route("/jobs", methods=["PUT"])
def put_job():
    return {}

@imported_app.post("/orders")
def create_order():
    return {}
""",
    )
    _write(
        tmp_path,
        "flask_app.py",
        """
from flask import Flask

def create_flask():
    return Flask(__name__)

web = create_flask()

@web.route("/tenants")
def tenants():
    return "ok"
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/items") in http
    assert ("PUT", "/jobs") in http
    assert ("POST", "/orders") in http
    assert ("GET", "/tenants") in http


def test_patch_decorator_still_not_a_route(tmp_path: Path) -> None:
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

@patch("x.y")
def test_x():
    pass
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/items") in http
    assert ("PATCH", "/x.y") not in http
    assert not any(p and "x.y" in str(p) for _m, p in http)


def test_headers_map_searchparams_not_http_clients(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.ts",
        """
import express from "express";
import axios from "axios";

const app = express();
app.get("/items", (req, res) => res.json({ ok: true }));

export function clean(config, headers, params, searchParams) {
  config.headers.delete('X');
  headers.get('X');
  params.delete('q');
  searchParams.get('q');
  const m = new Map();
  m.get(key);
  m.delete(key);
  const s = new Set();
  s.delete(key);
  const h = new Headers();
  h.get('X');
  h.delete('X');
  const u = new URLSearchParams();
  u.get('q');
  u.delete('q');
}

export function pull() {
  return axios.get("/items");
}
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/items") in http
    urls = _http_urls(graph)
    assert ("GET", "/items") in urls or any(
        e.type == "calls_http" and e.to_id.endswith("/items") for e in graph.edges.values()
    )
    assert not any(u in {"X", "/X", "{key}", "q", "/q"} for _m, u in urls)
    assert not any(
        e.type == "calls_http" and any(x in e.to_id for x in ("/X", "/q", "{key}"))
        for e in graph.edges.values()
    )


def test_sql_concat_hole_is_not_a_column(tmp_path: Path) -> None:
    sql = 'SELECT * FROM items t WHERE {helper(o)}'
    acc = analyze_sql(sql, schema_columns={"items": ["id", "status"]}, dynamic=True)
    cols = {a.column for a in acc if a.column}
    assert "o" not in cols
    assert "helper" not in cols
    assert not any(a.column and ("{" in a.column or "." in a.column) for a in acc)
    assert any(a.table == "items" and a.column is None for a in acc)

    _go_mod(tmp_path)
    _write(tmp_path, "schema.sql", "CREATE TABLE items (id text, status text);\n")
    _write(
        tmp_path,
        "q.go",
        """
package main

func helper(o string) string { return o }

func List(ctx context.Context, db Querier, o string) {
    db.Query(ctx, "SELECT * FROM items t WHERE " + helper(o))
}
""",
    )
    graph = scan_path(tmp_path)
    assert not any(
        e.to_id == "column:items.o" or e.to_id.endswith(".o") for e in graph.edges.values()
    )
    list_fn = function_id("main.List")
    assert any(
        e.from_id == list_fn and e.type == "reads" and "items" in e.to_id and "column:" not in e.to_id
        for e in graph.edges.values()
    ) or _edge(graph, "reads", list_fn, table_id("items"))


def test_hono_express_cross_file_mounts(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "items.ts",
        """
import { Hono } from "hono";

const h = (c) => c.json({ ok: true });
export const items = new Hono();
items.get('/:id', h);
items.post('/', h);
export default items;
""",
    )
    _write(
        tmp_path,
        "nested.ts",
        """
import { Hono } from "hono";
import { items } from "./items";

export const v1 = new Hono();
v1.route('/items', items);
""",
    )
    _write(
        tmp_path,
        "app.ts",
        """
import { Hono } from "hono";
import { v1 } from "./nested";
import itemsRouter from "./items";

const h = (c) => c.json({ ok: true });
export const app = new Hono();
app.route('/v1', v1);
app.get('/health', h);
""",
    )
    _write(
        tmp_path,
        "express.ts",
        """
import express from "express";
import { Router } from "express";

export const jobs = Router();
jobs.get('/:id', (req, res) => res.json({ ok: true }));

export const web = express();
web.use('/v1/jobs', jobs);
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/v1/items/:id") in http or ("GET", "/v1/items/{id}") in http
    assert ("POST", "/v1/items") in http or ("POST", "/v1/items/") in http
    assert ("GET", "/health") in http
    assert ("GET", "/v1/jobs/:id") in http or ("GET", "/v1/jobs/{id}") in http


def test_go_http_helper_composes_at_caller(tmp_path: Path) -> None:
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
    e.GET("/items", list)
    e.POST("/orders", create)
}

func list() {}
func create() {}
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

type Client struct {
    baseURL string
    http    *http.Client
}

func NewClient() *Client {
    return &Client{baseURL: os.Getenv("X_URL")}
}

func (c *Client) getJSON(ctx context.Context, path string, out any) error {
    req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL + path, nil)
    _ = req
    return err
}

func (c *Client) postJSON(ctx context.Context, path string, body any) error {
    u := fmt.Sprintf("%s%s", c.baseURL, path)
    req, err := http.NewRequest(http.MethodPost, u, nil)
    _ = req
    return err
}

func (c *Client) ListItems(ctx context.Context) error {
    var out []any
    return c.getJSON(ctx, "/items", &out)
}

func (c *Client) CreateOrder(ctx context.Context) error {
    return c.postJSON(ctx, "/orders", nil)
}
""",
    )
    graph = scan_path(tmp_path)
    list_fn = function_id("main.Client.ListItems")
    create_fn = function_id("main.Client.CreateOrder")
    assert _edge(graph, "calls_http", list_fn, "entrypoint:http:GET:/items")
    assert _edge(graph, "calls_http", create_fn, "entrypoint:http:POST:/orders")


def test_ts_axios_ky_baseurl_across_modules(tmp_path: Path) -> None:
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

@app.get("/items")
def list_items():
    return []

@app.post("/orders")
def create_order():
    return {}
""",
    )
    _write(
        tmp_path,
        "client.ts",
        """
import axios from "axios";
import ky from "ky";

const BASE = "http://catalog.internal";
export const api = axios.create({ baseURL: BASE });

export function makeClient() {
  return axios.create({ baseURL: BASE });
}

export const k = ky.create({ prefixUrl: BASE });
""",
    )
    _write(
        tmp_path,
        "other.ts",
        """
import { api, makeClient, k } from "./client";

export function pull() {
  return api.get("/items");
}

export function send() {
  const c = makeClient();
  return c.post("/orders");
}

export function viaKy() {
  return k.get("/items");
}

export function viaParam(client) {
  return client.get("/items");
}

export function useParam() {
  return viaParam(api);
}
""",
    )
    graph = scan_path(tmp_path)
    pull = function_id("other.pull")
    send = function_id("other.send")
    via_ky = function_id("other.viaKy")
    via_param = function_id("other.viaParam")
    assert _edge(graph, "calls_http", pull, "entrypoint:http:GET:/items")
    assert _edge(graph, "calls_http", send, "entrypoint:http:POST:/orders")
    assert _edge(graph, "calls_http", via_ky, "entrypoint:http:GET:/items")
    assert _edge(graph, "calls_http", via_param, "entrypoint:http:GET:/items")


def test_relative_fetch_matches_same_repo_only(tmp_path: Path) -> None:
    a = tmp_path / "catalog"
    b = tmp_path / "store"
    _write(a, "bizmap.yaml", "name: catalog\ndatabase: platform\n")
    _write(b, "bizmap.yaml", "name: store\ndatabase: platform\n")
    _write(
        a,
        "api.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.get("/projects")
def list_projects():
    return []
""",
    )
    _write(
        b,
        "web.ts",
        """
export function load() {
  return fetch('/projects');
}
""",
    )
    _write(
        b,
        "app.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.get("/health")
def health():
    return {}
""",
    )
    graph = scan_paths([a, b])
    load = "store/" + function_id("web.load")
    catalog_ep = "catalog/entrypoint:http:GET:/projects"
    assert _edge(graph, "calls_http", load, catalog_ep) is None
    assert not any(
        e.type == "calls_http" and e.from_id == load and "projects" in e.to_id
        for e in graph.edges.values()
    )

    mapped = tmp_path / "mapped"
    _write(
        mapped,
        "bizmap.yaml",
        "name: mapped\ndatabase: platform\nservices:\n  catalog: http://catalog.internal\n",
    )
    _write(
        mapped,
        "client.py",
        """
import httpx
CATALOG = "http://catalog.internal"

def pull():
    httpx.get(CATALOG + "/projects")
""",
    )
    graph2 = scan_paths([a, mapped])
    pull = "mapped/" + function_id("client.pull")
    assert _edge(graph2, "calls_http", pull, catalog_ep)


def test_relative_url_helper() -> None:
    assert is_relative_client_url("/projects")
    assert not is_relative_client_url("http://catalog.internal/projects")
    assert not is_relative_client_url("{X_URL}/projects")
    routes = [
        ("catalog/entrypoint:http:GET:/projects", "GET", "/projects"),
        ("store/entrypoint:http:GET:/projects", "GET", "/projects"),
    ]
    hits = match_http(
        "GET",
        "/projects",
        routes,
        caller_repo="store",
        route_repos={
            "catalog/entrypoint:http:GET:/projects": "catalog",
            "store/entrypoint:http:GET:/projects": "store",
        },
    )
    assert hits == ["store/entrypoint:http:GET:/projects"]


def test_injected_callable_across_modules_and_unresolved(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\nCREATE TABLE jobs (id text);\n")
    _write(
        tmp_path,
        "db.py",
        """
def run_query(sql):
    conn = object()
    conn.execute(sql)
""",
    )
    _write(
        tmp_path,
        "svc.py",
        """
from dataclasses import dataclass
from typing import Callable

@dataclass
class Svc:
    run_query: Callable

    def go(self):
        self.run_query("SELECT id, status FROM orders")

def handle_unknown(svc: Svc):
    svc.missing()
""",
    )
    _write(
        tmp_path,
        "main.py",
        """
from db import run_query
from svc import Svc

svc = Svc(run_query=run_query)

def handle():
    svc.go()
""",
    )
    graph = scan_path(tmp_path)
    go = function_id("svc.Svc.go")
    handle = function_id("main.handle")
    assert any(
        e.from_id == go and e.type == "reads" and "orders" in e.to_id for e in graph.edges.values()
    )
    assert _edge(graph, "calls", handle, go)
    unknown = graph.get(function_id("svc.handle_unknown"))
    assert unknown is not None
    notes = unknown.attrs.get("unresolved_calls") or []
    assert notes, unknown.attrs
    assert any("missing" in str(n) for n in notes)


def test_go_import_suffix_without_gomod_warns(tmp_path: Path, capsys) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE items (id text, status text);\n")
    _write(
        tmp_path,
        "internal/store/store.go",
        """
package store

func Save(id string) {}
""",
    )
    _write(
        tmp_path,
        "cmd/server/main.go",
        """
package main

import store "example.com/app/internal/store"

func Run() {
    store.Save("1")
}
""",
    )
    runner = CliRunner()
    out = tmp_path / "g.json"
    result = runner.invoke(main, ["scan", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "go.mod" in result.output.lower() or "warning" in result.output.lower()
    graph = scan_path(tmp_path)
    run = function_id("cmd/server.Run")
    save = function_id("internal/store.Save")
    assert _edge(graph, "calls", run, save)


def test_firestore_datetime_now_not_table_unknown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.py",
        """
from datetime import datetime
from firebase_functions import https_fn

db = object()

@https_fn.on_request()
def ingest(req):
    db.collection("jobs").document("1").update({"ts": datetime.now(), "status": "ok"})
    db.collection("jobs").document("2").set({"ts": datetime.now()})
""",
    )
    graph = scan_path(tmp_path)
    ingest = function_id("main.ingest")
    assert _edge(graph, "writes", ingest, collection_id("jobs"))
    assert not any(e.to_id == "table:unknown" for e in graph.edges.values())
    fn = graph.get(ingest)
    assert fn is None or "table:unknown" not in str(fn.attrs.get("unresolved_calls") or [])


def test_table_unknown_flag_on_entrypoint_not_edge(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE items (id text, status text);\n")
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import FastAPI

app = FastAPI()

def write_dynamic(name):
    conn = object()
    conn.execute("UPDATE " + name + " SET status = 'x'")

@app.post("/items")
def touch():
    write_dynamic("items")
    return {}
""",
    )
    graph = scan_path(tmp_path)
    origin = function_id("app.write_dynamic")
    ep = "entrypoint:http:POST:/items"
    assert _edge(graph, "writes", origin, "table:unknown")
    assert _edge(graph, "writes", ep, "table:unknown") is None
    assert _edge(graph, "reads", ep, "table:unknown") is None
    node = graph.get(ep)
    assert node is not None
    assert node.attrs.get("dynamic_table") is True
    assert int(node.attrs.get("dynamic_table_unknown") or 0) >= 1
