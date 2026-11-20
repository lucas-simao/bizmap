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

import io
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
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("nodes"), list)
            or not isinstance(data.get("edges"), list)
        ):
            raise ValueError(f"{self.path} is not a BizMap graph (expected an object with `nodes` and `edges` lists)")
        try:
            graph = Graph.from_dict(data)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.path} is not a BizMap graph ({type(exc).__name__}: {exc})") from exc
        self.graph = graph
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
        except (OSError, ValueError):
            pass  # keep serving the last good graph (JSONDecodeError is a ValueError)


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
# what `call_graph` / `function_data` accept: code, not data
_CODE = {"function", "entrypoint", "queue"}


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
    if is_route:
        # `POST /orders/:id/paid` names the route; its inline handler function
        # (`<module>.POST /orders/:id/paid`) is reached through it
        routes = {nid for nid in found if graph.nodes[nid].type == "entrypoint"}
        if routes:
            found = routes
    return sorted(found)


_KIND_WORDS = {
    "any": "function or route",
    "function": "function",
    "entrypoint": "route",
    "route": "route",
    "table": "table",
    "column": "column",
}


def _resolve(store: GraphStore, ref: str, kind: str) -> str:
    ids = _matches(store, ref, _TYPE_ALIASES[kind])
    if not ids:
        what = _KIND_WORDS.get(kind, kind)
        raise ToolError(f"no {what} matches {ref!r}. Use the `search` tool to find its id.")
    if len(ids) > 1:
        shown = ", ".join(ids[:10]) + (f" (+{len(ids) - 10} more)" if len(ids) > 10 else "")
        raise ToolError(f"{ref!r} matches {len(ids)} nodes: {shown}. Pass one of these ids.")
    return ids[0]


def _resolve_code(store: GraphStore, ref: str) -> str:
    """A function or route (tables/columns go to `table_usage`)."""
    ids = _matches(store, ref, _CODE)
    if not ids:
        data = _matches(store, ref, {"table", "view", "collection", "column"})
        hint = " It names a table/column: use `table_usage`." if data else " Use the `search` tool to find its id."
        raise ToolError(f"no function or route matches {ref!r}.{hint}")
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


def _data_rows(graph: Graph, edges: list[Any], column_limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
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
        cols = [slot["columns"][c] for c in sorted(slot["columns"])]
        row = {"table": slot["table"], "confidence": slot["confidence"], "columns": cols[:column_limit]}
        if len(cols) > column_limit:
            row["columns_total"] = len(cols)
        if slot["evidence"]:
            row["evidence"] = slot["evidence"]
        rows.append(row)
    return rows


# ── tools ────────────────────────────────────────────────────────────────────


def _flag_nested(out: dict[str, Any], keys: tuple[str, ...], nested: str) -> None:
    for key in keys:
        if any(f"{nested}_total" in row for row in out.get(key, [])):
            out["truncated"] = True


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
    node_id = _resolve_code(store, ref)
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
    node_id = _resolve_code(store, ref)
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
    _page(_data_rows(store.graph, writes, limit), limit, out, "writes")
    _page(_data_rows(store.graph, reads, limit), limit, out, "reads")
    _flag_nested(out, ("writes", "reads"), "columns")
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
    nested = 10
    for f in full["functions"]:
        row = _brief(store, f["function"])
        row["changed_lines"] = f["lines"]
        for key, ids in (("entrypoints", f["entrypoints"]), ("writes", f["writes"]), ("reads", f["reads"])):
            values = [_brief(store, e) for e in ids] if key == "entrypoints" else list(ids)
            row[key] = values[:nested]
            if len(values) > nested:
                row[f"{key}_total"] = len(values)
                out["truncated"] = True
        functions.append(row)
    _page(functions, limit, out, "functions")
    _page([_brief(store, e) for e in full["entrypoints"]], limit, out, "entrypoints")
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
    """The exact text sent in `content[0].text` (compact JSON)."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _cut_text(text: str, keep: int) -> str:
    lines = text.splitlines()
    dropped = len(lines) - keep
    return "\n".join(lines[:keep]) + (f"\n… ({dropped} more lines; ask about one function or use `table_usage`)\n" if dropped > 0 else "")


def _fit(payload: dict[str, Any], order: tuple[str, ...] = ()) -> dict[str, Any]:
    """Keep the result under ``MAX_TEXT`` characters by dropping items from
    the end of the lowest-priority list first (``order``), as few as needed
    (binary search), and say so with ``truncated``."""

    def size(obj: Any) -> int:
        return len(_as_text(obj))

    if size(payload) <= MAX_TEXT:
        return payload
    payload = dict(payload)
    payload["truncated"] = True
    keys = [k for k in order if k in payload] + sorted(
        k for k, v in payload.items() if isinstance(v, list | str) and k not in order and k not in {"truncated"}
    )
    for key in keys:
        value = payload[key]
        if isinstance(value, list):
            lo, hi = 0, len(value)
            while lo < hi:  # largest prefix that fits
                mid = (lo + hi + 1) // 2
                if size({**payload, key: value[:mid]}) <= MAX_TEXT:
                    lo = mid
                else:
                    hi = mid - 1
            payload[key] = value[:lo]
        elif isinstance(value, str) and len(value) > 200:
            n_lines = len(value.splitlines())
            lo, hi = 0, n_lines
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if size({**payload, key: _cut_text(value, mid)}) <= MAX_TEXT:
                    lo = mid
                else:
                    hi = mid - 1
            payload[key] = _cut_text(value, lo)
        if size(payload) <= MAX_TEXT:
            return payload
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
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
# which parts of a result give way first when it is over the size cap
_TRIM_ORDER: dict[str, tuple[str, ...]] = {
    "search": ("results",),
    "call_graph": ("entrypoints", "callees", "callers"),
    "function_data": ("reads", "writes"),
    "table_usage": ("entrypoints", "functions"),
    "impact": ("unmapped_files", "reads", "writes", "schema_changes", "markdown", "entrypoints", "functions"),
    "why_column": ("text",),
}


_JSON_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}


def _validate(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Check ``args`` against a tool's inputSchema (types, enums, ranges,
    required, no unknown keys). Returns an error message, or None."""
    props = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in args or args[key] is None or args[key] == "":
            return f"`{key}` is required"
    if schema.get("additionalProperties") is False:
        unknown = sorted(k for k in args if k not in props)
        if unknown:
            return f"unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(props))}"
    for key, value in args.items():
        spec = props.get(key) or {}
        want = spec.get("type")
        py = _JSON_TYPES.get(want)
        if py is None:
            continue
        ok = isinstance(value, py) and not (py is int and isinstance(value, bool))
        if not ok:
            article = "an" if want[0] in "aeiou" else "a"
            return f"`{key}` must be {article} {want}, got {type(value).__name__} {json.dumps(value)[:40]}"
        if "enum" in spec and value not in spec["enum"]:
            return f"`{key}` must be one of: {', '.join(map(str, spec['enum']))}"
        if want == "integer":
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                return f"`{key}` must be an integer from {lo} to {hi}"
    return None


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
        problem = _validate(_SCHEMAS[name], args)
        if problem:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": problem}], "isError": True},
            }
        store.refresh()
        try:
            payload = _fit(handler(store, args), _TRIM_ORDER.get(name, ()))
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


def _safe_handle(store: GraphStore, message: Any) -> dict[str, Any] | None:
    try:
        return handle(store, message)
    except Exception as exc:  # noqa: BLE001 - one bad message must not stop the server
        msg_id = message.get("id") if isinstance(message, dict) else None
        if isinstance(message, dict) and "id" not in message:
            return None
        return _error(msg_id, -32603, f"internal error: {type(exc).__name__}: {exc}")


def serve(store: GraphStore, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
    """Read newline-delimited JSON-RPC from stdin until EOF; answer on stdout.

    Input is read as UTF-8 bytes (invalid sequences replaced) and output is
    ASCII-only JSON, so no input and no stdout encoding can crash the loop;
    a failing message gets a -32603 error and the server keeps going."""
    if stdin is None:
        stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
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
                replies = [r for m in message for r in [_safe_handle(store, m)] if r is not None]
                response = replies or None
            else:
                response = _safe_handle(store, message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=True, sort_keys=True) + "\n")
            stdout.flush()
