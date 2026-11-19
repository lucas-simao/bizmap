-- Catalog schema extras: trigger function, RLS, and an RPC target.

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    email TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    user_id TEXT NOT NULL REFERENCES users (id),
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES orders (id),
    body TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT,
    action TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION log_order_write() RETURNS trigger AS $$
BEGIN
    INSERT INTO audit_log (order_id, action) VALUES (NEW.id, TG_OP);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_orders_audit
AFTER INSERT OR UPDATE ON orders
FOR EACH ROW EXECUTE FUNCTION log_order_write();

CREATE OR REPLACE FUNCTION apply_order_tax(order_id TEXT) RETURNS void AS $$
BEGIN
    UPDATE orders SET notes = coalesce(notes, '') || ' tax' WHERE id = order_id;
END;
$$ LANGUAGE plpgsql;

ALTER TABLE orders ENABLE ROW LEVEL SECURITY;

CREATE POLICY orders_tenant_select ON orders
    FOR SELECT
    TO app_user
    USING (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY orders_tenant_write ON orders
    FOR INSERT
    TO app_user
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
