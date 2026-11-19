package orders

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"

	"clinic/audit"
)

type Repo struct {
	db *pgxpool.Pool
}

func New(db *pgxpool.Pool) *Repo {
	return &Repo{db: db}
}

func (r *Repo) BindTenant(ctx context.Context, tenantID string) error {
	_, err := r.db.Exec(ctx, `SELECT set_config('app.tenant_id', $1, true)`, tenantID)
	if err != nil {
		return err
	}
	_, err = r.db.Query(ctx, `SELECT now()`)
	return err
}

func (r *Repo) Create(ctx context.Context, id, patientID string) error {
	_, err := r.db.Exec(ctx, `
INSERT INTO appointments (id, patient_id, status)
VALUES ($1, $2, 'pending')
`, id, patientID)
	return err
}

func (r *Repo) List(ctx context.Context) error {
	_, err := r.db.Query(ctx, `
SELECT d.status
FROM appointments a
JOIN LATERAL (SELECT status FROM appointments x WHERE x.id = a.id) d ON true
`)
	if err != nil {
		return err
	}
	_, err = r.db.Query(ctx, `
SELECT l.*
FROM appointments a
JOIN LATERAL (SELECT a.id AS aid, a.status) l ON true
`)
	if err != nil {
		return err
	}
	_, err = r.db.Query(ctx, `
SELECT r.notes, l.aid
FROM appointments a
LEFT JOIN LATERAL (SELECT a.id AS aid) l ON true
LEFT JOIN LATERAL (SELECT a.notes AS notes) r ON true
`)
	if err != nil {
		return err
	}
	_, err = r.db.Query(ctx, `
SELECT r.notes
FROM (SELECT notes FROM appointments WHERE id = $1) r
`, "0")
	return err
}

func (r *Repo) Files(ctx context.Context, id string) error {
	_, err := r.db.Query(ctx, `
SELECT files.file_id
FROM (
    SELECT file_id FROM attachments WHERE appointment_id = $1
) files
`, id)
	return err
}

func (r *Repo) Get(ctx context.Context, id string) error {
	_, err := r.db.Query(ctx, `
WITH last AS (
    SELECT id, status FROM appointments WHERE id = $1
)
SELECT last.id, last.status FROM last
`, id)
	return err
}

func (r *Repo) Update(ctx context.Context, id, status string) error {
	_, err := r.db.Exec(ctx, `
UPDATE appointments a
SET status = $2
FROM (
    SELECT status FROM appointments x WHERE x.id = $1
) l
WHERE a.id = $1
`, id, status)
	return err
}

func (r *Repo) Setup(ctx context.Context) error {
	_, err := r.db.Exec(ctx, `INSERT INTO appointments (id, patient_id, status) VALUES ($1, $2, 'setup')`, "0", "0")
	return err
}

func (r *Repo) Expire(ctx context.Context) error {
	_, err := r.db.Exec(ctx, `UPDATE appointments SET status = 'expired' WHERE status = 'pending'`)
	return err
}

type AuditRow struct {
	Action audit.Action
}

func (r *Repo) WriteAudit(ctx context.Context, row AuditRow) error {
	_, err := r.db.Exec(ctx, `INSERT INTO audit_log (action) VALUES ($1)`, row.Action)
	return err
}

func (r *Repo) PatchDynamic(ctx context.Context, table string) error {
	q := "UPDATE " + table + " SET status = $1"
	_, err := r.db.Exec(ctx, q, "x")
	return err
}

func (r *Repo) StaffCheck(ctx context.Context, role string) error {
	_, err := r.db.Query(ctx, `SELECT rolname FROM pg_roles WHERE rolname = $1`, role)
	return err
}
