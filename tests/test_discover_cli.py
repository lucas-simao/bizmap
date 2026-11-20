"""`bizmap scan --discover` end to end (real git repos, joint scan)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from bizmap.cli import main
from test_discover import clone, git, make_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

ORDERS_PY = '''
def list_orders(db):
    return db.execute("SELECT id, total FROM orders")
'''


@pytest.fixture()
def work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    shop = make_repo(work / "shop", "https://github.com/acme/shop.git")
    git(shop, "worktree", "add", "-q", "-b", "feature", str(work / "shop-feature"))
    up = make_repo(tmp_path / "up" / "orders")
    orders = clone(up, work / "orders", "git@github.com:acme/orders.git")
    (orders / "orders.py").write_text(ORDERS_PY)
    git(orders, "add", "-A")
    git(orders, "commit", "-q", "-m", "orders")
    clone(up, work / "old" / "orders-2023", "https://github.com/acme/orders")
    make_repo(work / "notes", "https://github.com/acme/notes.git", code=False)
    return work


def run(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_list_shows_choice_and_criterion(work: Path) -> None:
    res = run("scan", "--discover", str(work), "--list")
    assert res.exit_code == 0, res.output
    out = res.output
    assert "orders: orders  [folder_name: folder named like the repository (orders)]" in out
    assert "shop: shop  [main_checkout: main checkout (others are worktrees)]" in out
    assert "shop-feature" in out and "worktree" in out
    assert "notes: skipped (no supported source code)" in out
    assert "2 project(s) to scan: orders, shop" in out


def test_list_json(work: Path) -> None:
    res = run("scan", "--discover", str(work), "--list", "--json")
    data = json.loads(res.output)
    chosen = {g["name"]: (g["chosen"], g["criterion"]) for g in data["groups"]}
    assert chosen["shop"] == ("shop", "main_checkout")
    assert chosen["notes"] == (None, "no_code")
    assert chosen["orders"][0] == "orders"


def test_scan_discover_scans_one_copy_per_repo(work: Path, tmp_path: Path) -> None:
    out = tmp_path / "g.json"
    res = run("scan", "--discover", str(work), "--out", str(out), "-q")
    assert res.exit_code == 0, res.output
    assert "discovered 2 project(s): orders=orders, shop=shop" in res.output
    data = json.loads(out.read_text())
    # each repo's root relative to its own git toplevel, as for `bizmap scan ./a ./b`
    assert data["repo_roots"] == {"orders": ".", "shop": "."}
    assert data["repo_roots_base"] == "git"
    files = {n.get("evidence", {}).get("file") for n in data["nodes"] if n.get("evidence")}
    assert any(f and f.endswith("orders.py") for f in files)
    assert "orders-2023" not in json.dumps(data["nodes"])
    assert "shop-feature" not in json.dumps(data["nodes"])


def test_yaml_discover_section_and_flags_win(work: Path) -> None:
    (work / "bizmap.yaml").write_text("discover:\n  prefer: [shop-feature]\n  exclude: [old]\n")
    res = run("scan", "--discover", str(work), "--list")
    assert "shop: shop-feature  [preferred" in res.output
    assert "orders-2023" not in res.output.split("left out:")[0]
    assert "old  (excluded)" in res.output
    res = run("scan", "--discover", str(work), "--list", "--prefer", "shop")
    assert "shop: shop  [" in res.output


def test_misuse_errors(work: Path) -> None:
    res = CliRunner().invoke(main, ["scan", "--list"])
    assert res.exit_code == 2 and "--list only works with --discover" in res.output
    res = CliRunner().invoke(main, ["scan", str(work), "--discover", str(work)])
    assert res.exit_code == 2 and "not both" in res.output
    res = CliRunner().invoke(main, ["scan", "--discover", str(work), "--json"])
    assert res.exit_code == 2 and "--json needs --list" in res.output
    (work / "bizmap.yaml").write_text("discover:\n  depth: nope\n")
    res = CliRunner().invoke(main, ["scan", "--discover", str(work), "--list"])
    assert res.exit_code == 1 and "positive integer" in res.output


def test_nothing_to_scan(tmp_path: Path) -> None:
    res = CliRunner().invoke(main, ["scan", "--discover", str(tmp_path)])
    assert res.exit_code == 1 and "no projects with supported code" in res.output


def test_prefer_warning_goes_to_stderr(work: Path, tmp_path: Path) -> None:
    res = CliRunner().invoke(
        main, ["scan", "--discover", str(work), "--prefer", "nope", "--out", str(tmp_path / "g.json"), "-q"]
    )
    assert res.exit_code == 0
    assert "warning: --prefer 'nope' matches none" in res.output


def test_discover_without_a_value_uses_the_current_directory(work: Path, monkeypatch) -> None:
    monkeypatch.chdir(work)
    res = CliRunner().invoke(main, ["scan", "--discover", "--list"])
    assert res.exit_code == 0, res.output
    assert "2 project(s) to scan: orders, shop" in res.output
