package jobs

import (
	"context"
	"time"

	"shop/repository"
)

type ExpireJob struct {
	repo *repository.Repo
}

func NewExpire(repo *repository.Repo) *ExpireJob {
	return &ExpireJob{repo: repo}
}

// Run expires unpaid checkout sessions so reserved inventory can be released.
func (j *ExpireJob) Run(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = j.expire(ctx)
		}
	}
}

func (j *ExpireJob) expire(ctx context.Context) error {
	return j.repo.ExpirePendingOrders(ctx)
}
