package main

import (
	"context"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/labstack/echo/v4"

	orderctrl "clinic/controllers/orders"
	userctrl "clinic/controllers/users"
	"clinic/emailprefs"
	"clinic/jobs"
	"clinic/notify"
	"clinic/profile"
	orderrepo "clinic/repositories/orders"
	userrepo "clinic/repositories/users"
	"clinic/routes"
	ordersvc "clinic/services/orders"
	usersvc "clinic/services/users"
)

func main() {
	ctx := context.Background()
	pool, _ := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	orepo := orderrepo.New(pool)
	urepo := userrepo.New(pool)
	osvc := ordersvc.New(orepo, notify.Send)
	usvc := usersvc.New(urepo)
	oh := orderctrl.New(osvc)
	uh := userctrl.New(usvc)
	expire := jobs.NewExpire(orepo)

	e := echo.New()
	g := e.Group(routes.APIPrefix)
	g.POST("/appointments", oh.Create)
	g.GET("/appointments", oh.List)
	g.GET("/appointments/:id", oh.Get)
	g.PATCH("/appointments/:id", oh.Update)
	g.GET("/version", orderctrl.Wrap(oh.Version))
	g.POST("/patients", uh.Create)
	g.GET("/patients", uh.List)
	g.GET("/patients/:id", uh.Get)
	g.POST(routes.PendingPath+"/setup", oh.Setup)
	e.GET(emailprefs.Path, uh.Prefs)
	e.GET("/profile/photo", profile.StaffPhoto(profile.Config{DB: pool}))

	go expire.Run(ctx)
	go jobs.Start(ctx)
	go runLoop(ctx)
	go (&jobs.Poller{Repo: orepo}).Run(ctx)
	go func() {
		t := time.NewTicker(time.Minute)
		defer t.Stop()
		<-t.C
	}()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Second):
				_ = orepo.Expire(ctx)
				return
			}
		}
	}()

	_ = e.Start(":8080")
}

func runLoop(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			return
		}
	}
}
