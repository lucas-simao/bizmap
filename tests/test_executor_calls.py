"""Thread-pool / asyncio executors are calls to the function they run."""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_path

SCHEMA = """
CREATE TABLE sales (day date, total integer);
CREATE TABLE stock (sku text, qty integer);
CREATE TABLE refunds (id text, amount integer);
CREATE TABLE audit (id text);
CREATE TABLE events (id text);
"""


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _reach(graph, frm: str) -> set[str]:
    return {
        e.to_id.split(".")[0].replace("column:", "table:")
        for e in graph.edges.values()
        if e.type in {"reads", "writes"} and e.from_id == frm
    }


def test_executor_wrappers_reach_the_sql_tables(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        "reports.py",
        """
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from sqlalchemy import create_engine, text

app = FastAPI()
engine = create_engine("postgresql://")
pool = ThreadPoolExecutor(4)

SALES_SQL = "SELECT day, total FROM sales"


def run_query(sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def direct():
    return run_query("SELECT id FROM audit")


async def sales_summary():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, run_query, SALES_SQL)


def stock_levels():
    return pool.submit(run_query, "SELECT sku, qty FROM stock").result()


async def refunds():
    return await asyncio.to_thread(run_query, "SELECT id, amount FROM refunds")


def background():
    threading.Thread(target=run_query, args=("SELECT id FROM events",)).start()


@app.get("/reports/sales")
async def sales_endpoint():
    return await sales_summary()
""",
    )
    graph = scan_path(tmp_path)
    got = {
        name: _reach(graph, function_id(f"reports.{name}"))
        for name in ("direct", "sales_summary", "stock_levels", "refunds", "background")
    }
    assert "table:audit" in got["direct"], got
    assert "table:sales" in got["sales_summary"], got
    assert "table:stock" in got["stock_levels"], got
    assert "table:refunds" in got["refunds"], got
    assert "table:events" in got["background"], got
    assert "table:sales" in _reach(graph, "entrypoint:http:GET:/reports/sales"), got
    calls = {e.to_id for e in graph.edges.values() if e.type == "calls" and e.from_id == function_id("reports.stock_levels")}
    assert function_id("reports.run_query") in calls, calls



def test_submitted_function_with_its_own_sql_reaches_endpoint(tmp_path: Path) -> None:
    """The handler only hands functions to a pool / background tasks; the SQL
    lives inside those functions. The endpoint must still reach the tables."""
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        "jobs.py",
        """
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from fastapi import BackgroundTasks, FastAPI
from sqlalchemy import text

app = FastAPI()
pool = ThreadPoolExecutor(2)


def _load_stock(conn):
    return conn.execute(text("SELECT sku, qty FROM stock")).fetchall()


def _load_sales(conn, day):
    return conn.execute(text("SELECT day, total FROM sales WHERE day = :d"), {"d": day})


def _audit(conn):
    conn.execute(text("INSERT INTO audit (id) VALUES ('x')"))


@app.get("/dash")
async def dash(background: BackgroundTasks, conn=None):
    loop = asyncio.get_running_loop()
    stock = pool.submit(_load_stock, conn).result()
    sales = await loop.run_in_executor(pool, partial(_load_sales, conn), "2026-01-01")
    background.add_task(_audit, conn)
    return {"stock": stock, "sales": sales}
""",
    )
    graph = scan_path(tmp_path)
    reach = _reach(graph, "entrypoint:http:GET:/dash")
    assert {"table:stock", "table:sales", "table:audit"} <= reach, reach
