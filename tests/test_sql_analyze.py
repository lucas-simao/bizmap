from __future__ import annotations

from bizmap.graph import Confidence
from bizmap.sql_analyze import analyze_sql, looks_like_sql


def test_insert_columns_exact() -> None:
    acc = analyze_sql("INSERT INTO orders (id, user_id, status, total_cents) VALUES ($1,$2,$3,$4)")
    writes = {(a.table, a.column) for a in acc if a.kind == "write"}
    assert writes == {("orders", "id"), ("orders", "user_id"), ("orders", "status"), ("orders", "total_cents")}
    assert all(a.confidence == Confidence.EXACT for a in acc if a.kind == "write")


def test_update_set_and_where() -> None:
    acc = analyze_sql("UPDATE orders SET status = $1 WHERE id = $2")
    assert any(a.kind == "write" and a.column == "status" for a in acc)
    assert any(a.kind == "read" and a.column == "id" for a in acc)


def test_select_star_is_table_level_by_default() -> None:
    acc = analyze_sql("SELECT * FROM users", schema_columns={"users": ["id", "email"]})
    reads = [a for a in acc if a.kind == "read"]
    assert reads
    assert all(a.column is None for a in reads)
    assert {a.table for a in reads} == {"users"}
    assert all(a.confidence == Confidence.INFERRED for a in acc)


def test_select_star_expand_flag() -> None:
    acc = analyze_sql(
        "SELECT * FROM users", schema_columns={"users": ["id", "email"]}, expand_star=True
    )
    cols = {a.column for a in acc if a.kind == "read"}
    assert cols == {"id", "email"}
    assert all(a.confidence == Confidence.INFERRED for a in acc)


def test_insert_without_columns_is_table_level() -> None:
    acc = analyze_sql(
        "INSERT INTO orders VALUES ($1,$2,$3)",
        schema_columns={"orders": ["id", "user_id", "status"]},
    )
    writes = [a for a in acc if a.kind == "write"]
    assert writes
    assert all(a.column is None for a in writes)
    assert {a.table for a in writes} == {"orders"}


def test_cte_alias_is_not_a_table() -> None:
    acc = analyze_sql(
        "WITH last AS (SELECT id, status FROM orders) SELECT last.status FROM last"
    )
    tables = {a.table.lower() for a in acc}
    assert "last" not in tables
    assert "orders" in tables


def test_dynamic_table_name_is_unknown_not_set() -> None:
    acc = analyze_sql("UPDATE {table} SET status = $1", dynamic=True)
    assert acc
    assert all(a.unknown_table or a.table == "unknown" for a in acc)
    assert not any(a.table and a.table.lower() == "set" for a in acc)


def test_pg_roles_is_external() -> None:
    acc = analyze_sql("SELECT rolname FROM pg_roles")
    assert acc
    assert all(a.external and a.table.lower() == "pg_roles" for a in acc)


def test_join_reads_aliased_columns() -> None:
    acc = analyze_sql(
        "SELECT u.email, o.status FROM users u JOIN orders o ON o.user_id = u.id WHERE o.id = $1"
    )
    reads = {(a.table, a.column) for a in acc if a.kind == "read"}
    assert ("users", "email") in reads
    assert ("orders", "status") in reads
    assert ("orders", "user_id") in reads


def test_dynamic_update_unknown() -> None:
    acc = analyze_sql("UPDATE users SET  = $1", dynamic=True)
    assert acc
    assert all(a.table == "users" for a in acc)
    assert all(a.confidence == Confidence.UNKNOWN for a in acc)


def test_looks_like_sql() -> None:
    assert looks_like_sql("SELECT 1")
    assert looks_like_sql("\nINSERT INTO t VALUES (1)")
    assert not looks_like_sql("/api/orders")


def test_fromless_select_is_not_a_table() -> None:
    for sql in (
        "SELECT now()",
        "SELECT set_config('app.tenant_id', $1, true)",
        "SELECT 1",
    ):
        acc = analyze_sql(sql, known_tables={"appointments", "patients"})
        assert acc == [], sql


def test_subquery_alias_output_is_not_a_table() -> None:
    acc = analyze_sql(
        "SELECT files.file_id FROM (SELECT file_id FROM attachments WHERE appointment_id = $1) files",
        schema_columns={"attachments": ["id", "appointment_id", "file_id"]},
    )
    tables = {a.table.lower() for a in acc}
    assert "files" not in tables
    assert "attachments" in tables
    assert not any(a.table.lower() == "files" for a in acc)


def test_lateral_star_alias_is_not_a_table() -> None:
    acc = analyze_sql(
        "SELECT l.* FROM appointments a JOIN LATERAL (SELECT a.id AS aid, a.status) l ON true"
    )
    tables = {a.table.lower() for a in acc}
    assert "l" not in tables
    assert "appointments" in tables


# sqlglot 30.34+ reads `@` as ABS() in the postgres dialect: placeholders must never
# reach the parser as `@name`, or `WHERE id = $1` reports a phantom column `p1`.


def _cols(sql: str) -> set[tuple[str, str | None]]:
    return {(a.table, a.column) for a in analyze_sql(sql)}


def test_dollar_placeholder_is_not_a_column() -> None:
    got = _cols("SELECT id FROM orders WHERE id = $1")
    assert ("orders", "id") in got
    assert not any(c in ("p1", "1", "$1") for _t, c in got), got


def test_named_placeholder_is_not_a_column() -> None:
    got = _cols("SELECT id, total FROM orders WHERE customer_id = :cid AND status = :status")
    assert {("orders", "id"), ("orders", "total"), ("orders", "customer_id"), ("orders", "status")} <= got
    assert not any(c in ("cid", "status_param") for _t, c in got) and ("orders", "cid") not in got, got


def test_placeholders_in_writes_are_not_columns() -> None:
    got = _cols("UPDATE orders SET status = $2, paid_at = now() WHERE id = $1")
    assert ("orders", "status") in got and ("orders", "paid_at") in got
    assert not any(c in ("p1", "p2") for _t, c in got), got
    got = _cols("INSERT INTO orders (id, customer_id) VALUES (:id, :cid)")
    assert {("orders", "id"), ("orders", "customer_id")} <= got
    assert ("orders", "cid") not in got, got


def test_casts_and_percent_placeholders_still_parse() -> None:
    got = _cols("SELECT id FROM orders WHERE created_at > $1::timestamptz AND tier = %s")
    assert ("orders", "id") in got and ("orders", "created_at") in got and ("orders", "tier") in got
    assert not any(c in ("p1", "timestamptz") for _t, c in got), got
