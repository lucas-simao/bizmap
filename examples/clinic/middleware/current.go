package middleware

// Current is a package function. Callers of an *unscanned* middleware
// package must not be linked to consent.Item.Current.
func Current(id string) string {
	return id
}
