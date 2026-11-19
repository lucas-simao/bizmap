from __future__ import annotations

from collections.abc import Iterable
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


def iter_files(root: Path, suffixes: Iterable[str]) -> list[Path]:
    wanted = {s.lower() if s.startswith(".") else f".{s.lower()}" for s in suffixes}
    out: list[Path] = []
    root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() in wanted:
            out.append(path)
    return sorted(out)


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def discover_sql_files(root: Path) -> list[Path]:
    """Return SQL files in apply order: schema-like first, then migrations, then the rest."""
    files = iter_files(root, [".sql"])
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
