package notify

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Send writes a notification row. Services inject this as a function field.
func Send(ctx context.Context, msg string) error {
	_, err := (&pgxpool.Pool{}).Exec(ctx, `INSERT INTO notifications (body) VALUES ($1)`, msg)
	return err
}
