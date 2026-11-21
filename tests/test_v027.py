"""v0.2.7 — TS client params, Hono mounts, Python wrappers/SQL dicts, Go aliases, hashseed.

Synthetic fixtures only (orders, users, tenants, documents, jobs, items).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
    assert GRAPH_VERSION == "0.13.0"


def test_ts_axios_instance_param_and_class_client(tmp_path: Path) -> None:
    """axios/ky instance passed into object-literal factory or class ctor keeps baseURL."""
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
    e.GET("/api/v1/orders/:id", get)
}

func list() {}
func get() {}
""",
    )
    _write(
        web,
        "http.ts",
        """
import axios from "axios";

export const api = axios.create({
  baseURL: `${import.meta.env.VITE_API_URL}/api/v1`,
});
""",
    )
    _write(
        web,
        "orders.ts",
        """
import type { AxiosInstance } from "axios";

export const makeOrders = (client: AxiosInstance) => ({
  list: () => client.get('/orders'),
  get: (id) => client.get(`/orders/${id}`),
});

export class OrdersApi {
  constructor(private client: AxiosInstance) {}
  list() { return this.client.get('/orders') }
  get(id: string) { return this.client.get(`/orders/${id}`) }
}
""",
    )
    _write(
        web,
        "index.ts",
        """
import { api } from "./http";
import { makeOrders, OrdersApi } from "./orders";

export const orders = makeOrders(api);
export const viaClass = new OrdersApi(api);

export function pull() {
  return orders.list();
}
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    urls = _url_blob(graph)
    assert "{VITE_API_URL}/api/v1/orders" in urls, urls
    assert "/orders/:id" in urls or "/orders/{id}" in urls, urls
    list_ep = "api/entrypoint:http:GET:/api/v1/orders"
    get_ep = "api/entrypoint:http:GET:/api/v1/orders/:id"
    hits = [
        e
        for e in graph.edges.values()
        if e.type == "calls_http" and e.to_id in {list_ep, get_ep}
    ]
    assert hits, [(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http"]


def test_hono_long_chain_keeps_mount_prefix(tmp_path: Path) -> None:
    """Chained routes with more than 12 links all receive the mount prefix."""
    h = "(c) => c.json({ ok: true })"
    chain = ".use(mw)" + "".join(f".get('/n{i}', h)" for i in range(1, 16))
    _write(
        tmp_path,
        "routes.ts",
        f"""
import {{ Hono }} from "hono";

const mw = async (c, next) => next();
const h = {h};
export const r = new Hono(){chain};
""",
    )
    _write(
        tmp_path,
        "app.ts",
        """
import { Hono } from "hono";
import { r } from "./routes";

export const app = new Hono();
app.route('/v1/p', r);
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    for i in range(1, 16):
        assert ("GET", f"/v1/p/n{i}") in http, (i, http)


def test_hono_statement_subapp_inside_chained_export(tmp_path: Path) -> None:
    """Statement-style sub-app mounted from a chained export keeps the composed prefix."""
    _write(
        tmp_path,
        "child.ts",
        """
import { Hono } from "hono";

const h = (c) => c.json({ ok: true });
export const s = new Hono();
s.get('/x', h);

export const t = new Hono();
t.get('/y', h);
""",
    )
    _write(
        tmp_path,
        "mid.ts",
        """
import { Hono } from "hono";
import { s, t } from "./child";

export const r = new Hono().route('/', s);
export const q = new Hono().route('/sub', t);
""",
    )
    _write(
        tmp_path,
        "app.ts",
        """
import { Hono } from "hono";
import { r, q } from "./mid";

export const app = new Hono();
app.route('/api', r);
app.route('/api', q);
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/api/x") in http, http
    assert ("GET", "/api/sub/y") in http, http


def test_hono_serve_options_not_post_root(tmp_path: Path) -> None:
    """serve({ fetch: app.fetch }) and export default { fetch } are not POST /."""
    _write(
        tmp_path,
        "app.ts",
        """
import { Hono } from "hono";
import { serve } from "@hono/node-server";

const h = (c) => c.json({ ok: true });
const app = new Hono();
app.get('/orders', h);
const port = 3000;
serve({ fetch: app.fetch, port });
export default { fetch: app.fetch };
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/orders") in http
    assert ("POST", "/") not in http, http


def test_python_outbound_wrapper_composes_base(tmp_path: Path) -> None:
    """self._post(path) helpers compose {base}+literal path; uncomposed stays unresolved_http."""
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

@app.post("/internal/orders:sync")
def sync_orders():
    return {}
""",
    )
    _write(
        worker,
        "client.py",
        """
class Transport:
    def request(self, method, url, json=None):
        return url

class Client:
    def __init__(self, base_url, transport=None):
        self.base_url = base_url
        self._transport = transport or Transport()

    def _post(self, path, body):
        return self._transport.request('POST', self.base_url + path, json=body)

    def sync(self):
        return self._post('/internal/orders:sync', {})

    def dynamic(self, path):
        return self._post(path, {})
""",
    )
    graph = scan_paths([catalog, worker])
    sync = "worker/" + function_id("client.Client.sync")
    post = "worker/" + function_id("client.Client._post")
    dyn = "worker/" + function_id("client.Client.dynamic")
    ep = "catalog/entrypoint:http:POST:/internal/orders:sync"
    assert _edge(graph, "calls_http", sync, ep) or _edge(graph, "calls_http", post, ep)
    urls = _url_blob(graph)
    assert "{base}/internal/orders:sync" in urls or "/internal/orders:sync" in urls, urls
    dyn_node = graph.get(dyn)
    unresolved = list(graph.attrs.get("unresolved_http") or [])
    if dyn_node is not None:
        unresolved.extend(dyn_node.attrs.get("unresolved_http") or [])
        unresolved.extend(dyn_node.attrs.get("http_calls") or [])
    assert unresolved, (unresolved, dyn_node.attrs if dyn_node else None)


def test_python_sql_dict_module_alias_and_transform(tmp_path: Path) -> None:
    """SQL dicts via module alias, import-time strip/dedent, and rpc(name) reach tables."""
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _write(
        tmp_path,
        "pkg/__init__.py",
        "",
    )
    _write(
        tmp_path,
        "pkg/queries.py",
        '''
import textwrap

RAW = {
    "list": """
        SELECT id, status FROM orders
    """,
    "ins": "  INSERT INTO orders (id) VALUES ('1')  ",
}

Q = {k: textwrap.dedent(v) for k, v in RAW.items()}
Q2 = {k: v.strip() for k, v in RAW.items()}
''',
    )
    _write(
        tmp_path,
        "pkg/db.py",
        """
from . import queries

def run_query(sql: str) -> None:
    pass

def load_alias():
    run_query(queries.Q['list'])

def load_strip():
    run_query(queries.Q2['ins'])
""",
    )
    _write(
        tmp_path,
        "pkg/rpc.py",
        """
from .queries import Q
from .db import run_query

def rpc(name):
    run_query(Q[name])
""",
    )
    _write(
        tmp_path,
        "svc.py",
        """
import pkg.queries as q
from pkg.db import run_query, load_alias, load_strip
from pkg.rpc import rpc
from fastapi import FastAPI

def load_mod():
    run_query(q.Q['list'])

app = FastAPI()

@app.get("/orders")
def list_orders():
    load_alias()
    load_mod()
    rpc("list")
    return []

@app.post("/orders")
def create_order():
    load_strip()
    return {}
""",
    )
    graph = scan_path(tmp_path)
    orders = table_id("orders")
    list_fn = function_id("pkg.db.load_alias")
    mod_fn = function_id("svc.load_mod")
    strip_fn = function_id("pkg.db.load_strip")
    list_ep = "entrypoint:http:GET:/orders"
    create_ep = "entrypoint:http:POST:/orders"
    assert any(
        e.type == "reads" and e.to_id == orders and e.from_id in {list_fn, mod_fn, list_ep}
        for e in graph.edges.values()
    ), [(e.from_id, e.type, e.to_id) for e in graph.edges.values() if "orders" in e.to_id]
    assert any(
        e.type == "writes" and e.to_id == orders and e.from_id in {strip_fn, create_ep}
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == list_ep and e.type == "reads" and e.to_id == orders
        for e in graph.edges.values()
    ), [(e.from_id, e.type, e.to_id) for e in graph.edges.values() if e.from_id == list_ep]


def test_go_const_alias_across_packages(tmp_path: Path) -> None:
    """A const/var aliasing another package's const resolves when both packages are scanned."""
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "shared/shared.go",
        """
package shared

const PathPrefix = "/api"
const Base = "/items"
""",
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
    assert ("GET", "/api/orders") in http, http
    assert ("GET", "/items/v1/jobs") in http, http
    for _m, path in http:
        assert "{shared." not in (path or "")
        assert "{routes." not in (path or "")


def test_graph_json_stable_across_hash_seeds(tmp_path: Path) -> None:
    """Identical input yields byte-identical bizmap.json across PYTHONHASHSEED values."""
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\nCREATE TABLE items (id text);\n")
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import FastAPI

app = FastAPI()

@app.get("/orders")
def list_orders():
    conn = object()
    conn.execute("SELECT id, status FROM orders")
    conn.execute("SELECT id FROM items")
    return []

def mystery(store):
    store.save()
""",
    )
    src = Path(__file__).resolve().parents[1] / "src"
    payloads = []
    for seed in ("0", "1"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys; from pathlib import Path; from bizmap.scan import scan_path; "
                "g=scan_path(Path(sys.argv[1])); d=g.to_dict(); d.pop('generated_at', None); "
                "print(json.dumps(d, indent=2))",
                str(tmp_path),
            ],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        payloads.append(proc.stdout)
    assert payloads[0] == payloads[1], _json_diff(payloads[0], payloads[1])


def _json_diff(a: str, b: str) -> str:
    da, db = json.loads(a), json.loads(b)
    return f"len {len(a)} vs {len(b)} keys {set(da) ^ set(db)}"
