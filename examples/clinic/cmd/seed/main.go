package main

import (
	"context"
	"os"

	"github.com/jackc/pgx/v5/pgxpool"

	userrepo "clinic/repositories/users"
)

// Seed is a one-shot command and must not appear as a runtime writer when excluded.
func main() {
	ctx := context.Background()
	pool, _ := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	repo := userrepo.New(pool)
	_ = repo.Create(ctx, "seed", "seed@clinic.example", "Seed")
}
