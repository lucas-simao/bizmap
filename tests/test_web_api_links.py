"""A React/Vite front end and the two Go/echo APIs it calls, written in the
shapes a real front uses: an axios base client, domain modules that are
objects of methods (`export const widgetsHttpClient = { list: ... }`), a
client made by a factory around a second axios instance, react-query hooks
(`queryFn: () => client.m()`, `mutationFn: client.m`), and pages.

Every link of the chain must reach the graph: page -> hook -> client method ->
route. All names are fictional."""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_paths


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


CORE_MAIN = """
package main

import (
    "github.com/labstack/echo/v4"
    "example.com/core-api/internal/httpapi"
)

func main() {
    e := echo.New()
    api := e.Group("/api/v1")
    httpapi.Register(e, api)
    e.Start(":8080")
}
"""

CORE_REGISTER = """
package httpapi

import (
    "github.com/labstack/echo/v4"
    "example.com/core-api/internal/widgets"
    "example.com/core-api/internal/teams"
)

func Register(e *echo.Echo, api *echo.Group) {
    widgets.NewController().RegisterRoutes(api)
    teams.NewController().RegisterRoutes(api)
}
"""

CORE_WIDGETS = """
package widgets

import "github.com/labstack/echo/v4"

type Controller struct{}

func NewController() *Controller { return &Controller{} }

func (c *Controller) RegisterRoutes(g *echo.Group) {
    w := g.Group("/widgets")
    w.GET("", c.List)
    w.POST("", c.Create)
    w.GET("/counts", c.Counts)
    w.GET("/:widgetId", c.Get)
    w.PATCH("/:widgetId", c.Update)
}

func (c *Controller) List(ctx echo.Context) error   { return nil }
func (c *Controller) Create(ctx echo.Context) error { return nil }
func (c *Controller) Counts(ctx echo.Context) error { return nil }
func (c *Controller) Get(ctx echo.Context) error    { return nil }
func (c *Controller) Update(ctx echo.Context) error { return nil }
"""

CORE_TEAMS = """
package teams

import "github.com/labstack/echo/v4"

type Controller struct{}

func NewController() *Controller { return &Controller{} }

func (c *Controller) RegisterRoutes(g *echo.Group) {
    t := g.Group("/teams")
    t.GET("/:teamId", c.Get)
    t.GET("/:teamId/members", c.Members)
    t.DELETE("/:teamId/members/:userId", c.RemoveMember)
}

func (c *Controller) Get(ctx echo.Context) error          { return nil }
func (c *Controller) Members(ctx echo.Context) error      { return nil }
func (c *Controller) RemoveMember(ctx echo.Context) error { return nil }
"""

PORTAL_MAIN = """
package main

import "github.com/labstack/echo/v4"

func main() {
    e := echo.New()
    g := e.Group("/api/v1/client")
    g.GET("/sites/:siteId", getSite)
    g.GET("/portfolio/summary", portfolioSummary)
    e.Start(":8081")
}

func getSite(c echo.Context) error          { return nil }
func portfolioSummary(c echo.Context) error { return nil }
"""

WEB_FILES = {
    "src/api/api.ts": """
import axios from 'axios';

export const api = axios.create({ baseURL: `${import.meta.env.VITE_API_URL}/api/v1` });
""",
    "src/api/widgets/index.ts": """
import { api } from '../api';

export const widgetsHttpClient = {
  list: (params: { status?: string }) => api.get('/widgets', { params }).then((r) => r.data),
  get: (widgetId: string) => api.get(`/widgets/${widgetId}`).then((r) => r.data),
  async create(body: { name: string }) {
    const r = await api.post('/widgets', body);
    return r.data;
  },
  update: function (widgetId: string, body: { name: string }) {
    return api.patch(`/widgets/${encodeURIComponent(widgetId)}`, body);
  },
  countByStatus: () => api.get('/widgets/counts').then((r) => r.data),
};
""",
    "src/api/teams/index.ts": """
import { api } from '../api';

export async function listMembers(teamId: string) {
  const r = await api.get(`/teams/${teamId}/members`);
  return r.data;
}

export const removeMember = (teamId: string, userId: string) =>
  api.delete(`/teams/${teamId}/members/${userId}`);
""",
    "src/api/clientApiFactory.ts": """
import axios from 'axios';

export function createClientApi(baseUrl: string) {
  return axios.create({ baseURL: `${baseUrl}/api/v1/client` });
}
""",
    "src/api/clientApi.ts": """
import { createClientApi } from './clientApiFactory';

export const clientApi = createClientApi(import.meta.env.VITE_CLIENT_API_URL);
""",
    "src/api/delivery/client.ts": """
import type { AxiosInstance } from 'axios';

export function createDeliveryHttpClient(http: AxiosInstance) {
  return {
    getSite: (siteId: string) => http.get(`/sites/${siteId}`).then((r) => r.data),
    portfolioSummary: () => http.get('/portfolio/summary').then((r) => r.data),
  };
}
""",
    "src/api/delivery/index.ts": """
import { clientApi } from '../clientApi';
import { createDeliveryHttpClient } from './client';

export const deliveryHttpClient = createDeliveryHttpClient(clientApi);
""",
    "src/features/widgets/widgets.queries.ts": """
import { useQuery } from '@tanstack/react-query';
import { widgetsHttpClient as client } from '../../api/widgets';

export function useWidgets(params: { status?: string }) {
  return useQuery({ queryKey: ['widgets', params], queryFn: () => client.list(params) });
}

export function useWidget(widgetId: string) {
  return useQuery({ queryKey: ['widget', widgetId], queryFn: () => client.get(widgetId) });
}

export async function loadWidgetCounts() {
  return client.countByStatus();
}
""",
    "src/features/widgets/widgets.mutations.ts": """
import { useMutation } from '@tanstack/react-query';
import { widgetsHttpClient } from '../../api/widgets';

export function useCreateWidget() {
  return useMutation({ mutationFn: widgetsHttpClient.create });
}
""",
    "src/features/teams/teams.queries.ts": """
import { useQuery } from '@tanstack/react-query';
import { listMembers } from '../../api/teams';

export function useMembers(teamId: string) {
  return useQuery({ queryKey: ['members', teamId], queryFn: () => listMembers(teamId) });
}
""",
    "src/features/delivery/site.queries.ts": """
import { useQuery } from '@tanstack/react-query';
import { deliveryHttpClient } from '../../api/delivery';

export function useSite(siteId: string) {
  return useQuery({ queryKey: ['site', siteId], queryFn: () => deliveryHttpClient.getSite(siteId) });
}

export function usePortfolio() {
  return useQuery({ queryKey: ['portfolio'], queryFn: () => deliveryHttpClient.portfolioSummary() });
}
""",
    "src/pages/WidgetsPage.tsx": """
import { useWidgets, loadWidgetCounts } from '../features/widgets/widgets.queries';
import { useCreateWidget } from '../features/widgets/widgets.mutations';

export function WidgetsPage() {
  const { data } = useWidgets({ status: 'active' });
  const create = useCreateWidget();
  const refresh = () => loadWidgetCounts();
  return <div onClick={refresh}>{String(data)}{String(create)}</div>;
}
""",
    "src/pages/WidgetDetailPage.tsx": """
import { useWidget } from '../features/widgets/widgets.queries';
import { widgetsHttpClient } from '../api/widgets';

export function WidgetDetailPage({ widgetId }: { widgetId: string }) {
  const { data } = useWidget(widgetId);
  const rename = (name: string) => widgetsHttpClient.update(widgetId, { name });
  return <button onClick={() => rename('x')}>{String(data)}</button>;
}
""",
    "src/pages/TeamPage.tsx": """
import { useMembers } from '../features/teams/teams.queries';

export function TeamPage({ teamId }: { teamId: string }) {
  const { data } = useMembers(teamId);
  return <ul>{String(data)}</ul>;
}
""",
    "src/pages/SitePage.tsx": """
import { useSite, usePortfolio } from '../features/delivery/site.queries';

export function SitePage({ siteId }: { siteId: string }) {
  const { data } = useSite(siteId);
  const summary = usePortfolio();
  return <section>{String(data)}{String(summary)}</section>;
}
""",
}

YAML = """
services:
  core-api:
    env: VITE_API_URL
  portal-api:
    env: VITE_CLIENT_API_URL
"""


def build(root: Path) -> list[Path]:
    """Write the three fictional repos under ``root``; returns their roots."""
    core, portal, web = root / "core-api", root / "portal-api", root / "web"
    _write(root, "bizmap.yaml", YAML)
    _write(core, "go.mod", "module example.com/core-api\n\ngo 1.22\n")
    _write(core, "cmd/api/main.go", CORE_MAIN)
    _write(core, "internal/httpapi/register.go", CORE_REGISTER)
    _write(core, "internal/widgets/controller.go", CORE_WIDGETS)
    _write(core, "internal/teams/controller.go", CORE_TEAMS)
    _write(portal, "go.mod", "module example.com/portal-api\n\ngo 1.22\n")
    _write(portal, "main.go", PORTAL_MAIN)
    _write(web, "package.json", '{"name": "web", "dependencies": {"axios": "1", "react": "18"}}\n')
    for rel, src in WEB_FILES.items():
        _write(web, rel, src)
    return [core, portal, web]


ROUTES = {
    "core-api": {
        "GET /api/v1/widgets", "POST /api/v1/widgets", "GET /api/v1/widgets/counts",
        "GET /api/v1/widgets/:widgetId", "PATCH /api/v1/widgets/:widgetId",
        "GET /api/v1/teams/:teamId", "GET /api/v1/teams/:teamId/members",
        "DELETE /api/v1/teams/:teamId/members/:userId",
    },
    "portal-api": {"GET /api/v1/client/sites/:siteId", "GET /api/v1/client/portfolio/summary"},
}

# web function -> route it calls (method-level owners, never a `.module`)
CALLS = {
    "src.api.widgets.index.widgetsHttpClient.list": "core-api/entrypoint:http:GET:/api/v1/widgets",
    "src.api.widgets.index.widgetsHttpClient.get": "core-api/entrypoint:http:GET:/api/v1/widgets/:widgetId",
    "src.api.widgets.index.widgetsHttpClient.create": "core-api/entrypoint:http:POST:/api/v1/widgets",
    "src.api.widgets.index.widgetsHttpClient.update": "core-api/entrypoint:http:PATCH:/api/v1/widgets/:widgetId",
    "src.api.widgets.index.widgetsHttpClient.countByStatus": "core-api/entrypoint:http:GET:/api/v1/widgets/counts",
    "src.api.teams.index.listMembers": "core-api/entrypoint:http:GET:/api/v1/teams/:teamId/members",
    "src.api.teams.index.removeMember": "core-api/entrypoint:http:DELETE:/api/v1/teams/:teamId/members/:userId",
    "src.api.delivery.client.createDeliveryHttpClient.getSite":
        "portal-api/entrypoint:http:GET:/api/v1/client/sites/:siteId",
    "src.api.delivery.client.createDeliveryHttpClient.portfolioSummary":
        "portal-api/entrypoint:http:GET:/api/v1/client/portfolio/summary",
}

LINKS = [
    ("src.features.widgets.widgets.queries.useWidgets", "src.api.widgets.index.widgetsHttpClient.list"),
    ("src.features.widgets.widgets.queries.useWidget", "src.api.widgets.index.widgetsHttpClient.get"),
    ("src.features.widgets.widgets.mutations.useCreateWidget", "src.api.widgets.index.widgetsHttpClient.create"),
    ("src.features.widgets.widgets.queries.loadWidgetCounts", "src.api.widgets.index.widgetsHttpClient.countByStatus"),
    ("src.pages.WidgetDetailPage.WidgetDetailPage.rename", "src.api.widgets.index.widgetsHttpClient.update"),
    ("src.features.delivery.site.queries.useSite", "src.api.delivery.client.createDeliveryHttpClient.getSite"),
]


def _scan(tmp_path: Path):
    roots = build(tmp_path)
    return scan_paths(roots, config=tmp_path / "bizmap.yaml", base=tmp_path)


def _w(qual: str) -> str:
    return f"web/{function_id(qual)}"


def test_both_apis_register_their_routes_with_group_prefixes(tmp_path: Path) -> None:
    graph = _scan(tmp_path)
    for svc, want in ROUTES.items():
        got = {
            f"{n.attrs.get('method')} {n.attrs.get('path')}"
            for n in graph.nodes.values()
            if n.type == "entrypoint" and n.id.startswith(f"{svc}/")
        }
        assert got == want, (svc, sorted(got))


def test_every_web_call_belongs_to_a_method_not_to_a_module(tmp_path: Path) -> None:
    graph = _scan(tmp_path)
    http = sorted((e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http")
    assert not [f for f, _t in http if f.endswith(".module")], http
    assert {(_w(f), t) for f, t in CALLS.items()} <= set(http), http


def test_hooks_and_pages_call_the_client_methods(tmp_path: Path) -> None:
    graph = _scan(tmp_path)
    calls = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls"}
    missing = [(a, b) for a, b in LINKS if (_w(a), _w(b)) not in calls]
    assert not missing, missing


def test_the_second_api_keeps_its_env_through_the_factory(tmp_path: Path) -> None:
    graph = _scan(tmp_path)
    unresolved = graph.attrs.get("unresolved_http") or []
    assert not [u for u in unresolved if u.get("reason") == "base_unknown"], unresolved
    # the call knows its base: the env of the factory's argument, not `{baseUrl}`
    for fn, path in (("getSite", "/api/v1/client/sites/"), ("portfolioSummary", "/api/v1/client/portfolio/summary")):
        node = graph.nodes[_w(f"src.api.delivery.client.createDeliveryHttpClient.{fn}")]
        urls = [c.get("url") for c in node.attrs.get("http_calls") or []]
        assert urls and all(u.startswith("{VITE_CLIENT_API_URL}" + path) for u in urls), urls


def test_export_lists_four_screens_and_per_route_consumers(tmp_path: Path) -> None:
    from bizmap.export.book import build_book
    from bizmap.export.services import assign_services

    graph = _scan(tmp_path)
    data = graph.to_dict()
    index, chunks = build_book(data, assign_services(data))
    names = chunks["names"]["l"]
    web = next(s for s in index["services"] if s["name"] == "web")
    screens = sorted(sc["f"] for sc in chunks[web["screen_chunk"]]["screens"])
    assert screens == ["src/pages/SitePage.tsx", "src/pages/TeamPage.tsx",
                       "src/pages/WidgetDetailPage.tsx", "src/pages/WidgetsPage.tsx"], screens
    uses = {names[u["r"]]: u["ui"] for u in chunks[web["uses_chunk"]]["uses"]}
    assert uses["GET /api/v1/widgets"] == ["src/pages/WidgetsPage.tsx"]
    assert uses["PATCH /api/v1/widgets/:widgetId"] == ["src/pages/WidgetDetailPage.tsx"]
    assert uses["GET /api/v1/client/sites/:siteId"] == ["src/pages/SitePage.tsx"]
    assert uses["DELETE /api/v1/teams/:teamId/members/:userId"] == []
