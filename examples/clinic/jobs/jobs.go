package jobs

import (
	"context"
	"time"

	orderrepo "clinic/repositories/orders"
)

type ExpireJob struct {
	repo *orderrepo.Repo
}

func NewExpire(repo *orderrepo.Repo) *ExpireJob {
	return &ExpireJob{repo: repo}
}

func (j *ExpireJob) Run(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = j.repo.Expire(ctx)
		}
	}
}

type Poller struct {
	Repo *orderrepo.Repo
}

func (p *Poller) Run(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for range ticker.C {
		_ = p.Repo.Expire(ctx)
		return
	}
}

func Start(ctx context.Context) {
	ch := time.Tick(time.Minute)
	for range ch {
		_ = ctx
		return
	}
}
