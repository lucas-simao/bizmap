package orders

import (
	"net/http"
	"time"

	"github.com/labstack/echo/v4"

	mw "github.com/example/unscanned/middleware"
	ordersvc "clinic/services/orders"
)

type Clock interface {
	Now() time.Time
}

type Handler struct {
	svc   *ordersvc.Service
	clock Clock
}

func New(svc *ordersvc.Service) *Handler {
	return &Handler{svc: svc}
}

func NewHandler(svc *ordersvc.Service) *Handler {
	return &Handler{svc: svc}
}

// Create books a new appointment.
func (h *Handler) Create(c echo.Context) error {
	_ = mw.Current(c.Param("id"))
	if h.clock != nil {
		_ = h.clock.Now()
	}
	if err := h.svc.Create(c.Request().Context(), c.Param("id"), c.Param("patient")); err != nil {
		return err
	}
	return c.NoContent(http.StatusCreated)
}

// List returns appointments for the front desk.
func (h *Handler) List(c echo.Context) error {
	if err := h.svc.List(c.Request().Context()); err != nil {
		return err
	}
	return c.NoContent(http.StatusOK)
}

// Get loads one appointment.
func (h *Handler) Get(c echo.Context) error {
	if err := h.svc.Get(c.Request().Context(), c.Param("id")); err != nil {
		return err
	}
	return c.NoContent(http.StatusOK)
}

// Update changes appointment status.
func (h *Handler) Update(c echo.Context) error {
	if err := h.svc.Update(c.Request().Context(), c.Param("id"), "canceled"); err != nil {
		return err
	}
	return c.NoContent(http.StatusOK)
}

// Setup completes pending-path onboarding.
func (h *Handler) Setup(c echo.Context) error {
	if err := h.svc.Setup(c.Request().Context()); err != nil {
		return err
	}
	return c.NoContent(http.StatusOK)
}
