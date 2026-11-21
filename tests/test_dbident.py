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


@pytest.mark.parametrize(
    "line",
    [
        'db = psycopg2.connect(os.environ["DATABASE_URL"], options="-c search_path=" + os.environ["DB_SCHEMA"])',
        'db = psycopg2.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"])',
        "pool = new Pool({ host: process.env.DB_HOST, database: process.env.DB_NAME, user: process.env.DB_USER })",
        "pool, err := pgxpool.New(ctx, cfg.DBSchema)",
    ],
)
def test_env_that_is_not_a_dsn_names_no_database(line: str) -> None:
    idx = dbident.build_index({"x.py": line + "\n"}, {})
    assert not idx.named


def test_db_name_literal_default_next_to_a_connector() -> None:
    text = 'conn = psycopg2.connect(host=os.environ["DB_HOST"], dbname=os.getenv("DB_NAME", "orders"))\n'
    assert dbident.build_index({"x.py": text}, {}).resolve("x.py", "conn") == "orders"


def test_bizmap_yaml_can_name_any_env() -> None:
    text = 'db = psycopg2.connect(host=os.environ["DB_HOST"], dbname=os.environ["DB_SCHEMA"])\n'
    idx = dbident.build_index({"x.py": text}, {"databases": {"vendas": {"env": "DB_SCHEMA"}}})
    assert idx.resolve("x.py", "db") == "vendas"


@pytest.mark.parametrize(
    ("name", "dsn"),
    [("ORDERS_DATABASE_URL", True), ("OrdersDSN", True), ("PG_CONNECTION_STRING", True), ("DB_SCHEMA", False),
     ("DB_NAME", False), ("DB_HOST", False), ("DATABASE", False)],
)
def test_is_dsn_name(name: str, dsn: bool) -> None:
    assert dbident.is_dsn_name(name) is dsn
