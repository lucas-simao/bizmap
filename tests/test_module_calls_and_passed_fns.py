"""`from pkg import module` + `module.fn()`, and functions passed as arguments."""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_path

SCHEMA = """
CREATE TABLE sales (store text, total integer);
CREATE TABLE items (id text, name text);
CREATE TABLE audit (id text);
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


def _edge(graph, caller: str, callee: str):
    return graph.edges.get(f"calls:{function_id(caller)}->{function_id(callee)}")


def _project(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "reports/__init__.py", "")
    _write(
        tmp_path,
        "reports/sales.py",
        """
from sqlalchemy import text


def bind_store(store):
    return CONN.execute(text("SELECT store, total FROM sales WHERE store = :s"), {"s": store})
""",
    )
    _write(
        tmp_path,
        "reports/exports.py",
        """
from sqlalchemy import text


def export_items(params):
    return CONN.execute(text("SELECT id, name FROM items"))


def write_audit(params):
    CONN.execute(text("INSERT INTO audit (id) VALUES ('x')"))
""",
    )


def test_module_attribute_call_and_function_passed_as_argument(tmp_path: Path) -> None:
    """The evaluation's example: both dashboard routes reach their tables."""
    _project(tmp_path)
    _write(
        tmp_path,
        "app.py",
        """
from fastapi import FastAPI

from reports import exports, sales

app = FastAPI()


@app.get("/sales/{store}")
def sales_route(store: str):
    return sales.bind_store(store)


@app.get("/exports/items")
def export_items(request):
    return _exports_response(request, exports.export_items)


def _exports_response(request, fn):
    return fn(request.query_params)
""",
    )
    graph = scan_path(tmp_path)
    assert "table:sales" in _reach(graph, "entrypoint:http:GET:/sales/{store}")
    assert "table:items" in _reach(graph, "entrypoint:http:GET:/exports/items")
    direct = _edge(graph, "app.sales_route", "reports.sales.bind_store")
    assert direct is not None and direct.confidence.value == "exact"
    passed = _edge(graph, "app.export_items", "reports.exports.export_items")
    assert passed is not None, [e.id for e in graph.edges.values() if e.type == "calls"]
    assert passed.confidence.value == "inferred"
    assert passed.attrs.get("via") == "passed_as_arg"


def test_import_as_alias_self_method_and_negatives(tmp_path: Path) -> None:
    _project(tmp_path)
    _write(
        tmp_path,
        "svc.py",
        """
import reports.exports as ex


def run_with(fn, params):
    return fn(params)


def ignores(fn, params):
    return params


class Panel:
    def build(self, params):
        return ex.export_items(params)

    def render(self, params):
        return run_with(self.build, params)


def aliased(params):
    return ex.export_items(params)


def unused_param(params):
    return ignores(ex.write_audit, params)
""",
    )
    graph = scan_path(tmp_path)
    assert _edge(graph, "svc.aliased", "reports.exports.export_items") is not None
    assert _edge(graph, "svc.Panel.render", "svc.Panel.build") is not None
    assert "table:items" in _reach(graph, function_id("svc.Panel.render"))
    # the receiver never calls / forwards the parameter → no edge
    assert _edge(graph, "svc.unused_param", "reports.exports.write_audit") is None
