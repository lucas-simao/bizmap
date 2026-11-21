"""Round 45, items 4 and 5: a factory's internal `read(url)` helper keeps both
bases, a per-request `baseURL` built in a helper is followed (v2, never the
v1 route by its tail), and navigation paths / test mocks never link."""

from __future__ import annotations

from pathlib import Path

import pytest

from bizmap.scan import scan_path


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


API = """package main

import "github.com/labstack/echo/v4"

func h(c echo.Context) error { return nil }

func main() {
	e := echo.New()
	e.GET("/sites/:id", h)
	e.GET("/portfolio/summary", h)
	e.GET("/api/v1/reports/flow", h)
	e.GET("/organizations", h)
	e.GET("/projects/:projectId/history", h)
	e.GET("/oauth/callback", h)
	e.GET("/projects/:projectId/schedule", h)
	e.GET("/projects/:projectId", h)
	e.GET("/projects", h)
	e.GET("/widgets", h)
	e.POST("/reports", h)
	e.Start(":1")
}
"""


def _workspace(root: Path, web: dict[str, str]) -> None:
    _write(root, "bizmap.yaml", "repos: [api, web]\n")
    _write(root, "api/go.mod", "module example.com/api\n\ngo 1.22\n")
    _write(root, "api/main.go", API)
    _write(root, "api/bizmap.yaml", "name: api\n")
    _write(root, "web/package.json", "{}\n")
    _write(root, "web/bizmap.yaml", "name: web\n")
    for rel, body in web.items():
        _write(root, f"web/{rel}", body)


def _links_from(graph, suffix: str) -> set[str]:
    return {
        e.to_id.split("entrypoint:http:")[-1]
        for e in graph.edges.values()
        if e.type == "calls_http" and e.from_id.endswith(suffix)
    }


def _unresolved_from(graph, suffix: str) -> list[str]:
    return [u["url"] for u in graph.attrs.get("unresolved_http") or [] if str(u.get("from", "")).endswith(suffix)]


DELIVERY = {
    "src/api/clientApiFactory.ts": """import axios from 'axios';

export function createClientApi(baseUrl: string) {
  return axios.create({ baseURL: baseUrl });
}
""",
    "src/api/clientApi.ts": """import { createClientApi } from './clientApiFactory';

const PORTAL_URL = import.meta.env.VITE_PORTAL_API_URL ?? '';
export const clientApi = createClientApi(import.meta.env.VITE_CLIENT_API_URL);
export const portalApi = createClientApi(PORTAL_URL);
""",
    "src/api/delivery/client.ts": """import type { AxiosInstance } from 'axios';

export function createDeliveryHttpClient(http: AxiosInstance) {
  async function read<T>(url: string): Promise<T> {
    return (await http.get(url)).data;
  }
  return {
    getSite: (siteId: string) => read(`/delivery-sites/${siteId}`),
    getSummary: () => http.get('/delivery-summary').then((r) => r.data),
  };
}
""",
    "src/api/delivery/index.ts": """import { clientApi, portalApi } from '../clientApi';
import { createDeliveryHttpClient } from './client';

export const clientDelivery = createDeliveryHttpClient(clientApi);
export const portalDelivery = createDeliveryHttpClient(portalApi);
""",
}


def test_internal_read_helper_keeps_both_bases(tmp_path: Path) -> None:
    # paths the API does not serve: each call stays unresolved, once per base
    _workspace(tmp_path, DELIVERY)
    graph = scan_path(tmp_path)
    site = sorted(_unresolved_from(graph, "getSite"))
    summary = sorted(_unresolved_from(graph, "getSummary"))
    # the helper call goes out through the same bases as the direct call
    assert summary, graph.attrs.get("unresolved_http")
    assert len(site) == len(summary), (site, summary)
    assert {u.split("}")[0] for u in site} == {u.split("}")[0] for u in summary}


REPORTS = """import axios from 'axios';

const http = axios.create({ baseURL: `${import.meta.env.VITE_API_URL}/api/v1` });
const V2 = `${import.meta.env.VITE_API_URL}/api/v2`;
const reportsGet = <T>(url: string, params?: object) => http.request<T>({ method: 'GET', baseURL: V2, url, params });

export const reportsClient = {
  /** GET /api/v2/reports/flow */
  getFlow: (p: object) => reportsGet('/reports/flow', p),
};
"""


def test_per_request_base_in_a_helper_is_followed(tmp_path: Path) -> None:
    _workspace(tmp_path, {"src/reports.ts": REPORTS})
    graph = scan_path(tmp_path)
    assert _links_from(graph, "getFlow") == set(), _links_from(graph, "getFlow")
    assert "{VITE_API_URL}/api/v2/reports/flow" in _unresolved_from(graph, "getFlow"), (
        graph.attrs.get("unresolved_http")
    )


NAV = {
    "src/menu.constants.ts": "export const MENU = [{ label: 'Organizações', path: '/organizations' }];\n",
    "src/tools/clocks.ts": "export const VIEWS = { history: '/time-clocks/history' };\n"
    "export function current() { return VIEWS.history; }\n",
    "src/oauth.ts": "const APP_URL = import.meta.env.VITE_APP_URL;\n"
    "export function login() { const redirectUri = `${APP_URL}/oauth/callback`; return redirectUri; }\n",
    "src/__tests__/schedule.test.ts": "import { rest } from 'msw';\n"
    "export const handlers = [rest.get('/projects/:projectId/schedule', (req, res, ctx) => res(ctx.json({})))];\n",
}


def test_navigation_and_mocks_never_link(tmp_path: Path) -> None:
    _workspace(tmp_path, NAV)
    graph = scan_path(tmp_path)
    links = [
        (e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http" and e.from_id.startswith("web/")
    ]
    assert links == [], links


# Problem 1 of the round-45 review: the v2 base goes through a helper that
# hands the config to a function of another module (`api.request(config)`).
CROSS_MODULE_V2 = {
    "src/api/api.ts": """import axios from 'axios';

export const API_BASE_URL = import.meta.env.VITE_API_URL;
export const api = axios.create({ baseURL: `${API_BASE_URL}/api/v1` });
""",
    "src/api/statusRequest.ts": """import { api } from './api';

export const requestWithStatus = <T>(
  config: { method: string; baseURL?: string; url: string; params?: object },
  errs: object,
) => api.request<T>(config).then((r) => r.data);
""",
    "src/api/reports.ts": """import { API_BASE_URL } from './api';
import { requestWithStatus } from './statusRequest';

const V2 = `${API_BASE_URL}/api/v2`;
const reportsGet = <T>(url: string, params?: Record<string, unknown>) =>
  requestWithStatus<T>({ method: 'GET', baseURL: V2, url, params }, {});

export const reportsClient = {
  getFlow: (p: Record<string, unknown>) => reportsGet('/reports/flow', p),
};
""",
}


def test_v2_base_through_a_config_helper_of_another_module(tmp_path: Path) -> None:
    _workspace(tmp_path, CROSS_MODULE_V2)
    graph = scan_path(tmp_path)
    assert _links_from(graph, "getFlow") == set(), _links_from(graph, "getFlow")
    assert "{VITE_API_URL}/api/v2/reports/flow" in _unresolved_from(graph, "getFlow"), (
        graph.attrs.get("unresolved_http")
    )


# Problem 2: URL builders, route templates and menu constants are navigation.
URL_BUILDERS = {
    "src/utils/links.ts": """export const projectLink = (domain: string, projectId: string) =>
  (projectId ? `${domain}/projects/${projectId}` : '');
export const PROJECT_ROUTE_TEMPLATE = '/projects/:projectId';
export const MENU_ACCESS = { projects: '/projects' };
""",
    "src/export/sheet.ts": """import { projectLink, PROJECT_ROUTE_TEMPLATE, MENU_ACCESS } from '../utils/links';

export function rows(domain: string, items: { id: string }[]) {
  return items.map((it) => ({ link: projectLink(domain, it.id), hyperlink: createHyperlink(projectLink(domain, it.id)) }));
}

export function tabs() {
  return [PROJECT_ROUTE_TEMPLATE.replace(':projectId', 'x'), MENU_ACCESS.projects];
}

function createHyperlink(url: string) {
  return { url };
}
""",
    # real requests keep linking
    "src/api/calls.ts": """import axios from 'axios';

declare function apiGet(uid: string, path: string): Promise<unknown>;

export function loadWidgets(uid: string) {
  return apiGet(uid, '/widgets');
}

export function sendReport() {
  return axios.post('/reports', {});
}
""",
}


def test_url_builders_templates_and_menus_never_link(tmp_path: Path) -> None:
    _workspace(tmp_path, URL_BUILDERS)
    graph = scan_path(tmp_path)
    nav = [
        (e.from_id, e.to_id)
        for e in graph.edges.values()
        if e.type == "calls_http" and ("/utils/" in e.from_id or "utils.links" in e.from_id or "export.sheet" in e.from_id)
    ]
    assert nav == [], nav
    assert _links_from(graph, "loadWidgets") == {"GET:/widgets"}
    assert _links_from(graph, "sendReport") == {"POST:/reports"}


# Follow-up: the config wrapper spreads the caller's config into a new object
# (`{ ...config, validateStatus }`), arrow and `async function` forms.
_SPREAD_ARROW = """import { api } from './api';

export const requestWithStatus = <T>(config: object, errs: object) =>
  api.request<T>({ ...config, validateStatus: () => true }).then((r) => r.data);
"""
_SPREAD_ASYNC = """import type { AxiosRequestConfig } from 'axios';
import { api } from './api';

export async function requestWithStatus<T>(config: AxiosRequestConfig, errs: object): Promise<T> {
  let res;
  try {
    res = await api.request<T>({ ...config, validateStatus: () => true });
  } catch (e) {
    throw e;
  }
  return res.data as T;
}
"""


@pytest.mark.parametrize("wrapper", [_SPREAD_ARROW, _SPREAD_ASYNC], ids=["arrow", "async"])
def test_v2_base_through_a_spreading_config_wrapper(tmp_path: Path, wrapper: str) -> None:
    files = dict(CROSS_MODULE_V2)
    files["src/api/statusRequest.ts"] = wrapper
    _workspace(tmp_path, files)
    graph = scan_path(tmp_path)
    assert _links_from(graph, "getFlow") == set(), _links_from(graph, "getFlow")
    assert "{VITE_API_URL}/api/v2/reports/flow" in _unresolved_from(graph, "getFlow"), (
        graph.attrs.get("unresolved_http")
    )
    # the wrapper's own call takes its method from the config: never POST
    own = [
        u for u in graph.attrs.get("unresolved_http") or [] if str(u.get("from", "")).endswith("requestWithStatus")
    ]
    assert all(u["method"] != "POST" for u in own), own
