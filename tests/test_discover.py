"""`bizmap scan --discover`: finding, grouping and choosing repos.

Every repository here is a real, synthetic git repo built in a temp dir
(`git init`, `git clone`, `git worktree add`)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bizmap import discover as disc
from bizmap.discover import discover, format_discovery, load_discover_settings, normalize_origin

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(cwd: Path, *args: str, when: int | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(cwd),
    }
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when} +0000"
    out = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return out.stdout


def make_repo(path: Path, origin: str | None = None, *, when: int = 1_700_000_000, code: bool = True) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    if code:
        (path / "app.py").write_text("def handler():\n    return 1\n")
    else:
        (path / "README.md").write_text("notes\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init", when=when)
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


def clone(src: Path, dest: Path, origin: str) -> Path:
    git(src.parent, "clone", "-q", str(src), str(dest))
    git(dest, "remote", "set-url", "origin", origin)
    return dest


def commit(path: Path, when: int) -> None:
    (path / f"f{when}.py").write_text("x = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "more", when=when)


def only_group(d):
    assert len(d.groups) == 1, format_discovery(d)
    return d.groups[0]


# ── origin normalization ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/shop.git",
        "https://user@GitHub.com/Acme/Shop/",
        "git@github.com:acme/shop.git",
        "ssh://git@github.com/acme/shop",
        "ssh://git@github.com:22/acme/shop.git",
        "http://github.com/acme/shop",
    ],
)
def test_origin_forms_normalize_alike(url: str) -> None:
    assert normalize_origin(url) == "github.com/acme/shop"


def test_local_origin_is_absolute_path(tmp_path: Path) -> None:
    assert normalize_origin("../up/shop.git", base=tmp_path / "w") == "file:" + (tmp_path / "up" / "shop").as_posix()
    assert normalize_origin("file:///srv/git/shop.git") == "file:/srv/git/shop"


# ── grouping and choice ──────────────────────────────────────────────────────


def test_worktrees_group_with_their_main_checkout(tmp_path: Path) -> None:
    shop = make_repo(tmp_path / "shop", "https://github.com/acme/shop.git")
    git(shop, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "shop-feature"))
    git(shop, "worktree", "add", "-q", "-b", "hotfix", str(tmp_path / "wt" / "shop-hotfix"))
    g = only_group(discover(tmp_path))
    assert g.name == "shop"
    assert g.chosen.rel == "shop"
    assert g.criterion == "main_checkout"
    kinds = {m.rel: (m.kind, m.branch) for m in g.members}
    assert kinds == {
        "shop": ("main", "main"),
        "shop-feature": ("worktree", "feature"),
        "wt/shop-hotfix": ("worktree", "hotfix"),
    }


def test_worktrees_without_origin_group_by_git_dir(tmp_path: Path) -> None:
    shop = make_repo(tmp_path / "shop")
    git(shop, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "shop-feature"))
    g = only_group(discover(tmp_path))
    assert g.chosen.rel == "shop" and g.criterion == "main_checkout"


def test_lone_worktree_whose_main_checkout_is_elsewhere(tmp_path: Path) -> None:
    shop = make_repo(tmp_path / "elsewhere" / "shop", "https://github.com/acme/shop.git")
    work = tmp_path / "work"
    work.mkdir()
    git(shop, "worktree", "add", "-q", "-b", "feature", str(work / "shop-feature"))
    g = only_group(discover(work))
    assert g.chosen.rel == "shop-feature"
    assert g.chosen.kind == "worktree"
    assert g.criterion == "only_copy"


def test_clones_with_different_url_forms_are_one_project(tmp_path: Path) -> None:
    up = make_repo(tmp_path / "up" / "orders")
    work = tmp_path / "work"
    clone(up, work / "orders", "https://github.com/acme/orders.git")
    clone(up, work / "old" / "orders-copy", "git@GitHub.com:Acme/Orders")
    git(work / "old" / "orders-copy", "checkout", "-q", "-b", "experiment")
    g = only_group(discover(work))
    assert g.name == "orders"
    assert g.chosen.rel == "orders"
    assert g.criterion == "default_branch"


def test_default_branch_comes_from_origin_head(tmp_path: Path) -> None:
    up = make_repo(tmp_path / "up" / "orders")
    git(up, "branch", "-m", "main", "trunk")
    work = tmp_path / "work"
    a = clone(up, work / "a", "https://github.com/acme/orders.git")
    b = clone(up, work / "b", "https://github.com/acme/orders.git")
    git(a, "checkout", "-q", "-b", "main")  # named main, but trunk is the default
    assert (b / ".git" / "refs" / "remotes" / "origin" / "HEAD").exists()
    g = only_group(discover(work))
    assert g.chosen.rel == "b"
    assert g.chosen.default_branch == "trunk"
    assert g.criterion == "default_branch"


def test_most_recent_commit_wins_and_uses_git(tmp_path: Path) -> None:
    up = make_repo(tmp_path / "up" / "inventory")
    work = tmp_path / "work"
    a = clone(up, work / "a", "https://github.com/acme/inventory.git")
    clone(up, work / "b", "https://github.com/acme/inventory.git")
    commit(a, 1_800_000_000)
    g = only_group(discover(work))
    assert g.chosen.rel == "a"
    assert g.criterion == "latest_commit"
    assert g.chosen.time_source == "git"
    assert "git" in g.reason


def test_ref_mtime_fallback_without_git(tmp_path: Path, monkeypatch) -> None:
    up = make_repo(tmp_path / "up" / "inventory")
    work = tmp_path / "work"
    a = clone(up, work / "a", "https://github.com/acme/inventory.git")
    b = clone(up, work / "b", "https://github.com/acme/inventory.git")
    os.utime(a / ".git" / "refs" / "heads" / "main", (1_600_000_000, 1_600_000_000))
    os.utime(b / ".git" / "refs" / "heads" / "main", (1_650_000_000, 1_650_000_000))
    monkeypatch.setattr(disc.shutil, "which", lambda _name: None)
    g = only_group(discover(work))
    assert g.chosen.rel == "b"
    assert g.criterion == "latest_commit"
    assert g.chosen.time_source == "ref-mtime"
    assert "ref-mtime" in format_discovery(discover(work))


def test_full_tie_breaks_by_path(tmp_path: Path) -> None:
    up = make_repo(tmp_path / "up" / "customers")
    work = tmp_path / "work"
    clone(up, work / "z-copy", "https://github.com/acme/customers.git")
    clone(up, work / "a-copy", "https://github.com/acme/customers.git")
    g = only_group(discover(work))
    assert g.chosen.rel == "a-copy"
    assert g.criterion == "path_order"


def test_prefer_overrides_the_rules(tmp_path: Path) -> None:
    shop = make_repo(tmp_path / "shop", "https://github.com/acme/shop.git")
    git(shop, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "shop-feature"))
    g = only_group(discover(tmp_path, prefer=["shop-feature"]))
    assert g.chosen.rel == "shop-feature"
    assert g.criterion == "preferred"


def test_result_is_deterministic(tmp_path: Path) -> None:
    for name in ("shop", "orders", "inventory"):
        make_repo(tmp_path / name, f"https://github.com/acme/{name}.git")
    first = json.dumps(discover(tmp_path).to_dict(), sort_keys=True)
    assert first == json.dumps(discover(tmp_path).to_dict(), sort_keys=True)
    assert [g.name for g in discover(tmp_path).groups] == ["inventory", "orders", "shop"]


def test_same_name_different_repos_get_suffixes(tmp_path: Path) -> None:
    make_repo(tmp_path / "acme" / "api", "https://github.com/acme/api.git")
    make_repo(tmp_path / "other" / "api", "https://github.com/other/api.git")
    d = discover(tmp_path)
    assert [g.name for g in d.groups] == ["api", "api-2"]
    assert [g.chosen.rel for g in d.groups] == ["acme/api", "other/api"]


# ── what is found and what is left out ───────────────────────────────────────


def test_depth_dependency_dirs_and_nested_repos(tmp_path: Path) -> None:
    make_repo(tmp_path / "shop", "https://github.com/acme/shop.git")
    make_repo(tmp_path / "shop" / "vendor-copy", "https://github.com/acme/nested.git")  # inside a repo
    make_repo(tmp_path / "node_modules" / "lib", "https://github.com/acme/lib.git")
    make_repo(tmp_path / "a" / "b" / "deep", "https://github.com/acme/deep.git")
    d = discover(tmp_path)
    assert [g.name for g in d.groups] == ["shop"]
    d3 = discover(tmp_path, depth=3)
    assert [g.name for g in d3.groups] == ["deep", "shop"]


def test_groups_without_code_are_skipped(tmp_path: Path) -> None:
    make_repo(tmp_path / "docs", "https://github.com/acme/docs.git", code=False)
    make_repo(tmp_path / "shop", "https://github.com/acme/shop.git")
    d = discover(tmp_path)
    by = {g.name: g for g in d.groups}
    assert by["docs"].chosen is None and by["docs"].criterion == "no_code"
    assert [g.name for g in d.chosen] == ["shop"]
    assert "docs: skipped (no supported source code)" in format_discovery(d)


def test_code_only_in_dependency_dirs_does_not_count(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "site", "https://github.com/acme/site.git", code=False)
    (repo / "node_modules" / "x").mkdir(parents=True)
    (repo / "node_modules" / "x" / "index.js").write_text("module.exports = 1\n")
    assert discover(tmp_path).chosen == []


def test_include_and_exclude(tmp_path: Path) -> None:
    make_repo(tmp_path / "shop", "https://github.com/acme/shop.git")
    make_repo(tmp_path / "archive" / "orders", "https://github.com/acme/orders.git")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "job.py").write_text("def run():\n    pass\n")
    d = discover(tmp_path, exclude=["archive"])
    assert [g.name for g in d.groups] == ["shop"]
    assert ("archive", "excluded") in d.skipped
    d = discover(tmp_path, include=["scripts"])
    by = {g.name: g for g in d.groups}
    assert by["scripts"].chosen.kind == "folder"
    assert by["scripts"].criterion == "only_copy"


def test_dir_that_is_itself_a_repo(tmp_path: Path) -> None:
    shop = make_repo(tmp_path / "shop", "https://github.com/acme/shop.git")
    g = only_group(discover(shop))
    assert g.chosen.rel == "."


def test_empty_dir(tmp_path: Path) -> None:
    d = discover(tmp_path)
    assert d.groups == [] and "no git repositories found" in format_discovery(d)


# ── bizmap.yaml `discover:` ──────────────────────────────────────────────────


def test_discover_settings(tmp_path: Path) -> None:
    (tmp_path / "bizmap.yaml").write_text(
        "discover:\n  depth: 3\n  exclude: archive\n  prefer: [shop]\n  include:\n    - scripts\n"
    )
    assert load_discover_settings(tmp_path) == {
        "depth": 3,
        "exclude": ["archive"],
        "prefer": ["shop"],
        "include": ["scripts"],
    }


@pytest.mark.parametrize(
    "body, message",
    [
        ("discover: [a]\n", "must be a mapping"),
        ("discover:\n  depth: 0\n", "positive integer"),
        ("discover:\n  prefer: {a: 1}\n", "list of paths"),
        ("discover:\n  deep: 2\n", "unknown discover key"),
    ],
)
def test_discover_settings_errors(tmp_path: Path, body: str, message: str) -> None:
    (tmp_path / "bizmap.yaml").write_text(body)
    with pytest.raises(ValueError, match=message):
        load_discover_settings(tmp_path)
