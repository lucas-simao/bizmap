from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from pathlib import Path

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    ".idea",
    ".vscode",
}

SQL_NAME_PRIORITY = (
    "schema.sql",
    "ddl.sql",
    "init.sql",
)


def path_excluded(rel_posix: str, patterns: Sequence[str] | None) -> bool:
    """Return True if a root-relative path matches any exclude glob.

    Bare prefixes (``cmd/seed``, ``demo/``) match that directory tree.
    ``*`` / ``?`` use fnmatch. ``**`` is treated as a recursive directory glob.
    """
    if not patterns:
        return False
    rel = rel_posix.lstrip("./")
    for raw in patterns:
        pat = str(raw).strip().lstrip("./")
        if not pat:
            continue
        if _match_exclude(rel, pat):
            return True
    return False


def _match_exclude(rel: str, pat: str) -> bool:
    pat_noslash = pat.rstrip("/")
    if rel == pat_noslash or rel.startswith(pat_noslash + "/"):
        if "*" not in pat_noslash and "?" not in pat_noslash and "[" not in pat_noslash:
            return True
    candidates = [pat, pat_noslash]
    if pat.endswith("/") or pat.endswith("/**"):
        prefix = pat.rstrip("/").removesuffix("**").rstrip("/")
        if prefix and (rel == prefix or rel.startswith(prefix + "/")):
            return True
    if pat.startswith("**/"):
        rest = pat[3:]
        if fnmatch.fnmatch(rel, rest) or fnmatch.fnmatch(rel.split("/")[-1], rest):
            return True
        for i, _ in enumerate(rel.split("/")):
            tail = "/".join(rel.split("/")[i:])
            if fnmatch.fnmatch(tail, rest):
                return True
    if "**" in pat:
        prefix, _, suffix = pat.partition("**")
        prefix = prefix.rstrip("/")
        suffix = suffix.lstrip("/")
        if prefix and not (rel == prefix or rel.startswith(prefix + "/")):
            return False
        rest = rel[len(prefix) :].lstrip("/") if prefix else rel
        if not suffix:
            return True
        if fnmatch.fnmatch(rest, suffix) or fnmatch.fnmatch(rest, suffix.lstrip("/")):
            return True
        for i, _ in enumerate(rest.split("/")):
            tail = "/".join(rest.split("/")[i:])
            if fnmatch.fnmatch(tail, suffix):
                return True
        return False
    return any(fnmatch.fnmatch(rel, c) for c in candidates if c)


def iter_files(
    root: Path,
    suffixes: Iterable[str],
    *,
    exclude: Sequence[str] | None = None,
) -> list[Path]:
    wanted = {s.lower() if s.startswith(".") else f".{s.lower()}" for s in suffixes}
    out: list[Path] = []
    root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        rel_posix = rel(path, root)
        if path_excluded(rel_posix, exclude):
            continue
        if path.suffix.lower() in wanted:
            out.append(path)
    return sorted(out)


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def discover_sql_files(root: Path, *, exclude: Sequence[str] | None = None) -> list[Path]:
    """Return SQL files in apply order: schema-like first, then migrations, then the rest."""
    files = iter_files(root, [".sql"], exclude=exclude)
    schema_like: list[Path] = []
    migrations: list[Path] = []
    other: list[Path] = []
    for path in files:
        parts = [p.lower() for p in path.parts]
        name = path.name.lower()
        if name in SQL_NAME_PRIORITY or "schema" in parts:
            schema_like.append(path)
        elif "migrations" in parts or "migrate" in parts:
            migrations.append(path)
        else:
            other.append(path)

    def key(path: Path) -> tuple:
        return (path.as_posix().lower(),)

    def schema_key(path: Path) -> tuple:
        name = path.name.lower()
        rank = SQL_NAME_PRIORITY.index(name) if name in SQL_NAME_PRIORITY else 99
        return (rank, path.as_posix().lower())

    schema_like.sort(key=schema_key)
    migrations.sort(key=key)
    other.sort(key=key)
    return schema_like + migrations + other
