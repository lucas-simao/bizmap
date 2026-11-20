"""v0.2.6 — chained-export mounts, nested factories, composed clients, param URLs.

Synthetic fixtures only (orders, users, tenants, documents, jobs, items).
"""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import GRAPH_VERSION, collection_id, function_id, table_id
from bizmap.http_match import is_relative_client_url, path_template_match
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
    assert GRAPH_VERSION == "0.4.2"


def test_hono_chained_export_nested_mounts(tmp_path: Path) -> None:
    """Chained-export `new Hono().use().get()` keeps the full cross-file prefix."""
    _write(
        tmp_path,
        "routes.ts",
        """
import { Hono } from "hono";

const mw = async (c, next) => next();
const h = (c) => c.json({ ok: true });
export const r = new Hono().use(mw).get('/a', h).post('/b/:id', h);
""",
    )
    _write(
        tmp_path,
        "platform.ts",
        """
import { Hono } from "hono";
import { r } from "./routes";

export const b = new Hono();
b.route('/v1/p', r);
""",
    )
    _write(
        tmp_path,
        "app.ts",
        """
import { Hono } from "hono";
import { b } from "./platform";

const h = (c) => c.json({ ok: true });
export const app = new Hono().route('/api', b);
app.get('/health', h);
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/api/v1/p/a") in http
    assert ("POST", "/api/v1/p/b/:id") in http or ("POST", "/api/v1/p/b/{id}") in http
    assert ("GET", "/health") in http


def test_nested_python_app_factory_helpers(tmp_path: Path) -> None:
    """FastAPI/Flask apps returned through nested helpers keep caller routes."""
    _write(
        tmp_path,
        "factory.py",
        """
from fastapi import FastAPI
from flask import Flask

def new_app(**kw):
    return FastAPI(**kw)

def new_flask(**kw):
    return Flask(__name__, **kw)
""",
    )
    _write(
        tmp_path,
        "service.py",
        """
from factory import new_app, new_flask

def build(**kw):
    return new_app(**kw)

def create_service():
    return build()

def create_flask():
    return new_flask()
""",
    )
    _write(
        tmp_path,
        "main.py",
        """
from service import create_service, create_flask

app = create_service()

@app.get("/items")
def list_items():
    return []

@app.post("/orders")
def create_order():
    return {}

web = create_flask()

@web.route("/tenants")
def tenants():
    return "ok"
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/items") in http
    assert ("POST", "/orders") in http
    assert ("GET", "/tenants") in http


def test_deps_callable_kwargs_into_app_factory(tmp_path: Path) -> None:
    """Dataclass/NamedTuple callables built with kwargs resolve through create_app."""
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _write(
        tmp_path,
        "db.py",
        """
def _fetch():
    conn = object()
    conn.execute("SELECT id, status FROM orders")

def _save():
    conn = object()
    conn.execute("INSERT INTO orders (id) VALUES ('1')")
""",
    )
    _write(
        tmp_path,
        "deps.py",
        """
from dataclasses import dataclass
from typing import Callable, NamedTuple

@dataclass
class Deps:
    fetch: Callable
    save: Callable

class JobDeps(NamedTuple):
    fetch: Callable
    save: Callable
""",
    )
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import FastAPI
from deps import Deps

def create_app(deps: Deps):
    app = FastAPI()

    @app.get("/items")
    def list_items():
        return deps.fetch()

    @app.post("/items")
    def create_item():
        deps.save()
        return {}

    app.state.deps = deps
    return app

def handle_unknown(deps: Deps):
    deps.missing()
""",
    )
    _write(
        tmp_path,
        "main.py",
        """
from db import _fetch, _save
from deps import Deps, JobDeps
from app import create_app

deps = Deps(fetch=_fetch, save=_save)
app = create_app(deps)
jobs = JobDeps(fetch=_fetch, save=_save)
""",
    )
    graph = scan_path(tmp_path)
    fetch = function_id("db._fetch")
    save = function_id("db._save")
    list_items = function_id("app.create_app.list_items")
    create_item = function_id("app.create_app.create_item")
    assert _edge(graph, "calls", list_items, fetch)
    assert _edge(graph, "calls", create_item, save)
    assert any(
        e.from_id == fetch and e.type == "reads" and "orders" in e.to_id
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == save and e.type == "writes" and "orders" in e.to_id
        for e in graph.edges.values()
    )
    ep_get = "entrypoint:http:GET:/items"
    assert any(
        e.from_id == ep_get and e.type == "reads" and "orders" in e.to_id
        for e in graph.edges.values()
    ) or _edge(graph, "reads", list_items, table_id("orders"))
    unknown = graph.get(function_id("app.handle_unknown"))
    assert unknown is not None
    notes = unknown.attrs.get("unresolved_calls") or []
    assert notes, unknown.attrs
    assert any("missing" in str(n) for n in notes)


def test_ts_object_literal_factory_client_param(tmp_path: Path) -> None:
    """axios/ky instance passed into `makeApi(client) => ({ list: () => client.get })`."""
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

export const ENV = "http://catalog.internal";
export const api = axios.create({ baseURL: ENV });
export const k = ky.create({ prefixUrl: ENV });
""",
    )
    _write(
        tmp_path,
        "factory.ts",
        """
export const makeApi = (client) => ({
  list: () => client.get('/items'),
  create: () => client.post('/orders'),
});

export const makeBare = (client) => ({
  list: () => client.get('/jobs'),
});
""",
    )
    _write(
        tmp_path,
        "use.ts",
        """
import { api, k } from "./client";
import { makeApi } from "./factory";

export const svc = makeApi(api);
export const viaKy = makeApi(k);

export function pull() {
  return svc.list();
}
""",
    )
    graph = scan_path(tmp_path)
    make = function_id("factory.makeApi")
    # each returned member owns its call; the factory reaches them via `calls`
    assert _edge(graph, "calls_http", function_id("factory.makeApi.list"), "entrypoint:http:GET:/items")
    assert _edge(graph, "calls_http", function_id("factory.makeApi.create"), "entrypoint:http:POST:/orders")
    assert _edge(graph, "calls", make, function_id("factory.makeApi.list"))
    assert _edge(graph, "calls", make, function_id("factory.makeApi.create"))
    urls = _http_urls(graph)
    assert any("/items" in u for _m, u in urls)
    bare = function_id("factory.makeBare")
    bare_node = graph.get(bare)
    unresolved = list(graph.attrs.get("unresolved_http") or [])
    if bare_node is not None:
        unresolved.extend(bare_node.attrs.get("unresolved_http") or [])
        unresolved.extend(bare_node.attrs.get("http_calls") or [])
    assert any("jobs" in str(item) for item in unresolved), unresolved


def test_go_dynamic_url_segments_match_param_routes(tmp_path: Path) -> None:
    """PathEscape / sprintf dynamic segments normalize and match `:id` / `{id}`."""
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "api.go",
        """
package main

import "github.com/labstack/echo/v4"

func Mount(e *echo.Echo) {
    e.POST("/items/:id/approve", approve)
}

func approve() {}
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
    "net/url"
)

func Approve(id string) {
    http.Post("/items/" + url.PathEscape(id) + "/approve", "application/json", nil)
}

func ApproveFmt(id string) {
    http.Post(fmt.Sprintf("/items/%s/approve", id), "application/json", nil)
}
""",
    )
    graph = scan_path(tmp_path)
    ep = "entrypoint:http:POST:/items/:id/approve"
    alt = "entrypoint:http:POST:/items/{id}/approve"
    approve = function_id("main.Approve")
    fmt_fn = function_id("main.ApproveFmt")
    assert _edge(graph, "calls_http", approve, ep) or _edge(graph, "calls_http", approve, alt)
    assert _edge(graph, "calls_http", fmt_fn, ep) or _edge(graph, "calls_http", fmt_fn, alt)
    urls = _http_urls(graph)
    assert any("/items/" in u and "approve" in u for _m, u in urls)
    assert path_template_match("/items/:id/approve", "/items/{id}/approve")
    assert path_template_match("/items/{id}/approve", "/items/" + "{id}" + "/approve")


def test_constructor_base_is_not_relative_fetch(tmp_path: Path) -> None:
    """Constructor/config base_url is `{base}+path`, not a same-repo-only relative fetch."""
    assert is_relative_client_url("/projects")
    assert not is_relative_client_url("{base}/internal/x")
    assert not is_relative_client_url("{base_url}/internal/x")
    assert not is_relative_client_url("{baseURL}/internal/x")
    catalog = tmp_path / "catalog"
    worker = tmp_path / "worker"
    _write(catalog, "bizmap.yaml", "name: catalog\ndatabase: platform\n")
    _write(worker, "bizmap.yaml", "name: worker\ndatabase: platform\n")
    _write(
        catalog,
        "api.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.post("/internal/x")
def ingest():
    return {}

@app.get("/projects")
def list_projects():
    return []
""",
    )
    _write(
        worker,
        "client.py",
        """
import httpx

class C:
    def __init__(self, base_url):
        self.base_url = base_url

    def _post(self, url):
        httpx.post(url)

    def go(self):
        self._post(self.base_url + '/internal/x')
""",
    )
    _write(
        worker,
        "bare.ts",
        """
export function load() {
  return fetch('/projects');
}
""",
    )
    graph = scan_paths([catalog, worker])
    go = "worker/" + function_id("client.C.go")
    post = "worker/" + function_id("client.C._post")
    catalog_ep = "catalog/entrypoint:http:POST:/internal/x"
    assert _edge(graph, "calls_http", go, catalog_ep) or _edge(
        graph, "calls_http", post, catalog_ep
    )
    load = "worker/" + function_id("bare.load")
    assert _edge(graph, "calls_http", load, "catalog/entrypoint:http:GET:/projects") is None

    # Go struct field baseURL in a second pair of roots.
    gcat = tmp_path / "gcat"
    gcli = tmp_path / "gcli"
    _write(gcat, "bizmap.yaml", "name: gcat\ndatabase: platform\n")
    _write(gcli, "bizmap.yaml", "name: gcli\ndatabase: platform\n")
    _go_mod(gcat, "gcat")
    _go_mod(gcli, "gcli")
    _write(
        gcat,
        "api.go",
        """
package main

import "github.com/labstack/echo/v4"

func Mount(e *echo.Echo) {
    e.POST("/internal/x", ingest)
}

func ingest() {}
""",
    )
    _write(
        gcli,
        "client.go",
        """
package main

import "net/http"

type Client struct {
    baseURL string
}

func NewClient(baseURL string) *Client {
    return &Client{baseURL: baseURL}
}

func (c *Client) Go() {
    http.Post(c.baseURL + "/internal/x", "application/json", nil)
}
""",
    )
    ggraph = scan_paths([gcat, gcli])
    ggo = "gcli/" + function_id("main.Client.Go")
    gep = "gcat/entrypoint:http:POST:/internal/x"
    assert _edge(ggraph, "calls_http", ggo, gep)


def test_python_dict_get_is_not_http(tmp_path: Path) -> None:
    """payload.get / Mapping.get are not HTTP; httpx/requests/aiohttp/urllib are."""
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
""",
    )
    _write(
        tmp_path,
        "client.py",
        """
import httpx
from typing import Mapping

def handle(payload: dict, params: dict, d: Mapping):
    payload.get('k')
    params.get('q')
    d.get('x')
    payload.get('url')

def pull():
    httpx.get("http://catalog.internal/items")
""",
    )
    graph = scan_path(tmp_path)
    handle = function_id("client.handle")
    pull = function_id("client.pull")
    assert _edge(graph, "calls_http", pull, "entrypoint:http:GET:/items")
    assert not any(
        e.type == "calls_http" and e.from_id == handle for e in graph.edges.values()
    )
    handle_node = graph.get(handle)
    if handle_node is not None:
        calls = handle_node.attrs.get("http_calls") or []
        notes = handle_node.attrs.get("unresolved_http") or []
        assert not any("k" in str(x) or "q" in str(x) or "/url" in str(x) for x in calls + notes)
    urls = _http_urls(graph)
    assert not any(u in {"k", "/k", "q", "/q", "x", "/x"} for _m, u in urls)


def test_firestore_doc_ref_variable_write(tmp_path: Path) -> None:
    """Assigned doc-ref update writes the collection; datetime.now is not table:unknown."""
    _write(
        tmp_path,
        "main.py",
        """
from datetime import datetime
from firebase_functions import https_fn

db = object()

@https_fn.on_request()
def ingest(req):
    x = "1"
    ref = db.collection("documents").document(x)
    ref.update({"at": datetime.now(), "status": "ok"})
""",
    )
    graph = scan_path(tmp_path)
    ingest = function_id("main.ingest")
    assert _edge(graph, "writes", ingest, collection_id("documents"))
    assert not any(e.to_id == "table:unknown" for e in graph.edges.values())
    fn = graph.get(ingest)
    assert fn is None or "table:unknown" not in str(fn.attrs.get("unresolved_calls") or [])
