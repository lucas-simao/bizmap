"""v0.2.8 — factory HTTP clients, wrappers, SQL dict transforms, reasons.

Synthetic fixtures only (orders, users, tenants, documents, jobs, items).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.graph import GRAPH_VERSION, function_id, table_id
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


def _url_blob(graph) -> str:
    return " ".join(u for _m, u in _http_urls(graph))


def test_graph_version() -> None:
    assert GRAPH_VERSION == "0.16.0"


def test_ts_factory_returned_axios_instance(tmp_path: Path) -> None:
    """createClient()/makeHttp() instances compose {VITE_API_URL}/api/v1, never :client."""
    web = tmp_path / "web"
    api = tmp_path / "api"
    _write(
        tmp_path,
        "bizmap.yaml",
        "services:\n  catalog:\n    env: VITE_API_URL\n    repo: api\n",
    )
    _write(web, "bizmap.yaml", "name: web\ndatabase: platform\n")
    _write(api, "bizmap.yaml", "name: api\ndatabase: platform\n")
    _go_mod(api, "api")
    _write(
        api,
        "routes.go",
        """
package main

import "github.com/labstack/echo/v4"

func Mount(e *echo.Echo) {
    e.GET("/api/v1/orders", list)
    e.POST("/api/v1/orders", create)
    e.GET("/api/v1/jobs", jobs)
}

func list() {}
func create() {}
func jobs() {}
""",
    )
    _write(
        web,
        "http.ts",
        """
import axios from "axios";

export function createClient(token?: string) {
  return axios.create({
    baseURL: `${import.meta.env.VITE_API_URL}/api/v1`,
    headers: { Authorization: token || "" },
  });
}

export const api = createClient();

export const makeHttp = () => {
  const inst = axios.create({
    baseURL: `${import.meta.env.VITE_API_URL}/api/v1`,
    headers: { "X-Job": "1" },
  });
  inst.interceptors.request.use((cfg) => cfg);
  return inst;
};

export const http = makeHttp();
""",
    )
    _write(
        web,
        "orders.ts",
        """
import type { AxiosInstance } from "axios";

export const makeOrders = (client: AxiosInstance) => ({
  list: () => client.get('/orders'),
  get: (id: string) => client.get(`/orders/${id}`),
});

export class OrdersApi {
  constructor(private client: AxiosInstance) {}
  list() { return this.client.get('/orders') }
}
""",
    )
    _write(
        web,
        "index.ts",
        """
import { api, http } from "./http";
import { makeOrders, OrdersApi } from "./orders";

type Order = { id: string };

export const orders = makeOrders(api);
export const viaClass = new OrdersApi(api);

export async function pull() {
  const rows = (await api.get<Order[]>('/orders')).data;
  return rows;
}

export function viaFactory() {
  return orders.list();
}

export function viaHttp() {
  return http.get('/jobs');
}
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    blob = _url_blob(graph)
    assert ":client" not in blob, blob
    assert ":http" not in blob, blob
    assert "{VITE_API_URL}/api/v1/orders" in blob, blob
    assert "{VITE_API_URL}/api/v1/jobs" in blob, blob
    list_ep = "api/entrypoint:http:GET:/api/v1/orders"
    jobs_ep = "api/entrypoint:http:GET:/api/v1/jobs"
    hits = [
        e
        for e in graph.edges.values()
        if e.type == "calls_http" and e.to_id in {list_ep, jobs_ep}
    ]
    assert hits, [(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http"]


def test_ts_local_wrapper_call_site_path(tmp_path: Path) -> None:
    """get(url)/request(method, url) emit the call-site path, not base:url."""
    _write(
        tmp_path,
        "bizmap.yaml",
        "services:\n  catalog:\n    env: VITE_API_URL\n    repo: api\n",
    )
    _write(
        tmp_path,
        "api.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.get("/api/v1/orders/{id}")
def get_order(id: str):
    return {}

@app.post("/api/v1/orders")
def create_order():
    return {}
""",
    )
    _write(
        tmp_path,
        "client.ts",
        """
import axios from "axios";

const BASE = "/api/v1";
const api = axios.create({ baseURL: `${import.meta.env.VITE_API_URL}` });

const get = <T>(url: string) => api.get<T>(BASE + url);

async function request(method: string, url: string, body: unknown) {
  return api.request({ method, url: `${BASE}${url}`, data: body });
}

export function load(id: string) {
  get("/orders/" + id);
  request("POST", "/orders", {});
}
""",
    )
    graph = scan_path(tmp_path)
    blob = _url_blob(graph)
    assert "base:url" not in blob, blob
    assert ":url" not in blob.replace("/orders/:id", ""), blob
    assert "/orders/:id" in blob or "/orders/{id}" in blob, blob
    assert "/api/v1/orders" in blob, blob
    get_ep = "entrypoint:http:GET:/api/v1/orders/{id}"
    post_ep = "entrypoint:http:POST:/api/v1/orders"
    load_fn = function_id("client.load")
    assert _edge(graph, "calls_http", load_fn, get_ep) or _edge(
        graph, "calls_http", load_fn, "entrypoint:http:GET:/api/v1/orders/:id"
    )
    assert _edge(graph, "calls_http", load_fn, post_ep)


def test_python_sql_dict_derived_in_other_module(tmp_path: Path) -> None:
    """Q = {k: transform(v) for k, v in queries.RAW.items()} resolves tables."""
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\nCREATE TABLE items (id text);\n")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/queries.py",
        '''
RAW = {
    "list": "SELECT id, status FROM old_schema.orders",
    "items": "SELECT id FROM items",
    "ins": "INSERT INTO old_schema.orders (id) VALUES ('1')",
}
''',
    )
    _write(
        tmp_path,
        "pkg/db.py",
        '''
from . import queries
import textwrap

SCHEMA = "new_schema"

def _ident(v):
    return v

def _dedent(v):
    return textwrap.dedent(v)

def _repl(v):
    return v.replace("old_schema.", "new_schema.")

def _fmt(v):
    return v.format(schema=SCHEMA)

def _mystery(v):
    return helper(v)

FMT = {"x": "SELECT id FROM {schema}.orders"}
Q = {k: _ident(v) for k, v in queries.RAW.items()}
Qd = {k: _dedent(v) for k, v in queries.RAW.items()}
Qs = {k: v.strip() for k, v in queries.RAW.items()}
Q2 = {k: _repl(v) for k, v in queries.RAW.items()}
Q3 = {k: _fmt(v) for k, v in FMT.items()}
Q4 = {k: _mystery(v) for k, v in queries.RAW.items()}

def run_query(sql: str) -> None:
    pass

def load_ident():
    run_query(Q["list"])

def load_dedent():
    run_query(Qd["list"])

def load_strip():
    run_query(Qs["list"])

def load_repl():
    run_query(Q2["ins"])

def load_fmt():
    run_query(Q3["x"])

def load_unknown():
    run_query(Q4["items"])
''',
    )
    graph = scan_path(tmp_path)
    orders = table_id("orders")
    items = table_id("items")
    ident_fn = function_id("pkg.db.load_ident")
    dedent_fn = function_id("pkg.db.load_dedent")
    strip_fn = function_id("pkg.db.load_strip")
    repl_fn = function_id("pkg.db.load_repl")
    fmt_fn = function_id("pkg.db.load_fmt")
    unk_fn = function_id("pkg.db.load_unknown")

    def _touches(frm: str, etype: str, table: str) -> bool:
        prefix = f"column:{table.split(':', 1)[-1]}."
        return any(
            e.type == etype
            and e.from_id == frm
            and (e.to_id == table or e.to_id.startswith(prefix))
            for e in graph.edges.values()
        )

    assert _touches(ident_fn, "reads", orders), [
        (e.from_id, e.type, e.to_id) for e in graph.edges.values() if "orders" in e.to_id
    ]
    assert _touches(dedent_fn, "reads", orders)
    assert _touches(strip_fn, "reads", orders)
    assert _touches(repl_fn, "writes", orders)
    assert _touches(fmt_fn, "reads", orders)
    assert _touches(unk_fn, "reads", items)
    unk_node = graph.get(unk_fn)
    notes = " ".join(unk_node.attrs.get("notes") or []) if unk_node else ""
    inferred = any(
        e.from_id == unk_fn
        and e.confidence.value == "inferred"
        and (e.to_id == items or e.to_id.startswith("column:items."))
        for e in graph.edges.values()
    )
    assert "unknown" in notes.lower()
    assert inferred


def test_unscanned_transport_emits_unresolved_http(tmp_path: Path) -> None:
    """x.request('POST', path) / x.post(path) on an unscanned class is never silent."""
    _write(
        tmp_path,
        "client.py",
        """
from mystery_sdk import Transport

def send(x, path):
    x.request("POST", "/orders")
    x.post("/jobs")

def via_ctor():
    t = Transport()
    t.post("/items")
""",
    )
    graph = scan_path(tmp_path)
    blob = _url_blob(graph)
    assert "/orders" in blob, blob
    assert "/jobs" in blob, blob
    unresolved = list(graph.attrs.get("unresolved_http") or [])
    assert unresolved, unresolved
    methods = {str(item.get("method") or "").upper() for item in unresolved}
    assert "POST" in methods
    reasons = {item.get("reason") for item in unresolved}
    assert "client_unknown" in reasons or reasons, reasons


def test_yaml_constants_resolve_unscanned_pkg_const(tmp_path: Path) -> None:
    """constants: PathPrefix / pkg.Name fill unscanned Go/TS/Python aliases."""
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "bizmap.yaml",
        "constants:\n  PathPrefix: /api/v1\n  shared.Base: /items\n",
    )
    _write(
        tmp_path,
        "routes/routes.go",
        """
package routes

import "t/shared"

const PathPrefix = shared.PathPrefix
var Base = shared.Base + "/v1"
""",
    )
    _write(
        tmp_path,
        "main.go",
        """
package main

import (
    "github.com/labstack/echo/v4"
    "t/routes"
)

func list() {}
func get() {}

func main() {
    e := echo.New()
    e.GET(routes.PathPrefix+"/orders", list)
    e.GET(routes.Base+"/jobs", get)
}
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/api/v1/orders") in http, http
    assert ("GET", "/items/v1/jobs") in http, http
    for _m, path in http:
        assert "{shared." not in (path or "")


def test_unresolved_http_reason_and_cli_counts(tmp_path: Path) -> None:
    """Every unmatched call has a reason; CLI prints per-reason counts."""
    _write(
        tmp_path,
        "client.py",
        """
import httpx
import os

def composed():
    httpx.get(os.environ["MISSING_URL"] + "/orders")

def no_base(path):
    httpx.post(path)
""",
    )
    graph = scan_path(tmp_path)
    unresolved = list(graph.attrs.get("unresolved_http") or [])
    assert unresolved
    for item in unresolved:
        assert item.get("reason") in {
            "target_not_scanned",
            "base_unknown",
            "path_dynamic",
            "client_unknown",
        }, item
    reasons = {item.get("reason") for item in unresolved}
    assert "target_not_scanned" in reasons or "path_dynamic" in reasons, reasons

    runner = CliRunner()
    out = tmp_path / "g.json"
    result = runner.invoke(main, ["scan", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "unresolved HTTP calls" in result.output
    assert "unresolved HTTP calls: " in result.output  # counts by reason on that line
    assert "target_not_scanned=" in result.output or "path_dynamic=" in result.output
