"""`bizmap scan` tells, once per file, when its output sits unignored in a git repo."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bizmap.gitignore_hint import gitignore_hint, not_ignored

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_hint_for_an_unignored_graph_once(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "orders-api")
    graph = repo / "bizmap.graph.json"
    graph.write_text("{}")
    root, loose = not_ignored([graph])
    assert root == repo.resolve() and loose == [graph.resolve()]
    hint = gitignore_hint([graph])
    assert hint is not None
    assert "git does not ignore bizmap.graph.json" in hint and "  /bizmap.graph.json" in hint
    assert gitignore_hint([graph]) is None  # said once
    # a second file still gets its own hint
    mmd = repo / "bizmap.graph.mmd"
    mmd.write_text("graph TD")
    hint2 = gitignore_hint([graph, mmd])
    assert hint2 is not None and "/bizmap.graph.mmd" in hint2 and "/bizmap.graph.json" not in hint2


def test_no_hint_when_ignored_outside_git_or_silenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path / "shop-web")
    (repo / ".gitignore").write_text("/bizmap.graph.json\n")
    graph = repo / "bizmap.graph.json"
    graph.write_text("{}")
    assert gitignore_hint([graph]) is None
    plain = tmp_path / "not-a-repo" / "bizmap.graph.json"
    plain.parent.mkdir()
    plain.write_text("{}")
    assert gitignore_hint([plain]) is None
    other = _repo(tmp_path / "billing-worker") / "bizmap.graph.json"
    other.write_text("{}")
    monkeypatch.setenv("BIZMAP_NO_GITIGNORE_HINT", "1")
    assert gitignore_hint([other]) is None


def test_a_tracked_graph_is_pointed_out(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "tracked")
    graph = repo / "out" / "bizmap.graph.json"
    graph.parent.mkdir()
    graph.write_text("{}")
    subprocess.run(["git", "-C", str(repo), "add", "out/bizmap.graph.json"], check=True)
    (repo / ".gitignore").write_text("bizmap.graph.json\n")  # too late: already tracked
    hint = gitignore_hint([graph], once=False)
    assert hint is not None and "/out/bizmap.graph.json" in hint


def test_scan_prints_the_hint_and_writes_nothing_else(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    from click.testing import CliRunner

    from bizmap.cli import main

    repo = _repo(tmp_path / "orders-api")
    (repo / "go.mod").write_text("module example.com/orders-api\n\ngo 1.22\n")
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    run = CliRunner().invoke
    res = run(main, ["scan", str(repo), "--out", str(repo / "bizmap.graph.json")])
    assert res.exit_code == 0, res.output
    assert "git does not ignore bizmap.graph.json" in res.output
    res = run(main, ["who-writes", "table:orders", "--graph", str(repo / "bizmap.graph.json")])
    assert res.exit_code in (0, 1)
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True, check=True).stdout
    assert "bizmap.graph.idx" not in status and not (repo / "bizmap.graph.idx").exists()
    res = run(main, ["scan", str(repo), "--out", str(repo / "bizmap.graph.json")])
    assert "git does not ignore" not in res.output  # once
