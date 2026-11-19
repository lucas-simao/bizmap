CREATE TABLE orders (
    id          text PRIMARY KEY,
    user_id     text NOT NULL,
    status      text NOT NULL DEFAULT 'pending',
    total_cents integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    paid_at     timestamptz
);
