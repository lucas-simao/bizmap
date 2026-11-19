package main

import (
	"net/http"

	"github.com/labstack/echo/v4"
)

type Handler struct {
	repo    *Repo
	billing *Billing
}

func NewHandler(repo *Repo, billing *Billing) *Handler {
	return &Handler{repo: repo, billing: billing}
}

type newOrder struct {
	UserID     string `json:"user_id"`
	TotalCents int    `json:"total_cents"`
}

// CreateOrder records a checkout as pending and asks billing for an invoice.
func (h *Handler) CreateOrder(c echo.Context) error {
	var in newOrder
	if err := c.Bind(&in); err != nil {
		return err
	}
	ctx := c.Request().Context()
	id, err := h.repo.InsertOrder(ctx, in.UserID, in.TotalCents)
	if err != nil {
		return err
	}
	if err := h.billing.CreateInvoice(ctx, id, in.TotalCents); err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, map[string]string{"id": id})
}

// GetOrder returns the order status for the confirmation page.
func (h *Handler) GetOrder(c echo.Context) error {
	o, err := h.repo.GetOrder(c.Request().Context(), c.Param("id"))
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, o)
}

// MarkPaid is called by billing once the payment provider confirms the charge.
func (h *Handler) MarkPaid(c echo.Context) error {
	if err := h.repo.MarkPaid(c.Request().Context(), c.Param("id")); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}
