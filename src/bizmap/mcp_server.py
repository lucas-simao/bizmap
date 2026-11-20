"""Read-only MCP server over a BizMap graph (`bizmap mcp`).

Speaks MCP (JSON-RPC 2.0, one message per line) over stdio. It only reads the
graph JSON produced by `bizmap scan`: no network, no model, no writes, and the
same graph + arguments always give the same answer (results are sorted).

Tools:

- ``search``         find functions, routes, tables, columns by name
- ``call_graph``     who calls a function / what it calls, and the routes that reach it
- ``function_data``  tables/columns a function reads and writes (optionally through callees)
- ``table_usage``    functions and routes that read/write a table or column
- ``impact``         changed functions → routes and data for a unified diff (`bizmap impact`)
- ``why_column``     the `bizmap why` text for TABLE.COLUMN

Output contract (every tool): node items are `{id, type, label, repo?,
evidence: "file:line", why?}`; items that come from an edge also carry
`confidence` (exact > inferred > heuristic > unknown). Lists hold at most
`limit` items (default 20, max 100) next to `<list>_total`, with `truncated`
when anything was cut; the whole result stays under ``MAX_TEXT`` characters.
Order is stable: search by match quality then id; call graphs by depth, edge
type, id; data by table then column; everything else by id.

The graph is reloaded when its file changes, so a long-lived session sees a
fresh `bizmap scan`.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path
from typing import IO, Any

from bizmap import __version__
from bizmap.graph import Graph
from bizmap.impact import compute_impact, render_markdown
from bizmap.why import format_why

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_CALL_EDGES = {"calls", "calls_http", "triggers", "enqueues"}
_DATA_EDGES = {"reads", "writes"}
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# ceiling for one tool result (JSON text); lists are halved until it fits
MAX_TEXT = 20_000

INSTRUCTIONS = (
    "BizMap answers questions about a codebase's data lineage from a graph built by "
    "`bizmap scan`: routes/jobs (entrypoints) → functions → tables and columns, plus HTTP "
    "calls between services. Use `search` to find ids, then `call_graph`, `function_data` "
    "or `table_usage`; `impact` maps a unified diff to affected routes and data. Results "
    "are static analysis: edges carry a confidence (exact > inferred > heuristic > unknown)."
)


class ToolError(Exception):
    """A tool-level failure: reported to the client with ``isError: true``."""


# ── graph access ─────────────────────────────────────────────────────────────


class GraphStore:
    """The graph plus edge indexes, reloaded when the file's mtime/size change."""

    def __init__(self, path: Path, repo_root: Path | None = None) -> None:
        self.path = path
        self.repo_root = repo_root
        self._stamp: tuple[float, int] | None = None
        self.graph = Graph()
        self.out: dict[str, list[Any]] = {}
        self.inc: dict[str, list[Any]] = {}
        self.load()

    def load(self) -> None:
        stat = self.path.stat()
        stamp = (stat.st_mtime, stat.st_size)
        if stamp == self._stamp:
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.graph = Graph.from_dict(data)
        self.out, self.inc = {}, {}
        for edge in self.graph.edges.values():
            self.out.setdefault(edge.from_id, []).append(edge)
            self.inc.setdefault(edge.to_id, []).append(edge)
        for idx in (self.out, self.inc):
            for edges in idx.values():
                edges.sort(key=lambda e: (e.type, e.from_id, e.to_id))
        self._stamp = stamp

    def refresh(self) -> None:
        try:
            self.load()
        except (OSError, json.JSONDecodeError):
            pass  # keep serving the last good graph


_RANK = {"exact": 4, "inferred": 3, "heuristic": 2, "unknown": 1}
_BY_RANK = {v: k for k, v in _RANK.items()}


def _leaf(node_id: str) -> str:
    head = node_id.split(":", 1)[0]
    return node_id.split("/", 1)[1] if "/" in head else node_id


def _repo(node_id: str) -> str | None:
    head = node_id.split(":", 1)[0]
    return node_id.split("/", 1)[0] if "/" in head else None


def _label(graph: Graph, node_id: str) -> str:
    node = graph.get(node_id)
    leaf = _leaf(node_id)
    if node is None:
        return leaf
    a = node.attrs
    if node.type == "entrypoint":
        if a.get("kind") == "http":
            return f"{a.get('method')} {a.get('path')}"
        return f"{a.get('kind')} {a.get('symbol') or a.get('handler') or a.get('path') or ''}".strip()
    if node.type == "function":
        return str(a.get("name") or leaf.removeprefix("function:"))
    if node.type == "column":
        return f"{a.get('table')}.{a.get('name')}"
    return str(a.get("name") or leaf.split(":", 1)[-1])


def _ev_str(file: Any, line: Any) -> str | None:
    if not file:
        return None
    return f"{file}:{line}" if line else str(file)


def _edge_evidence(edge: Any) -> str | None:
    ev = edge.evidence
    return _ev_str(ev.file, ev.line) if ev is not None else None


def _brief(store: GraphStore, node_id: str) -> dict[str, Any]:
    """`{id, type, label, repo?, evidence?, end_line?, why?, method?, path?}`;
    ``evidence`` is `file:line` where the node is defined."""
    graph = store.graph
    node = graph.get(node_id)
    out: dict[str, Any] = {"id": node_id, "label": _label(graph, node_id)}
    if node is None:
        return out
    out["type"] = node.type
    a = node.attrs
    repo = a.get("repo") or _repo(node_id)
    if repo:
        out["repo"] = repo
    ev = a.get("evidence") or {}
    evidence = _ev_str(ev.get("file"), a.get("start_line") or ev.get("line"))
    if evidence:
        out["evidence"] = evidence
        if a.get("end_line"):
            out["end_line"] = a["end_line"]
    if a.get("why"):
        out["why"] = a["why"]
    if node.type == "entrypoint":
        for key in ("kind", "method", "path"):
            if a.get(key):
                out[key] = a[key]
    return out


_TYPE_ALIASES = {
    "function": {"function"},
    "entrypoint": {"entrypoint"},
    "route": {"entrypoint"},
    "table": {"table", "view", "collection"},
    "column": {"column"},
    "any": {"function", "entrypoint", "table", "view", "collection", "column", "queue"},
}


def _matches(store: GraphStore, ref: str, types: set[str]) -> list[str]:
    """Node ids ``ref`` names: an exact id, a repo-qualified id, `GET /x`, a
    table / `table.column`, or a function's name or name suffix."""
    graph = store.graph
    ref = ref.strip()
    if not ref:
        return []
    if ref in graph.nodes and graph.nodes[ref].type in types:
        return [ref]
    found: set[str] = set()
    low = ref.lower()
    parts = ref.split(None, 1)
    is_route = len(parts) == 2 and parts[0].upper() in {
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ALL"
    }
    for nid, node in graph.nodes.items():
        if node.type not in types:
            continue
        leaf = _leaf(nid)
        if leaf == ref or leaf.lower() == low:
            found.add(nid)
            continue
        a = node.attrs
        if node.type == "entrypoint" and is_route:
            if str(a.get("method", "")).upper() == parts[0].upper() and a.get("path") == parts[1]:
                found.add(nid)
        elif node.type == "function":
            name = str(a.get("name") or leaf.removeprefix("function:"))
            if name == ref or name.endswith("." + ref):
                found.add(nid)
        elif node.type in {"table", "view", "collection"}:
            if str(a.get("name", "")).lower() == low:
                found.add(nid)
        elif node.type == "column":
            if f"{a.get('table')}.{a.get('name')}".lower() == low:
                found.add(nid)
    return sorted(found)


def _resolve(store: GraphStore, ref: str, kind: str) -> str:
    ids = _matches(store, ref, _TYPE_ALIASES[kind])
    if not ids:
        raise ToolError(f"no {kind} matches {ref!r}. Use the `search` tool to find its id.")
    if len(ids) > 1:
        shown = ", ".join(ids[:10]) + (f" (+{len(ids) - 10} more)" if len(ids) > 10 else "")
        raise ToolError(f"{ref!r} matches {len(ids)} nodes: {shown}. Pass one of these ids.")
    return ids[0]


def _limit(args: dict[str, Any]) -> int:
    value = args.get("limit", DEFAULT_LIMIT)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIMIT:
        raise ToolError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return value


def _page(rows: list[Any], limit: int, out: dict[str, Any], key: str) -> None:
    """``out[key]`` = first ``limit`` rows, ``out[key + "_total"]`` = all."""
    out[key] = rows[:limit]
    out[f"{key}_total"] = len(rows)
    if len(rows) > limit:
        out["truncated"] = True


def _entrypoints_reaching(store: GraphStore, node_id: str) -> list[tuple[str, str]]:
    """(entrypoint id, confidence) for every entrypoint reaching ``node_id``:
    the confidence of the strongest path (its weakest edge)."""
    graph = store.graph
    best: dict[str, int] = {node_id: 4}
    frontier = [node_id]
    while frontier:
        nxt: list[str] = []
        for cur in frontier:
            for edge in store.inc.get(cur, []):
                if edge.type not in _CALL_EDGES:
                    continue
                rank = min(best[cur], _RANK.get(edge.confidence.value, 1))
                if rank > best.get(edge.from_id, 0):
                    best[edge.from_id] = rank
                    nxt.append(edge.from_id)
        frontier = sorted(set(nxt))
    return sorted(
        (nid, _BY_RANK[r]) for nid, r in best.items()
        if nid != node_id and (n := graph.get(nid)) is not None and n.type == "entrypoint"
    )


def _ep_rows(store: GraphStore, eps: list[tuple[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for nid, conf in eps:
        row = _brief(store, nid)
        row["confidence"] = conf
        rows.append(row)
    return rows


def _table_of(graph: Graph, target: str) -> tuple[str, str | None]:
    node = graph.get(target)
    if node is not None and node.type == "column":
        return str(node.attrs.get("table")), str(node.attrs.get("name"))
    return _label(graph, target), None


def _data_rows(graph: Graph, edges: list[Any]) -> list[dict[str, Any]]:
    """Group data edges by table: `{table, confidence, evidence, columns:
    [{column, confidence, evidence}]}`; strongest confidence wins, the
    first evidence (sorted) is shown. Sorted by table, then column."""
    tables: dict[str, dict[str, Any]] = {}
    for edge in sorted(edges, key=lambda e: (e.to_id, -_RANK.get(e.confidence.value, 1), _edge_evidence(e) or "")):
        table, col = _table_of(graph, edge.to_id)
        conf = edge.confidence.value
        slot = tables.setdefault(table, {"table": table, "confidence": conf, "evidence": None, "columns": {}})
        if _RANK.get(conf, 1) > _RANK.get(slot["confidence"], 1):
            slot["confidence"] = conf
        if slot["evidence"] is None:
            slot["evidence"] = _edge_evidence(edge)
        if col:
            prev = slot["columns"].get(col)
            if prev is None or _RANK.get(conf, 1) > _RANK.get(prev["confidence"], 1):
                slot["columns"][col] = {"column": col, "confidence": conf, "evidence": _edge_evidence(edge)}
    rows = []
    for name in sorted(tables):
        slot = tables[name]
        row = {"table": slot["table"], "confidence": slot["confidence"], "columns": [slot["columns"][c] for c in sorted(slot["columns"])]}
        if slot["evidence"]:
            row["evidence"] = slot["evidence"]
        rows.append(row)
    return rows


# ── tools ────────────────────────────────────────────────────────────────────


def tool_search(store: GraphStore, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("`query` is required, e.g. {\"query\": \"markPaid\"}")
    kind = str(args.get("kind") or "any")
    if kind not in _TYPE_ALIASES:
        raise ToolError(f"`kind` must be one of: {', '.join(sorted(_TYPE_ALIASES))}")
    limit = _limit(args)
    graph = store.graph
    low = query.lower()
    scored: list[tuple[int, str]] = []
    for nid, node in graph.nodes.items():
        if node.type not in _TYPE_ALIASES[kind]:
            continue
        label = _label(graph, nid)
        hay = [label.lower(), _leaf(nid).lower()]
        short = label.lower().rsplit(".", 1)[-1]
        if low in {label.lower(), short}:
            score = 0
        elif label.lower().startswith(low) or short.startswith(low):
            score = 1
        elif any(low in h for h in hay):
            score = 2
        elif node.attrs.get("why") and low in str(node.attrs["why"]).lower():
            score = 3
        else:
            continue
        scored.append((score, nid))
    scored.sort()
    out: dict[str, Any] = {"query": query, "kind": kind, "truncated": False}
    rows = []
    for score, nid in scored:
        row = _brief(store, nid)
        row["match"] = ("exact", "prefix", "substring", "doc")[score]
        rows.append(row)
    _page(rows, limit, out, "results")
    return out


def tool_call_graph(store: GraphStore, args: dict[str, Any]) -> dict[str, Any]:
    ref = str(args.get("function") or "").strip()
    if not ref:
        raise ToolError("`function` is required: an id from `search`, a name, or `METHOD /path`")
    node_id = _resolve(store, ref, "any")
    direction = str(args.get("direction") or "both")
    if direction not in {"callers", "callees", "both"}:
        raise ToolError("`direction` must be callers, callees or both")
    depth = args.get("depth", 1)
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= 5:
        raise ToolError("`depth` must be an integer from 1 to 5")
    limit = _limit(args)

    def walk(forward: bool) -> list[dict[str, Any]]:
        seen = {node_id}
        frontier = [node_id]
        rows: list[dict[str, Any]] = []
        for level in range(1, depth + 1):
            nxt: list[str] = []
            for cur in frontier:
                edges = store.out.get(cur, []) if forward else store.inc.get(cur, [])
                for edge in edges:
                    if edge.type not in _CALL_EDGES:
                        continue
                    other = edge.to_id if forward else edge.from_id
                    if other in seen:
                        continue
                    seen.add(other)
                    nxt.append(other)
                    row = _brief(store, other)
                    row.update({"edge": edge.type, "confidence": edge.confidence.value, "depth": level})
                    call_ev = _edge_evidence(edge)
                    if call_ev:
                        row["call_evidence"] = call_ev
                    if edge.attrs.get("via"):
                        row["via"] = edge.attrs["via"]
                    rows.append(row)
            frontier = sorted(nxt)
        rows.sort(key=lambda r: (r["depth"], r["edge"], r["id"]))
        return rows

    out: dict[str, Any] = {"node": _brief(store, node_id), "direction": direction, "depth": depth, "truncated": False}
    if direction in {"callers", "both"}:
        _page(walk(False), limit, out, "callers")
        _page(_ep_rows(store, _entrypoints_reaching(store, node_id)), limit, out, "entrypoints")
    if direction in {"callees", "both"}:
        _page(walk(True), limit, out, "callees")
    return out


def tool_function_data(store: GraphStore, args: dict[str, Any]) -> dict[str, Any]:
    ref = str(args.get("function") or "").strip()
    if not ref:
        raise ToolError("`function` is required: an id from `search`, a name, or `METHOD /path`")
    node_id = _resolve(store, ref, "any")
    transitive = bool(args.get("transitive", False))
    limit = _limit(args)
    reads: list[Any] = []
    writes: list[Any] = []
    seen: set[str] = set()
    q = deque([node_id])
    while q:
        cur = q.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        for edge in store.out.get(cur, []):
            if edge.type == "reads":
                reads.append(edge)
            elif edge.type == "writes":
                writes.append(edge)
            elif transitive and edge.type in _CALL_EDGES:
                q.append(edge.to_id)
    out: dict[str, Any] = {"node": _brief(store, node_id), "transitive": transitive, "truncated": False}
    _page(_data_rows(store.graph, writes), limit, out, "writes")
    _page(_data_rows(store.graph, reads), limit, out, "reads")
    return out


def tool_table_usage(store: GraphStore, args: dict[str, Any]) -> dict[str, Any]:
    ref = str(args.get("table") or "").strip()
    if not ref:
        raise ToolError("`table` is required: `orders`, or `orders.status` for one column")
    limit = _limit(args)
    graph = store.graph
    if "." in ref and _matches(store, ref, {"column"}):
        targets = [_resolve(store, ref, "column")]
    else:
        table_id = _resolve(store, ref, "table")
        tname = str(graph.nodes[table_id].attrs.get("name") or ref).lower()
        repo = graph.nodes[table_id].attrs.get("repo") or _repo(table_id)
        targets = [table_id] + sorted(
            nid
            for nid, n in graph.nodes.items()
            if n.type == "column"
            and str(n.attrs.get("table", "")).lower() == tname
            and (n.attrs.get("repo") or _repo(nid)) == repo
        )
    by_fn: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    direct_eps: dict[str, str] = {}
    for target in targets:
        for edge in store.inc.get(target, []):
            if edge.type not in _DATA_EDGES:
                continue
            src = graph.get(edge.from_id)
            if src is None:
                continue
            conf = edge.confidence.value
            if src.type == "entrypoint":
                if _RANK.get(conf, 1) > _RANK.get(direct_eps.get(edge.from_id, "unknown"), 0):
                    direct_eps[edge.from_id] = conf
                continue
            if src.type != "function":
                continue
            _t, col = _table_of(graph, target)
            col = col or "*"
            slot = by_fn.setdefault(edge.from_id, {"reads": {}, "writes": {}})[edge.type]
            prev = slot.get(col)
            if prev is None or _RANK.get(conf, 1) > _RANK.get(prev["confidence"], 1):
                slot[col] = {"column": col, "confidence": conf, "evidence": _edge_evidence(edge)}
    functions = []
    for fid in sorted(by_fn):
        row = _brief(store, fid)
        row["writes"] = [by_fn[fid]["writes"][c] for c in sorted(by_fn[fid]["writes"])]
        row["reads"] = [by_fn[fid]["reads"][c] for c in sorted(by_fn[fid]["reads"])]
        row["confidence"] = _BY_RANK[max(_RANK.get(x["confidence"], 1) for x in row["writes"] + row["reads"])]
        functions.append(row)
    eps: dict[str, int] = {k: _RANK.get(v, 1) for k, v in direct_eps.items()}
    for fid, fn_slot in by_fn.items():
        fn_rank = max(_RANK.get(x["confidence"], 1) for d in fn_slot.values() for x in d.values())
        for ep, conf in _entrypoints_reaching(store, fid):
            rank = min(fn_rank, _RANK.get(conf, 1))
            eps[ep] = max(eps.get(ep, 0), rank)
    out: dict[str, Any] = {"target": _brief(store, targets[0]), "truncated": False}
    _page(functions, limit, out, "functions")
    _page(_ep_rows(store, [(e, _BY_RANK[r]) for e, r in sorted(eps.items())]), limit, out, "entrypoints")
    return out


def tool_impact(store: GraphStore, args: dict[str, Any]) -> dict[str, Any]:
    diff = args.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        raise ToolError("`diff` is required: unified diff text, e.g. the output of `git diff --unified=0 main...`")
    prefix = str(args.get("prefix") or ".")
    limit = _limit(args)
    result = compute_impact(store.graph, diff, prefix=prefix, repo_root=store.repo_root)
    full = result.to_dict()
    out: dict[str, Any] = {"truncated": False}
    functions = []
    for f in full["functions"]:
        row = _brief(store, f["function"])
        row["changed_lines"] = f["lines"]
        row["entrypoints"] = [_brief(store, e) for e in f["entrypoints"]]
        row["writes"] = f["writes"]
        row["reads"] = f["reads"]
        functions.append(row)
    _page(functions, limit, out, "functions")
    _page(full["entrypoints"], limit, out, "entrypoints")
    _page(full["writes"], limit, out, "writes")
    _page(full["reads"], limit, out, "reads")
    _page(full["schema_changes"], limit, out, "schema_changes")
    _page(full["unmapped_files"], limit, out, "unmapped_files")
    for key in ("hint", "warnings"):
        if full.get(key):
            out[key] = full[key]
    if args.get("markdown"):
        out["markdown"] = render_markdown(store.graph, result, max_chars=MAX_TEXT // 2)
    return out


def tool_why_column(store: GraphStore, args: dict[str, Any]) -> dict[str, Any]:
    column = str(args.get("column") or "").strip()
    if not column:
        raise ToolError("`column` is required: `table.column`, e.g. `orders.status`")
    try:
        text = format_why(store.graph, column)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if text.startswith("unknown column"):
        raise ToolError(f"{text.strip()}. Use `search` with kind `column` to find it.")
    return {"column": column, "text": text, "truncated": False}


def _as_text(payload: Any) -> str:
    """The exact text sent in `content[0].text`."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the JSON text under ``MAX_TEXT`` characters: halve the longest list
    (or cut the text fields) until it fits, and say so with ``truncated``."""

    def size(obj: Any) -> int:
        return len(_as_text(obj))

    if size(payload) <= MAX_TEXT:
        return payload
    payload = dict(payload)
    payload["truncated"] = True
    for _ in range(64):
        if size(payload) <= MAX_TEXT:
            return payload
        lists = [(k, v) for k, v in payload.items() if isinstance(v, list) and v]
        texts = [(k, v) for k, v in payload.items() if isinstance(v, str) and len(v) > 200]
        if lists:
            key, value = max(lists, key=lambda kv: (size(kv[1]), kv[0]))
            payload[key] = value[: len(value) // 2]
        elif texts:
            key, value = max(texts, key=lambda kv: (len(kv[1]), kv[0]))
            payload[key] = value[: len(value) // 2].rsplit("\n", 1)[0] + "\n… (truncated)"
        else:
            break
    return payload


_REF = {"type": "string", "description": "Node id from `search`, a function name, or `METHOD /path` for a route."}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "title": "Search the lineage graph",
        "description": "Find functions, routes (entrypoints), tables and columns by name, path or doc "
        "comment. Returns ids to pass to the other tools.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to look for (e.g. `markPaid`, `/orders`, `orders.status`)."},
                "kind": {"type": "string", "enum": sorted(_TYPE_ALIASES), "default": "any"},
                "limit": _LIMIT,
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": tool_search,
    },
    {
        "name": "call_graph",
        "title": "Callers and callees",
        "description": "Who calls a function (and which routes/jobs reach it) and what it calls, "
        "across services through HTTP calls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": _REF,
                "direction": {"type": "string", "enum": ["callers", "callees", "both"], "default": "both"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
                "limit": _LIMIT,
            },
            "required": ["function"],
            "additionalProperties": False,
        },
        "handler": tool_call_graph,
    },
    {
        "name": "function_data",
        "title": "Tables and columns a function reads and writes",
        "description": "Tables/columns a function or route reads and writes, grouped by table; with "
        "`transitive`, also through everything it calls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": _REF,
                "transitive": {"type": "boolean", "default": False},
                "limit": _LIMIT,
            },
            "required": ["function"],
            "additionalProperties": False,
        },
        "handler": tool_function_data,
    },
    {
        "name": "table_usage",
        "title": "Who uses a table or column",
        "description": "Functions that read/write a table (or one column, `table.column`) and the "
        "routes/jobs that reach them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "`orders`, or `orders.status` for one column."},
                "limit": _LIMIT,
            },
            "required": ["table"],
            "additionalProperties": False,
        },
        "handler": tool_table_usage,
    },
    {
        "name": "impact",
        "title": "Impact of a diff",
        "description": "Map a unified diff to the changed functions, the routes/jobs that reach them "
        "and the tables/columns they read or write (same logic as `bizmap impact`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "Unified diff text, paths relative to the git root."},
                "prefix": {"type": "string", "default": ".", "description": "Scanned directory relative to the git root (graphs without git)."},
                "markdown": {"type": "boolean", "default": False, "description": "Also return the PR-comment Markdown."},
                "limit": _LIMIT,
            },
            "required": ["diff"],
            "additionalProperties": False,
        },
        "handler": tool_impact,
    },
    {
        "name": "why_column",
        "title": "Why does this column change?",
        "description": "Who writes and reads TABLE.COLUMN and what triggers them (the `bizmap why` text).",
        "inputSchema": {
            "type": "object",
            "properties": {"column": {"type": "string", "description": "`table.column`"}},
            "required": ["column"],
            "additionalProperties": False,
        },
        "handler": tool_why_column,
    },
]
_HANDLERS = {t["name"]: t["handler"] for t in TOOLS}


def _tool_list() -> list[dict[str, Any]]:
    out = []
    for tool in TOOLS:
        entry = {k: v for k, v in tool.items() if k != "handler"}
        entry["annotations"] = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        out.append(entry)
    return out


# ── JSON-RPC / MCP plumbing ──────────────────────────────────────────────────


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(store: GraphStore, message: Any) -> dict[str, Any] | None:
    """One JSON-RPC message in, the response out (None for notifications)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    method = message.get("method")
    msg_id = message.get("id")
    is_notification = "id" not in message
    if not isinstance(method, str):
        return None if is_notification else _error(msg_id, -32600, "invalid request")
    params = message.get("params") or {}
    if is_notification:
        return None  # notifications/initialized, cancelled, ...: nothing to answer
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "bizmap", "title": "BizMap", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_list()}}
    if method == "tools/call":
        if not isinstance(params, dict):
            return _error(msg_id, -32602, "params must be an object")
        name = params.get("name")
        handler = _HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return _error(msg_id, -32602, f"unknown tool: {name!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(msg_id, -32602, "arguments must be an object")
        store.refresh()
        try:
            payload = _fit(handler(store, args))
        except ToolError as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
        text = _as_text(payload)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "structuredContent": payload,
                "isError": False,
            },
        }
    if method in {"resources/list", "prompts/list"}:
        key = method.split("/", 1)[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}
    return _error(msg_id, -32601, f"method not found: {method}")


def serve(store: GraphStore, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
    """Read newline-delimited JSON-RPC from stdin until EOF; answer on stdout.
    Nothing but protocol messages is written to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response: Any = _error(None, -32700, "parse error")
        else:
            if isinstance(message, list):  # batch (older protocol versions)
                replies = [r for m in message for r in [handle(store, m)] if r is not None]
                response = replies or None
            else:
                response = handle(store, message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n")
            stdout.flush()
