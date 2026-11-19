package main

import "github.com/labstack/echo/v4"

// registerOrderRoutes mounts the order endpoints under the group it receives,
// so their full path depends on the caller's prefix (`/api` + `/orders`).
func registerOrderRoutes(g *echo.Group, h *Handler) {
	orders := g.Group("/orders")
	orders.POST("", h.CreateOrder)
	orders.GET("/:id", h.GetOrder)
	orders.POST("/:id/paid", h.MarkPaid)
}
