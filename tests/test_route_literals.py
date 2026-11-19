"""Front-end → API links from route literals (``via: route_literal``).

When a TS/JS client is built through layers the detectors cannot follow
(DI containers, registries, generic wrappers), an API-looking string such as
``/orders/${id}`` is still matched to the single backend route it names.

Synthetic fixtures only (orders, items, invoices).
"""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.http_match import route_literal_hits, suffix_template_match
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
    assert edge.confidence.value == "inferred"
    assert edge.attrs.get("via") == "route_literal"
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
