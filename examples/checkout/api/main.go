package main

import (
	"context"
	"log"
	"os"
	"os/signal"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/labstack/echo/v4"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()

	repo := NewRepo(pool)
	h := NewHandler(repo, NewBilling(os.Getenv("BILLING_URL")))
	expire := NewExpireJob(repo)

	e := echo.New()
	api := e.Group("/api")
	registerOrderRoutes(api, h)

	go expire.Run(ctx)

	if err := e.Start(":8080"); err != nil {
		log.Fatal(err)
	}
}
