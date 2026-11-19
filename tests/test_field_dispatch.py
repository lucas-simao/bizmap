"""Functions stored in a dataclass / NamedTuple / dict field and called through it."""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_path

SCHEMA = """
CREATE TABLE sales (day date, total integer);
CREATE TABLE stock (sku text, qty integer);
CREATE TABLE audit (id text);
"""


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _reach(graph, frm: str) -> set[str]:
    out = set()
    for e in graph.edges.values():
        if e.type in {"reads", "writes"} and e.from_id == frm:
            out.add(e.to_id.split(".")[0].replace("column:", "table:"))
    return out


def _callees(graph, fn: str) -> set[str]:
    return {e.to_id for e in graph.edges.values() if e.type == "calls" and e.from_id == function_id(fn)}


BUILDERS = """
from sqlalchemy import text


def _build_sales(conn):
    return conn.execute(text("SELECT day, total FROM sales"))


def _build_stock(conn):
    return conn.execute(text("SELECT sku, qty FROM stock"))


def _write_audit(conn):
    conn.execute(text("INSERT INTO audit (id) VALUES ('x')"))
"""


def test_dataclass_field_dispatch_reaches_both_tables(tmp_path: Path) -> None:
    """The exact pattern from the evaluation."""
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "builders.py", BUILDERS)
    _write(
        tmp_path,
        "panels.py",
        """
from dataclasses import dataclass
from typing import Callable

from fastapi import FastAPI

from builders import _build_sales, _build_stock

app = FastAPI()


@dataclass
class PanelDef:
    name: str
    build: Callable[..., dict]


PANELS = [PanelDef("sales", build=_build_sales), PanelDef("stock", build=_build_stock)]


def assemble(kind, conn):
    for p in PANELS:
        if p.name == kind:
            return p.build(conn)


@app.get("/panels/{kind}")
def panel(kind: str, conn=None):
    return assemble(kind, conn)
""",
    )
    graph = scan_path(tmp_path)
    assert {function_id("builders._build_sales"), function_id("builders._build_stock")} <= _callees(
        graph, "panels.assemble"
    )
    reach = _reach(graph, "entrypoint:http:GET:/panels/{kind}")
    assert {"table:sales", "table:stock"} <= reach, reach
    assert "table:audit" not in reach, reach


def test_namedtuple_positional_getattr_dict_steps_and_partial(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "builders.py", BUILDERS)
    _write(
        tmp_path,
        "flows.py",
        """
import functools
from typing import Callable, NamedTuple

import builders


class Step(NamedTuple):
    label: str
    run: Callable


STEP_DEFS = (Step("sales", builders._build_sales),)
ROWS = [{"name": "stock", "fetch": builders._build_stock}]
STEPS = [functools.partial(builders._write_audit)]


def via_namedtuple(conn):
    return [s.run(conn) for s in STEP_DEFS]


def via_getattr(conn):
    return [getattr(s, "run")(conn) for s in STEP_DEFS]


def via_dict_rows(conn):
    return [row["fetch"](conn) for row in ROWS]


def via_steps(conn):
    for fn in STEPS:
        fn(conn)
""",
    )
    graph = scan_path(tmp_path)
    sales, stock, audit = (
        function_id("builders._build_sales"),
        function_id("builders._build_stock"),
        function_id("builders._write_audit"),
    )
    assert sales in _callees(graph, "flows.via_namedtuple")
    assert sales in _callees(graph, "flows.via_getattr")
    assert stock in _callees(graph, "flows.via_dict_rows")
    assert audit in _callees(graph, "flows.via_steps")


def test_methods_and_self_are_not_field_dispatch(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "builders.py", BUILDERS)
    _write(
        tmp_path,
        "svc.py",
        """
from dataclasses import dataclass
from typing import Callable

from builders import _build_sales


@dataclass
class Job:
    run: Callable


JOBS = [Job(run=_build_sales)]


class Worker:
    def run(self, conn):
        return None

    def go(self, conn):
        return self.run(conn)


def runner(worker, conn):
    return worker.run(conn)
""",
    )
    graph = scan_path(tmp_path)
    # `run` is also a method name → ambiguous, never linked by field dispatch
    assert function_id("builders._build_sales") not in _callees(graph, "svc.runner")
    assert function_id("builders._build_sales") not in _callees(graph, "svc.Worker.go")
