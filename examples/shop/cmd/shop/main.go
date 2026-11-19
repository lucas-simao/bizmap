package main

import (
	"context"
	"log"
	"os"
	"os/signal"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/labstack/echo/v4"

	"shop/handlers"
	"shop/jobs"
	"shop/repository"
	"shop/service"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	pool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()

	repo := repository.New(pool)
	svc := service.New(repo)
	h := handlers.New(svc)
	expire := jobs.NewExpire(repo)

	e := echo.New()
	api := e.Group("/api")
	api.POST("/users", h.CreateUser)
	api.GET("/users/:id", h.GetUser)
	api.POST("/orders", h.CreateOrder)
	api.GET("/orders/:id", h.GetOrder)

	go expire.Run(ctx)

	if err := e.Start(":8080"); err != nil {
		log.Fatal(err)
	}
}
