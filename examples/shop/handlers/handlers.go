package handlers

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"shop/service"
)

type Handler struct {
	svc *service.Service
}

func New(svc *service.Service) *Handler {
	return &Handler{svc: svc}
}

// CreateUser registers a shopper so they can place orders.
func (h *Handler) CreateUser(c echo.Context) error {
	var in service.NewUser
	if err := c.Bind(&in); err != nil {
		return err
	}
	out, err := h.svc.CreateUser(c.Request().Context(), in)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, out)
}

// GetUser loads a shopper profile for the account page.
func (h *Handler) GetUser(c echo.Context) error {
	out, err := h.svc.GetUser(c.Request().Context(), c.Param("id"))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, out)
}

// CreateOrder records a paid checkout and reserves inventory.
func (h *Handler) CreateOrder(c echo.Context) error {
	var in service.NewOrder
	if err := c.Bind(&in); err != nil {
		return err
	}
	out, err := h.svc.CreateOrder(c.Request().Context(), in)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, out)
}

// GetOrder returns an order and its line items for the confirmation screen.
func (h *Handler) GetOrder(c echo.Context) error {
	out, err := h.svc.GetOrder(c.Request().Context(), c.Param("id"))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, out)
}
