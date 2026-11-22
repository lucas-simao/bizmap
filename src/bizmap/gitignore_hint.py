"""Tell the user, once per file, when `bizmap scan` wrote its output inside a
git work tree and git does not ignore it.

`bizmap.graph.json` can reach tens of megabytes; committed by a `git add .`
it bloats the history (and over 100 MB GitHub refuses the push). The scan
never edits `.gitignore` itself: it prints the line to add.

"Once": a marker per absolute path in the cache folder (`query.cache_dir`),
so the hint does not repeat on every scan. ``BIZMAP_NO_GITIGNORE_HINT=1``
silences it. Any git problem (no git, not a repo, a timeout) → no hint.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def not_ignored(paths: list[Path]) -> tuple[Path, list[Path]] | None:
    """(work-tree root, the paths inside it that git does not ignore), or
    ``None`` when the paths are not in a git work tree."""
    if not paths:
        return None
    first = Path(paths[0]).resolve()
    top = _git(first.parent, "rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0 or not top.stdout.strip():
        return None
    root = Path(top.stdout.strip()).resolve()
    out: list[Path] = []
    for p in paths:
        full = Path(p).resolve()
        try:
            full.relative_to(root)
        except ValueError:
            continue  # written outside this work tree
        res = _git(root, "check-ignore", "-q", str(full))
        if res is None or res.returncode not in (0, 1):
            return None  # git could not answer: say nothing
        if res.returncode == 1:
            out.append(full)
    return root, out


def _marker(path: Path) -> Path:
    from bizmap.query import cache_dir

    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]
    return cache_dir().parent / "hints" / f"gitignore-{digest}"


def gitignore_hint(paths: list[Path], once: bool = True) -> str | None:
    """The message to print, or ``None`` (ignored, outside git, already told)."""
    if os.environ.get("BIZMAP_NO_GITIGNORE_HINT", "").strip().lower() in _TRUE:
        return None
    found = not_ignored([Path(p) for p in paths])
    if not found:
        return None
    root, loose = found
    if once:
        loose = [p for p in loose if not _marker(p).exists()]
    if not loose:
        return None
    rels = [p.relative_to(root).as_posix() for p in loose]
    lines = [
        "note: git does not ignore "
        + ", ".join(rels)
        + " (generated, can be large). Add to "
        + (root / ".gitignore").as_posix()
        + ":",
        *[f"  /{r}" for r in rels],
        "(said once per file; BIZMAP_NO_GITIGNORE_HINT=1 silences it)",
    ]
    if once:
        for p in loose:
            m = _marker(p)
            with contextlib.suppress(OSError):
                m.parent.mkdir(parents=True, exist_ok=True)
                m.write_text(str(p) + "\n", encoding="utf-8")
    return "\n".join(lines)
