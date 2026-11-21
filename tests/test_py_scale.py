"""Python phase: the graph of a synthetic FastAPI monorepo is unchanged by
the round-41 performance fixes (golden summary made with 0.5.0), a bad SQL
string never stops the scan, and (`-m perf`) the phase stays linear."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bench_py  # noqa: E402

from bizmap.scan import scan_path  # noqa: E402
from bizmap.snapshot import summarize  # noqa: E402

GOLDEN = Path(__file__).parent / "snapshots" / "py_bench_30.txt"


def test_graph_matches_the_golden_summary(tmp_path: Path) -> None:
    root = tmp_path / "shop"
    bench_py.build(root, 30)
    actual = summarize(scan_path(root).to_dict())
    if os.environ.get("BIZMAP_UPDATE_SNAPSHOTS"):
        GOLDEN.write_text(actual, encoding="utf-8")
    assert actual == GOLDEN.read_text(encoding="utf-8")


def test_truncated_sql_does_not_stop_the_scan(tmp_path: Path) -> None:
    (tmp_path / "schema.sql").write_text("CREATE TABLE orders (id INT, status TEXT);\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "def check(conn):\n"
        '    conn.execute("UPDATE orders ...")\n'
        "\n"
        "def ok(conn):\n"
        '    conn.execute("UPDATE orders SET status = 1")\n',
        encoding="utf-8",
    )
    graph = scan_path(tmp_path)
    writers = {e.from_id for e in graph.edges.values() if e.type == "writes"}
    assert "function:app.ok" in writers


def test_table_name_survives_a_dot_without_expression() -> None:
    from sqlglot import exp

    from bizmap.sql_analyze import _table_name, analyze_sql

    broken = exp.Table(this=exp.Dot(this=exp.Identifier(this=""), expression=None), catalog=exp.to_identifier("orders"))
    assert isinstance(_table_name(broken), str)
    assert isinstance(analyze_sql("UPDATE orders ..."), list)


@pytest.mark.perf
def test_python_phase_is_close_to_linear(tmp_path: Path) -> None:
    small, big = tmp_path / "a" / "shop", tmp_path / "b" / "shop"
    bench_py.build(small, 150)
    bench_py.build(big, 300)
    t0 = time.perf_counter()
    scan_path(small)
    t_small = time.perf_counter() - t0
    t0 = time.perf_counter()
    scan_path(big)
    t_big = time.perf_counter() - t0
    ratio = (t_big / t_small) / 2
    print(f"py 150: {t_small:.1f}s  300: {t_big:.1f}s  normalized ratio {ratio:.2f}")
    assert ratio <= 1.3, (t_small, t_big)  # was ~1.75 (quadratic) in 0.5.0
    assert t_big < 120, t_big
