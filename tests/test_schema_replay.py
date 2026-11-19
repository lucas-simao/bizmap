from __future__ import annotations

from bizmap.schema_replay import replay_files, replay_sql, split_sql_statements


def test_split_skips_leading_comments(example_root) -> None:
    sql = (example_root / "schema.sql").read_text(encoding="utf-8")
    stmts = split_sql_statements(sql)
    assert stmts[0][1].upper().startswith("CREATE TABLE")
    assert "users" in stmts[0][1]
    assert all(not s[1].lstrip().startswith("--") for s in stmts)


def test_split_respects_dollar_quotes_with_semicolons() -> None:
    sql = """
    CREATE TABLE t (id int);
    DO $$
    BEGIN
      ALTER TABLE t ADD COLUMN x int;
    END
    $$;
    SELECT 1;
    """
    stmts = split_sql_statements(sql)
    assert len(stmts) == 3
    assert stmts[1][1].upper().startswith("DO")


def test_replay_create_alter_fk_and_triggers(example_root) -> None:
    schema = replay_files([example_root / "schema.sql"], example_root)
    assert "users" in schema.tables
    assert "email" in schema.tables["users"].columns
    assert schema.tables["orders"].columns["notes"].data_type.upper().startswith("TEXT")
    fks = {fk.name: fk for fk in schema.tables["orders"].foreign_keys if fk.name}
    assert "orders_user_id_fkey" in fks
    assert fks["orders_user_id_fkey"].ref_table == "users"
    # FK added inside DO $$
    item_fks = {fk.name: fk for fk in schema.tables["order_items"].foreign_keys if fk.name}
    assert "order_items_sku_fkey" in item_fks
    assert "trg_orders_audit" in schema.triggers
    assert schema.triggers["trg_orders_audit"].table == "orders"
    assert "INSERT" in schema.triggers["trg_orders_audit"].events
    assert schema.functions["log_order_change"].accesses
    assert any(a.table == "audit_log" and a.kind == "write" for a in schema.functions["log_order_change"].accesses)
    inv = [a for a in schema.functions["decrement_inventory"].accesses if a.kind == "write"]
    assert any(a.table == "inventory" and a.column == "qty" for a in inv)


def test_drop_then_add_constraint() -> None:
    sql = """
    CREATE TABLE a (id int PRIMARY KEY);
    CREATE TABLE b (a_id int);
    ALTER TABLE b ADD CONSTRAINT fk1 FOREIGN KEY (a_id) REFERENCES a (id);
    ALTER TABLE b DROP CONSTRAINT IF EXISTS fk1;
    ALTER TABLE b ADD CONSTRAINT fk2 FOREIGN KEY (a_id) REFERENCES a (id);
    """
    schema = replay_sql(sql)
    names = {fk.name for fk in schema.tables["b"].foreign_keys}
    assert "fk1" not in names
    assert "fk2" in names
