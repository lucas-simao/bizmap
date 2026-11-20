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
    assert "28 more distinct calls (use --verbose" in text
    assert sum(1 for line in lines if line.startswith("warning: ")) == 10 and "4 more warnings" in text
    assert text.startswith("skipped: 3 generated dirs, 12 generated/minified files")
    assert len(lines) < 30


def test_verbose_lists_everything() -> None:
    lines = scan_summary(_graph_with_unresolved(1300), verbose=True)
    assert sum(1 for line in lines if line.startswith("  GET ")) == 38
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


def test_summary_names_skipped_paths() -> None:
    g = Graph()
    g.attrs["skipped"] = {"generated_dirs": 2, "generated_files": 7}
    g.attrs["skipped_paths"] = {
        "generated_dirs": ["coverage", "web/.next"],
        "generated_files": [f"public/f{i}.min.js" for i in range(7)],
    }
    text = "\n".join(scan_summary(g))
    assert "  generated dirs: coverage, web/.next\n" in text + "\n"
    assert "public/f4.min.js … +2 more" in text and "public/f5.min.js" not in text
    verbose = "\n".join(scan_summary(g, verbose=True))
    assert "public/f6.min.js" in verbose and "more" not in verbose


def test_phase_start_lines_and_heartbeat() -> None:
    import time

    out = io.StringIO()
    p = progress.start(threshold=0, stream=out, heartbeat=0, live=False)
    progress.begin("shop: js/ts", 19)
    p.tick()  # nothing printed since the phase began → heartbeat line
    t0 = time.perf_counter()
    progress.done("shop: files (0 go, 1 py, 19 js/ts, 1 sql)", t0)
    progress.done("api: files (2 go, 0 py, 0 js/ts, 0 sql)", t0)
    progress.stop()
    text = out.getvalue()
    assert "shop: js/ts: 19 files…" in text
    assert "still in shop: js/ts" in text
    last = text.strip().splitlines()[-1]
    assert last.startswith("[bizmap] done in") and "(files " in last and "go," not in last


def test_heartbeat_waits_for_the_threshold() -> None:
    out = io.StringIO()
    p = progress.start(threshold=60, stream=out, heartbeat=0, live=False)
    progress.begin("shop: go", 3)
    p.tick()
    progress.stop()
    assert out.getvalue() == ""


def test_phase_kind() -> None:
    assert progress.phase_kind("shop: files (0 go, 1 py)") == "files"
    assert progress.phase_kind("merge + cross-service links (2 repos)") == "merge + cross-service links"
    assert progress.phase_kind("shop: js/ts") == "js/ts"


def test_status_lines_only_when_live_and_throttled() -> None:
    out = io.StringIO()
    progress.start(threshold=60, stream=out, live=False)
    progress.status("go: 1/10 files analysed")
    progress.stop()
    assert out.getvalue() == ""
    out = io.StringIO()
    progress.start(threshold=0, stream=out, live=False)
    progress.note("listing")
    progress.status("go: 1/10 files analysed", every=0)
    progress.status("go: 2/10 files analysed", every=3600)  # throttled
    progress.stop()
    text = out.getvalue()
    assert "go: 1/10 files analysed" in text and "2/10" not in text


def test_prefix_and_counters() -> None:
    out = io.StringIO()
    progress.start(threshold=0, stream=out, live=False)
    progress.set_prefix("[repo 2/3] ")
    progress.note("go: 10 files…")
    progress.counter("go: parsing", 4, 10, every=0)
    progress.set_prefix("")
    progress.stop()
    text = out.getvalue()
    assert "[repo 2/3] go: 10 files…" in text
    assert "[repo 2/3] go: parsing 4/10" in text


def test_scan_says_it_started(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    res = CliRunner().invoke(main, ["scan", str(tmp_path), "--out", str(tmp_path / "g.json")])
    assert res.exit_code == 0, res.output
    assert "bizmap " in res.output and "scanning" in res.output
