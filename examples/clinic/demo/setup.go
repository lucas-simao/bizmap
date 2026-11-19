package demo

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"

	orderrepo "clinic/repositories/orders"
)

func Setup(ctx context.Context, pool *pgxpool.Pool) error {
	repo := orderrepo.New(pool)
	return repo.Create(ctx, "demo", "demo-patient")
}
