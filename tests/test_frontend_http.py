"""Round 40, finding 2: front-end calls through an env base link to the API
scanned with them (env inference, `prefix:`, suffix match) and a wrapper
imported from another module is followed."""

from __future__ import annotations

from pathlib import Path

from bizmap.cli import scan_summary
from bizmap.scan import scan_path

API = '''from fastapi import FastAPI

app = FastAPI()


@app.get("/api/v1/orders/{order_id}/items")
def order_items(order_id: str):
    return []


@app.get("/api/v1/me")
def me():
    return {}


@app.get("/api/v1/carts/{cart_id}")
def cart(cart_id: str):
    return {}
'''

CLIENT = """import axios from 'axios';

export const http = axios.create({ baseURL: import.meta.env.VITE_API_URL });
"""

PAGES = """import { http } from './client';

export async function loadItems(id: string) {
  return http.get(`/api/v1/orders/${id}/items`);
}

export async function loadMe() {
  return http.get('/me');
}

export async function loadCart(id: string) {
  return http.get(`/carts/${id}`);
}
"""


def _w(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _workspace(root: Path, services: str = "") -> None:
    _w(root, "bizmap.yaml", "repos: [web, api]\n" + services)
    _w(root, "web/bizmap.yaml", "name: web\n")
    _w(root, "api/bizmap.yaml", "name: api\n")
    _w(root, "api/app.py", API)
    _w(root, "web/src/client.ts", CLIENT)
    _w(root, "web/src/pages.ts", PAGES)


def _links(graph) -> dict[str, tuple[str, str | None]]:
    out = {}
    for e in graph.edges.values():
        if e.type == "calls_http":
            fn = e.from_id.split("function:")[-1].removeprefix("src.")
            out[fn] = (e.to_id.split("entrypoint:http:")[-1], e.attrs.get("via"))
    return out


def test_env_key_is_inferred_from_calls_that_matched(tmp_path: Path) -> None:
    _workspace(tmp_path)
    graph = scan_path(tmp_path)
    links = _links(graph)
    assert links["pages.loadItems"][0].endswith("/api/v1/orders/{order_id}/items"), links
    # `/me` (one literal segment) goes to the repo VITE_API_URL already reaches;
    # the env value carries /api/v1, so the route matches by its unique tail
    assert links["pages.loadMe"] == ("GET:/api/v1/me", "env_inferred"), links
    assert links["pages.loadCart"] == ("GET:/api/v1/carts/{cart_id}", "env_inferred"), links
    assert graph.attrs["inferred_services"] == [{"env": "VITE_API_URL", "repo": "api", "calls": 2}]
    text = "\n".join(scan_summary(graph))
    assert "linked 2 call(s) through VITE_API_URL to api" in text


def test_service_prefix(tmp_path: Path) -> None:
    _workspace(tmp_path, "services:\n  api: { env: VITE_API_URL, prefix: /api/v1 }\n")
    graph = scan_path(tmp_path)
    links = _links(graph)
    assert links["pages.loadMe"] == ("GET:/api/v1/me", None), links
    assert links["pages.loadCart"] == ("GET:/api/v1/carts/{cart_id}", None), links
    assert not graph.attrs.get("unresolved_http")


def test_hint_for_keys_that_still_do_not_link(tmp_path: Path) -> None:
    _workspace(tmp_path)
    (tmp_path / "web/src/pages.ts").write_text(
        "import { http } from './client';\n"
        "export async function loadMe() { return http.get('/me'); }\n",
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    text = "\n".join(scan_summary(graph))
    assert graph.attrs["service_hints"] == [{"env": "VITE_API_URL", "repo": "api", "calls": 1, "matching": 1}], (
        graph.attrs.get("unresolved_http")
    )
    assert "    api: { env: VITE_API_URL }" in text


WRAPPER = """import axios from 'axios';

const http = axios.create({ baseURL: import.meta.env.VITE_API_URL });

export function getJSON(path: string) {
  return http.get(path);
}
"""

USES_WRAPPER = """import { getJSON } from '../lib/http';

export async function listOrders() {
  return getJSON('/api/v1/orders/7/items?page=1');
}
"""


def test_wrapper_imported_from_another_module(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _w(tmp_path, "web/src/lib/http.ts", WRAPPER)
    _w(tmp_path, "web/src/a/api.ts", USES_WRAPPER)
    graph = scan_path(tmp_path)
    links = _links(graph)
    assert links.get("a.api.listOrders", ("",))[0].endswith("/api/v1/orders/{order_id}/items"), links
