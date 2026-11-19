"""Calls into code outside the scan leave a trace; SQL handed to them is kept."""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_path

SCHEMA = "CREATE TABLE orders (id text, status text);\nCREATE TABLE items (id text);\n"


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _data_edges(graph, fn: str) -> list:
    return [
        e
        for e in graph.edges.values()
        if e.type in {"reads", "writes"} and e.from_id == function_id(fn)
    ]


def test_sql_to_unscanned_sink_through_executor_and_partial(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        "svc/orders.py",
        """
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from db.rpc import rpc_sql

pool = ThreadPoolExecutor(2)
ITEMS_SQL = "SELECT id FROM items"


async def load_orders(conn):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(pool, partial(rpc_sql, conn), "SELECT id FROM orders")


def load_items(conn):
    return pool.submit(rpc_sql, conn, ITEMS_SQL).result()


def direct(conn):
    return rpc_sql(conn, "UPDATE orders SET status = 'x' WHERE id = '1'")
""",
    )
    graph = scan_path(tmp_path)
    for fn, table in (
        ("svc.orders.load_orders", "orders"),
        ("svc.orders.load_items", "items"),
        ("svc.orders.direct", "orders"),
    ):
        edges = [e for e in _data_edges(graph, fn) if table in e.to_id]
        assert edges, (fn, [e.id for e in _data_edges(graph, fn)])
        assert all(e.attrs.get("sink_not_scanned") is True for e in edges), (fn, [(e.id, e.attrs) for e in edges])
        assert all(e.confidence.value == "inferred" for e in edges), (fn, [(e.id, e.confidence) for e in edges])
        node = graph.get(function_id(fn))
        assert "db.rpc.rpc_sql" in (node.attrs.get("unresolved_calls") or []), (fn, node.attrs)
        assert (node.attrs.get("unresolved_call_reasons") or {}).get("db.rpc.rpc_sql") == "target_not_scanned"


def test_local_sql_variable_to_dotted_unscanned_sink(tmp_path: Path) -> None:
    """The real case: `sql = "..."; await conn.fetch(sql)` / `db.conn.fetch(...)`
    with `db` not scanned → table resolved, `sink_not_scanned`, no table:unknown."""
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        "api/handlers.py",
        """
import db
from db import conn


async def list_orders():
    sql = "SELECT id, status FROM orders WHERE status = 'open'"
    return await conn.fetch(sql)


async def list_items():
    return await db.conn.fetch("SELECT id FROM items")
""",
    )
    graph = scan_path(tmp_path)
    for fn, table, sink in (
        ("api.handlers.list_orders", "orders", "db.conn.fetch"),
        ("api.handlers.list_items", "items", "db.conn.fetch"),
    ):
        edges = _data_edges(graph, fn)
        assert [e for e in edges if table in e.to_id], (fn, [e.id for e in edges])
        assert not [e for e in edges if e.to_id == "table:unknown"], (fn, [e.id for e in edges])
        assert all(e.attrs.get("sink_not_scanned") for e in edges if table in e.to_id)
        node = graph.get(function_id(fn))
        assert (node.attrs.get("unresolved_call_reasons") or {}).get(sink) == "target_not_scanned", node.attrs


def test_declared_third_party_and_resolved_targets_are_not_unresolved(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "requirements.txt", "stripe>=7\nPyYAML==6.0\n")
    _write(tmp_path, "helpers.py", "def run(x):\n    return x\n")
    _write(
        tmp_path,
        "svc.py",
        """
import stripe
import yaml

import helpers
from helpers import run


def charge(amount):
    stripe.Charge.create(amount=amount)
    yaml.safe_load("a: 1")
    helpers.run(1)
    return run(2)
""",
    )
    graph = scan_path(tmp_path)
    node = graph.get(function_id("svc.charge"))
    assert (node.attrs.get("unresolved_calls") or []) == [], node.attrs.get("unresolved_calls")
