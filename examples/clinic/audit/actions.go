package audit

type Action int

const (
	Unknown Action = iota
	AppointmentCanceled
	AppointmentCreated
	PatientRegistered
)
