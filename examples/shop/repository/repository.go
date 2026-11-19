package repository

import (
	"context"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const insertUserSQL = `
INSERT INTO users (id, email, name, status)
VALUES ($1, $2, $3, 'active')
`

const getUserSQL = `
SELECT id, email, name, status FROM users WHERE id = $1
`

const insertOrderSQL = `
INSERT INTO orders (id, user_id, status, total_cents)
VALUES ($1, $2, 'pending', $3)
`

const insertItemSQL = `
INSERT INTO order_items (id, order_id, sku, qty, unit_cents)
VALUES ($1, $2, $3, $4, $5)
`

const selectOrderSQL = `
SELECT id, user_id, status, total_cents, notes FROM orders WHERE id = $1
`

const selectItemsSQL = `
SELECT id, order_id, sku, qty, unit_cents FROM order_items WHERE order_id = $1
`

const selectInventorySQL = `
SELECT sku, qty FROM inventory WHERE sku = $1
`

const expirePendingSQL = `
UPDATE orders
SET status = 'expired'
WHERE status = 'pending' AND created_at < now() - interval '1 hour'
`

const selectUsersBase = `SELECT id, email FROM users`
const selectUsersFilt = ` WHERE status = 'active'`

type Repo struct {
	db *pgxpool.Pool
}

func New(db *pgxpool.Pool) *Repo {
	return &Repo{db: db}
}

type User struct {
	ID     string
	Email  string
	Name   string
	Status string
}

type OrderItem struct {
	ID        string
	OrderID   string
	SKU       string
	Qty       int
	UnitCents int
}

type Order struct {
	ID         string
	UserID     string
	Status     string
	TotalCents int
	Notes      *string
	Items      []OrderItem
}

func (r *Repo) InsertUser(ctx context.Context, id, email, name string) (User, error) {
	_, err := r.db.Exec(ctx, insertUserSQL, id, email, name)
	if err != nil {
		return User{}, err
	}
	return User{ID: id, Email: email, Name: name, Status: "active"}, nil
}

func (r *Repo) GetUser(ctx context.Context, id string) (User, error) {
	row := r.db.QueryRow(ctx, getUserSQL, id)
	var u User
	err := row.Scan(&u.ID, &u.Email, &u.Name, &u.Status)
	return u, err
}

func (r *Repo) InsertOrder(ctx context.Context, id, userID string, items []OrderItem) (Order, error) {
	tx, err := r.db.Begin(ctx)
	if err != nil {
		return Order{}, err
	}
	defer tx.Rollback(ctx)

	total := 0
	for _, it := range items {
		var qty int
		if err := tx.QueryRow(ctx, selectInventorySQL, it.SKU).Scan(new(string), &qty); err != nil {
			return Order{}, err
		}
		total += it.UnitCents * it.Qty
	}

	if _, err := tx.Exec(ctx, insertOrderSQL, id, userID, total); err != nil {
		return Order{}, err
	}
	for _, it := range items {
		if _, err := tx.Exec(ctx, insertItemSQL, it.ID, id, it.SKU, it.Qty, it.UnitCents); err != nil {
			return Order{}, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return Order{}, err
	}
	return Order{ID: id, UserID: userID, Status: "pending", TotalCents: total, Items: items}, nil
}

func (r *Repo) GetOrder(ctx context.Context, id string) (Order, error) {
	var o Order
	err := r.db.QueryRow(ctx, selectOrderSQL, id).Scan(&o.ID, &o.UserID, &o.Status, &o.TotalCents, &o.Notes)
	if err != nil {
		return Order{}, err
	}
	rows, err := r.db.Query(ctx, selectItemsSQL, id)
	if err != nil {
		return Order{}, err
	}
	defer rows.Close()
	for rows.Next() {
		var it OrderItem
		if err := rows.Scan(&it.ID, &it.OrderID, &it.SKU, &it.Qty, &it.UnitCents); err != nil {
			return Order{}, err
		}
		o.Items = append(o.Items, it)
	}
	return o, rows.Err()
}

func (r *Repo) ExpirePendingOrders(ctx context.Context) error {
	_, err := r.db.Exec(ctx, expirePendingSQL)
	return err
}

func (r *Repo) ListActiveUsers(ctx context.Context) (pgx.Rows, error) {
	q := selectUsersBase + selectUsersFilt
	return r.db.Query(ctx, q)
}

// UpdateUserField demonstrates dynamically built SQL (column name is not a literal).
func (r *Repo) UpdateUserField(ctx context.Context, col string, val any) error {
	q := "UPDATE users SET " + col + " = $1"
	_, err := r.db.Exec(ctx, q, val)
	return err
}
