"""Compact, diff-friendly text summary of a graph.

Used for golden snapshot tests: one line per node / edge / unresolved call,
sorted, without timestamps or line numbers, so a detector change shows up as
a readable ``+``/``-`` diff of links gained and lost.
"""

from __future__ import annotations

import difflib
from typing import Any

from bizmap.graph import Graph


def _edge_line(edge: dict[str, Any]) -> str:
    conf = edge.get("confidence") or ""
    return f"{edge['type']} {edge['from']} -> {edge['to']} [{conf}]"


def _unresolved_line(item: dict[str, Any]) -> str:
    method = item.get("method") or "?"
    url = item.get("url") or "?"
    frm = item.get("from") or "?"
    reason = item.get("reason") or "?"
    return f"{reason} {method} {url} from {frm}"


def summarize(graph: Graph | dict[str, Any]) -> str:
    """Return the snapshot text for a graph (or its ``to_dict()``)."""
    data = graph.to_dict() if isinstance(graph, Graph) else graph
    nodes = sorted(f"{n['type']} {n['id']}" for n in data.get("nodes", []))
    edges = sorted(_edge_line(e) for e in data.get("edges", []))
    unresolved = sorted(_unresolved_line(u) for u in data.get("unresolved_http", []) or [])
    out: list[str] = [f"# nodes ({len(nodes)})", *nodes, f"# edges ({len(edges)})", *edges]
    out += [f"# unresolved_http ({len(unresolved)})", *unresolved]
    return "\n".join(out) + "\n"


def diff(expected: str, actual: str, *, name: str = "snapshot") -> str:
    """Unified diff (expected → actual); empty string when identical."""
    if expected == actual:
        return ""
    lines = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=f"{name} (expected)",
        tofile=f"{name} (actual)",
        lineterm="",
        n=0,
    )
    return "\n".join(lines)
