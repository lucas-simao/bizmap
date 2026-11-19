package consent

type Item struct {
	ID string
}

// Current is a method whose name collides with middleware.Current.
func (i *Item) Current() string {
	return i.ID
}
