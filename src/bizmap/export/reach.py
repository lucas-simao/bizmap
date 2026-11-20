"""Who reads or writes a table, across services (Python side of the export filter).

This is the multi-hop traversal of the MCP ``table_usage`` tool, without its
page limit: functions with a ``reads``/``writes`` edge to the table or one of
its columns, and every entrypoint that reaches one of those functions through
``calls`` / ``calls_http`` / ``triggers`` / ``enqueues`` (so a front-end route
in one repo reaches a table written by another repo's handler).

The HTML page runs the same traversal in the browser (``reach.js``); a test
keeps the two in step with ``tool_table_usage``. When 0.4.3 ships its own
reach through ``calls_http``, swap the body of :func:`table_reach` (and
``reach.js``) for it: callers only rely on the returned shape.
"""

from __future__ import annotations

from typing import Any

from bizmap.graph import Graph
from bizmap.mcp_server import (
    _BY_RANK,
    _DATA_EDGES,
    _RANK,
    GraphStore,
    _entrypoints_reaching,
    _repo,
)


def store_from_dict(data: dict[str, Any]) -> GraphStore:
    """A read-only :class:`GraphStore` over an already-loaded graph dict."""
    store = GraphStore.__new__(GraphStore)
    store.path = None  # type: ignore[assignment]
    store.repo_root = None
    store._stamp = None
    store.graph = Graph.from_dict(data)
    store.out, store.inc = {}, {}
    for edge in store.graph.edges.values():
        store.out.setdefault(edge.from_id, []).append(edge)
        store.inc.setdefault(edge.to_id, []).append(edge)
    for idx in (store.out, store.inc):
        for edges in idx.values():
            edges.sort(key=lambda e: (e.type, e.from_id, e.to_id))
    return store


def table_reach(store: GraphStore, table_id: str) -> dict[str, Any]:
    """``{"functions": {fn_id: {"reads": rank, "writes": rank}},
    "entrypoints": {ep_id: confidence}, "via": {ep_id: [fn_id, ...]}}``.

    Ranks/confidences follow ``table_usage``: a function's confidence is its
    strongest edge to the table; an entrypoint's is the best over the
    functions it reaches of min(path confidence, function confidence).
    """
    graph = store.graph
    node = graph.nodes[table_id]
    tname = str(node.attrs.get("name") or table_id.split(":", 1)[-1]).lower()
    repo = node.attrs.get("repo") or _repo(table_id)
    targets = [table_id] + sorted(
        nid
        for nid, n in graph.nodes.items()
        if n.type == "column"
        and str(n.attrs.get("table", "")).lower() == tname
        and (n.attrs.get("repo") or _repo(nid)) == repo
    )
    functions: dict[str, dict[str, int]] = {}
    direct: dict[str, int] = {}
    for target in targets:
        for edge in store.inc.get(target, []):
            if edge.type not in _DATA_EDGES:
                continue
            src = graph.get(edge.from_id)
            if src is None:
                continue
            rank = _RANK.get(edge.confidence.value, 1)
            if src.type == "entrypoint":
                # same comparison as table_usage (an `unknown`-only direct edge is not kept)
                if rank > direct.get(edge.from_id, 1):
                    direct[edge.from_id] = rank
                continue
            if src.type != "function":
                continue
            slot = functions.setdefault(edge.from_id, {})
            slot[edge.type] = max(slot.get(edge.type, 0), rank)
    eps: dict[str, int] = dict(direct)
    via: dict[str, set[str]] = {}
    for fid, slot in functions.items():
        fn_rank = max(slot.values())
        for ep, conf in _entrypoints_reaching(store, fid):
            rank = min(fn_rank, _RANK.get(conf, 1))
            eps[ep] = max(eps.get(ep, 0), rank)
            via.setdefault(ep, set()).add(fid)
    return {
        "functions": {k: functions[k] for k in sorted(functions)},
        "entrypoints": {k: _BY_RANK[eps[k]] for k in sorted(eps)},
        "via": {k: sorted(v) for k, v in sorted(via.items())},
    }
