package profile

import (
	"net/http"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/labstack/echo/v4"
)

type Config struct {
	DB *pgxpool.Pool
}

// StaffPhoto is a handler factory: Echo registers the returned closure.
func StaffPhoto(cfg Config) echo.HandlerFunc {
	return func(c echo.Context) error {
		_, err := cfg.DB.Query(
			c.Request().Context(),
			`SELECT id, url FROM photos WHERE patient_id = $1`,
			c.Param("id"),
		)
		if err != nil {
			return err
		}
		return c.NoContent(http.StatusOK)
	}
}
