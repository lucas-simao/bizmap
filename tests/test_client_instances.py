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
app.get('/api/v1/client/org/settings/users/:id/history', h);
app.get('/api/v1/client/org/settings/users/:id', h);
"""


def _calls_http(graph, fn: str) -> list:
    return [e for e in graph.edges.values() if e.type == "calls_http" and e.from_id.endswith(function_id(fn))]


def test_axios_instance_passed_to_function_factory_returning_object(tmp_path: Path) -> None:
    web, api = tmp_path / "web", tmp_path / "api"
    _write(tmp_path, "bizmap.yaml", "services:\n  api:\n    env: VITE_API_URL\n")
    _write(api, "server.ts", SERVER)
    _write(
        web,
        "src/http/portal.ts",
        """
import axios from 'axios';
export const portalApi = axios.create({ baseURL: `${import.meta.env.VITE_API_URL}/api/v1/client` });
""",
    )
    _write(
        web,
        "src/clients/org.ts",
        """
import type { AxiosInstance } from 'axios';

export function createOrgClient(http: AxiosInstance) {
  return {
    history: (id: string) => http.get(`/org/settings/users/${id}/history`),
    user(id: string) {
      return http.get(`/org/settings/users/${id}`);
    },
  };
}
""",
    )
    _write(
        web,
        "src/screens/settings.ts",
        """
import { portalApi } from '../http/portal';
import { createOrgClient } from '../clients/org';

const client = createOrgClient(portalApi);

export const loadHistory = (id: string) => client.history(id);
""",
    )
    graph = scan_paths([web, api], config=tmp_path / "bizmap.yaml")
    edges = [e for e in graph.edges.values() if e.type == "calls_http" and e.from_id.startswith("web/")]
    got = sorted((e.to_id, e.confidence.value) for e in edges)
    assert ("api/entrypoint:http:GET:/api/v1/client/org/settings/users/:id/history", "inferred") in got, (
        got,
        graph.attrs.get("unresolved_http"),
    )
    assert ("api/entrypoint:http:GET:/api/v1/client/org/settings/users/:id", "inferred") in got, got
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
    req, err := http.NewRequest(http.MethodGet, c.baseURL+"/api/v1/client/org/settings/users/"+id+"/history", nil)
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
        ("api/entrypoint:http:GET:/api/v1/client/org/settings/users/:id/history", "inferred")
    ], ([e.id for e in edges], graph.attrs.get("unresolved_http"))
