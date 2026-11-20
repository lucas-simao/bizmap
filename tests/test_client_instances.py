"""HTTP client instances passed as parameters / stored in struct fields keep
the base they were created with (axios.create baseURL, Go constructor env)."""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_paths


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


SERVER = """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.get('/api/v1/shop/settings/users/:id/history', h);
app.get('/api/v1/shop/settings/users/:id', h);
"""


def _calls_http(graph, fn: str) -> list:
    return [e for e in graph.edges.values() if e.type == "calls_http" and e.from_id.endswith(function_id(fn))]


def test_axios_instance_passed_to_function_factory_returning_object(tmp_path: Path) -> None:
    web, api = tmp_path / "web", tmp_path / "api"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: VITE_API_URL\n")
    _write(api, "server.ts", SERVER)
    _write(
        web,
        "src/http/shop.ts",
        """
import axios from 'axios';
export const shopApi = axios.create({ baseURL: `${import.meta.env.VITE_API_URL}/api/v1/shop` });
""",
    )
    _write(
        web,
        "src/clients/shop.ts",
        """
import type { AxiosInstance } from 'axios';

export function createShopClient(http: AxiosInstance) {
  return {
    history: (id: string) => http.get(`/settings/users/${id}/history`),
    user(id: string) {
      return http.get(`/settings/users/${id}`);
    },
  };
}
""",
    )
    _write(
        web,
        "src/screens/settings.ts",
        """
import { shopApi } from '../http/shop';
import { createShopClient } from '../clients/shop';

const client = createShopClient(shopApi);

export const loadHistory = (id: string) => client.history(id);
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    edges = [e for e in graph.edges.values() if e.type == "calls_http" and e.from_id.startswith("web/")]
    got = sorted((e.to_id, e.confidence.value) for e in edges)
    assert ("api/entrypoint:http:GET:/api/v1/shop/settings/users/:id/history", "inferred") in got, (
        got,
        graph.attrs.get("unresolved_http"),
    )
    assert ("api/entrypoint:http:GET:/api/v1/shop/settings/users/:id", "inferred") in got, got
    assert not [u for u in graph.attrs.get("unresolved_http") or [] if u.get("reason") == "base_unknown"]


def test_go_struct_field_base_from_constructor_env(tmp_path: Path) -> None:
    caller, api = tmp_path / "worker", tmp_path / "api"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: API_URL\n")
    _write(api, "server.ts", SERVER)
    _write(caller, "go.mod", "module worker\n\ngo 1.22\n")
    _write(
        caller,
        "client/client.go",
        """
package client

import (
    "net/http"
    "os"
)

type Client struct {
    baseURL string
    http    *http.Client
}

func New() *Client {
    return &Client{baseURL: os.Getenv("API_URL"), http: http.DefaultClient}
}

func (c *Client) History(id string) error {
    req, err := http.NewRequest(http.MethodGet, c.baseURL+"/api/v1/shop/settings/users/"+id+"/history", nil)
    if err != nil {
        return err
    }
    _, err = c.http.Do(req)
    return err
}
""",
    )
    graph = scan_paths([caller, api], config=tmp_path / "bizmap.yaml")
    edges = _calls_http(graph, "client.Client.History")
    assert [(e.to_id, e.confidence.value) for e in edges] == [
        ("api/entrypoint:http:GET:/api/v1/shop/settings/users/:id/history", "inferred")
    ], ([e.id for e in edges], graph.attrs.get("unresolved_http"))


def _shop_factory(web: Path) -> None:
    _write(
        web,
        "src/clients/shop.ts",
        """
import type { AxiosInstance } from 'axios';

export function createShopClient(http: AxiosInstance) {
  const api = {
    history: (id: string) => http.get(`/settings/users/${id}/history`),
  };
  return api;
}
""",
    )


def test_axios_instance_variants(tmp_path: Path) -> None:
    """Barrel re-export of the instance, factory called inside a hook, inline
    axios.create argument, factory returning a variable."""
    web, api = tmp_path / "web", tmp_path / "api"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: VITE_API_URL\n")
    _write(api, "server.ts", SERVER)
    _write(
        web,
        "src/http/shop.ts",
        """
import axios from 'axios';
export const shopApi = axios.create({ baseURL: `${import.meta.env.VITE_API_URL}/api/v1/shop` });
""",
    )
    _write(web, "src/http/index.ts", "export { shopApi } from './shop';\n")
    _shop_factory(web)
    _write(
        web,
        "src/screens/a.ts",
        """
import { shopApi } from '../http';
import { createShopClient } from '../clients/shop';

export function useOrg() {
  const client = createShopClient(shopApi);
  return { load: (id: string) => client.history(id) };
}
""",
    )
    _write(
        web,
        "src/screens/b.ts",
        """
import axios from 'axios';
import { createShopClient } from '../clients/shop';

export const shopB = createShopClient(
  axios.create({ baseURL: `${import.meta.env.VITE_API_URL}/api/v1/shop` }),
);
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    edges = [e for e in graph.edges.values() if e.type == "calls_http" and e.from_id.startswith("web/")]
    got = sorted({(e.to_id, e.confidence.value) for e in edges})
    assert got == [("api/entrypoint:http:GET:/api/v1/shop/settings/users/:id/history", "inferred")], (
        got,
        graph.attrs.get("unresolved_http"),
    )
    assert not [u for u in graph.attrs.get("unresolved_http") or [] if u.get("reason") == "base_unknown"], graph.attrs.get(
        "unresolved_http"
    )


def test_go_client_in_service_field_with_constructor_param(tmp_path: Path) -> None:
    caller, api = tmp_path / "worker", tmp_path / "api"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: API_URL\n")
    _write(api, "server.ts", SERVER)
    _write(caller, "go.mod", "module worker\n\ngo 1.22\n")
    _write(
        caller,
        "shopclient/client.go",
        """
package shopclient

import "net/http"

type Client struct {
    base string
}

func New(baseURL string) *Client {
    return &Client{base: baseURL}
}

func (c *Client) History(id string) (*http.Response, error) {
    return http.Get(c.base + "/api/v1/shop/settings/users/" + id + "/history")
}
""",
    )
    _write(
        caller,
        "service/service.go",
        """
package service

import (
    "os"

    "worker/shopclient"
)

type Service struct {
    shop *shopclient.Client
}

func NewService() *Service {
    return &Service{shop: shopclient.New(os.Getenv("API_URL"))}
}

func (s *Service) Sync(id string) error {
    _, err := s.shop.History(id)
    return err
}
""",
    )
    graph = scan_paths([caller, api], config=tmp_path / "bizmap.yaml")
    edges = _calls_http(graph, "shopclient.Client.History")
    assert [(e.to_id, e.confidence.value) for e in edges] == [
        ("api/entrypoint:http:GET:/api/v1/shop/settings/users/:id/history", "inferred")
    ], ([e.id for e in edges], graph.attrs.get("unresolved_http"))


def test_go_struct_field_base_from_config_env_tag_and_ctor_param(tmp_path: Path) -> None:
    worker, reports, other = tmp_path / "worker", tmp_path / "reports", tmp_path / "other"
    _write(tmp_path, "bizmap.yaml", "services:\n  reports:\n    env: REPORT_API_URL\n")
    route = """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.get('/v1/reports/:id', h);
app.get('/v1/exports/:id', h);
"""
    _write(reports, "server.ts", route)
    _write(other, "server.ts", route)  # same paths elsewhere: only the env can disambiguate
    _write(worker, "go.mod", "module worker\n\ngo 1.22\n")
    _write(
        worker,
        "config/config.go",
        """
package config

type Config struct {
    ReportAPIURL string `env:"REPORT_API_URL,required"`
    Port         int    `env:"PORT"`
}
""",
    )
    _write(
        worker,
        "clients/report.go",
        """
package clients

import (
    "net/http"
    "os"

    "worker/config"
)

type ReportClient struct {
    baseURL string
    http    *http.Client
}

func NewReportClient(cfg config.Config) *ReportClient {
    return &ReportClient{baseURL: cfg.ReportAPIURL, http: http.DefaultClient}
}

func (c *ReportClient) Get(id string) (*http.Response, error) {
    return c.http.Get(c.baseURL + "/v1/reports/" + id)
}

type ExportClient struct {
    baseURL string
}

func NewExportClient(baseURL string) *ExportClient {
    return &ExportClient{baseURL: baseURL}
}

func (c *ExportClient) Get(id string) (*http.Response, error) {
    return http.Get(c.baseURL + "/v1/exports/" + id)
}

func Wire(cfg config.Config) (*ExportClient, *ExportClient) {
    return NewExportClient(os.Getenv("REPORT_API_URL")), NewExportClient(cfg.ReportAPIURL)
}
""",
    )
    graph = scan_paths([worker, reports, other], config=tmp_path / "bizmap.yaml")
    for fn, ep in (
        ("clients.ReportClient.Get", "reports/entrypoint:http:GET:/v1/reports/:id"),
        ("clients.ExportClient.Get", "reports/entrypoint:http:GET:/v1/exports/:id"),
    ):
        edges = _calls_http(graph, fn)
        assert [(e.to_id, e.confidence.value) for e in edges] == [(ep, "inferred")], (
            fn,
            [e.id for e in edges],
            [u for u in graph.attrs.get("unresolved_http") or [] if fn.split(".")[-2] in str(u.get("from"))],
        )


def _inventory_project(tmp_path: Path, ctor_value: str) -> object:
    worker, inv = tmp_path / "worker", tmp_path / "inventory"
    _write(tmp_path, "bizmap.yaml", "services:\n  inventory:\n    env: INVENTORY_URL\n")
    _write(
        inv,
        "server.ts",
        """
import express from 'express';
const app = express();
export function h(req: any, res: any) { res.json({}); }
app.get('/internal/stock', h);
""",
    )
    _write(worker, "go.mod", "module worker\n\ngo 1.22\n")
    _write(
        worker,
        "inv/client.go",
        f"""
package inv

import (
    "net/http"
    "strings"
)

type Client struct {{
    baseURL string
    http    *http.Client
}}

func New(baseURL string, tp http.RoundTripper) *Client {{
    return &Client{{baseURL: {ctor_value}, http: &http.Client{{Transport: tp}}}}
}}

func (c *Client) Stock() error {{
    req, _ := http.NewRequest("GET", c.baseURL+"/internal/stock", nil)
    _, err := c.http.Do(req)
    _ = strings.TrimSpace
    return err
}}
""",
    )
    _write(
        worker,
        "main.go",
        """
package main

import (
    "net/http"
    "os"

    "worker/inv"
)

func main() {
    c := inv.New(os.Getenv("INVENTORY_URL"), http.DefaultTransport)
    _ = c.Stock()
}
""",
    )
    return scan_paths([worker, inv], config=tmp_path / "bizmap.yaml")


def test_go_client_ctor_trimright_param(tmp_path: Path) -> None:
    """Regression (#50): `strings.TrimRight(baseURL, "/")` around a constructor
    parameter must not turn the base into an opaque path_dynamic hole."""
    graph = _inventory_project(tmp_path, 'strings.TrimRight(baseURL, "/")')
    edges = _calls_http(graph, "inv.Client.Stock")
    assert [(e.to_id, e.confidence.value) for e in edges] == [
        ("inventory/entrypoint:http:GET:/internal/stock", "inferred")
    ], ([e.id for e in edges], graph.attrs.get("unresolved_http"))


def test_go_client_ctor_plain_param_control(tmp_path: Path) -> None:
    graph = _inventory_project(tmp_path, "baseURL")
    edges = _calls_http(graph, "inv.Client.Stock")
    assert [(e.to_id, e.confidence.value) for e in edges] == [
        ("inventory/entrypoint:http:GET:/internal/stock", "inferred")
    ], ([e.id for e in edges], graph.attrs.get("unresolved_http"))
