from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bizmap.graph import (
    Evidence,
    Graph,
    column_id,
    parse_confidence,
)


@dataclass
class ScanConfig:
    """Scanner options loaded from bizmap.yaml (plus CLI overrides)."""

    exclude: list[str] = field(default_factory=list)
    expand_star: bool = False
    path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    database: str | None = None
    services: dict[str, Any] = field(default_factory=dict)
    repos: list[str] = field(default_factory=list)
    constants: dict[str, str] = field(default_factory=dict)
    workspace: Path | None = None
    # generated / ignored paths: `scan: {include: [...], gitignore: false, skip_generated: false}`
    include: list[str] = field(default_factory=list)
    gitignore: bool = True
    skip_generated: bool = True


def git_toplevel(start: Path) -> Path | None:
    """Return the git working tree root that contains ``start``, if any."""
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _yaml_in(directory: Path) -> Path | None:
    for name in ("bizmap.yaml", "bizmap.yml"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def find_overrides(root: Path, config: Path | None = None) -> Path | None:
    """Locate bizmap.yaml.

    Search order: explicit ``--config`` path, the scan root, then the git
    toplevel (so excludes work when scanning a subdirectory).
    """
    if config is not None:
        path = config.expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        if path.is_file():
            return path
        return None
    found = _yaml_in(root.resolve())
    if found is not None:
        return found
    git_root = git_toplevel(root)
    if git_root is not None and git_root != root.resolve():
        return _yaml_in(git_root)
    return None


def _workspace_config(root: Path, own: Path | None, max_up: int = 4) -> tuple[Path, dict] | None:
    """The nearest ancestor bizmap.yaml whose ``repos:`` lists ``root``.

    Lets ``bizmap scan web`` (one repo) see the ``services:`` / ``database:``
    declared once at the workspace level, so per-repo scans + ``registry
    merge`` give the same graph as a joint scan.
    """
    root = root.resolve()
    for depth, parent in enumerate(root.parents):
        if depth >= max_up:
            break
        candidate = _yaml_in(parent)
        if candidate is None or (own is not None and candidate.resolve() == own.resolve()):
            continue
        try:
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        repos = data.get("repos") or []
        if isinstance(repos, str):
            repos = [repos]
        listed = set()
        for item in repos:
            target = Path(str(item))
            target = target if target.is_absolute() else (parent / target)
            listed.add(target.resolve())
        if root in listed:
            return candidate, data
    return None


def load_scan_config(root: Path, config: Path | None = None) -> ScanConfig:
    cfg = _load_scan_config(root, config=config)
    if config is not None:
        return cfg
    ws = _workspace_config(root, cfg.path)
    if ws is None:
        return cfg
    ws_path, data = ws
    services = data.get("services") or {}
    if isinstance(services, dict):
        merged = {str(k): v for k, v in services.items() if v is not None}
        merged.update(cfg.services)
        cfg.services = merged
    database = data.get("database")
    if isinstance(database, dict):
        database = database.get("name") or database.get("id")
    if not cfg.database and database:
        cfg.database = str(database)
    cfg.workspace = ws_path
    return cfg


class ConfigError(ValueError):
    """bizmap.yaml has a value of the wrong type (shown without a traceback)."""


_SCAN_KEYS = ("include", "exclude", "gitignore", "skip_generated", "expand_star")


def _globs(path: Path, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"{path}: {key} must be a path/glob or a list of them, got {value!r}")


def _flag(path: Path, key: str, value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{path}: {key} must be true or false, got {value!r}")


def _load_scan_config(root: Path, config: Path | None = None) -> ScanConfig:
    path = find_overrides(root, config=config)
    if path is None:
        return ScanConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML ({' '.join(str(exc).split())})") from exc
    if not isinstance(data, dict):
        return ScanConfig(path=path)
    scan_opts = data.get("scan")
    if scan_opts is None:
        scan_opts = {}
    if not isinstance(scan_opts, dict):
        raise ConfigError(
            f"{path}: `scan:` must be a mapping ({', '.join(_SCAN_KEYS)}), got {scan_opts!r}"
        )
    unknown = sorted(str(k) for k in scan_opts if k not in _SCAN_KEYS)
    if unknown:
        raise ConfigError(
            f"{path}: unknown scan key(s): {', '.join(unknown)} (known: {', '.join(_SCAN_KEYS)})"
        )
    exclude = _globs(path, "exclude", data.get("exclude")) + _globs(
        path, "scan.exclude", scan_opts.get("exclude")
    )
    expand = data.get("expand_star")
    expand = _flag(path, "expand_star", expand, False) if expand is not None else _flag(
        path, "scan.expand_star", scan_opts.get("expand_star"), False
    )
    services = data.get("services") or {}
    if not isinstance(services, dict):
        services = {}
    repos = data.get("repos") or []
    if isinstance(repos, str):
        repos = [repos]
    name = data.get("name") or data.get("repo")
    database = data.get("database")
    if isinstance(database, dict):
        database = database.get("name") or database.get("id")
    constants = parse_yaml_constants(data.get("constants"))
    include = _globs(path, "scan.include", scan_opts.get("include"))
    return ScanConfig(
        include=include,
        gitignore=_flag(path, "scan.gitignore", scan_opts.get("gitignore"), True),
        skip_generated=_flag(path, "scan.skip_generated", scan_opts.get("skip_generated"), True),
        exclude=list(dict.fromkeys(exclude)),
        expand_star=expand,
        path=path,
        raw=data,
        name=str(name) if name else None,
        database=str(database) if database else None,
        services={str(k): v for k, v in services.items() if v is not None},
        repos=[str(x) for x in repos],
        constants=constants,
    )


def parse_yaml_constants(raw: Any) -> dict[str, str]:
    """``constants: { PathPrefix: /api/v1, pkg.Name: /x }`` → name → value."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        out[str(key)] = str(value)
    return out


def lookup_yaml_constant(
    constants: dict[str, str] | None,
    name: str,
    pkg: str | None = None,
) -> str | None:
    """Resolve a const by bare name, ``pkg.Name``, or import-path suffix."""
    if not constants or not name:
        return None
    keys: list[str] = []
    if pkg:
        keys.append(f"{pkg}.{name}")
        last = str(pkg).rstrip("/").split("/")[-1].split(".")[-1]
        if last and last != pkg:
            keys.append(f"{last}.{name}")
    keys.append(name)
    seen: set[str] = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        if key in constants:
            return constants[key]
    return None


def apply_overrides(graph: Graph, path: Path) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return
    columns = (data.get("overrides") or {}).get("columns") or data.get("columns") or {}
    for col_name, spec in columns.items():
        if not isinstance(spec, dict):
            continue
        table, _, column = str(col_name).partition(".")
        cid = column_id(table, column or col_name)
        node = graph.get(cid)
        if node is None:
            graph.add_node(cid, "column", table=table.lower(), name=(column or col_name).lower())
            node = graph.get(cid)
        if spec.get("why") and node is not None:
            node.attrs["why"] = spec["why"]
        for writer in spec.get("writers") or []:
            _edge_from_override(graph, "writes", writer, cid)
        for reader in spec.get("readers") or []:
            _edge_from_override(graph, "reads", reader, cid)

    for raw in (data.get("overrides") or {}).get("add_edges") or data.get("add_edges") or []:
        etype = raw.get("type")
        frm = raw.get("from")
        to = raw.get("to")
        if not (etype and frm and to):
            continue
        evidence = None
        if raw.get("evidence"):
            evidence = Evidence.from_dict(raw["evidence"])
        graph.add_edge(
            etype,
            frm,
            to,
            confidence=parse_confidence(raw.get("confidence")),
            evidence=evidence,
        )


def _edge_from_override(graph: Graph, etype: str, spec: Any, target_id: str) -> None:
    if isinstance(spec, str):
        spec = {"symbol": spec}
    symbol = spec.get("symbol") or spec.get("from") or spec.get("id")
    if not symbol:
        return
    if not symbol.startswith(("function:", "entrypoint:", "db_trigger:")):
        from_id = f"function:{symbol}"
        if from_id not in graph.nodes:
            # try matching by suffix
            matches = [nid for nid in graph.nodes if nid.endswith("." + symbol) or nid.endswith(":" + symbol)]
            from_id = matches[0] if len(matches) == 1 else from_id
            if from_id not in graph.nodes:
                graph.add_node(from_id, "function", name=symbol)
    else:
        from_id = symbol
    evidence = Evidence.from_dict(spec.get("evidence")) if spec.get("evidence") else None
    graph.add_edge(
        etype,
        from_id,
        target_id,
        confidence=parse_confidence(spec.get("confidence") or "inferred"),
        evidence=evidence,
        why=spec.get("why"),
    )
    if spec.get("why") and from_id in graph.nodes and not graph.nodes[from_id].attrs.get("why"):
        graph.nodes[from_id].attrs["why"] = spec["why"]
