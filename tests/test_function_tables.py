"""Dispatch through a dict/list of functions reaches every member's tables.

`BUILDERS = {"sales": _build_sales, ...}; BUILDERS[kind](conn)` — the
assembler calls every builder (`inferred`), so dashboard endpoints reach the
tables their builders query.
"""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_path

SCHEMA = """
CREATE TABLE sales_daily (day date, total integer);
CREATE TABLE order_items (item_id text, qty integer);
CREATE TABLE refunds (id text, amount integer);
"""


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _reads(graph, frm: str) -> set[str]:
    return {
        e.to_id.split(".")[0].replace("column:", "table:")
        for e in graph.edges.values()
        if e.type == "reads" and e.from_id == frm
    }


def test_dict_of_builders_reaches_every_table(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        "builders.py",
        """
from sqlalchemy import text


def _build_sales(conn):
    return conn.execute(text("SELECT day, total FROM sales_daily"))


def _build_items(conn):
    return conn.execute(text("SELECT item_id, qty FROM order_items"))


def _build_refunds(conn):
    return conn.execute(text("SELECT id, amount FROM refunds"))


BUILDERS = {"sales": _build_sales, "items": _build_items}
EXTRA = [_build_refunds]
""",
    )
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import FastAPI

import builders
from builders import BUILDERS, EXTRA

app = FastAPI()


def assemble(kind, conn):
    return BUILDERS[kind](conn)


def assemble_get(kind, conn):
    builder = BUILDERS.get(kind)
    return builder(conn)


def assemble_all(conn):
    out = [b(conn) for b in EXTRA]
    for build in builders.BUILDERS.values():
        out.append(build(conn))
    return out


@app.get("/dash/{kind}")
def dashboard(kind: str, conn=None):
    return assemble(kind, conn)


@app.get("/dash-all")
def dashboard_all(conn=None):
    return assemble_all(conn)
""",
    )
    graph = scan_path(tmp_path)
    for fn in ("app.assemble", "app.assemble_get"):
        calls = {e.to_id for e in graph.edges.values() if e.type == "calls" and e.from_id == function_id(fn)}
        assert calls >= {function_id("builders._build_sales"), function_id("builders._build_items")}, (fn, calls)
    ep = _reads(graph, "entrypoint:http:GET:/dash/{kind}")
    assert {"table:sales_daily", "table:order_items"} <= ep, ep
    assert "table:refunds" not in ep, ep
    ep_all = _reads(graph, "entrypoint:http:GET:/dash-all")
    assert {"table:sales_daily", "table:order_items"} <= ep_all, ep_all


def test_non_function_dicts_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        "cfg.py",
        """
LABELS = {"sales": "Sales", "items": "Items"}


def label(kind):
    return LABELS[kind].upper()
""",
    )
    graph = scan_path(tmp_path)
    calls = [e.id for e in graph.edges.values() if e.type == "calls" and e.from_id == function_id("cfg.label")]
    assert calls == [], calls
