"""Golden snapshots of the bundled examples.

Every detector change that adds or removes a link in ``examples/`` shows up
here as a ``+``/``-`` diff. If the change is intended, refresh with::

    BIZMAP_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py

and review the diff of ``tests/snapshots/*.txt`` in the PR.

The same scans also run under several ``PYTHONHASHSEED`` values in fresh
interpreters: identical input must give byte-identical graphs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bizmap.scan import scan_path
from bizmap.snapshot import diff, summarize

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = Path(__file__).resolve().parent / "snapshots"

CASES = {
    "shop": ROOT / "examples" / "shop",
    "clinic": ROOT / "examples" / "clinic",
    # pyplatform/bizmap.yaml lists `repos:`, so this is a joint multi-repo scan.
    "pyplatform": ROOT / "examples" / "pyplatform",
    # web (TS) → api (Go) → billing (Python) → api; README demo.
    "checkout": ROOT / "examples" / "checkout",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_example_snapshot(name: str) -> None:
    actual = summarize(scan_path(CASES[name]))
    snap = SNAP_DIR / f"{name}.txt"
    if os.environ.get("BIZMAP_UPDATE_SNAPSHOTS"):
        SNAP_DIR.mkdir(exist_ok=True)
        snap.write_text(actual, encoding="utf-8")
        return
    expected = snap.read_text(encoding="utf-8") if snap.exists() else ""
    delta = diff(expected, actual, name=f"snapshots/{name}.txt")
    if delta:
        pytest.fail(
            f"Graph for examples/{name} changed. If intended, run "
            f"BIZMAP_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py\n{delta}",
            pytrace=False,
        )


_SCAN_SNIPPET = (
    "import json,sys; from pathlib import Path; from bizmap.scan import scan_path; "
    "d=scan_path(Path(sys.argv[1])).to_dict(); d.pop('generated_at', None); "
    "print(json.dumps(d, indent=1, sort_keys=True))"
)


@pytest.mark.parametrize("name", sorted(CASES))
def test_example_deterministic_across_hash_seeds(name: str) -> None:
    src = ROOT / "src"
    outputs: dict[str, str] = {}
    for seed in ("0", "1", "4242"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", _SCAN_SNIPPET, str(CASES[name])],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        outputs[seed] = proc.stdout
    base = outputs["0"]
    for seed, out in outputs.items():
        if out != base:
            a = summarize(json.loads(base))
            b = summarize(json.loads(out))
            pytest.fail(
                f"examples/{name}: PYTHONHASHSEED=0 vs {seed} differ\n"
                + (diff(a, b, name=name) or "(same links; attribute/ordering difference)"),
                pytrace=False,
            )
