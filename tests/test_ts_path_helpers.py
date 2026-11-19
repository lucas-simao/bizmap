"""Pure TS path helpers inside client URLs (`/client${site(siteId)}/orders`)."""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.http_match import classify_unresolved_http
from bizmap.scan import scan_paths


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


API = """
from fastapi import FastAPI

app = FastAPI()


@app.get("/api/v1/client/sites/{site_id}/orders")
def site_orders(site_id: str):
    return []


@app.get("/settings/teams/{team_id}/members")
def team_members(team_id: str):
    return []


@app.get("/api/v1/projects/{project_id}/tasks")
def project_tasks(project_id: str):
    return []
"""


def _calls(graph, fn: str) -> set[str]:
    return {
        e.to_id for e in graph.edges.values() if e.type == "calls_http" and e.from_id.endswith(function_id(fn))
    }


def _unresolved(graph, fn: str) -> list[dict]:
    return [u for u in graph.attrs.get("unresolved_http") or [] if str(u.get("from", "")).endswith(function_id(fn))]


def test_helpers_are_evaluated_with_caller_argument_names(tmp_path: Path) -> None:
    web, api = tmp_path / "web", tmp_path / "api"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: VITE_API_URL\n")
    _write(api, "app.py", API)
    _write(
        web,
        "src/paths.ts",
        """
export const project = (pid: string) => `/projects/${pid}`;
""",
    )
    _write(
        web,
        "src/client.ts",
        """
import axios from 'axios';
import { project } from './paths';

const api = axios.create({ baseURL: import.meta.env.VITE_API_URL });

const site = (id: string) => `/sites/${id}`;

function seg(id: string) {
  return '/teams/' + id;
}

const weird = (id: string) => compute(id);

export async function siteOrders(siteId: string) {
  return api.get(`/api/v1/client${site(siteId)}/orders`);
}

export async function members(teamId: string) {
  return api.get('/settings' + seg(teamId) + '/members');
}

export async function projectTasks(projectId: string) {
  return api.get(`/api/v1${project(projectId)}/tasks`);
}

export async function opaque(id: string) {
  return api.get(`/api/v1/x${weird(id)}/y`);
}
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    assert _calls(graph, "src.client.siteOrders") == {"api/entrypoint:http:GET:/api/v1/client/sites/{site_id}/orders"}
    assert _calls(graph, "src.client.members") == {"api/entrypoint:http:GET:/settings/teams/{team_id}/members"}
    assert _calls(graph, "src.client.projectTasks") == {"api/entrypoint:http:GET:/api/v1/projects/{project_id}/tasks"}
    for fn in ("src.client.siteOrders", "src.client.members", "src.client.projectTasks"):
        assert _unresolved(graph, fn) == [], (fn, _unresolved(graph, fn))
    opaque = _unresolved(graph, "src.client.opaque")
    assert opaque and opaque[0]["reason"] == "path_dynamic", opaque


def test_call_hole_is_path_dynamic_not_base_unknown() -> None:
    assert classify_unresolved_http("{baseUrl}/api/v1/client{site(siteId)}/orders") == "path_dynamic"
    assert classify_unresolved_http("{baseUrl}/settings/{seg(id)}/members") == "path_dynamic"
    assert classify_unresolved_http("{baseUrl}/api/v1/orders") == "base_unknown"


def test_real_world_helpers_with_encoding_alias_and_objects(tmp_path: Path) -> None:
    """The evaluation's pattern: encodeURIComponent inside helpers, `@/` imports,
    object helpers, and an Express API with `:id` params."""
    web, api = tmp_path / "web", tmp_path / "api"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: VITE_API_URL\n")
    _write(
        api,
        "server.ts",
        """
import express from 'express';

const app = express();

export function siteOrders(req: any, res: any) { res.json([]); }
export function orgMembers(req: any, res: any) { res.json([]); }
export function teamBoard(req: any, res: any) { res.json([]); }
export function itemById(req: any, res: any) { res.json({}); }

app.get('/api/v1/client/sites/:id/orders', siteOrders);
app.get('/settings/:org/members', orgMembers);
app.get('/api/v1/teams/:team/board', teamBoard);
app.get('/api/v1/items/:id', itemById);
""",
    )
    _write(web, "tsconfig.json", '{ "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["src/*"] } } }\n')
    _write(
        web,
        "src/lib/paths.ts",
        """
export const site = (id: string) => `/sites/${encodeURIComponent(id)}`;
export const seg = (s: string) => `/${encodeURIComponent(s)}`;
export const routes = {
  team: (t: string) => `/teams/${String(t)}`,
};
""",
    )
    _write(
        web,
        "src/lib/http.ts",
        """
import axios from 'axios';
export const api = axios.create({ baseURL: import.meta.env.VITE_API_URL });
""",
    )
    _write(
        web,
        "src/features/screens.ts",
        """
import { api } from '@/lib/http';
import { site, seg, routes } from '@/lib/paths';

export async function loadSiteOrders(siteId: string) {
  return api.get(`/api/v1/client${site(siteId)}/orders`);
}

export async function loadMembers(orgId: string) {
  return api.get(`/settings${seg(orgId)}/members`);
}

export async function loadBoard(teamId: string) {
  return api.get(`/api/v1${routes.team(teamId)}/board`);
}

export async function loadItem(item: { id: number }) {
  return api.get('/api/v1/items/' + encodeURIComponent(item.id.toString()));
}
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    expected = {
        "src.features.screens.loadSiteOrders": "api/entrypoint:http:GET:/api/v1/client/sites/:id/orders",
        "src.features.screens.loadMembers": "api/entrypoint:http:GET:/settings/:org/members",
        "src.features.screens.loadBoard": "api/entrypoint:http:GET:/api/v1/teams/:team/board",
        "src.features.screens.loadItem": "api/entrypoint:http:GET:/api/v1/items/:id",
    }
    for fn, ep in expected.items():
        assert _calls(graph, fn) == {ep}, (fn, _calls(graph, fn), _unresolved(graph, fn))
        assert _unresolved(graph, fn) == [], (fn, _unresolved(graph, fn))


def test_base_helpers_namespace_imports_and_composed_helpers(tmp_path: Path) -> None:
    """`${base()}/x` with base() in another file returning `${API_URL}/api/v1/client`,
    `import * as P` + `P.site(id)`, object helpers, and helpers composed from
    imported helpers."""
    web, api = tmp_path / "web", tmp_path / "api"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: VITE_API_URL\n")
    _write(
        api,
        "server.ts",
        """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.get('/api/v1/client/sites/:id/orders', h);
app.get('/api/v1/client/sites/:id/summary', h);
app.get('/api/v1/client/orders/:id', h);
app.get('/api/v1/client/sites/:id/stock', h);
""",
    )
    _write(web, "tsconfig.json", '{ "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["src/*"] } } }\n')
    _write(
        web,
        "src/lib/base.ts",
        """
const API_URL = import.meta.env.VITE_API_URL;
export const base = () => `${API_URL}/api/v1/client`;
""",
    )
    _write(
        web,
        "src/lib/paths.ts",
        """
import { base } from '@/lib/base';

export const site = (siteId: string) => `/sites/${encodeURIComponent(siteId)}`;
export const paths = { order: (id: string) => `/orders/${id}` };
export const siteStock = (siteId: string) => `${base()}/sites/${siteId}/stock`;
""",
    )
    _write(
        web,
        "src/screens.ts",
        """
import axios from 'axios';
import { base } from '@/lib/base';
import { paths, siteStock } from '@/lib/paths';
import * as P from '@/lib/paths';

const http = axios.create();

export const listSiteOrders = (siteId: string) => http.get(`${base()}${P.site(siteId)}/orders`);
export const siteSummary = (siteId: string) => http.get(base() + P.site(siteId) + '/summary');
export const getOrder = (id: string) => http.get(`${base()}${paths.order(id)}`);
export const getStock = (siteId: string) => http.get(siteStock(siteId));
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    expected = {
        "src.screens.listSiteOrders": "api/entrypoint:http:GET:/api/v1/client/sites/:id/orders",
        "src.screens.siteSummary": "api/entrypoint:http:GET:/api/v1/client/sites/:id/summary",
        "src.screens.getOrder": "api/entrypoint:http:GET:/api/v1/client/orders/:id",
        "src.screens.getStock": "api/entrypoint:http:GET:/api/v1/client/sites/:id/stock",
    }
    for fn, ep in expected.items():
        assert _calls(graph, fn) == {ep}, (fn, _calls(graph, fn), _unresolved(graph, fn))
        assert _unresolved(graph, fn) == [], (fn, _unresolved(graph, fn))
    for helper in ("src.lib.base.base", "src.lib.paths.site", "src.lib.paths.siteStock"):
        assert _calls(graph, helper) == set(), helper
