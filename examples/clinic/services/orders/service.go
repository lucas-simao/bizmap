package orders

import (
	"context"

	"clinic/audit"
	orderrepo "clinic/repositories/orders"
)

type Notifier func(ctx context.Context, msg string) error

type Service struct {
	repo *orderrepo.Repo
	ring Notifier
}

func New(repo *orderrepo.Repo, ring Notifier) *Service {
	return &Service{repo: repo, ring: ring}
}

func (s *Service) Create(ctx context.Context, id, patientID string) error {
	if err := s.repo.BindTenant(ctx, patientID); err != nil {
		return err
	}
	if err := s.repo.Create(ctx, id, patientID); err != nil {
		return err
	}
	if s.ring != nil {
		_ = s.ring(ctx, id)
	}
	return s.repo.WriteAudit(ctx, orderrepo.AuditRow{Action: audit.AppointmentCreated})
}

func (s *Service) List(ctx context.Context) error {
	return s.repo.List(ctx)
}

func (s *Service) Get(ctx context.Context, id string) error {
	return s.repo.Get(ctx)
}

func (s *Service) Update(ctx context.Context, id, status string) error {
	if err := s.repo.Update(ctx, id, status); err != nil {
		return err
	}
	return s.repo.WriteAudit(ctx, orderrepo.AuditRow{Action: audit.AppointmentCanceled})
}

func (s *Service) Setup(ctx context.Context) error {
	return s.repo.Setup(ctx)
}
