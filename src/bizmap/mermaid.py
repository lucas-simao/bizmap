from __future__ import annotations

import re

from bizmap.graph import Graph

_SAFE = re.compile(r"[^A-Za-z0-9_]")


def _mid(node_id: str) -> str:
    cleaned = _SAFE.sub("_", node_id)
    if cleaned and cleaned[0].isdigit():
        cleaned = "n_" + cleaned
    return cleaned or "node"


def _label(node) -> str:
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


def render_mermaid(graph: Graph) -> str:
    """Render a mermaid flowchart from the lineage graph."""
    lines = ["flowchart LR"]
    groups = {
        "entrypoints": [],
        "functions": [],
        "data": [],
        "triggers": [],
    }
    for node in graph.nodes.values():
        if node.type == "entrypoint":
            groups["entrypoints"].append(node)
        elif node.type == "function":
            groups["functions"].append(node)
        elif node.type == "db_trigger":
            groups["triggers"].append(node)
        else:
            groups["data"].append(node)

    def emit_group(title: str, nodes: list) -> None:
        if not nodes:
            return
        lines.append(f"  subgraph {title}")
        for node in sorted(nodes, key=lambda n: n.id):
            lines.append(f'    {_mid(node.id)}["{_escape(_label(node))}"]')
        lines.append("  end")

    emit_group("entrypoints", groups["entrypoints"])
    emit_group("functions", groups["functions"])
    emit_group("db_triggers", groups["triggers"])
    emit_group("data", groups["data"])

    for edge in sorted(graph.edges.values(), key=lambda e: e.id):
        if edge.from_id not in graph.nodes or edge.to_id not in graph.nodes:
            continue
        label = f"{edge.type} {edge.confidence.value}"
        lines.append(
            f"  {_mid(edge.from_id)} -->|{_escape(label)}| {_mid(edge.to_id)}"
        )
    return "\n".join(lines) + "\n"
