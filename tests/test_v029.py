"""v0.2.9 — route HTTP regression, stable labels, TS paths/factories, Go sql, mermaid.

Synthetic fixtures only (shop, orders, items, billing, invoices, sites).
"""

from __future__ import annotations

import json
from pathlib import Path

from bizmap.graph import GRAPH_VERSION, function_id, table_id
from bizmap.mermaid import render_mermaid
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


def _graph_bytes(graph) -> bytes:
    payload = graph.to_dict()
    payload.pop("generated_at", None)
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def test_graph_version() -> None:
    assert GRAPH_VERSION == "0.14.2"


def test_fastapi_router_factory_is_not_calls_http(tmp_path: Path) -> None:
    """@router.get/@router.post in make_router() are entrypoints, not outbound HTTP."""
    _write(
        tmp_path,
        "routes.py",
        """
from fastapi import APIRouter
def make_router() -> APIRouter:
    router = APIRouter()
    @router.get("/internal/items/list")
    def list_items():
        return []
    @router.post("/internal/items/release")
    def release_item():
        return {}
    return router
""",
    )
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import FastAPI
from routes import make_router
app = FastAPI()
app.include_router(make_router())
""",
    )
    graph = scan_path(tmp_path)
    http = _http_eps(graph)
    assert ("GET", "/internal/items/list") in http, http
    assert ("POST", "/internal/items/release") in http, http
    assert len(http) == 2, http
    http_edges = [e for e in graph.edges.values() if e.type == "calls_http"]
    assert http_edges == [], [(e.from_id, e.to_id) for e in http_edges]
    maker = graph.get(function_id("routes.make_router"))
    assert maker is None or not (maker.attrs.get("http_calls") or [])


def test_unresolved_calls_stable_across_scans(tmp_path: Path) -> None:
    """Shared method names pick sorted labels; two scans match byte-for-byte."""
    _write(
        tmp_path,
        "models.py",
        """
class A:
    def ident(self):
        return 1

class B:
    def ident(self):
        return 2

def run(item: A):
    item = B()
    return item.model_dump()
""",
    )
    first = scan_path(tmp_path)
    second = scan_path(tmp_path)
    node = first.get(function_id("models.run"))
    assert node is not None
    labels = list(node.attrs.get("unresolved_calls") or [])
    assert labels == sorted(labels), labels
    assert labels == ["A.model_dump", "B.model_dump"], labels
    assert _graph_bytes(first) == _graph_bytes(second)


def test_ts_tsconfig_paths_and_factory_arg_binding(tmp_path: Path) -> None:
    """@/* via tsconfig + createApi(API_URL) compose {VITE_API_URL}/api/v1/shop/..."""
    web = tmp_path / "web"
    api = tmp_path / "api"
    _write(
        tmp_path,
        "bizmap.yaml",
        "services:\n  api:\n    env: [VITE_API_URL]\n",
    )
    _write(web, "bizmap.yaml", "name: web\ndatabase: shop\n")
    _write(api, "bizmap.yaml", "name: api\ndatabase: shop\n")
    _write(
        web,
        "tsconfig.json",
        """
{
  "extends": "./tsconfig.base.json",
  "compilerOptions": {
    "paths": { "@/*": ["src/*"] }
  }
}
""",
    )
    _write(
        web,
        "tsconfig.base.json",
        '{ "compilerOptions": { "baseUrl": "." } }\n',
    )
    _write(
        web,
        "src/api/factory.ts",
        """
import axios from "axios";
export function createApi(baseUrl: string | undefined) {
  const instance = axios.create({ baseURL: `${baseUrl}/api/v1/shop` });
  return instance;
}
""",
    )
    _write(
        web,
        "src/api/instance.ts",
        """
import { createApi } from './factory';
const API_URL = import.meta.env.VITE_API_URL;
export const shopApi = createApi(API_URL);
""",
    )
    _write(
        web,
        "src/api/use.ts",
        """
import { shopApi } from '@/api/instance';
export async function listSites() {
  return shopApi.get('/orders/sites');
}
""",
    )
    _write(
        api,
        "app.py",
        """
from fastapi import FastAPI
app = FastAPI()

@app.get("/api/v1/shop/orders/sites")
def list_sites():
    return []
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    blob = _url_blob(graph)
    assert "client_unknown" not in blob, blob
    assert "{baseUrl}" not in blob, blob
    assert "{VITE_API_URL}/api/v1/shop/orders/sites" in blob, blob
    list_fn = "web/" + function_id("src.api.use.listSites")
    ep = "api/entrypoint:http:GET:/api/v1/shop/orders/sites"
    assert _edge(graph, "calls_http", list_fn, ep), [
        (e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http"
    ]


def test_ts_at_slash_fallback_without_tsconfig(tmp_path: Path) -> None:
    """Without tsconfig, @/ still maps to src/ (inferred)."""
    _write(
        tmp_path,
        "src/api/factory.ts",
        """
import axios from "axios";
export function createApi(baseUrl: string | undefined) {
  const instance = axios.create({ baseURL: `${baseUrl}/api/v1/shop` });
  return instance;
}
""",
    )
    _write(
        tmp_path,
        "src/api/instance.ts",
        """
import { createApi } from './factory';
const API_URL = import.meta.env.VITE_API_URL;
export const shopApi = createApi(API_URL);
""",
    )
    _write(
        tmp_path,
        "src/api/use.ts",
        """
import { shopApi } from '@/api/instance';
export async function listSites() {
  return shopApi.get('/orders/sites');
}
""",
    )
    graph = scan_path(tmp_path)
    blob = _url_blob(graph)
    assert "{VITE_API_URL}/api/v1/shop/orders/sites" in blob, blob
    assert "client_unknown" not in blob, blob


def test_ts_typed_axios_instance_param_binds_call_site(tmp_path: Path) -> None:
    """Pick<AxiosInstance, ...> params inherit shopApi's composed base."""
    _write(
        tmp_path,
        "src/api/factory.ts",
        """
import axios from "axios";
export function createApi(baseUrl: string | undefined) {
  const instance = axios.create({ baseURL: `${baseUrl}/api/v1/shop` });
  return instance;
}
""",
    )
    _write(
        tmp_path,
        "src/api/instance.ts",
        """
import type { AxiosInstance } from "axios";
import { createApi } from './factory';

const API_URL = import.meta.env.VITE_API_URL;
export const shopApi = createApi(API_URL);

export const makeOrdersClient = (http: Pick<AxiosInstance, 'get' | 'post'>) => ({
  list: () => http.get('/orders/exports'),
});

export const ordersClient = makeOrdersClient(shopApi);
""",
    )
    _write(
        tmp_path,
        "src/api/use.ts",
        """
import { ordersClient } from '@/api/instance';
export function listExports() {
  return ordersClient.list();
}
""",
    )
    graph = scan_path(tmp_path)
    blob = _url_blob(graph)
    assert "{baseUrl}" not in blob, blob
    assert "{http}" not in blob, blob
    assert "{VITE_API_URL}/api/v1/shop/orders/exports" in blob, blob


def test_go_database_sql_picks_sql_argument(tmp_path: Path) -> None:
    """database/sql Exec(sql) / QueryRow(sql) record exact column edges."""
    _go_mod(tmp_path)
    _write(
        tmp_path,
        "schema.sql",
        """
CREATE TABLE orders (
    id text,
    user_id text,
    status text,
    total_cents integer
);
""",
    )
    _write(
        tmp_path,
        "store.go",
        """
package main

import (
    "database/sql"
)

var db *sql.DB

func InsertOrder(id, userID string, total int) error {
    _, err := db.Exec("INSERT INTO orders (id, user_id, status, total_cents) VALUES ($1,$2,'pending',$3)", id, userID, total)
    return err
}

func OrderStatus(id string) (string, int, error) {
    var status string
    var total int
    err := db.QueryRow("SELECT status, total_cents FROM orders WHERE id = $1", id).Scan(&status, &total)
    return status, total, err
}
""",
    )
    graph = scan_path(tmp_path)
    orders = table_id("orders")
    insert_fn = function_id("main.InsertOrder")
    status_fn = function_id("main.OrderStatus")

    def _col(frm: str, etype: str, col: str) -> bool:
        return any(
            e.type == etype
            and e.from_id == frm
            and e.to_id == f"column:orders.{col}"
            and e.confidence.value == "exact"
            for e in graph.edges.values()
        )

    assert _col(insert_fn, "writes", "id"), [
        (e.from_id, e.type, e.to_id) for e in graph.edges.values() if "orders" in e.to_id
    ]
    assert _col(insert_fn, "writes", "user_id")
    assert _col(insert_fn, "writes", "status")
    assert _col(insert_fn, "writes", "total_cents")
    assert _col(status_fn, "reads", "status")
    assert _col(status_fn, "reads", "total_cents")
    assert any(
        e.type == "writes" and e.from_id == insert_fn and e.to_id == orders
        for e in graph.edges.values()
    ) or _col(insert_fn, "writes", "id")


def test_mermaid_joint_scan_draws_cross_service_http(tmp_path: Path) -> None:
    """Joint --mermaid groups one subgraph per repo and shows calls_http hops."""
    web = tmp_path / "web"
    api = tmp_path / "api"
    billing = tmp_path / "billing"
    _write(
        tmp_path,
        "bizmap.yaml",
        "services:\n  api:\n    env: [API_URL]\n  billing:\n    env: [BILLING_URL]\n",
    )
    _write(web, "bizmap.yaml", "name: web\ndatabase: shop\n")
    _write(api, "bizmap.yaml", "name: api\ndatabase: shop\n")
    _write(billing, "bizmap.yaml", "name: billing\ndatabase: shop\n")
    _write(
        web,
        "checkout.py",
        """
import httpx
import os

def checkout():
    httpx.post(os.environ["API_URL"] + "/orders")
""",
    )
    _write(
        api,
        "app.py",
        """
import httpx
import os
from fastapi import FastAPI

app = FastAPI()

@app.post("/orders")
def create_order():
    httpx.post(os.environ["BILLING_URL"] + "/invoices")
    return {}
""",
    )
    _write(
        billing,
        "schema.sql",
        "CREATE TABLE invoices (id text, order_id text, total_cents integer);\n",
    )
    _write(
        billing,
        "app.py",
        """
from fastapi import FastAPI

app = FastAPI()

@app.post("/invoices")
def create_invoice():
    conn = object()
    conn.execute("INSERT INTO invoices (id, order_id, total_cents) VALUES ('1', 'o', 100)")
    return {}
""",
    )
    graph = scan_paths([web, api, billing], config=tmp_path / "bizmap.yaml")
    checkout_fn = "web/" + function_id("checkout.checkout")
    api_ep = "api/entrypoint:http:POST:/orders"
    billing_ep = "billing/entrypoint:http:POST:/invoices"
    assert _edge(graph, "calls_http", checkout_fn, api_ep)
    create_fn = "api/" + function_id("app.create_order")
    assert _edge(graph, "calls_http", create_fn, billing_ep) or _edge(
        graph, "calls_http", api_ep, billing_ep
    )
    invoices = table_id("invoices")
    assert any(
        e.type == "writes" and e.to_id == invoices or e.to_id.startswith("column:invoices.")
        for e in graph.edges.values()
    )
    mmd = render_mermaid(graph)
    assert "subgraph web" in mmd, mmd
    assert "subgraph api" in mmd, mmd
    assert "subgraph billing" in mmd, mmd
    assert "checkout" in mmd, mmd
    assert "POST /orders" in mmd, mmd
    assert "POST /invoices" in mmd, mmd
    assert "invoices" in mmd, mmd
    assert "calls_http" in mmd, mmd
