"""Merge per-repo graphs into one registry graph with qualified ids."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bizmap.graph import (
    GRAPH_VERSION,
    Edge,
    Graph,
    Node,
    edge_id,
)

SHARED_PREFIXES = ("table:", "column:", "collection:", "db_trigger:", "view:")


def repo_name_of(graph: Graph, fallback: str = "repo") -> str:
    return str(graph.repo or graph.attrs.get("repo") or fallback)


def database_of(graph: Graph) -> str | None:
    db = graph.database or graph.attrs.get("database")
    return str(db) if db else None


def _qualify_id(node_id: str, repo: str, shared: bool) -> str:
    if node_id.startswith(f"{repo}/"):
        return node_id
    if shared and node_id.startswith((*SHARED_PREFIXES, "queue:pubsub:")):
        return node_id
    if node_id.startswith("table:unknown"):
        return f"{repo}/{node_id}" if not shared else node_id
    return f"{repo}/{node_id}"


# a Pub/Sub topic is the same everywhere in a project: producer and push
# subscription in two repos meet on one node
GLOBAL_PREFIXES = ("queue:pubsub:",)


def _is_shared_node(node_id: str, share_data: bool) -> bool:
    if node_id.startswith(GLOBAL_PREFIXES):
        return True
    if not share_data:
        return False
    return node_id.startswith(SHARED_PREFIXES)


def qualify_graph(graph: Graph, repo: str | None = None, *, share_data: bool = False) -> Graph:
    """Return a copy whose function/entrypoint/queue ids are repo-qualified."""
    repo = repo or repo_name_of(graph)
    out = Graph()
    out.version = graph.version
    out.generated_at = graph.generated_at
    out.schema = graph.schema
    out.repo = repo
    out.database = graph.database
    out.attrs = dict(graph.attrs)
    out.attrs["repo"] = repo
    if graph.database:
        out.attrs["database"] = graph.database
    id_map: dict[str, str] = {}
    for nid, node in graph.nodes.items():
        new_id = _qualify_id(nid, repo, _is_shared_node(nid, share_data))
        id_map[nid] = new_id
        attrs = dict(node.attrs)
        attrs.setdefault("repo", repo)
        out.nodes[new_id] = Node(id=new_id, type=node.type, attrs=attrs)
    for edge in graph.edges.values():
        frm = id_map.get(edge.from_id, _qualify_id(edge.from_id, repo, _is_shared_node(edge.from_id, share_data)))
        to = id_map.get(edge.to_id, _qualify_id(edge.to_id, repo, _is_shared_node(edge.to_id, share_data)))
        eid = edge_id(edge.type, frm, to)
        attrs = dict(edge.attrs)
        if edge.type == "feeds":
            from bizmap.dataflow import qualify_feed_attrs

            attrs = qualify_feed_attrs(
                attrs,
                lambda i: id_map.get(i) or _qualify_id(i, repo, _is_shared_node(i, share_data)),
            )
        out.edges[eid] = Edge(
            id=eid,
            type=edge.type,
            from_id=frm,
            to_id=to,
            confidence=edge.confidence,
            evidence=edge.evidence,
            attrs=attrs,
        )
    return out


def merge_graphs(graphs: list[Graph], *, version: str | None = None) -> Graph:
    """Merge graphs. Tables/collections share ids when ``database`` matches."""
    merged = Graph()
    merged.version = version or GRAPH_VERSION
    merged.generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos: list[str] = []
    databases: set[str] = set()
    schema_tables: dict[str, Any] = {}
    schema_views: dict[str, Any] = {}
    for graph in graphs:
        db = database_of(graph)
        if db:
            databases.add(db)
    share_all = len(databases) == 1 and all(database_of(g) == next(iter(databases)) for g in graphs if database_of(g))
    # If every graph that declares a database uses the same name, share data nodes.
    declared = [database_of(g) for g in graphs]
    share_data = len({d for d in declared if d}) == 1 and any(declared)

    for graph in graphs:
        repo = repo_name_of(graph, fallback="repo")
        repos.append(repo)
        share = share_data and (database_of(graph) is not None or share_all)
        qualified = qualify_graph(graph, repo, share_data=share)
        for tname, tspec in (qualified.schema.get("tables") or {}).items():
            if tname not in schema_tables:
                schema_tables[tname] = tspec
            else:
                cols = dict(schema_tables[tname].get("columns") or {})
                cols.update(tspec.get("columns") or {})
                schema_tables[tname]["columns"] = cols
                fks = list(schema_tables[tname].get("foreign_keys") or [])
                seen = {(tuple(fk.get("columns") or []), fk.get("ref_table")) for fk in fks}
                for fk in tspec.get("foreign_keys") or []:
                    sig = (tuple(fk.get("columns") or []), fk.get("ref_table"))
                    if sig not in seen:
                        fks.append(fk)
                        seen.add(sig)
                schema_tables[tname]["foreign_keys"] = fks
                pols = list(schema_tables[tname].get("policies") or [])
                names = {p.get("name") for p in pols}
                for p in tspec.get("policies") or []:
                    if p.get("name") not in names:
                        pols.append(p)
                if pols:
                    schema_tables[tname]["policies"] = pols
        for vname, vspec in (qualified.schema.get("views") or {}).items():
            if vname not in schema_views:
                schema_views[vname] = vspec
        for nid, node in qualified.nodes.items():
            existing = merged.nodes.get(nid)
            if existing:
                for k, v in node.attrs.items():
                    if v in (None, "", [], {}):
                        continue
                    if existing.attrs.get(k) in (None, "", [], {}):
                        existing.attrs[k] = v
                continue
            merged.nodes[nid] = Node(id=nid, type=node.type, attrs=dict(node.attrs))
        for edge in qualified.edges.values():
            if edge.type == "feeds" and edge.id in merged.edges:
                from bizmap.dataflow import merge_feed_attrs

                existing = merged.edges[edge.id]
                merge_feed_attrs(existing.attrs, edge.attrs)
                if edge.confidence.value == "exact":
                    existing.confidence = edge.confidence
                continue
            merged.add_edge(
                edge.type,
                edge.from_id,
                edge.to_id,
                confidence=edge.confidence,
                evidence=edge.evidence,
                **edge.attrs,
            )
    merged.schema = {"tables": schema_tables}
    if schema_views:
        merged.schema["views"] = schema_views
    merged.attrs["repos"] = repos
    skipped: dict[str, int] = {}
    for graph in graphs:
        for key, value in (graph.attrs.get("skipped") or {}).items():
            skipped[key] = skipped.get(key, 0) + int(value)
    if skipped:
        merged.attrs["skipped"] = dict(sorted(skipped.items()))
    in_process = sum(int(g.attrs.get("in_process_http") or 0) for g in graphs)
    if in_process:
        merged.attrs["in_process_http"] = in_process
    skipped_paths: dict[str, list[str]] = {}
    for graph in graphs:
        prefix = f"{repo_name_of(graph)}:" if len(graphs) > 1 else ""
        for key, paths in (graph.attrs.get("skipped_paths") or {}).items():
            skipped_paths.setdefault(key, []).extend(
                p if (prefix and str(p).startswith(prefix)) else prefix + str(p) for p in paths
            )
    if skipped_paths:
        merged.attrs["skipped_paths"] = {k: sorted(v) for k, v in sorted(skipped_paths.items())}
    roots: dict[str, str] = {}
    bases: set[str] = set()
    for graph in graphs:
        name = repo_name_of(graph)
        for k, v in (graph.attrs.get("repo_roots") or {}).items():
            roots.setdefault(str(k or name), str(v))
        if graph.attrs.get("repo_roots"):
            bases.add(str(graph.attrs.get("repo_roots_base") or "scan"))
    if roots:
        merged.attrs["repo_roots"] = roots
        merged.attrs["repo_roots_base"] = "git" if bases == {"git"} else "scan"
    services: dict = {}
    queues: dict = {}
    for graph in graphs:
        for k, v in (graph.attrs.get("services") or {}).items():
            services.setdefault(k, v)
        for k, v in (graph.attrs.get("queues") or {}).items():
            queues.setdefault(k, v)
    if services:
        merged.attrs["services"] = services
    if queues:
        merged.attrs["queues"] = queues
    if len(databases) == 1:
        merged.database = next(iter(databases))
        merged.attrs["database"] = merged.database
    return merged


def graph_from_json(data: dict[str, Any]) -> Graph:
    graph = Graph.from_dict(data)
    graph.repo = data.get("repo")
    graph.database = data.get("database")
    graph.attrs["repo"] = graph.repo
    graph.attrs["database"] = graph.database
    graph.attrs["repos"] = data.get("repos") or []
    graph.attrs["services"] = data.get("services") or {}
    graph.attrs["repo_roots"] = data.get("repo_roots") or {}
    graph.attrs["repo_roots_base"] = data.get("repo_roots_base")
    graph.attrs["skipped"] = data.get("skipped") or {}
    graph.attrs["skipped_paths"] = data.get("skipped_paths") or {}
    graph.attrs["in_process_http"] = int(data.get("in_process_http") or 0)
    graph.attrs["queues"] = data.get("queues") or {}
    graph.attrs["unresolved_http"] = data.get("unresolved_http") or []
    return graph
