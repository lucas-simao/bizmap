from __future__ import annotations

import re
from collections import deque

from bizmap.graph import Graph, Node, table_id

_SAFE = re.compile(r"[^A-Za-z0-9_]")

DEFAULT_LIMIT = 80


def _mid(node_id: str) -> str:
    cleaned = _SAFE.sub("_", node_id)
    if cleaned and cleaned[0].isdigit():
        cleaned = "n_" + cleaned
    return cleaned or "node"


def _label(node: Node) -> str:
    t = node.type
    a = node.attrs
    if t == "entrypoint":
        if a.get("kind") == "http":
            return f"{a.get('method', '')} {a.get('path', '')}".strip()
        if a.get("kind") == "scheduled_job":
            return f"job {a.get('symbol') or a.get('handler') or node.id}"
        return str(a.get("symbol") or node.id)
    if t == "function":
        return str(a.get("name") or node.id.removeprefix("function:"))
    if t == "column":
        return f"{a.get('table')}.{a.get('name')}"
    if t == "table":
        return str(a.get("name") or node.id.removeprefix("table:"))
    if t == "db_trigger":
        return str(a.get("name") or node.id.removeprefix("db_trigger:"))
    return node.id


def _escape(label: str) -> str:
    return label.replace('"', "'")


def _table_id_of(node_id: str) -> str | None:
    if node_id.startswith("table:"):
        return node_id
    if node_id.startswith("column:"):
        table = node_id.split(":", 1)[1].split(".", 1)[0]
        return table_id(table)
    return None


def _entrypoints_reaching(graph: Graph, node_id: str) -> set[str]:
    found: set[str] = set()
    seen: set[str] = set()
    q = deque([node_id])
    while q:
        cur = q.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        node = graph.get(cur)
        if node and node.type == "entrypoint":
            found.add(cur)
        for edge in graph.incoming(cur):
            if edge.type in {"calls", "triggers"}:
                q.append(edge.from_id)
    return found


def _rollup_edges(graph: Graph) -> dict[tuple[str, str, str], str]:
    """entrypoint -> table edges, rolled up from function/column accesses."""
    rolled: dict[tuple[str, str, str], str] = {}

    def keep(ep: str, kind: str, table: str, conf: str) -> None:
        key = (ep, kind, table)
        rank = {"exact": 3, "inferred": 2, "unknown": 1}
        prev = rolled.get(key)
        if prev is None or rank.get(conf, 0) > rank.get(prev, 0):
            rolled[key] = conf

    for edge in graph.edges.values():
        table = _table_id_of(edge.to_id)
        if table is None or edge.type not in {"reads", "writes"}:
            continue
        src = graph.get(edge.from_id)
        if src is None:
            continue
        if src.type == "entrypoint":
            keep(edge.from_id, edge.type, table, edge.confidence.value)
            continue
        if src.type in {"function", "db_trigger"}:
            for ep in _entrypoints_reaching(graph, edge.from_id):
                keep(ep, edge.type, table, "inferred")
    return rolled


def _emit(
    lines: list[str],
    nodes: list[Node],
    edges: list[tuple[str, str, str, str]],
    *,
    limit: int,
    extra_note: str | None = None,
) -> None:
    entrypoints = [n for n in nodes if n.type == "entrypoint"]
    data = [n for n in nodes if n.type in {"table", "column"}]
    functions = [n for n in nodes if n.type == "function"]
    triggers = [n for n in nodes if n.type == "db_trigger"]

    def take(group: list[Node], remaining: int) -> list[Node]:
        group = sorted(group, key=lambda n: n.id)
        if len(group) <= remaining:
            return group
        return group[: max(0, remaining)]

    selected: list[Node] = []
    remaining = limit
    for group in (entrypoints, data, triggers, functions):
        chunk = take(group, remaining)
        selected.extend(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    selected_ids = {n.id for n in selected}
    truncated = len(nodes) > len(selected) or len(edges) > limit * 4

    def emit_group(title: str, group: list[Node]) -> None:
        present = [n for n in group if n.id in selected_ids]
        if not present:
            return
        lines.append(f"  subgraph {title}")
        for node in sorted(present, key=lambda n: n.id):
            lines.append(f'    {_mid(node.id)}["{_escape(_label(node))}"]')
        lines.append("  end")

    emit_group("entrypoints", entrypoints)
    emit_group("functions", functions)
    emit_group("db_triggers", triggers)
    emit_group("data", data)

    shown = 0
    for frm, kind, to, conf in sorted(edges, key=lambda e: (e[2], e[0], e[1])):
        if frm not in selected_ids or to not in selected_ids:
            continue
        if shown >= limit * 3:
            truncated = True
            break
        lines.append(f"  {_mid(frm)} -->|{_escape(f'{kind} {conf}')}| {_mid(to)}")
        shown += 1
    if truncated:
        lines.append("  %% truncated — use --mermaid-focus TABLE or --mermaid-limit N")
    if extra_note:
        lines.append(f"  %% {extra_note}")


def render_mermaid(
    graph: Graph,
    *,
    focus: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Render a readable mermaid flowchart.

    Default is entrypoint → table rollup (no per-column / per-function explosion).
    ``focus`` restricts the diagram to one table, its columns, writers/readers,
    and the entrypoints that reach them.
    """
    lines = ["flowchart LR"]
    if focus:
        _render_focus(graph, focus, lines, limit)
    else:
        _render_rollup(graph, lines, limit)
    return "\n".join(lines) + "\n"


def _render_rollup(graph: Graph, lines: list[str], limit: int) -> None:
    rolled = _rollup_edges(graph)
    node_ids: set[str] = set()
    edges: list[tuple[str, str, str, str]] = []
    for (ep, kind, table), conf in rolled.items():
        node_ids.add(ep)
        node_ids.add(table)
        edges.append((ep, kind, table, conf))
    # Always include HTTP/job entrypoints even if they have no data edges yet.
    for node in graph.nodes.values():
        if node.type == "entrypoint":
            node_ids.add(node.id)
    nodes = [graph.nodes[i] for i in node_ids if i in graph.nodes]
    _emit(lines, nodes, edges, limit=limit, extra_note="entrypoint→table rollup")


def _render_focus(graph: Graph, focus: str, lines: list[str], limit: int) -> None:
    focus = focus.lower()
    tid = table_id(focus) if not focus.startswith("table:") else focus
    table_name = tid.split(":", 1)[1]
    keep: set[str] = {tid}
    for node in graph.nodes.values():
        if node.type == "column" and str(node.attrs.get("table", "")).lower() == table_name:
            keep.add(node.id)
    # functions / triggers that read/write this table or its columns
    fns: set[str] = set()
    for edge in graph.edges.values():
        if edge.type not in {"reads", "writes"}:
            continue
        if edge.to_id == tid or edge.to_id.startswith(f"column:{table_name}."):
            src = graph.get(edge.from_id)
            if src and src.type in {"function", "db_trigger", "entrypoint"}:
                keep.add(edge.from_id)
                if src.type in {"function", "db_trigger"}:
                    fns.add(edge.from_id)
    for fn in list(fns):
        keep.update(_entrypoints_reaching(graph, fn))
    # include call edges among kept functions
    nodes = [graph.nodes[i] for i in keep if i in graph.nodes]
    edges: list[tuple[str, str, str, str]] = []
    for edge in graph.edges.values():
        if edge.from_id in keep and edge.to_id in keep:
            if edge.type in {"reads", "writes", "triggers", "calls"}:
                edges.append((edge.from_id, edge.type, edge.to_id, edge.confidence.value))
    _emit(
        lines,
        nodes,
        edges,
        limit=limit,
        extra_note=f"focus {table_name}",
    )
