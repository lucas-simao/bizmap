"""Echo groups created inside a function from a group *parameter* keep the
caller's prefix, at any depth and across packages."""

from __future__ import annotations

from pathlib import Path

from bizmap.scan import scan_path


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _routes(graph) -> set[tuple[str, str]]:
    return {
        (n.attrs.get("method"), n.attrs.get("path"))
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    }


def test_go_echo_nested_group_via_param(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module app\n\ngo 1.22\n")
    _write(
        tmp_path,
        "main.go",
        """
package main

import (
    "github.com/labstack/echo/v4"

    "app/settings"
)

func main() {
    e := echo.New()
    api := e.Group("/api/v1/client")
    settings.RegisterRoutes(api)
    e.Start(":8080")
}
""",
    )
    _write(
        tmp_path,
        "settings/routes.go",
        """
package settings

import (
    "github.com/labstack/echo/v4"

    "app/settings/admin"
)

type H struct{}

func (h *H) Me(c echo.Context) error      { return nil }
func (h *H) History(c echo.Context) error { return nil }

func RegisterRoutes(g *echo.Group) {
    h := &H{}
    g.GET("/me", h.Me)
    s := g.Group("/org/settings")
    s.GET("/users/:id/history", h.History)
    adm := s.Group("/admin")
    admin.Register(adm)
}
""",
    )
    _write(
        tmp_path,
        "settings/admin/admin.go",
        """
package admin

import "github.com/labstack/echo/v4"

type A struct{}

func (a *A) Audit(c echo.Context) error { return nil }

func Register(g *echo.Group) {
    a := &A{}
    deep := g.Group("/audit")
    deep.GET("/:id", a.Audit)
}
""",
    )
    routes = _routes(scan_path(tmp_path))
    assert ("GET", "/api/v1/client/me") in routes, routes
    assert ("GET", "/api/v1/client/org/settings/users/:id/history") in routes, routes
    assert ("GET", "/api/v1/client/org/settings/admin/audit/:id") in routes, routes
    assert not any(p.startswith("/org/") or p.startswith("/audit") for _m, p in routes), routes


def test_empty_path_route_is_the_group_root(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module app\n\ngo 1.22\n")
    _write(
        tmp_path,
        "main.go",
        """
package main

import "github.com/labstack/echo/v4"

type H struct{}

func (h *H) List(c echo.Context) error   { return nil }
func (h *H) Create(c echo.Context) error { return nil }

func main() {
    e := echo.New()
    h := &H{}
    items := e.Group("/api/items")
    items.GET("", h.List)
    items.POST(``, h.Create)
    e.Start(":8080")
}
""",
    )
    routes = _routes(scan_path(tmp_path))
    assert {("GET", "/api/items"), ("POST", "/api/items")} <= routes, routes
