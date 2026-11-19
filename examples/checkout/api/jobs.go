package main

import (
	"context"
	"time"
)

type ExpireJob struct {
	repo *Repo
}

func NewExpireJob(repo *Repo) *ExpireJob {
	return &ExpireJob{repo: repo}
}

// Run expires unpaid orders every minute so abandoned carts free their slot.
func (j *ExpireJob) Run(ctx context.Context) {
	t := time.NewTicker(time.Minute)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			_ = j.repo.ExpirePending(ctx)
		}
	}
}
