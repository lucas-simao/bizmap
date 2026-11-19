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


def test_select_star_inferred_from_schema() -> None:
    acc = analyze_sql("SELECT * FROM users", schema_columns={"users": ["id", "email"]})
    cols = {a.column for a in acc if a.kind == "read"}
    assert cols == {"id", "email"}
    assert all(a.confidence == Confidence.INFERRED for a in acc)


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
