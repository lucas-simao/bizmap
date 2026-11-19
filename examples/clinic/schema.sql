-- Synthetic clinic schema for Go resolution accuracy tests.
CREATE TABLE IF NOT EXISTS patients (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS appointments (
    id UUID PRIMARY KEY,
    patient_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS photos (
    id UUID PRIMARY KEY,
    patient_id UUID NOT NULL,
    url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_prefs (
    id UUID PRIMARY KEY,
    patient_id UUID NOT NULL,
    path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    payload JSONB
);

CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id UUID PRIMARY KEY,
    appointment_id UUID NOT NULL,
    file_id UUID NOT NULL
);
