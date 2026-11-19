from __future__ import annotations

import re
from collections import deque

from bizmap.graph import Graph, Node, table_id, view_id

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
        if a.get("kind") in {"http", "firebase_http", "firebase_call", "cloud_task"}:
            method = a.get("method") or ""
            path = a.get("path") or ""
            return f"{method} {path}".strip() or str(a.get("symbol") or node.id)
        if a.get("kind") == "scheduled_job":
            return f"job {a.get('symbol') or a.get('handler') or node.id}"
        if a.get("kind") == "firestore":
            return f"firestore {a.get('collection') or a.get('path') or a.get('symbol') or node.id}"
        if a.get("kind") == "worker":
            return f"worker {a.get('symbol') or a.get('handler') or node.id}"
        if a.get("kind") == "celery_task":
            return f"celery {a.get('symbol') or a.get('path') or node.id}"
        if a.get("kind") == "pubsub":
            return f"pubsub {a.get('topic') or a.get('path') or node.id}"
        return str(a.get("symbol") or node.id)
    if t == "function":
        return str(a.get("name") or node.id.removeprefix("function:"))
    if t == "column":
        return f"{a.get('table')}.{a.get('name')}"
    if t == "table":
        return str(a.get("name") or node.id.removeprefix("table:"))
    if t == "view":
        prefix = "mat " if a.get("materialized") else "view "
        return prefix + str(a.get("name") or node.id.removeprefix("view:"))
    if t == "db_trigger":
        return str(a.get("name") or node.id.removeprefix("db_trigger:"))
    if t == "collection":
        return f"coll {a.get('name') or node.id.removeprefix('collection:')}"
    if t == "queue":
        return f"{a.get('kind') or 'queue'} {a.get('name') or node.id}"
    return node.id


def _escape(label: str) -> str:
    return label.replace('"', "'")


def _table_id_of(node_id: str) -> str | None:
    # repo-qualified ids: catalog/table:orders
    leaf = node_id.split("/")[-1] if "/" in node_id else node_id
    prefix = node_id[: -len(leaf)] if leaf != node_id else ""
    if leaf.startswith("table:"):
        return node_id
    if leaf.startswith("collection:"):
        return node_id
    if leaf.startswith("view:"):
        return node_id
    if leaf.startswith("column:"):
        table = leaf.split(":", 1)[1].split(".", 1)[0]
        return prefix + table_id(table)
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
            if edge.type in {"calls", "triggers", "enqueues", "calls_http"}:
                q.append(edge.from_id)
    return found


def _rollup_edges(graph: Graph) -> dict[tuple[str, str, str], str]:
    """entrypoint -> table edges, rolled up from function/column accesses."""
    rolled: dict[tuple[str, str, str], str] = {}

    def keep(ep: str, kind: str, table: str, conf: str) -> None:
        key = (ep, kind, table)
        rank = {"exact": 4, "inferred": 3, "heuristic": 2, "unknown": 1}
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


def _is_multi_repo(graph: Graph) -> bool:
    repos = [str(r) for r in (graph.attrs.get("repos") or []) if r]
    if len(set(repos)) >= 2:
        return True
    seen = {
        str(n.attrs.get("repo") or "")
        for n in graph.nodes.values()
        if n.attrs.get("repo")
    }
    return len(seen) >= 2


def _node_repo(node: Node, graph: Graph) -> str:
    repo = str(node.attrs.get("repo") or "")
    if repo:
        return repo
    nid = node.id
    if "/" in nid:
        prefix, rest = nid.split("/", 1)
        leaf = rest.split(":", 1)[0] if ":" in rest else rest
        if leaf in {
            "entrypoint",
            "function",
            "queue",
            "table",
            "column",
            "collection",
            "view",
            "db_trigger",
        }:
            return prefix
    return str(graph.repo or graph.attrs.get("repo") or "")


def _handler_entrypoints(graph: Graph) -> dict[str, list[str]]:
    """function id → HTTP entrypoints that trigger it."""
    out: dict[str, list[str]] = {}
    for edge in graph.edges.values():
        if edge.type != "triggers":
            continue
        src = graph.get(edge.from_id)
        dst = graph.get(edge.to_id)
        if src is None or dst is None:
            continue
        if src.type == "entrypoint" and dst.type == "function":
            out.setdefault(edge.to_id, []).append(edge.from_id)
    for fid in out:
        out[fid] = sorted(out[fid])
    return out


def _rollup_http_edges(graph: Graph) -> tuple[set[str], list[tuple[str, str, str, str]]]:
    """calls_http from a function or entrypoint to another entrypoint.

    Handler functions roll up to their HTTP entrypoint so a joint scan shows
    ``web.checkout → api POST /orders → billing POST /invoices``.
    """
    handlers = _handler_entrypoints(graph)
    node_ids: set[str] = set()
    edges: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in graph.edges.values():
        if edge.type != "calls_http":
            continue
        frm = edge.from_id
        eps = handlers.get(frm) or []
        if len(eps) == 1:
            frm = eps[0]
        node_ids.add(frm)
        node_ids.add(edge.to_id)
        key = (frm, edge.to_id)
        if key in seen:
            continue
        seen.add(key)
        edges.append((frm, "calls_http", edge.to_id, edge.confidence.value))
    return node_ids, edges


def _subgraph_title(name: str) -> str:
    cleaned = _SAFE.sub("_", name or "repo")
    if cleaned and cleaned[0].isdigit():
        cleaned = "r_" + cleaned
    return cleaned or "repo"


def _emit_by_repo(
    lines: list[str],
    nodes: list[Node],
    edges: list[tuple[str, str, str, str]],
    graph: Graph,
    *,
    limit: int,
    extra_note: str | None = None,
) -> None:
    by_repo: dict[str, list[Node]] = {}
    for node in nodes:
        by_repo.setdefault(_node_repo(node, graph) or "shared", []).append(node)

    def take(group: list[Node], remaining: int) -> list[Node]:
        group = sorted(group, key=lambda n: n.id)
        if len(group) <= remaining:
            return group
        return group[: max(0, remaining)]

    selected: list[Node] = []
    remaining = limit
    for repo in sorted(by_repo):
        chunk = take(by_repo[repo], remaining)
        selected.extend(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    selected_ids = {n.id for n in selected}
    truncated = len(nodes) > len(selected) or len(edges) > limit * 4

    for repo in sorted(by_repo):
        present = [n for n in by_repo[repo] if n.id in selected_ids]
        if not present:
            continue
        title = _subgraph_title(repo)
        lines.append(f"  subgraph {title}")
        for node in sorted(present, key=lambda n: n.id):
            lines.append(f'    {_mid(node.id)}["{_escape(_label(node))}"]')
        lines.append("  end")

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


def _emit(
    lines: list[str],
    nodes: list[Node],
    edges: list[tuple[str, str, str, str]],
    *,
    limit: int,
    extra_note: str | None = None,
) -> None:
    entrypoints = [n for n in nodes if n.type == "entrypoint"]
    data = [n for n in nodes if n.type in {"table", "column", "collection", "queue", "view"}]
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
    A multi-repo / joint scan also draws ``calls_http`` edges between services
    (function or entrypoint in repo A → entrypoint in repo B), grouped in one
    subgraph per repo.
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
    if _is_multi_repo(graph):
        http_ids, http_edges = _rollup_http_edges(graph)
        node_ids.update(http_ids)
        edges.extend(http_edges)
        nodes = [graph.nodes[i] for i in node_ids if i in graph.nodes]
        _emit_by_repo(
            lines,
            nodes,
            edges,
            graph,
            limit=limit,
            extra_note="cross-service calls_http + entrypoint→table rollup",
        )
        return
    nodes = [graph.nodes[i] for i in node_ids if i in graph.nodes]
    _emit(lines, nodes, edges, limit=limit, extra_note="entrypoint→table rollup")


def _render_focus(graph: Graph, focus: str, lines: list[str], limit: int) -> None:
    focus = focus.lower()
    if focus.startswith("view:"):
        tid = focus
    elif focus.startswith("table:"):
        tid = focus
    elif view_id(focus) in graph.nodes:
        tid = view_id(focus)
    else:
        tid = table_id(focus)
    table_name = tid.split(":", 1)[1]
    keep: set[str] = {tid}
    if tid in graph.nodes and graph.nodes[tid].type == "view":
        for edge in graph.outgoing(tid, "derives_from"):
            keep.add(edge.to_id)
    for node in graph.nodes.values():
        if node.type == "column" and str(node.attrs.get("table", "")).lower() == table_name:
            keep.add(node.id)
        if node.type == "view":
            if any(
                e.to_id == tid or e.to_id.endswith("/" + tid)
                for e in graph.outgoing(node.id, "derives_from")
            ):
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
            if edge.type in {
                "reads",
                "writes",
                "triggers",
                "calls",
                "calls_http",
                "enqueues",
                "derives_from",
            }:
                edges.append((edge.from_id, edge.type, edge.to_id, edge.confidence.value))
    _emit(
        lines,
        nodes,
        edges,
        limit=limit,
        extra_note=f"focus {table_name}",
    )
