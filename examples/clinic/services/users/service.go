package users

import (
	"context"

	userrepo "clinic/repositories/users"
)

type Service struct {
	repo *userrepo.Repo
}

func New(repo *userrepo.Repo) *Service {
	return &Service{repo: repo}
}

func (s *Service) Create(ctx context.Context, id, email, name string) error {
	return s.repo.Create(ctx, id, email, name)
}

func (s *Service) List(ctx context.Context) error {
	return s.repo.List(ctx)
}

func (s *Service) Get(ctx context.Context, id string) error {
	return s.repo.Get(ctx, id)
}

func (s *Service) Prefs(ctx context.Context, id string) error {
	return s.repo.Prefs(ctx, id)
}
