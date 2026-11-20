"""Go phase scaling (round 38). Slow: run with `pytest -m perf` (CI runs it).

The checks compare timings taken within one run (1k vs 4k files), so a
machine busy with other work slows both sides; the absolute bounds are
loose for the same reason. A failure under heavy load is worth a re-run
before it is worth a bug report."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bench_go  # noqa: E402

from bizmap.scan import scan_path  # noqa: E402

pytestmark = pytest.mark.perf


def _time(root: Path) -> float:
    t0 = time.perf_counter()
    scan_path(root)
    return time.perf_counter() - t0


def test_go_phase_is_close_to_linear(tmp_path: Path) -> None:
    small, big = tmp_path / "small", tmp_path / "big"
    n_small = bench_go.build(small, 1000)
    n_big = bench_go.build(big, 4000)
    t_small, t_big = _time(small), _time(big)
    ratio = (t_big / t_small) / (n_big / n_small)
    print(f"1k: {t_small:.1f}s  4k: {t_big:.1f}s  normalized ratio {ratio:.2f}")
    assert ratio <= 1.25, (t_small, t_big)
    assert t_big < 60, t_big


def test_http_client_with_many_methods(tmp_path: Path) -> None:
    bench_go.build(tmp_path, 0, client=800)
    elapsed = _time(tmp_path)
    assert elapsed < 5, elapsed  # was ~25 s: the client's file was re-analysed per call


def test_heavy_repo_all_files_stays_linear(tmp_path: Path) -> None:
    """Bigger files, many standard-library calls, tests and generated code
    analysed too (round 39)."""
    small, big = tmp_path / "small", tmp_path / "big"
    n_small = bench_go.build(small, 1000, heavy=True)
    n_big = bench_go.build(big, 4000, heavy=True)
    for root in (small, big):
        (root / "bizmap.yaml").write_text("scan:\n  skip_generated: false\n", encoding="utf-8")
    t_small, t_big = _time(small), _time(big)
    ratio = (t_big / t_small) / (n_big / n_small)
    print(f"heavy 1k: {t_small:.1f}s  4k: {t_big:.1f}s  normalized ratio {ratio:.2f}")
    assert ratio <= 1.35, (t_small, t_big)
    assert t_big < 120, t_big
