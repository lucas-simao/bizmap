"""`execute(text("..."))` and SQLAlchemy builders never add phantom unknown edges."""

from __future__ import annotations

from pathlib import Path

from bizmap.scan import scan_path


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_text_and_builders_have_no_unknown_edges(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE invoices (id text, status text, total integer);\n")
    _write(
        tmp_path,
        "billing.py",
        '''
from sqlalchemy import Column, Integer, MetaData, String, Table, delete, insert, text, update

meta = MetaData()
invoices = Table("invoices", meta, Column("id", String), Column("status", String), Column("total", Integer))


def open_invoice(conn, invoice_id):
    conn.execute(
        text(
            "INSERT INTO invoices (id, status, total) "
            "VALUES (:id, 'open', 0)"
        ),
        {"id": invoice_id},
    )


def settle(conn, invoice_id):
    conn.execute(text("UPDATE invoices SET status = 'paid' WHERE id = :id"), {"id": invoice_id})


def builders(conn, invoice_id):
    conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(status="void"))
    conn.execute(insert(invoices).values(id=invoice_id, status="open", total=0))
    conn.execute(delete(invoices).where(invoices.c.id == invoice_id))
''',
    )
    graph = scan_path(tmp_path)
    bad = [
        e.id
        for e in graph.edges.values()
        if e.from_id.startswith("function:billing.")
        and (e.confidence.value == "unknown" or e.to_id == "table:unknown")
    ]
    assert bad == [], bad
    writes = {
        e.to_id
        for e in graph.edges.values()
        if e.type == "writes" and e.from_id == "function:billing.open_invoice"
    }
    assert "column:invoices.status" in writes, writes
