"""Front-end → API links from route literals (``via: route_literal``).

When a TS/JS client is built through layers the detectors cannot follow
(DI containers, registries, generic wrappers), an API-looking string such as
``/orders/${id}`` is still matched to the single backend route it names.

Synthetic fixtures only (orders, items, invoices).
"""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.http_match import (
    base_is_known,
    route_literal_hits,
    service_for_url,
    suffix_template_match,
)
from bizmap.scan import scan_path, scan_paths


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


API = """
from fastapi import FastAPI

app = FastAPI()


@app.get("/api/v1/orders/{order_id}")
def get_order(order_id: str):
    return {}


@app.get("/api/v1/orders")
def list_orders():
    return []


@app.post("/api/v1/orders/{order_id}/cancel")
def cancel_order(order_id: str):
    return {}


@app.get("/api/v1/invoices/{invoice_id}")
def get_invoice(invoice_id: str):
    return {}


@app.get("/admin/invoices/{invoice_id}")
def admin_invoice(invoice_id: str):
    return {}
"""


def _calls_http_from(graph, fn_suffix: str) -> set[str]:
    return {
        e.to_id
        for e in graph.edges.values()
        if e.type == "calls_http" and e.from_id.endswith(fn_suffix)
    }


def _project(tmp_path: Path, web_files: dict[str, str]) -> tuple[Path, Path]:
    web = tmp_path / "web"
    api = tmp_path / "api"
    _write(api, "app.py", API)
    for rel, src in web_files.items():
        _write(web, rel, src)
    return web, api


def test_untraceable_client_links_by_literal(tmp_path: Path) -> None:
    """A client resolved from a DI container still links via its path literal."""
    web, api = _project(
        tmp_path,
        {
            "src/orders.ts": """
import { container } from './container';

export async function loadOrder(id: string) {
  const http = container.resolve('http');
  return http.get(`/orders/${id}`);
}

export async function cancelOrder(id: string) {
  const http = container.resolve('http');
  return http.post('/orders/' + id + '/cancel', {});
}
""",
        },
    )
    graph = scan_paths([web, api])
    load = _calls_http_from(graph, function_id("src.orders.loadOrder"))
    assert load == {"api/entrypoint:http:GET:/api/v1/orders/{order_id}"}, load
    cancel = _calls_http_from(graph, function_id("src.orders.cancelOrder"))
    assert cancel == {"api/entrypoint:http:POST:/api/v1/orders/{order_id}/cancel"}, cancel
    edge = next(
        e
        for e in graph.edges.values()
        if e.type == "calls_http" and e.from_id.endswith(function_id("src.orders.loadOrder"))
    )
    assert edge.confidence.value == "heuristic"
    assert edge.attrs.get("via") == "route_literal"
    assert edge.attrs.get("match") == "suffix"
    assert edge.evidence is not None and edge.evidence.file == "src/orders.ts"


def test_frontend_navigation_strings_do_not_link(tmp_path: Path) -> None:
    """Router paths, links, redirects, comparisons, and assets are not API calls."""
    web, api = _project(
        tmp_path,
        {
            "src/nav.tsx": """
import { useNavigate, Link } from 'react-router-dom';

export const routes = [{ path: '/api/v1/orders', element: null }];

export function OrdersLink() {
  return <Link to="/api/v1/orders">Orders</Link>;
}

export function goToOrder(id: string) {
  const navigate = useNavigate();
  navigate('/api/v1/orders/' + id);
}

export function isOrders(pathname: string) {
  return pathname === '/api/v1/orders' || pathname.startsWith('/api/v1/orders/');
}

export function logo() {
  return '/api/v1/orders/logo.png';
}
""",
        },
    )
    graph = scan_paths([web, api])
    linked = [
        (e.from_id, e.to_id)
        for e in graph.edges.values()
        if e.type == "calls_http" and e.from_id.startswith("web/")
    ]
    assert linked == [], linked


def test_ambiguous_literal_links_nothing(tmp_path: Path) -> None:
    """`/invoices/:id` matches both /api/v1/... and /admin/... → no guess."""
    web, api = _project(
        tmp_path,
        {
            "src/invoices.ts": """
export async function loadInvoice(api: any, id: string) {
  return api.get(`/invoices/${id}`);
}
""",
        },
    )
    graph = scan_paths([web, api])
    assert _calls_http_from(graph, function_id("src.invoices.loadInvoice")) == set()


def test_single_segment_suffix_needs_method_hint(tmp_path: Path) -> None:
    """`'/orders'` alone (no call verb) is too vague; `.get('/orders')` is not."""
    web, api = _project(
        tmp_path,
        {
            "src/list.ts": """
export const ORDERS_KEY = '/orders';

export async function listOrders(client: any) {
  return client.get('/orders');
}
""",
        },
    )
    graph = scan_paths([web, api])
    hits = _calls_http_from(graph, function_id("src.list.listOrders"))
    assert hits == {"api/entrypoint:http:GET:/api/v1/orders"}, hits
    module_hits = _calls_http_from(graph, function_id("src.list.module"))
    assert module_hits == set(), module_hits


def test_route_registration_is_not_a_literal_call(tmp_path: Path) -> None:
    """A TS backend registering a route never `calls_http` its own routes."""
    _write(
        tmp_path,
        "server.ts",
        """
import express from 'express';

const app = express();

export function getOrder(req: any, res: any) {
  res.json({});
}

app.get('/api/v1/orders/:id', getOrder);
app.listen(3000);
""",
    )
    graph = scan_path(tmp_path)
    linked = [(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http"]
    assert linked == [], linked


def test_literal_clears_matching_unresolved_http(tmp_path: Path) -> None:
    """When a literal links a call, its `unresolved_http` entry is dropped."""
    web, api = _project(
        tmp_path,
        {
            "src/orders.ts": """
export async function loadOrder(id: string) {
  const res = await fetch(`${window.__CONFIG__.apiBase}/orders/${id}`);
  return res.json();
}
""",
        },
    )
    graph = scan_paths([web, api])
    hits = _calls_http_from(graph, function_id("src.orders.loadOrder"))
    assert hits == {"api/entrypoint:http:GET:/api/v1/orders/{order_id}"}, hits
    leftovers = [
        u
        for u in graph.attrs.get("unresolved_http") or []
        if str(u.get("from", "")).endswith(function_id("src.orders.loadOrder"))
    ]
    assert leftovers == [], leftovers


def test_route_literal_hits_rules() -> None:
    routes = [
        ("a", "GET", "/api/v1/orders/:id"),
        ("b", "GET", "/api/v1/orders"),
        ("c", "POST", "/api/v1/orders"),
        ("d", "GET", "/admin/items/{id}"),
        ("e", "GET", "/api/v1/items/{id}"),
    ]
    assert suffix_template_match("/api/v1/orders/:id", "/orders/:id")
    assert not suffix_template_match("/api/v1/orders/:id", "/api/v1/orders/:id")
    assert route_literal_hits("/api/v1/orders/:id", None, routes) == ["a"]
    assert route_literal_hits("/orders/:id", "GET", routes) == ["a"]
    assert route_literal_hits("/orders/:id", None, routes) == []
    assert route_literal_hits("/orders", "POST", routes) == ["c"]
    assert sorted(route_literal_hits("/items/:id", "GET", routes)) == ["d", "e"]
    assert route_literal_hits("/:id", "GET", routes) == []


def test_bare_fetch_literal_stays_same_repo(tmp_path: Path) -> None:
    """`fetch('/x')` is page-relative: it never links to another repo's route."""
    web, api = _project(
        tmp_path,
        {
            "src/bare.ts": """
export async function loadAll() {
  return fetch('/api/v1/orders');
}
""",
        },
    )
    graph = scan_paths([web, api])
    assert _calls_http_from(graph, function_id("src.bare.loadAll")) == set()


def test_known_base_to_unscanned_service_never_reroutes(tmp_path: Path) -> None:
    """web → `api` (VITE_API_URL, not scanned); `billing` has the same path.

    Neither the traced client nor the literal fallback may link web → billing;
    the call stays `target_not_scanned` for service `api`, and the discarded
    billing route is recorded for audit.
    """
    web = tmp_path / "web"
    billing = tmp_path / "billing"
    _write(
        tmp_path,
        "bizmap.yaml",
        """
services:
  api:
    url: http://api.internal
    env: VITE_API_URL
  billing:
    url: http://billing.internal
    env: BILLING_URL
""",
    )
    _write(
        web,
        "src/http.ts",
        """
import axios from 'axios';
export const api = axios.create({ baseURL: import.meta.env.VITE_API_URL });
""",
    )
    _write(
        web,
        "src/items.ts",
        """
import { api } from './http';

export async function loadItem(id: string) {
  return api.get(`/api/v1/items/${id}`);
}
""",
    )
    _write(
        billing,
        "app.py",
        """
from fastapi import FastAPI

app = FastAPI()


@app.get("/api/v1/items/{item_id}")
def get_item(item_id: str):
    return {}
""",
    )
    graph = scan_paths([web, billing], config=tmp_path / "bizmap.yaml")
    fn = function_id("src.items.loadItem")
    assert _calls_http_from(graph, fn) == set()
    unresolved = [
        u for u in graph.attrs.get("unresolved_http") or [] if str(u.get("from", "")).endswith(fn)
    ]
    assert len(unresolved) == 1, unresolved
    assert unresolved[0]["reason"] == "target_not_scanned"
    assert unresolved[0].get("service") == "api"
    node = next(n for n in graph.nodes.values() if n.id.endswith(fn))
    skipped = node.attrs.get("route_literal_skipped") or []
    assert skipped and skipped[0]["reason"] == "base_known", skipped
    assert skipped[0]["discarded"] == ["billing/entrypoint:http:GET:/api/v1/items/{item_id}"]


def test_known_base_to_scanned_service_links_only_there(tmp_path: Path) -> None:
    """Same path in two scanned repos: the env-mapped service wins, exactly once."""
    web = tmp_path / "web"
    api = tmp_path / "api"
    billing = tmp_path / "billing"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: VITE_API_URL\n")
    _write(
        web,
        "src/items.ts",
        """
import axios from 'axios';
const api = axios.create({ baseURL: import.meta.env.VITE_API_URL });

export async function loadItem(id: string) {
  return api.get(`/api/v1/items/${id}`);
}
""",
    )
    route = """
from fastapi import FastAPI

app = FastAPI()


@app.get("/api/v1/items/{item_id}")
def get_item(item_id: str):
    return {}
"""
    _write(api, "app.py", route)
    _write(billing, "app.py", route)
    graph = scan_paths([web, api, billing], config=tmp_path / "bizmap.yaml")
    hits = _calls_http_from(graph, function_id("src.items.loadItem"))
    assert hits == {"api/entrypoint:http:GET:/api/v1/items/{item_id}"}, hits


def test_service_helpers() -> None:
    services = {
        "api": {"url": "http://api.internal", "env": ["VITE_API_URL"]},
        "pay": {"env": "PAY_URL", "repo": "billing"},
    }
    assert service_for_url("{VITE_API_URL}/x", services) == "api"
    assert service_for_url("http://api.internal/x", services) == "api"
    assert service_for_url("{PAY_URL}/charge", services) == "billing"
    assert service_for_url("{base}/x", services) is None
    assert base_is_known("{VITE_API_URL}/x")
    assert base_is_known("https://h/x")
    assert not base_is_known("{base}/x")
    assert not base_is_known("{window.__CONFIG__.apiBase}/x")
    assert not base_is_known("/x")
