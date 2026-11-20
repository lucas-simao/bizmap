"""README promise: per-repo `bizmap scan` + `registry merge` == joint scan.

Runs both paths through the CLI on every multi-repo example and compares the
full graph JSON (only `generated_at` may differ).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from bizmap.cli import main
from bizmap.snapshot import diff, summarize

ROOT = Path(__file__).resolve().parents[1]
WORKSPACES = ["checkout", "pyplatform"]


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("generated_at", None)
    # where each repo sat on disk during the scan (for `bizmap impact`), not
    # lineage: separately scanned graphs can't know their workspace layout
    data.pop("repo_roots", None)
    return data


@pytest.mark.parametrize("name", WORKSPACES)
def test_registry_merge_equals_joint_scan(name: str, tmp_path: Path) -> None:
    ws = ROOT / "examples" / name
    repos = yaml.safe_load((ws / "bizmap.yaml").read_text(encoding="utf-8"))["repos"]
    runner = CliRunner()

    joint = tmp_path / "joint.json"
    res = runner.invoke(main, ["scan", str(ws), "--out", str(joint)])
    assert res.exit_code == 0, res.output

    parts: list[str] = []
    for repo in repos:
        out = tmp_path / f"{repo}.json"
        res = runner.invoke(main, ["scan", str(ws / repo), "--out", str(out)])
        assert res.exit_code == 0, res.output
        parts.append(str(out))
    merged = tmp_path / "merged.json"
    res = runner.invoke(main, ["registry", "merge", *parts, "--out", str(merged)])
    assert res.exit_code == 0, res.output

    a, b = _load(joint), _load(merged)
    if a != b:
        link_diff = diff(summarize(a), summarize(b), name=f"{name}: joint vs merge")
        keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        pytest.fail(f"top-level keys differing: {keys}\n{link_diff}", pytrace=False)
