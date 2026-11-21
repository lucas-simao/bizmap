"""Which service each graph node belongs to (read-only view of the graph).

A service is a repository by default: the `repo/` prefix of merged/joint
ids (`billing/function:app.pay`), else the node's `repo` attribute, else the
graph's own `repo` (single-repo scan). Data nodes without a prefix (tables,
views, collections, columns, DB triggers of a shared `database:`) belong to
no service: several services can use the same table.

`bizmap.yaml` can split one repo (a monorepo) into several services by path
prefix. Only `bizmap export` reads this key; `scan` ignores it::

    export:
      services:
        orders: [services/orders/, libs/orders/]
        billing: services/billing/
        # in a multi-repo graph, scope a prefix to one repo with `repo:`
        admin: "api:cmd/admin/"

A node whose evidence file starts with one of the prefixes moves to that
service (longest prefix wins); anything else stays in its repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

DATA_TYPES = frozenset({"table", "view", "collection", "column", "db_trigger"})
DEFAULT_SERVICE = "app"


class ExportConfigError(ValueError):
    """`export:` in bizmap.yaml is malformed."""


def id_repo(node_id: str) -> str | None:
    """`billing/function:x` -> `billing`; unprefixed ids -> None."""
    head = node_id.split(":", 1)[0]
    return node_id.split("/", 1)[0] if "/" in head else None


def parse_service_split(raw: Any, source: str = "bizmap.yaml") -> list[tuple[str | None, str, str]]:
    """`export.services` -> [(repo | None, path prefix, service)], longest prefix first."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ExportConfigError(f"{source}: `export.services` must map a service name to path prefixes")
    rules: list[tuple[str | None, str, str]] = []
    for name, prefixes in raw.items():
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if not isinstance(prefixes, list) or not all(isinstance(p, str) and p.strip() for p in prefixes):
            raise ExportConfigError(
                f"{source}: `export.services.{name}` must be a path prefix or a list of them"
            )
        for prefix in prefixes:
            repo: str | None = None
            prefix = prefix.strip()
            if ":" in prefix:
                repo, prefix = prefix.split(":", 1)
            rules.append((repo or None, prefix.removeprefix("./"), str(name)))
    rules.sort(key=lambda r: (-len(r[1]), r[1], r[2]))
    return rules


EXPORT_KEYS = ("services", "domains", "domains_by", "openapi")


def load_export_config(config: Path | None) -> dict[str, Any]:
    """`export:` of bizmap.yaml -> {"services": split rules, "domains": rules, "domains_by": str}."""
    from bizmap.export.domains import DomainConfigError, parse_domain_rules

    out: dict[str, Any] = {"services": [], "domains": {}, "domains_by": "path", "openapi": {}}
    export = _export_section(config)
    if not export:
        return out
    out["services"] = parse_service_split(export.get("services"), str(config))
    try:
        out["domains"] = parse_domain_rules(export.get("domains"), str(config))
    except DomainConfigError as exc:
        raise ExportConfigError(str(exc)) from exc
    by = export.get("domains_by", "path")
    if by not in ("path", "folder"):
        raise ExportConfigError(f"{config}: `export.domains_by` must be `path` or `folder`")
    out["domains_by"] = by
    spec = export.get("openapi") or {}
    if isinstance(spec, str):
        spec = {"": spec}
    if not isinstance(spec, dict) or not all(isinstance(v, str) for v in spec.values()):
        raise ExportConfigError(f"{config}: `export.openapi` must map a service to a spec file path")
    out["openapi"] = {str(k): v for k, v in spec.items()}
    return out


def load_service_split(config: Path | None) -> list[tuple[str | None, str, str]]:
    export = _export_section(config)
    return parse_service_split(export.get("services"), str(config)) if export else []


def _export_section(config: Path | None) -> dict[str, Any]:
    if config is None or not config.is_file():
        return {}
    import yaml  # bizmap dependency; imported lazily so the module stays light

    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExportConfigError(f"{config}: invalid YAML ({' '.join(str(exc).split())})") from exc
    if not isinstance(data, dict):
        return {}
    export = data.get("export")
    if export is None:
        return {}
    if not isinstance(export, dict):
        raise ExportConfigError(f"{config}: `export:` must be a mapping ({', '.join(EXPORT_KEYS)})")
    unknown = sorted(str(k) for k in export if k not in EXPORT_KEYS)
    if unknown:
        raise ExportConfigError(
            f"{config}: unknown export key(s): {', '.join(unknown)} (known: {', '.join(EXPORT_KEYS)})"
        )
    return export


def _evidence_file(node: dict[str, Any]) -> str | None:
    ev = node.get("evidence")
    if isinstance(ev, dict) and ev.get("file"):
        return str(ev["file"])
    return None


def assign_services(
    data: dict[str, Any], split: list[tuple[str | None, str, str]] | None = None
) -> dict[str, str | None]:
    """Node id -> service name (None for shared data nodes)."""
    default = str(data.get("repo") or DEFAULT_SERVICE)
    split = split or []
    out: dict[str, str | None] = {}
    for node in data.get("nodes") or []:
        nid = node["id"]
        repo = id_repo(nid) or node.get("repo")
        if repo is None and node.get("type") in DATA_TYPES:
            out[nid] = None
            continue
        repo = str(repo or default)
        service = repo
        if split:
            file = _evidence_file(node)
            if file:
                for rule_repo, prefix, name in split:
                    if rule_repo is not None and rule_repo != repo:
                        continue
                    if file.startswith(prefix):
                        service = name
                        break
        out[nid] = service
    return out
