package main

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	insertOrderSQL = `INSERT INTO orders (id, user_id, status, total_cents) VALUES ($1, $2, 'pending', $3)`
	selectOrderSQL = `SELECT id, status, total_cents FROM orders WHERE id = $1`
	markPaidSQL    = `UPDATE orders SET status = 'paid', paid_at = now() WHERE id = $1`
	expireSQL      = `UPDATE orders SET status = 'expired' WHERE status = 'pending' AND created_at < now() - interval '30 minutes'`
)

type Order struct {
	ID         string `json:"id"`
	Status     string `json:"status"`
	TotalCents int    `json:"total_cents"`
}

type Repo struct {
	db *pgxpool.Pool
}

func NewRepo(db *pgxpool.Pool) *Repo {
	return &Repo{db: db}
}

// InsertOrder stores a new pending order.
func (r *Repo) InsertOrder(ctx context.Context, userID string, totalCents int) (string, error) {
	id := fmt.Sprintf("ord_%d", time.Now().UnixNano())
	_, err := r.db.Exec(ctx, insertOrderSQL, id, userID, totalCents)
	return id, err
}

// GetOrder loads one order by id.
func (r *Repo) GetOrder(ctx context.Context, id string) (Order, error) {
	var o Order
	err := r.db.QueryRow(ctx, selectOrderSQL, id).Scan(&o.ID, &o.Status, &o.TotalCents)
	return o, err
}

// MarkPaid settles an order after billing confirms payment.
func (r *Repo) MarkPaid(ctx context.Context, id string) error {
	_, err := r.db.Exec(ctx, markPaidSQL, id)
	return err
}

// ExpirePending releases orders that were never paid.
func (r *Repo) ExpirePending(ctx context.Context) error {
	_, err := r.db.Exec(ctx, expireSQL)
	return err
}
