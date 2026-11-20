"""JS/TS phase scaling (round 40). Slow: run with `pytest -m perf`.

Timings are compared within one run (1k vs 4k files), so a busy machine
slows both; the absolute bound is loose for the same reason."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bench_js  # noqa: E402

from bizmap.scan import scan_path  # noqa: E402

pytestmark = pytest.mark.perf


def _time(root: Path) -> float:
    t0 = time.perf_counter()
    scan_path(root)
    return time.perf_counter() - t0


def test_js_phase_is_close_to_linear(tmp_path: Path) -> None:
    small, big = tmp_path / "small", tmp_path / "big"
    n_small = bench_js.build(small, 1000)
    n_big = bench_js.build(big, 4000)
    t_small, t_big = _time(small), _time(big)
    ratio = (t_big / t_small) / (n_big / n_small)
    print(f"js 1k: {t_small:.1f}s  4k: {t_big:.1f}s  normalized ratio {ratio:.2f}")
    assert ratio <= 1.3, (t_small, t_big)
    assert t_big < 90, t_big
