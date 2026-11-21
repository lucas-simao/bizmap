"""Database identity: the `db` a table lives in, from the connection that reaches it."""

from __future__ import annotations

import pytest

from bizmap import dbident


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ("ORDERS_DATABASE_URL", "orders_db"),
        ("SUMMARY_DATABASE_URL", "summary_db"),
        ("PG_BILLING_DSN", "billing_db"),
        ("REPORTS_DB", "reports_db"),
        ("DATABASE_URL", None),
        ("DBURL", None),
    ],
)
def test_db_name_from_env(env: str, name: str | None) -> None:
    assert dbident.db_name_from_env(env) == name


def test_connection_from_config_field_and_pgxpool() -> None:
    texts = {
        "db.go": 'pool, err := pgxpool.New(ctx, cfg.OrdersDatabaseURL)\nrows, _ := pool.Query(ctx, "SELECT 1")\n',
        "eng.py": "engine = create_engine(settings.SUMMARY_DATABASE_URL)\nwith engine.connect() as conn:\n    conn.execute(q)\n",
    }
    idx = dbident.build_index(texts, {})
    assert idx.resolve("db.go", "pool") == "orders_db"
    assert idx.resolve("eng.py", "conn") == "summary_db"
