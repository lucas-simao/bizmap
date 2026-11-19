package users

import (
	"net/http"

	"github.com/labstack/echo/v4"

	usersvc "clinic/services/users"
)

type Handler struct {
	svc *usersvc.Service
}

func New(svc *usersvc.Service) *Handler {
	return &Handler{svc: svc}
}

// Create registers a patient.
func (h *Handler) Create(c echo.Context) error {
	if err := h.svc.Create(c.Request().Context(), c.Param("id"), "a@b.c", "Ann"); err != nil {
		return err
	}
	return c.NoContent(http.StatusCreated)
}

// List lists patients (repository uses SELECT *).
func (h *Handler) List(c echo.Context) error {
	if err := h.svc.List(c.Request().Context()); err != nil {
		return err
	}
	return c.NoContent(http.StatusOK)
}

// Get loads one patient.
func (h *Handler) Get(c echo.Context) error {
	if err := h.svc.Get(c.Request().Context(), c.Param("id")); err != nil {
		return err
	}
	return c.NoContent(http.StatusOK)
}

// Prefs reads email preference rows.
func (h *Handler) Prefs(c echo.Context) error {
	if err := h.svc.Prefs(c.Request().Context(), c.Param("id")); err != nil {
		return err
	}
	return c.NoContent(http.StatusOK)
}
