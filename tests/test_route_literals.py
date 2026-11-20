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
import { container } from './container';

export const ORDERS_KEY = '/orders';

export async function listOrders() {
  return container.resolve('http').get('/orders');
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
  const res = await fetch(`${window.__CONFIG__.apiBase}/api/v1/orders/${id}`);
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



def test_literal_unique_in_own_repo_but_ambiguous_overall_links_nothing(tmp_path: Path) -> None:
    """web has its own BFF route and billing has the same path: after the
    merge the literal has two owners, so no `heuristic` edge may survive."""
    web = tmp_path / "web"
    billing = tmp_path / "billing"
    _write(
        web,
        "src/bff.ts",
        """
import express from 'express';
const app = express();
export function bffItem(req: any, res: any) { res.json({}); }
app.get('/api/v1/items/:id', bffItem);
""",
    )
    _write(
        web,
        "src/screens.ts",
        """
import { container } from './container';

export async function loadItem(id: string) {
  return container.resolve('http').get(`/items/${id}`);
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
    alone = scan_path(web)
    assert _calls_http_from(alone, function_id("src.screens.loadItem")) == {
        "entrypoint:http:GET:/api/v1/items/:id"
    }
    graph = scan_paths([web, billing])
    fn = function_id("src.screens.loadItem")
    assert _calls_http_from(graph, fn) == set(), _calls_http_from(graph, fn)
    node = next(n for n in graph.nodes.values() if n.id.endswith(fn))
    skipped = node.attrs.get("route_literal_skipped") or []
    assert skipped and skipped[0]["reason"] == "ambiguous", skipped
    assert len(skipped[0]["discarded"]) == 2, skipped


def test_path_helper_is_never_the_origin(tmp_path: Path) -> None:
    """`site()` only builds the path; the request (and the edge) is `getSite`."""
    web, api = _project(
        tmp_path,
        {
            "src/sites.ts": """
import { container } from './container';

const site = (id: string) => `/delivery/sites/${encodeURIComponent(id)}`;

export const getSite = (id: string) => container.resolve('http').get(site(id));
""",
        },
    )
    _write(
        api,
        "delivery.py",
        """
from fastapi import FastAPI

app = FastAPI()


@app.get("/api/v1/delivery/sites/{site_id}")
def get_site(site_id: str):
    return {}
""",
    )
    graph = scan_paths([web, api])
    assert _calls_http_from(graph, function_id("src.sites.site")) == set()
    assert _calls_http_from(graph, function_id("src.sites.getSite")) == {
        "api/entrypoint:http:GET:/api/v1/delivery/sites/{site_id}"
    }


def test_literal_with_base_resolved_to_scanned_service_is_inferred() -> None:
    """Known base → service `api` scanned → its single matching route, `inferred`."""
    from bizmap.graph import Graph
    from bizmap.scan import _link_route_literals

    graph = Graph()
    graph.repo = "web"
    graph.attrs["services"] = {"api": {"env": "VITE_API_URL"}}
    for repo in ("api", "billing"):
        graph.add_node(
            f"{repo}/entrypoint:http:GET:/v1/items/:id",
            "entrypoint",
            kind="http",
            method="GET",
            path="/v1/items/:id",
            repo=repo,
            evidence={"file": "app.py", "line": 1, "symbol": "h"},
        )
    graph.add_node(
        "web/function:src.items.load",
        "function",
        repo="web",
        http_calls=[{"method": "GET", "url": "{VITE_API_URL}/v1/items/{id}"}],
        route_literals=[
            {
                "path": "/v1/items/:id",
                "method": "GET",
                "evidence": {"file": "src/items.ts", "line": 3, "symbol": "src.items.load"},
            }
        ],
    )
    _link_route_literals(graph)
    edges = [e for e in graph.edges.values() if e.type == "calls_http"]
    assert [(e.to_id, e.confidence.value, e.attrs.get("via")) for e in edges] == [
        ("api/entrypoint:http:GET:/v1/items/:id", "inferred", "route_literal")
    ]


def test_helper_in_other_file_with_unresolvable_base_moves_to_caller(tmp_path: Path) -> None:
    """Regression (#31): `getItem → http.get(itemPath(id))`, itemPath in another file
    building `${base}/v1/items/${id}` with an unresolvable base. The link must
    survive and leave `getItem`, not `itemPath`."""
    web, api = _project(
        tmp_path,
        {
            "src/paths.ts": """
import { getBase } from './config';
const base = getBase();
export const itemPath = (id: string) => `${base}/v1/items/${id}`;
""",
            "src/items.ts": """
import { itemPath } from './paths';
import { container } from './container';

export async function getItem(id: string) {
  return container.resolve('http').get(itemPath(id));
}
""",
        },
    )
    _write(
        api,
        "items.py",
        """
from fastapi import FastAPI

app = FastAPI()


@app.get("/v1/items/{item_id}")
def item(item_id: str):
    return {}
""",
    )
    graph = scan_paths([web, api])
    assert _calls_http_from(graph, function_id("src.paths.itemPath")) == set()
    hits = _calls_http_from(graph, function_id("src.items.getItem"))
    assert hits == {"api/entrypoint:http:GET:/v1/items/{item_id}"}, hits


def test_uncalled_helper_keeps_its_literal(tmp_path: Path) -> None:
    """No caller found → the link stays on the helper (origin: path_helper)."""
    web, api = _project(
        tmp_path,
        {"src/orphan.ts": "export const orderPath = (id: string) => `${window.base}/api/v1/orders/${id}`;\n"},
    )
    graph = scan_paths([web, api])
    edges = [e for e in graph.edges.values() if e.type == "calls_http" and e.from_id.endswith("orderPath")]
    assert len(edges) == 1 and edges[0].attrs.get("origin") == "path_helper", [(e.id, e.attrs) for e in edges]


def test_unmapped_env_base_self_call_is_kept_cross_repo_is_not(tmp_path: Path) -> None:
    """Regression (#31 c): a route in service A fetches `${otherApiUrl}/v1/report-video`
    which exists in A. The env base is not in `services:`: keep the self-call
    (heuristic), never link to another repo on that basis."""
    a = tmp_path / "svc_a"
    b = tmp_path / "svc_b"
    _write(
        a,
        "server.ts",
        """
import express from 'express';
const app = express();
const otherApiUrl = process.env.OTHER_API_URL;

export async function proxyReport(req: any, res: any) {
  const r = await fetch(`${otherApiUrl}/v1/report-video?x=${req.query.x}`);
  res.json(await r.json());
}

export function reportVideo(req: any, res: any) { res.json({}); }

export async function proxyInvoice(req: any, res: any) {
  const r = await fetch(`${otherApiUrl}/v1/invoices/${req.params.id}`);
  res.json(await r.json());
}

app.get('/proxy/report', proxyReport);
app.get('/v1/report-video', reportVideo);
app.get('/proxy/invoice/:id', proxyInvoice);
""",
    )
    _write(
        b,
        "app.py",
        """
from fastapi import FastAPI

app = FastAPI()


@app.get("/v1/invoices/{invoice_id}")
def invoice(invoice_id: str):
    return {}
""",
    )
    graph = scan_paths([a, b])
    report = _calls_http_from(graph, function_id("server.proxyReport"))
    assert report == {"svc_a/entrypoint:http:GET:/v1/report-video"}, report
    # The literal fallback never crosses repos on an unmapped env base. (The
    # traced matcher's own rule — 2+ literal segments may match across repos
    # when the base is not declared — is unchanged and not via route_literal.)
    cross = [
        e
        for e in graph.edges.values()
        if e.type == "calls_http" and e.from_id.endswith("proxyInvoice") and e.attrs.get("via") == "route_literal"
    ]
    assert cross == [], [(e.id, e.attrs) for e in cross]


def _two_repos(tmp_path: Path, web_files: dict[str, str], server: str) -> tuple[Path, Path]:
    web, api = tmp_path / "web", tmp_path / "api"
    _write(api, "server.ts", server)
    for rel, src in web_files.items():
        _write(web, rel, src)
    return web, api


def test_explicit_base_requires_whole_path(tmp_path: Path) -> None:
    """(a) POST `${API_BASE_URL}/api/v2/items/${id}/pdf-report` must not link to
    POST /api/v1/client/items/:id/pdf-report by its tail."""
    web, api = _two_repos(
        tmp_path,
        {
            "src/pdf.ts": """
const API_BASE_URL = window.__cfg.base;
const itemPdf = (id: string) => `/items/${id}/pdf-report`;

export async function makePdf(id: string) {
  return fetch(`${API_BASE_URL}/api/v2/items/${id}/pdf-report`, { method: 'POST' });
}

export async function makePdf2(id: string) {
  return fetch(`${API_BASE_URL}/api/v2${itemPdf(id)}`, { method: 'POST' });
}
""",
        },
        """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.post('/api/v1/client/items/:id/pdf-report', h);
""",
    )
    graph = scan_paths([web, api])
    for fn in ("src.pdf.makePdf", "src.pdf.makePdf2", "src.pdf.itemPdf"):
        assert _calls_http_from(graph, function_id(fn)) == set(), (fn, _calls_http_from(graph, function_id(fn)))


def test_helper_intermediate_value_is_not_a_request(tmp_path: Path) -> None:
    """(b) `${base(id)}/photos` with base(id) = `/public/docs/${id}`: only the
    final URL links; the intermediate never links to /public/docs/:id."""
    web, api = _two_repos(
        tmp_path,
        {
            "src/docs.ts": """
import { container } from './c';

const base = (id: string) => `/public/docs/${id}`;

export async function fetchPhotos(id: string) {
  return container.resolve('http').get(`${base(id)}/photos`);
}
""",
        },
        """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.get('/public/docs/:id', h);
app.get('/public/docs/:id/photos', h);
""",
    )
    graph = scan_paths([web, api])
    assert _calls_http_from(graph, function_id("src.docs.fetchPhotos")) == {
        "api/entrypoint:http:GET:/public/docs/:id/photos"
    }
    assert _calls_http_from(graph, function_id("src.docs.base")) == set()


def test_get_item_through_aliased_encoding_helper(tmp_path: Path) -> None:
    """(c) getItem → http.get(itemPath(id)), itemPath in another file via `@/`,
    with encodeURIComponent, to GET /delivery/items/:item_id."""
    web, api = _two_repos(
        tmp_path,
        {
            "tsconfig.json": '{ "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["src/*"] } } }\n',
            "src/lib/paths.ts": "export const itemPath = (id: string) => `/delivery/items/${encodeURIComponent(id)}`;\n",
            "src/items.ts": """
import { itemPath } from '@/lib/paths';
import { container } from '@/lib/c';

export const getItem = (id: string) => container.resolve('http').get(itemPath(id));
""",
        },
        """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.get('/delivery/items/:item_id', h);
""",
    )
    graph = scan_paths([web, api])
    assert _calls_http_from(graph, function_id("src.items.getItem")) == {
        "api/entrypoint:http:GET:/delivery/items/:item_id"
    }


def test_module_const_literal_origin_is_the_function(tmp_path: Path) -> None:
    """(d) URL assembled in handle() from a module-level const: the edge leaves
    handle, never `<module>.module`."""
    web, api = _two_repos(
        tmp_path,
        {
            "src/index.ts": """
const REPORT = '/v1/report-video';
const DIRECT = '/v1/reports/latest';

import { container } from './c';

export async function handle(req: any) {
  const r = await container.resolve('http').get(window.__cfg.base + REPORT);
  return r.data;
}

export async function latest(http: any) {
  return http.get(DIRECT);
}
""",
        },
        """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.get('/v1/report-video', h);
app.get('/v1/reports/latest', h);
""",
    )
    graph = scan_paths([web, api])
    module_edges = [
        e.id for e in graph.edges.values() if e.type == "calls_http" and ".module" in e.from_id
    ]
    assert module_edges == [], module_edges
    assert _calls_http_from(graph, function_id("src.index.handle")) == {"api/entrypoint:http:GET:/v1/report-video"}
    assert _calls_http_from(graph, function_id("src.index.latest")) == {"api/entrypoint:http:GET:/v1/reports/latest"}


def test_object_member_helper_is_attributed_and_never_suffix_matched(tmp_path: Path) -> None:
    """Problem 3: `paths.pdfReport(id)` is an object-member helper. Used inside
    `${API_BASE_URL}/api/v2${...}` it must not link to /api/v1/client/...; an
    uncalled member helper is kept but matched whole only."""
    web, api = _two_repos(
        tmp_path,
        {
            "src/paths.ts": """
export const paths = {
  pdfReport: (id: string) => `/items/${id}/pdf-report`,
  nested: { orphan: (id: string) => `/items/${id}/pdf-report` },
};
""",
            "src/pdf.ts": """
import { paths } from './paths';
import { container } from './c';

const API_BASE_URL = window.__cfg.base;

export const makePdf = (id: string) =>
  fetch(`${API_BASE_URL}/api/v2${paths.pdfReport(id)}`, { method: 'POST' });

export const exportZ = (id: string) => container.resolve('http').get(paths.zed(id));
""",
            "src/zed.ts": "",
        },
        """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.post('/api/v1/client/items/:id/pdf-report', h);
""",
    )
    graph = scan_paths([web, api])
    web_links = [
        (e.from_id, e.to_id, e.confidence.value)
        for e in graph.edges.values()
        if e.type == "calls_http" and e.from_id.startswith("web/")
    ]
    assert web_links == [], web_links


def test_object_member_helper_direct_call_moves_to_caller(tmp_path: Path) -> None:
    web, api = _two_repos(
        tmp_path,
        {
            "src/paths.ts": "export const paths = { report: (id: string) => `/v9/reports/${id}` };\n",
            "src/screens.ts": """
import { paths } from './paths';
import { container } from './c';

export const loadReport = (id: string) => container.resolve('http').get(paths.report(id));
""",
        },
        """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.get('/v9/reports/:id', h);
""",
    )
    graph = scan_paths([web, api])
    assert _calls_http_from(graph, function_id("src.screens.loadReport")) == {"api/entrypoint:http:GET:/v9/reports/:id"}
    assert not [e for e in graph.edges.values() if e.type == "calls_http" and ".module" in e.from_id]
