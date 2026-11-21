"""Round 45, items 2 and 3: URL labels without code or query strings (Go,
JS/TS), helpers with dynamic paths grouped, one method per call site."""

from __future__ import annotations

import re
from pathlib import Path

from bizmap.cli import scan_summary
from bizmap.scan import scan_path

FORBIDDEN = re.compile(r"[() ]|=>|\.\.\.")


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


GO_FORECAST = """package forecast

import (
	"fmt"
	"net/http"
	"net/url"
	"os"
)

var baseURL = os.Getenv("FORECAST_API_URL")

func Search(city string) (*http.Response, error) {
	params := url.Values{}
	params.Set("q", city)
	return http.Get(fmt.Sprintf("%s/?%s", baseURL, params.Encode()))
}

func Daily(q url.Values) (*http.Response, error) {
	return http.Get(baseURL + "/daily?" + q.Encode())
}

func Lookup(u *url.URL, q url.Values) {
	u.RawQuery = q.Encode()
	http.Get(u.String())
}

func Send(method string) {
	req, _ := http.NewRequest(method, baseURL+"/send", nil)
	http.DefaultClient.Do(req)
}

type Client struct{ base string }

func (c *Client) get(path string) (*http.Response, error) {
	req, _ := http.NewRequest(http.MethodGet, c.base+path, nil)
	return http.DefaultClient.Do(req)
}

func (c *Client) Stations() { c.get("/stations") }
func (c *Client) One(id string) { c.get(fmt.Sprintf("/stations/%s/%s", id, c.base)) }
func (c *Client) Other(p string) { c.get(p) }
"""

JS_CLIENT = """import axios from 'axios';

const api = axios.create({ baseURL: import.meta.env.VITE_API_URL });

export async function search(config: object, id: string) {
  await api.get(`/items/${id}?page=${1}`);
  await api.request({ ...config, validateStatus: () => true });
  await api.post('/items');
}
"""


def _labels(graph) -> list[tuple[str, str]]:
    out = [(u["method"], u["url"]) for u in graph.attrs.get("unresolved_http") or []]
    for node in graph.nodes.values():
        out += [(c.get("method", ""), c.get("url", "")) for c in node.attrs.get("http_calls") or []]
    return out


def test_go_labels_and_query(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module example.com/shop\n\ngo 1.22\n")
    _write(tmp_path, "forecast/forecast.go", GO_FORECAST)
    graph = scan_path(tmp_path)
    labels = _labels(graph)
    assert labels
    for method, url in labels:
        assert not FORBIDDEN.search(url), url
        assert "?" not in url, url
        assert not FORBIDDEN.search(method), method
    urls = {u for _m, u in labels}
    assert "{FORECAST_API_URL}/" in urls
    assert "{FORECAST_API_URL}/daily" in urls
    by_fn = {u["from"].split(":")[-1]: u for u in graph.attrs.get("unresolved_http") or []}
    assert by_fn["forecast.Send"]["method"] == "{dynamic}"  # never GET and POST


def test_js_labels(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", "{}\n")
    _write(tmp_path, "src/api.ts", JS_CLIENT)
    graph = scan_path(tmp_path)
    for _method, url in _labels(graph):
        assert not FORBIDDEN.search(url), url
        assert "?" not in url, url


def test_helper_with_dynamic_paths_is_grouped(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module example.com/shop\n\ngo 1.22\n")
    _write(tmp_path, "forecast/forecast.go", GO_FORECAST)
    graph = scan_path(tmp_path)
    via = [u for u in graph.attrs.get("unresolved_http") or [] if u.get("via")]
    assert any(u["url"].endswith("/stations") for u in via), via
    text = "\n".join(scan_summary(graph, verbose=True))
    assert "via forecast.Client.get" in text, text


API = """package main

import "github.com/labstack/echo/v4"

func list(c echo.Context) error   { return nil }
func create(c echo.Context) error { return nil }

func main() {
	e := echo.New()
	e.GET("/x", list)
	e.POST("/x", create)
	e.Start(":1")
}
"""

WEB = """import axios from 'axios';

const api = axios.create({ baseURL: import.meta.env.VITE_API_URL });

export async function createX() {
  return api.post('/x');
}

export async function createY() {
  return api.request({ method: 'POST', url: '/x' });
}

export function pathOnly() {
  return '/x';
}
"""


def test_one_method_per_call_site(tmp_path: Path) -> None:
    _write(tmp_path, "bizmap.yaml", "repos: [api, web]\nservices:\n  api: { env: VITE_API_URL }\n")
    _write(tmp_path, "api/go.mod", "module example.com/api\n\ngo 1.22\n")
    _write(tmp_path, "api/main.go", API)
    _write(tmp_path, "api/bizmap.yaml", "name: api\n")
    _write(tmp_path, "web/package.json", "{}\n")
    _write(tmp_path, "web/bizmap.yaml", "name: web\n")
    _write(tmp_path, "web/src/api.ts", WEB)
    graph = scan_path(tmp_path)
    by_caller: dict[str, set[str]] = {}
    for e in graph.edges.values():
        if e.type == "calls_http":
            target = graph.nodes[e.to_id]
            by_caller.setdefault(e.from_id, set()).add(str(target.attrs.get("method")))
    create_x = next(k for k in by_caller if k.endswith("createX"))
    create_y = next(k for k in by_caller if k.endswith("createY"))
    assert by_caller[create_x] == {"POST"}
    assert by_caller[create_y] == {"POST"}
    for caller, methods in by_caller.items():
        assert len(methods) == 1, (caller, methods)
