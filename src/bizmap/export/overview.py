"""Service-level summary of a graph, shared by every `bizmap export` format.

- **services**: one row per service (repo, or a `bizmap.yaml` split) with its
  route / function / table counts.
- **connections**: `calls_http` links between services, counted per
  (caller service, callee service) pair.
- **risks** ("many depend on it"): a service called by, or a table read or
  written by, ``RISK_MIN_USERS`` or more *other* services. Sorted by how many
  services depend on it; the first ``RISK_TOP`` are flagged ``top``.

Read-only: works on the graph JSON as loaded, never mutates it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from bizmap.export.services import id_repo

RISK_MIN_USERS = 3
RISK_TOP = 5
DATA_EDGES = frozenset({"reads", "writes"})
TABLE_TYPES = frozenset({"table", "view", "collection"})


def column_tables(nodes: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Column id -> id of its table/view/collection node (when present)."""
    out: dict[str, str] = {}
    for nid, node in nodes.items():
        if node.get("type") != "column":
            continue
        repo = id_repo(nid)
        prefix = f"{repo}/" if repo else ""
        name = str(node.get("table") or nid.split(":", 1)[-1].rsplit(".", 1)[0])
        for kind in ("table", "view", "collection"):
            cand = f"{prefix}{kind}:{name}"
            if cand in nodes:
                out[nid] = cand
                break
    return out


def data_target(nodes: dict[str, dict[str, Any]], col_table: dict[str, str], to_id: str) -> str | None:
    """The table-like node a reads/writes edge lands on (columns -> their table)."""
    node = nodes.get(to_id)
    if node is None:
        return None
    if node.get("type") in TABLE_TYPES:
        return to_id
    return col_table.get(to_id)


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node["id"].split(":", 1)[-1])


def build_overview(data: dict[str, Any], svc_of: dict[str, str | None]) -> dict[str, Any]:
    nodes = {n["id"]: n for n in data.get("nodes") or []}
    col_table = column_tables(nodes)
    names = sorted({s for s in svc_of.values() if s is not None})
    counts: dict[str, dict[str, int]] = {s: {"routes": 0, "jobs": 0, "functions": 0} for s in names}
    for nid, node in nodes.items():
        svc = svc_of.get(nid)
        if svc is None:
            continue
        kind = node.get("type")
        if kind == "entrypoint":
            counts[svc]["routes" if node.get("kind") == "http" else "jobs"] += 1
        elif kind == "function":
            counts[svc]["functions"] += 1

    links: dict[tuple[str, str], int] = defaultdict(int)
    table_users: dict[str, set[str]] = defaultdict(set)
    svc_tables: dict[str, set[str]] = defaultdict(set)
    for edge in data.get("edges") or []:
        etype = edge.get("type")
        src = svc_of.get(edge["from"])
        if etype == "calls_http":
            dst = svc_of.get(edge["to"])
            if src and dst and src != dst:
                links[(src, dst)] += 1
        elif etype in DATA_EDGES and src:
            table = data_target(nodes, col_table, edge["to"])
            if table is not None:
                table_users[table].add(src)
                svc_tables[src].add(table)

    callers: dict[str, set[str]] = defaultdict(set)
    for (src, dst) in links:
        callers[dst].add(src)

    services = []
    for name in names:
        row = {"name": name, **counts[name], "tables": len(svc_tables.get(name, ()))}
        row["calls_out"] = sum(n for (a, _b), n in links.items() if a == name)
        row["calls_in"] = sum(n for (_a, b), n in links.items() if b == name)
        services.append(row)

    risks: list[dict[str, Any]] = []
    for name, users in callers.items():
        if len(users) >= RISK_MIN_USERS:
            risks.append({"kind": "service", "id": name, "name": name, "users": sorted(users)})
    for table, users in table_users.items():
        if len(users) >= RISK_MIN_USERS:
            risks.append(
                {"kind": "table", "id": table, "name": _label(nodes[table]), "users": sorted(users)}
            )
    risks.sort(key=lambda r: (-len(r["users"]), r["kind"], r["name"], r["id"]))
    for i, risk in enumerate(risks):
        risk["count"] = len(risk["users"])
        risk["top"] = i < RISK_TOP

    return {
        "services": services,
        "connections": [
            {"from": a, "to": b, "links": n} for (a, b), n in sorted(links.items())
        ],
        "tables": {t: sorted(u) for t, u in sorted(table_users.items())},
        "risks": risks,
        "totals": {"nodes": len(nodes), "edges": len(data.get("edges") or [])},
    }
