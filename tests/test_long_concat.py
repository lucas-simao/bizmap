"""Generated `+` chains with thousands of terms resolve without recursion,
and a detector crash on one file never aborts the scan."""

from __future__ import annotations

from pathlib import Path

import pytest

from bizmap.scan import _analyze_safely, scan_path

N = 5000


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _chain(n: int, lit: str) -> str:
    return " + ".join([lit] * n)


SCHEMA = "CREATE TABLE orders (id int primary key, status text);\n"


def test_go_const_var_paren_and_function_body(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(tmp_path, "go.mod", "module example.com/shop\n\ngo 1.22\n")
    _write(
        tmp_path,
        "orders.go",
        "package shop\n\n"
        'import "database/sql"\n\n'
        f'const orderSelect = "SELECT status FROM orders WHERE id = $1" + {_chain(N, chr(34) + " " + chr(34))}\n'
        f'var orderVar = ({_chain(N, chr(34) + "x" + chr(34))})\n\n'
        "func Load(db *sql.DB) {\n"
        "\tdb.Query(orderSelect, 1)\n"
        "}\n\n"
        "func Build(db *sql.DB) {\n"
        f'\tq := "SELECT id FROM orders" + {_chain(N, chr(34) + " " + chr(34))}\n'
        "\tdb.Query(q)\n"
        "}\n",
    )
    graph = scan_path(tmp_path)
    reads = {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "reads"}
    assert any(f.endswith("Load") and t == "column:orders.status" for f, t in reads), sorted(reads)
    assert any(f.endswith("Build") and t == "column:orders.id" for f, t in reads), sorted(reads)
    assert not (graph.attrs.get("warnings") or [])


@pytest.mark.parametrize("ext", ["js", "ts"])
def test_js_ts_module_const_and_function_body(tmp_path: Path, ext: str) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        f"orders.{ext}",
        "import { Pool } from 'pg';\nconst pool = new Pool();\n"
        f"const ORDER_SELECT = 'SELECT status FROM orders WHERE id = $1' + {_chain(N, chr(39) + ' ' + chr(39))};\n"
        "export async function load() { return pool.query(ORDER_SELECT, [1]); }\n"
        "export async function build() {\n"
        f"  const q = 'SELECT id FROM orders' + {_chain(N, chr(39) + ' ' + chr(39))};\n"
        "  return pool.query(q);\n"
        "}\n",
    )
    graph = scan_path(tmp_path)
    reads = {(e.from_id.rsplit(".", 1)[-1], e.to_id) for e in graph.edges.values() if e.type == "reads"}
    assert ("load", "column:orders.status") in reads, sorted(reads)
    assert ("build", "column:orders.id") in reads, sorted(reads)


def test_python_module_const_and_function_body(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA)
    _write(
        tmp_path,
        "orders.py",
        f'ORDER_SELECT = "SELECT status FROM orders WHERE id = %s" + {_chain(N, chr(34) + " " + chr(34))}\n\n'
        "def load(cur):\n"
        "    cur.execute(ORDER_SELECT, (1,))\n\n"
        "def build(cur):\n"
        f'    q = "SELECT id FROM orders" + {_chain(N, chr(34) + " " + chr(34))}\n'
        "    cur.execute(q)\n",
    )
    graph = scan_path(tmp_path)
    reads = {(e.from_id.rsplit(".", 1)[-1], e.to_id) for e in graph.edges.values() if e.type == "reads"}
    assert ("load", "column:orders.status") in reads, sorted(reads)
    assert ("build", "column:orders.id") in reads, sorted(reads)


def test_detector_crash_skips_only_the_bad_file(tmp_path: Path) -> None:
    files = [tmp_path / "ok1.go", tmp_path / "bad.go", tmp_path / "ok2.go"]
    seen: list[list[str]] = []

    def analyze(batch):  # noqa: ANN001, ANN202
        names = [p.name for p in batch]
        seen.append(names)
        if "bad.go" in names:
            raise RecursionError("maximum recursion depth exceeded")
        return names

    failures: list[str] = []
    result = _analyze_safely("Go", analyze, files, tmp_path, failures)
    assert result == ["ok1.go", "ok2.go"]
    assert failures == ["Go detector failed on bad.go (RecursionError); file skipped"]


def test_two_packages_one_pathological(tmp_path: Path) -> None:
    """The scan finishes and keeps the healthy package's lineage."""
    _write(tmp_path, "schema.sql", SCHEMA + "CREATE TABLE inventory (sku text primary key, stock int);\n")
    _write(tmp_path, "go.mod", "module example.com/app\n\ngo 1.22\n")
    _write(
        tmp_path,
        "inventory/inventory.go",
        'package inventory\n\nimport "database/sql"\n\nfunc Stock(db *sql.DB) {\n\tdb.Query("SELECT stock FROM inventory")\n}\n',
    )
    _write(
        tmp_path,
        "shop/orders.go",
        f'package shop\n\nconst orderSelect = {_chain(N, chr(34) + "x" + chr(34))}\n',
    )
    graph = scan_path(tmp_path)
    assert any(e.to_id == "column:inventory.stock" for e in graph.edges.values())
