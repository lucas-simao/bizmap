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
    services: dict[str, str] = field(default_factory=dict)
    repos: list[str] = field(default_factory=list)


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


def load_scan_config(root: Path, config: Path | None = None) -> ScanConfig:
    path = find_overrides(root, config=config)
    if path is None:
        return ScanConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return ScanConfig(path=path)
    exclude = data.get("exclude") or (data.get("scan") or {}).get("exclude") or []
    if isinstance(exclude, str):
        exclude = [exclude]
    expand = data.get("expand_star")
    if expand is None:
        expand = (data.get("scan") or {}).get("expand_star")
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
    return ScanConfig(
        exclude=[str(x) for x in exclude],
        expand_star=bool(expand),
        path=path,
        raw=data,
        name=str(name) if name else None,
        database=str(database) if database else None,
        services={str(k): str(v) for k, v in services.items()},
        repos=[str(x) for x in repos],
    )


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
