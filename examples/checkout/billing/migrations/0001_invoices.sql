CREATE TABLE invoices (
    id           text PRIMARY KEY,
    order_id     text NOT NULL,
    amount_cents integer NOT NULL,
    status       text NOT NULL DEFAULT 'open',
    provider_ref text
);
