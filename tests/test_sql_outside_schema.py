"""SQL that touches tables outside the scanned schema is never silently dropped.

A table missing from the replayed DDL (it lives in another repo or in an
unscanned migration) still gets an edge; a SQLAlchemy builder on a model
that is not scanned still leaves a `table:unknown` trace.
"""

from __future__ import annotations

from pathlib import Path

from bizmap.scan import scan_path


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _targets(graph, fn: str, etype: str) -> dict[str, str]:
    return {
        e.to_id: e.confidence.value
        for e in graph.edges.values()
        if e.type == etype and e.from_id == f"function:{fn}"
    }


def test_raw_sql_on_table_without_ddl_keeps_an_edge(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _write(
        tmp_path,
        "repo.py",
        """
from sqlalchemy import text


def external_ids(conn):
    return conn.execute(text("SELECT id FROM external_table WHERE active"))


def bump_external(conn):
    conn.execute(text("UPDATE external_counts SET n = n + 1 WHERE id = :id"), {"id": 1})
""",
    )
    graph = scan_path(tmp_path)
    reads = _targets(graph, "repo.external_ids", "reads")
    assert any("external_table" in t for t in reads), reads
    writes = _targets(graph, "repo.bump_external", "writes")
    assert any("external_counts" in t for t in writes), writes
    node = next(n for n in graph.nodes.values() if "external_table" in n.id)
    assert node.attrs.get("kind") == "unresolved_relation" or node.attrs.get("schema_missing"), node.attrs


def test_builder_on_unscanned_model_leaves_unknown_trace(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE orders (id text, status text);\n")
    _write(
        tmp_path,
        "svc.py",
        """
from sqlalchemy import select, update

from shared_models.billing import Invoice


def void_invoice(session, invoice_id):
    session.execute(update(Invoice).where(Invoice.id == invoice_id).values(status="void"))


def list_invoices(session):
    return session.execute(select(Invoice)).scalars().all()
""",
    )
    graph = scan_path(tmp_path)
    assert _targets(graph, "svc.void_invoice", "writes"), [
        e.id for e in graph.edges.values() if "void_invoice" in e.id
    ]
    assert _targets(graph, "svc.list_invoices", "reads"), [
        e.id for e in graph.edges.values() if "list_invoices" in e.id
    ]
