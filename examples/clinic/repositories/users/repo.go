package users

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
)

type Repo struct {
	db *pgxpool.Pool
}

func New(db *pgxpool.Pool) *Repo {
	return &Repo{db: db}
}

func (r *Repo) Create(ctx context.Context, id, email, name string) error {
	_, err := r.db.Exec(ctx, `
INSERT INTO patients (id, email, name, status)
VALUES ($1, $2, $3, 'active')
`, id, email, name)
	return err
}

func (r *Repo) List(ctx context.Context) error {
	_, err := r.db.Query(ctx, `SELECT * FROM patients`)
	return err
}

func (r *Repo) Get(ctx context.Context, id string) error {
	_, err := r.db.Query(ctx, `SELECT id, email, name, status FROM patients WHERE id = $1`, id)
	return err
}

func (r *Repo) Prefs(ctx context.Context, id string) error {
	_, err := r.db.Query(ctx, `SELECT id, path FROM email_prefs WHERE patient_id = $1`, id)
	return err
}
