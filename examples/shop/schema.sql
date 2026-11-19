-- Idempotent shop schema used by the BizMap example.
-- Applied as a single file (CREATE/ALTER IF NOT EXISTS, DO $$ guards).

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total_cents INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS notes TEXT;

CREATE TABLE IF NOT EXISTS order_items (
    id UUID PRIMARY KEY,
    order_id UUID NOT NULL,
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL,
    unit_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT PRIMARY KEY,
    qty INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    action TEXT NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_user_id_fkey;
ALTER TABLE orders ADD CONSTRAINT orders_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users (id);

ALTER TABLE order_items DROP CONSTRAINT IF EXISTS order_items_order_id_fkey;
ALTER TABLE order_items ADD CONSTRAINT order_items_order_id_fkey
    FOREIGN KEY (order_id) REFERENCES orders (id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'order_items_sku_fkey'
    ) THEN
        ALTER TABLE order_items
            ADD CONSTRAINT order_items_sku_fkey
            FOREIGN KEY (sku) REFERENCES inventory (sku);
    END IF;
END
$$;

-- Audit every order insert/update into audit_log.
CREATE OR REPLACE FUNCTION log_order_change() RETURNS trigger AS $$
BEGIN
    INSERT INTO audit_log (table_name, action, payload)
    VALUES (
        'orders',
        TG_OP,
        jsonb_build_object('id', NEW.id, 'status', NEW.status)
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_orders_audit ON orders;
CREATE TRIGGER trg_orders_audit
AFTER INSERT OR UPDATE ON orders
FOR EACH ROW EXECUTE FUNCTION log_order_change();

-- Decrement on-hand inventory when an order item is inserted.
CREATE OR REPLACE FUNCTION decrement_inventory() RETURNS trigger AS $$
BEGIN
    UPDATE inventory
    SET qty = qty - NEW.qty,
        updated_at = now()
    WHERE sku = NEW.sku;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_order_items_inventory ON order_items;
CREATE TRIGGER trg_order_items_inventory
AFTER INSERT ON order_items
FOR EACH ROW EXECUTE FUNCTION decrement_inventory();
