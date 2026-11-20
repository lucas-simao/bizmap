"""`bizmap scan` output: counts + top 10 (everything with --verbose), and
progress on stderr only for slow scans."""

from __future__ import annotations

import io
import json
from pathlib import Path

from click.testing import CliRunner

from bizmap import progress
from bizmap.cli import main, scan_summary
from bizmap.graph import Graph


def _graph_with_unresolved(n: int) -> Graph:
    g = Graph()
    g.attrs["unresolved_http"] = [
        {"url": f"/api/v1/item{i % 37}/" + "x" * (300 if i == 0 else 3), "method": "get", "reason": "target_not_scanned" if i % 3 else "path_dynamic", "keys": []}
        for i in range(n)
    ]
    g.attrs["warnings"] = [f"warning number {i}" for i in range(14)]
    g.attrs["skipped"] = {"generated_dirs": 3, "generated_files": 12}
    return g


def test_summary_is_counts_plus_top_10() -> None:
    lines = scan_summary(_graph_with_unresolved(1300))
    text = "\n".join(lines)
    assert "1300 unresolved HTTP calls: target_not_scanned=866, path_dynamic=434" in text
    calls = [line for line in lines if line.startswith("  GET ")]
    assert len(calls) == 10
    assert all(len(line) <= 112 for line in calls), calls  # long/minified fragments are shortened
    assert "27 more distinct calls (use --verbose" in text
    assert sum(1 for line in lines if line.startswith("warning: ")) == 10 and "4 more warnings" in text
    assert text.startswith("skipped: 3 generated dirs, 12 generated/minified files")
    assert len(lines) < 30


def test_verbose_lists_everything() -> None:
    lines = scan_summary(_graph_with_unresolved(1300), verbose=True)
    assert sum(1 for line in lines if line.startswith("  GET ")) == 37
    assert sum(1 for line in lines if line.startswith("warning: ")) == 14
    assert any(len(line) > 300 for line in lines)  # not shortened


def test_progress_only_after_threshold() -> None:
    quiet = io.StringIO()
    progress.start(threshold=60, stream=quiet)
    progress.note("listing files")
    progress.done("shop: go", 0.0 + __import__("time").perf_counter())
    progress.stop()
    assert quiet.getvalue() == ""
    loud = io.StringIO()
    progress.start(threshold=0, stream=loud)
    progress.note("listing files")
    t0 = __import__("time").perf_counter()
    progress.done("shop: go", t0)
    progress.stop()
    out = loud.getvalue()
    assert "listing files" in out and "shop: go" in out and "done in" in out


def test_cli_stdout_is_unchanged_by_progress(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    out = tmp_path / "g.json"
    res = CliRunner().invoke(main, ["scan", str(tmp_path), "--out", str(out), "--quiet"])
    assert res.exit_code == 0, res.output
    assert res.output.startswith("wrote ")
    assert json.loads(out.read_text(encoding="utf-8"))["nodes"]
