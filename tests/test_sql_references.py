"""SQL by reference (``via: sql_reference`` / ``sql_key``).

Dashboards often keep SQL in a module dict or constant and hand it to an
executor the detectors cannot resolve (dependency injection, a service
object, a generic runner). The function that *names* the SQL still reads or
writes its tables, and so does its HTTP entrypoint.

Synthetic fixtures only (sales_daily, order_items, orders).
"""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_path

SCHEMA = """
CREATE TABLE sales_daily (day date, total integer);
CREATE TABLE order_items (item_id text, qty integer);
CREATE TABLE orders (id text, status text);
"""

QUERIES = '''
QUERIES = {
    "sales_by_day": "SELECT day, total FROM sales_daily",
    "top_items": "SELECT item_id, qty FROM order_items ORDER BY qty DESC",
    "close_orders": "UPDATE orders SET status = 'closed' WHERE status = 'paid'",
}
'''


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _edges(graph, etype: str, frm: str, table: str):
    return [
        e
        for e in graph.edges.values()
        if e.type == etype
        and e.from_id == frm
        and (e.to_id == f"table:{table}" or e.to_id.startswith(f"column:{table}."))
    ]


def test_dict_entry_passed_to_injected_runner(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "queries.py", QUERIES)
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import Depends, FastAPI

from queries import QUERIES

app = FastAPI()


def get_service():
    raise NotImplementedError


@app.get("/dash/top-items")
def top_items(svc=Depends(get_service)):
    \"\"\"Top sold items for the dashboard.\"\"\"
    return svc.fetch_rows(QUERIES["top_items"])


@app.post("/jobs/close")
def close(svc=Depends(get_service)):
    return svc.apply(QUERIES.get("close_orders"))
""",
    )
    graph = scan_path(tmp_path)
    fid = function_id("app.top_items")
    reads = _edges(graph, "reads", fid, "order_items")
    assert reads, [e.id for e in graph.edges.values() if e.from_id == fid]
    assert all(e.confidence.value == "inferred" for e in reads)
    assert all(e.attrs.get("via") == "sql_reference" for e in reads)
    assert reads[0].evidence is not None and reads[0].evidence.file == "app.py"
    ep = "entrypoint:http:GET:/dash/top-items"
    assert _edges(graph, "reads", ep, "order_items"), [
        e.id for e in graph.edges.values() if e.from_id == ep
    ]
    writes = _edges(graph, "writes", function_id("app.close"), "orders")
    assert writes, [e.id for e in graph.edges.values() if e.from_id == function_id("app.close")]


def test_module_constant_via_import_alias(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        "sql.py",
        'SALES_SQL = "SELECT day, total FROM sales_daily"\n',
    )
    _write(
        tmp_path,
        "report.py",
        """
import sql as q
from sql import SALES_SQL


def daily(runner):
    return runner.go(q.SALES_SQL)


def daily_direct(runner):
    return runner.go(SALES_SQL)
""",
    )
    graph = scan_path(tmp_path)
    for name in ("report.daily", "report.daily_direct"):
        assert _edges(graph, "reads", function_id(name), "sales_daily"), name


def test_unique_dict_key_string_argument(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "queries.py", QUERIES)
    _write(
        tmp_path,
        "panel.py",
        """
import logging

log = logging.getLogger(__name__)


def sales_panel(store):
    log.info("sales_by_day")
    return store.named("sales_by_day")


def only_logs():
    log.info("sales_by_day")
""",
    )
    graph = scan_path(tmp_path)
    reads = _edges(graph, "reads", function_id("panel.sales_panel"), "sales_daily")
    assert reads and all(e.attrs.get("via") == "sql_key" for e in reads), reads
    assert not _edges(graph, "reads", function_id("panel.only_logs"), "sales_daily")


def test_direct_execution_keeps_exact_without_via(tmp_path: Path) -> None:
    """A function that executes the SQL itself keeps its exact edge, no `via`."""
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "queries.py", QUERIES)
    _write(
        tmp_path,
        "direct.py",
        """
from queries import QUERIES


def sales(conn):
    return conn.execute(QUERIES["sales_by_day"])
""",
    )
    graph = scan_path(tmp_path)
    reads = _edges(graph, "reads", function_id("direct.sales"), "sales_daily")
    assert reads
    assert all("via" not in e.attrs for e in reads), [(e.id, e.attrs) for e in reads]


def test_module_level_definitions_are_not_readers(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "queries.py", QUERIES)
    graph = scan_path(tmp_path)
    readers = [
        e.from_id
        for e in graph.edges.values()
        if e.type in {"reads", "writes"} and e.from_id.startswith("function:queries")
    ]
    assert readers == [], readers
