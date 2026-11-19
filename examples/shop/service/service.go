package service

import (
	"context"

	"shop/repository"
)

type Service struct {
	repo *repository.Repo
}

func New(repo *repository.Repo) *Service {
	return &Service{repo: repo}
}

type NewUser struct {
	ID    string
	Email string
	Name  string
}

type NewOrder struct {
	ID     string
	UserID string
	Items  []repository.OrderItem
}

func (s *Service) CreateUser(ctx context.Context, in NewUser) (repository.User, error) {
	return s.repo.InsertUser(ctx, in.ID, in.Email, in.Name)
}

func (s *Service) GetUser(ctx context.Context, id string) (repository.User, error) {
	return s.repo.GetUser(ctx, id)
}

func (s *Service) CreateOrder(ctx context.Context, in NewOrder) (repository.Order, error) {
	if _, err := s.repo.GetUser(ctx, in.UserID); err != nil {
		return repository.Order{}, err
	}
	return s.repo.InsertOrder(ctx, in.ID, in.UserID, in.Items)
}

func (s *Service) GetOrder(ctx context.Context, id string) (repository.Order, error) {
	return s.repo.GetOrder(ctx, id)
}
