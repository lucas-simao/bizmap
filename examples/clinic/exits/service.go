package exits

import "time"

type Service struct{}

// Now collides with Clock.Now on controllers; unique-name fallback must not apply.
func (s *Service) Now() time.Time {
	return time.Unix(0, 0)
}
