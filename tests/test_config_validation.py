"""bizmap.yaml `scan:` / `exclude:` mistakes give one clear line (round 37)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bizmap.cli import main
from bizmap.overrides import ConfigError, load_scan_config


@pytest.mark.parametrize(
    "body, message",
    [
        ("scan: 5\n", "`scan:` must be a mapping"),
        ("exclude: 5\n", "exclude must be a path/glob or a list of them, got 5"),
        ("scan:\n  exclude: [1]\n", "scan.exclude must be a path/glob"),
        ("scan:\n  include: {a: 1}\n", "scan.include must be a path/glob"),
        ("scan:\n  skip_generated: 'no'\n", "scan.skip_generated must be true or false, got 'no'"),
        ("scan:\n  gitignore: 0\n", "scan.gitignore must be true or false"),
        ("scan:\n  bogus: 1\n", "unknown scan key(s): bogus"),
        ("expand_star: maybe\n", "expand_star must be true or false"),
        ("scan: [\n", "invalid YAML"),
    ],
)
def test_bad_scan_config(tmp_path: Path, body: str, message: str) -> None:
    (tmp_path / "bizmap.yaml").write_text(body)
    with pytest.raises(ConfigError) as info:
        load_scan_config(tmp_path)
    assert message in str(info.value)
    assert "bizmap.yaml" in str(info.value)


def test_good_scan_config(tmp_path: Path) -> None:
    (tmp_path / "bizmap.yaml").write_text(
        "exclude: demo\nscan:\n  include: .yarn\n  exclude: [cmd/seed]\n  skip_generated: false\n  gitignore: true\n"
    )
    cfg = load_scan_config(tmp_path)
    assert cfg.exclude == ["demo", "cmd/seed"]
    assert cfg.include == [".yarn"]
    assert cfg.skip_generated is False and cfg.gitignore is True


def test_cli_shows_the_error_without_traceback(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    (tmp_path / "bizmap.yaml").write_text("scan:\n  skip_generated: 'no'\n")
    res = CliRunner().invoke(main, ["scan", str(tmp_path), "--out", str(tmp_path / "g.json"), "-q"])
    assert res.exit_code == 1
    assert "Error:" in res.output and "scan.skip_generated must be true or false" in res.output
    assert "Traceback" not in res.output
