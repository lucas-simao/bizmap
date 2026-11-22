from __future__ import annotations

from collections import deque

from bizmap.dataflow import why_lines
from bizmap.graph import Graph, collection_id, column_id, table_id, view_id


def _label(graph: Graph, node_id: str) -> str:
    node = graph.get(node_id)
    if not node:
        return node_id
    a = node.attrs
    repo = a.get("repo")
    prefix = f"{repo}/" if repo and repo not in node_id.split(":")[0:1] else ""
    if node.type == "entrypoint":
        kind = a.get("kind")
        if kind == "http":
            text = f"HTTP {a.get('method')} {a.get('path')}"
        elif kind == "scheduled_job":
            text = f"scheduled job {a.get('symbol') or a.get('handler')}"
        elif kind == "firestore":
            text = f"firestore {a.get('event') or a.get('method')} {a.get('collection') or a.get('path')}"
        elif kind == "celery_task":
            text = f"celery task {a.get('symbol') or a.get('path')}"
        elif kind == "worker":
            text = f"worker {a.get('symbol') or a.get('handler')}"
        elif kind == "pubsub":
            text = f"pubsub {a.get('topic') or a.get('path')}"
        elif kind == "cloud_task":
            text = f"cloud task {a.get('symbol') or a.get('path')}"
        elif kind == "firebase_http":
            text = f"firebase HTTP {a.get('path')}"
        elif kind == "firebase_call":
            text = f"firebase call {a.get('path')}"
        else:
            text = str(kind or node_id)
        return f"{prefix}{text}" if repo else text
    if node.type == "function":
        name = str(a.get("name") or node_id.removeprefix("function:"))
        return f"{repo}/{name}" if repo and not name.startswith(str(repo)) else name
    if node.type == "db_trigger":
        return f"db trigger {a.get('name')} on {a.get('table')}"
    if node.type == "collection":
        return f"collection {a.get('name') or node_id.removeprefix('collection:')}"
    if node.type == "view":
        kind = "materialized view" if a.get("materialized") else "view"
        return f"{kind} {a.get('name') or node_id.removeprefix('view:')}"
    if node.type == "queue":
        return f"queue {a.get('kind')} {a.get('name')}"
    return node_id


def _evidence_str(edge) -> str:
    if not edge.evidence:
        return ""
    ev = edge.evidence
    return f"  ({ev.file}:{ev.line} {ev.symbol})"


def _is_schema_fn(graph: Graph, node_id: str) -> bool:
    node = graph.get(node_id)
    if not node:
        return False
    if node.attrs.get("role") == "schema":
        return True
    ev = node.attrs.get("evidence") or {}
    file = str(ev.get("file") or "")
    parts = file.replace("\\", "/").lower().split("/")
    return "alembic" in parts or "migrations" in parts or "migrate" in parts


def _entrypoints_for(graph: Graph, node_id: str) -> list[str]:
    """Walk reverse calls/triggers to find entrypoints that can reach node_id."""
    found: list[str] = []
    seen: set[str] = set()
    q = deque([node_id])
    while q:
        cur = q.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        node = graph.get(cur)
        if node and node.type == "entrypoint":
            found.append(cur)
        for edge in graph.incoming(cur):
            if edge.type in {"calls", "triggers", "enqueues"}:
                q.append(edge.from_id)
    return found


MAX_HTTP_HOPS = 5


def _http_upstream(graph: Graph, ep: str, lines: list[str], indent: str, depth: int, seen: set[str]) -> None:
    """Who calls ``ep`` over HTTP, and who calls *them*, across services:
    `invoked via HTTP from gateway/… ← edge/… ← web/…` (cycles skipped,
    at most MAX_HTTP_HOPS)."""
    for http_edge in sorted(graph.incoming(ep, "calls_http"), key=lambda e: e.from_id):
        caller = http_edge.from_id
        caller_eps = sorted(e for e in _entrypoints_for(graph, caller) if e not in seen)
        where = ""
        if caller_eps:
            shown = ", ".join(_label(graph, e) for e in caller_eps[:3])
            more = f" +{len(caller_eps) - 3}" if len(caller_eps) > 3 else ""
            where = f" (behind {shown}{more})"
        prefix = "invoked via HTTP from" if depth == 0 else "← via HTTP from"
        lines.append(f"{indent}{prefix} {_label(graph, caller)}{where}")
        if depth + 1 >= MAX_HTTP_HOPS:
            continue
        for cep in caller_eps:
            seen.add(cep)
            _http_upstream(graph, cep, lines, indent + "  ", depth + 1, seen)


def _heading_for(graph: Graph, nid: str, table: str = "", column: str = "") -> tuple[str, str] | None:
    node = graph.get(nid)
    if node is None:
        return None
    if node.type == "column":
        dtype = node.attrs.get("data_type") or ""
        extra = f" ({dtype})" if dtype else ""
        return f"column: {node.attrs.get('table')}.{node.attrs.get('name')}{extra}", "column"
    if node.type == "table":
        heading = f"table: {node.attrs.get('name') or table}"
        if column:
            heading += f"  (no column node for {column}; showing table-level lineage)"
        return heading, "table"
    if node.type == "collection":
        return f"collection: {node.attrs.get('name') or table}", "collection"
    if node.type == "view":
        heading = f"view: {node.attrs.get('name') or table}"
        if node.attrs.get("materialized"):
            heading = f"materialized {heading}"
        return heading, "view"
    return None


def ambiguous_targets(graph: Graph, spec: str) -> list[str]:
    """Node ids in *different repos* that `table.column` (or `table`) names in
    a multi-repo graph; empty when it is unique or an exact id."""
    spec = spec.strip()
    if spec in graph.nodes:
        return []
    table, _, column = spec.partition(".")
    low_t, low_c = table.lower(), column.lower()
    if column:
        leafs = {f"column:{low_t}.{low_c}"}
    else:
        leafs = {f"table:{low_t}", f"collection:{low_t}", f"view:{low_t}"}
    hits = sorted(
        nid for nid in graph.nodes if nid in leafs or any(nid.endswith("/" + leaf) for leaf in leafs)
    )
    return hits if len(hits) > 1 else []


def _resolve_target(graph: Graph, spec: str) -> tuple[str, str, str]:
    """Return (node_id, heading, lookup_kind)."""
    if spec in graph.nodes:
        found = _heading_for(graph, spec)
        if found:
            return spec, found[0], found[1]
    table, _, column = spec.partition(".")
    candidates: list[str] = []
    if column:
        candidates.extend(
            [
                column_id(table, column),
                f"{table}/{column_id(table.split('/')[-1], column)}" if "/" in table else "",
            ]
        )
        # repo-qualified column ids
        for nid in graph.nodes:
            if nid.endswith(f"/column:{table.lower()}.{column.lower()}") or nid == column_id(table, column):
                candidates.append(nid)
    candidates.extend(
        [
            table_id(table),
            collection_id(table),
            view_id(table),
        ]
    )
    for nid in graph.nodes:
        if (
            nid.endswith(f"/table:{table.lower()}")
            or nid.endswith(f"/collection:{table.lower()}")
            or nid.endswith(f"/view:{table.lower()}")
        ):
            candidates.append(nid)
    seen: set[str] = set()
    for cid in candidates:
        if not cid or cid in seen:
            continue
        seen.add(cid)
        node = graph.get(cid)
        if node is None:
            continue
        if node.type == "column":
            dtype = node.attrs.get("data_type") or ""
            extra = f" ({dtype})" if dtype else ""
            heading = f"column: {node.attrs.get('table')}.{node.attrs.get('name')}{extra}"
            return cid, heading, "column"
        if node.type == "table":
            heading = f"table: {node.attrs.get('name') or table}"
            if column:
                heading += f"  (no column node for {column}; showing table-level lineage)"
            return cid, heading, "table"
        if node.type == "collection":
            heading = f"collection: {node.attrs.get('name') or table}"
            return cid, heading, "collection"
        if node.type == "view":
            heading = f"view: {node.attrs.get('name') or table}"
            if node.attrs.get("materialized"):
                heading = f"materialized {heading}"
            return cid, heading, "view"
    return "", "", ""


def format_why(graph: Graph, table_dot_column: str) -> str:
    spec = table_dot_column.strip()
    clash = ambiguous_targets(graph, spec)
    if clash:
        raise ValueError(
            f"{spec!r} exists in {len(clash)} repos: {', '.join(clash)}. Pass one of these ids."
        )
    cid, heading, kind = _resolve_target(graph, spec)
    if not cid:
        if "." not in spec:
            raise ValueError("expected TABLE.COLUMN (or a table/collection name)")
        return f"unknown column: {table_dot_column}\n"
    node = graph.get(cid)
    lines: list[str] = [heading]
    if node and node.attrs.get("why"):
        lines.append(f"why: {node.attrs['why']}")
    if node and node.type == "table" and node.attrs.get("policies"):
        lines.append(f"policies: {', '.join(str(p) for p in node.attrs['policies'])}")
    if kind == "view":
        bases: list[str] = []
        for edge in graph.outgoing(cid, "derives_from"):
            b = graph.get(edge.to_id)
            bases.append(str((b.attrs.get("name") if b else None) or edge.to_id))
        if bases:
            lines.append(f"derives from: {', '.join(bases)}")

    writers = list(graph.incoming(cid, "writes"))
    readers = list(graph.incoming(cid, "reads"))
    if kind in {"table", "column"}:
        table_name = ""
        if kind == "column" and node:
            table_name = str(node.attrs.get("table") or "")
        elif node:
            table_name = str(node.attrs.get("name") or "")
        table_nid = table_id(table_name) if table_name else cid
        for vnode in graph.nodes.values():
            if vnode.type != "view":
                continue
            if not any(
                e.to_id == table_nid or e.to_id.endswith("/" + table_nid)
                for e in graph.outgoing(vnode.id, "derives_from")
            ):
                continue
            readers.extend(graph.incoming(vnode.id, "reads"))
            writers.extend(graph.incoming(vnode.id, "writes"))
            vname = str(vnode.attrs.get("name") or "")
            if vname:
                for coln in graph.nodes.values():
                    if coln.type == "column" and str(coln.attrs.get("table") or "") == vname:
                        readers.extend(graph.incoming(coln.id, "reads"))
                        writers.extend(graph.incoming(coln.id, "writes"))
    if kind == "table" and node is not None:
        # a table's lineage is mostly on its columns: who reads/writes any of them
        tname = str(node.attrs.get("name") or "").lower()
        repo_prefix = cid.split("table:", 1)[0]
        for coln in graph.nodes.values():
            if (
                coln.type == "column"
                and str(coln.attrs.get("table") or "").lower() == tname
                and coln.id.startswith(repo_prefix + "column:")
            ):
                readers.extend(graph.incoming(coln.id, "reads"))
                writers.extend(graph.incoming(coln.id, "writes"))
        rank = {"exact": 3, "inferred": 2, "heuristic": 1, "unknown": 0}

        def best_per_source(edges):  # one line per function, strongest edge
            best: dict[str, object] = {}
            for edge in edges:
                prev = best.get(edge.from_id)
                if prev is None or rank[edge.confidence.value] > rank[prev.confidence.value]:
                    best[edge.from_id] = edge
            return list(best.values())

        writers = best_per_source(writers)
        readers = best_per_source(readers)
    if kind == "column":
        table = node.attrs.get("table") if node else spec.split(".", 1)[0]
        tid = table_id(str(table))
        if graph.get(tid) is None:
            for nid in graph.nodes:
                if nid.endswith(f"/table:{str(table).lower()}"):
                    tid = nid
                    break
        col_prefix = tid.split("table:", 1)[0] + f"column:{str(table).lower()}."

        def names_other_columns(fid: str, etype: str) -> bool:
            """``fid`` lists its own columns of this table, and not this one
            (`UPDATE orders SET status = …` does not write `discount_cents`)."""
            own = [e for e in graph.outgoing(fid) if e.type == etype and e.to_id.lower().startswith(col_prefix.lower())]
            return bool(own) and not any(e.to_id == cid for e in own)

        for edge in graph.incoming(tid, "writes"):
            # a table-level copy on a caller (derived) or a writer of other columns is not this column's writer
            if edge.attrs.get("derived") or names_other_columns(edge.from_id, "writes"):
                continue
            writers.append(edge)
        for edge in graph.incoming(tid, "reads"):
            if edge.attrs.get("derived") or names_other_columns(edge.from_id, "reads"):
                continue
            readers.append(edge)

    def split_schema(edges):
        runtime = []
        schema_e = []
        for edge in edges:
            if _is_schema_fn(graph, edge.from_id):
                schema_e.append(edge)
            else:
                runtime.append(edge)
        return runtime, schema_e

    def dump(title: str, edges, *, skip_entrypoints: bool = True) -> None:
        lines.append("")
        lines.append(f"{title}:")
        if not edges:
            lines.append("  (none)")
            return
        edges = sorted(
            edges,
            key=lambda e: ({"exact": 0, "inferred": 1, "heuristic": 2, "unknown": 3}[e.confidence.value], e.from_id),
        )
        for edge in edges:
            src = graph.get(edge.from_id)
            if skip_entrypoints and src and src.type == "entrypoint":
                continue
            why = ""
            if src and src.attrs.get("why"):
                why = f" — {src.attrs['why']}"
            via = edge.attrs.get("via")
            via_s = f" via {via}" if via else ""
            lines.append(
                f"  [{edge.confidence.value}] {_label(graph, edge.from_id)}{_evidence_str(edge)}{via_s}{why}"
            )
            situation = edge.attrs.get("situation")
            if situation:
                if isinstance(situation, list):
                    sit = ", ".join(str(s) for s in situation)
                else:
                    sit = str(situation)
                lines.append(f"      situation: {sit}")
            for ep in _entrypoints_for(graph, edge.from_id):
                ep_node = graph.get(ep)
                ep_why = ""
                if ep_node and ep_node.attrs.get("why"):
                    ep_why = f" — {ep_node.attrs['why']}"
                lines.append(f"      triggered by {_label(graph, ep)}{ep_why}")
                _http_upstream(graph, ep, lines, "      ", 0, {ep})

    runtime_w, schema_w = split_schema(writers)
    runtime_r, schema_r = split_schema(readers)
    dump("writers", runtime_w)
    dump("readers", runtime_r)
    if schema_w or schema_r:
        dump("defined/altered by", schema_w + schema_r)
    lines.extend(why_lines(graph, cid))
    lines.append("")
    return "\n".join(lines)
