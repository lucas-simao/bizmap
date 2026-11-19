from __future__ import annotations

from collections import deque

from bizmap.graph import Graph, column_id, table_id


def _label(graph: Graph, node_id: str) -> str:
    node = graph.get(node_id)
    if not node:
        return node_id
    a = node.attrs
    if node.type == "entrypoint":
        if a.get("kind") == "http":
            return f"HTTP {a.get('method')} {a.get('path')}"
        if a.get("kind") == "scheduled_job":
            return f"scheduled job {a.get('symbol') or a.get('handler')}"
        return str(a.get("kind") or node_id)
    if node.type == "function":
        return str(a.get("name") or node_id.removeprefix("function:"))
    if node.type == "db_trigger":
        return f"db trigger {a.get('name')} on {a.get('table')}"
    return node_id


def _evidence_str(edge) -> str:
    if not edge.evidence:
        return ""
    ev = edge.evidence
    return f"  ({ev.file}:{ev.line} {ev.symbol})"


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
            if edge.type in {"calls", "triggers"}:
                q.append(edge.from_id)
    return found


def format_why(graph: Graph, table_dot_column: str) -> str:
    table, _, column = table_dot_column.partition(".")
    if not column:
        raise ValueError("expected TABLE.COLUMN")
    cid = column_id(table, column)
    node = graph.get(cid)
    lines: list[str] = []
    if node is None:
        tid = table_id(table)
        if graph.get(tid) is None:
            return f"unknown column: {table_dot_column}\n"
        cid = tid
        node = graph.get(tid)
        lines.append(f"table: {table}  (no column node for {column}; showing table-level lineage)")
    else:
        dtype = node.attrs.get("data_type") or ""
        extra = f" ({dtype})" if dtype else ""
        lines.append(f"column: {table}.{column}{extra}")
        if node.attrs.get("why"):
            lines.append(f"why: {node.attrs['why']}")

    writers = list(graph.incoming(cid, "writes"))
    readers = list(graph.incoming(cid, "reads"))
    # Table-level edges (SELECT *, column-less INSERT) also apply to every column.
    if cid.startswith("column:"):
        tid = table_id(table)
        for edge in graph.incoming(tid, "writes"):
            writers.append(edge)
        for edge in graph.incoming(tid, "reads"):
            readers.append(edge)

    def dump(title: str, edges) -> None:
        lines.append("")
        lines.append(f"{title}:")
        if not edges:
            lines.append("  (none)")
            return
        # prefer exact, then inferred, then unknown; de-dupe functions vs entrypoints
        edges = sorted(edges, key=lambda e: ({"exact": 0, "inferred": 1, "unknown": 2}[e.confidence.value], e.from_id))
        for edge in edges:
            src = graph.get(edge.from_id)
            if src and src.type == "entrypoint":
                # shown under triggered-by of the function writers
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

    dump("writers", writers)
    dump("readers", readers)
    lines.append("")
    return "\n".join(lines)
