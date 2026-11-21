"""Round 45, item 1: gin routes (Group chains, groups passed to functions in
other packages, middleware in Group, Handle(method, ...), Any)."""

from __future__ import annotations

from pathlib import Path

from bizmap.scan import scan_path

MAIN = """package main

import (
	"github.com/gin-gonic/gin"
	"example.com/shop/internal/routes"
)

func health(c *gin.Context)      {}
func listOrders(c *gin.Context)  {}
func getOrder(c *gin.Context)    {}
func createOrder(c *gin.Context) {}
func dailyReport(c *gin.Context) {}
func patchOrder(c *gin.Context)  {}
func ping(c *gin.Context)        {}

func authMiddleware() gin.HandlerFunc { return func(c *gin.Context) {} }

func main() {
	r := gin.Default()
	api := r.Group("/api")
	v1 := api.Group("/v1")
	v1.GET("/health", health)
	{
		orders := v1.Group("/orders")
		orders.GET("", listOrders)
		orders.GET("/:id", getOrder)
		orders.POST("", createOrder)
	}
	routes.RegisterCustomers(v1)
	admin := r.Group("/admin", authMiddleware())
	admin.Group("/reports").GET("/daily", dailyReport)
	r.Handle("PATCH", "/api/v1/orders/:id", patchOrder)
	r.Any("/api/v1/ping", ping)
	r.Run()
}
"""

CUSTOMERS = """package routes

import "github.com/gin-gonic/gin"

type Handler struct{}

func (h *Handler) Get(c *gin.Context)    {}
func (h *Handler) Update(c *gin.Context) {}

func RegisterCustomers(rg *gin.RouterGroup) {
	h := &Handler{}
	customers := rg.Group("/customers")
	customers.GET("/:id", h.Get)
	customers.PUT("/:id", func(c *gin.Context) { h.Update(c) })
}
"""


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_gin_routes(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module example.com/shop\n\ngo 1.22\n\nrequire github.com/gin-gonic/gin v1.10.0\n")
    _write(tmp_path, "main.go", MAIN)
    _write(tmp_path, "internal/routes/customers.go", CUSTOMERS)
    graph = scan_path(tmp_path)
    routes = {
        (n.attrs["method"], n.attrs["path"]): n.id
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    }
    assert set(routes) == {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/orders"),
        ("GET", "/api/v1/orders/:id"),
        ("POST", "/api/v1/orders"),
        ("GET", "/api/v1/customers/:id"),
        ("PUT", "/api/v1/customers/:id"),
        ("GET", "/admin/reports/daily"),
        ("PATCH", "/api/v1/orders/:id"),
        ("ANY", "/api/v1/ping"),
    }, routes
    handlers = {
        (e.from_id, e.to_id) for e in graph.edges.values() if e.type == "triggers" and e.from_id.startswith("entrypoint:")
    }
    expect = {
        ("GET", "/api/v1/health"): "function:main.health",
        ("GET", "/api/v1/orders/:id"): "function:main.getOrder",
        ("PATCH", "/api/v1/orders/:id"): "function:main.patchOrder",
        ("GET", "/admin/reports/daily"): "function:main.dailyReport",
        ("GET", "/api/v1/customers/:id"): "function:internal/routes.Handler.Get",
    }
    for key, fn in expect.items():
        assert (routes[key], fn) in handlers, (key, sorted(h for h in handlers if h[0] == routes[key]))
    # every route has a handler
    assert all(any(h[0] == eid for h in handlers) for eid in routes.values())
