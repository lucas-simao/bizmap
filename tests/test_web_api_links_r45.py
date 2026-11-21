"""Round 45, items 4 and 5: a factory's internal `read(url)` helper keeps both
bases, a per-request `baseURL` built in a helper is followed (v2, never the
v1 route by its tail), and navigation paths / test mocks never link."""

from __future__ import annotations

from pathlib import Path

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


DELIVERY = """import axios from 'axios';

const createClientApi = (base: string) => axios.create({ baseURL: base });
const PORTAL_URL = import.meta.env.VITE_PORTAL_API_URL ?? '';

export const createDeliveryHttpClient = (api: any) => {
  async function read<T>(url: string) { return (await api.get(url)).data; }
  return {
    getSite: (id: string) => read(`/sites/${id}`),
    getSummary: () => api.get('/portfolio/summary'),
  };
};

export const clientDelivery = createDeliveryHttpClient(createClientApi(import.meta.env.VITE_CLIENT_API_URL));
export const portalDelivery = createDeliveryHttpClient(createClientApi(PORTAL_URL));
"""


def test_internal_read_helper_keeps_both_bases(tmp_path: Path) -> None:
    _workspace(tmp_path, {"src/delivery.ts": DELIVERY})
    graph = scan_path(tmp_path)
    site = sorted(_unresolved_from(graph, "getSite"))
    summary = sorted(_unresolved_from(graph, "getSummary"))
    assert len(summary) == 2, summary
    assert len(site) == 2, site
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
