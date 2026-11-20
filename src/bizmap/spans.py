"""Source spans for function nodes that the detectors did not measure.

Detectors record ``end_line`` for named functions. Inline handlers
(``router.get('/x', async (req, res) => {...})``, ``e.GET("/x", func(c) {...})``),
module pseudo-functions (``<module>.module``) and SQL functions from
migrations are filled here by re-reading the file the node's evidence points
at, so a diff line inside any of them maps to a node (``bizmap impact``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bizmap.graph import Evidence, Graph

_FN_TYPES = {
    # Go
    "function_declaration",
    "method_declaration",
    "func_literal",
    # Python
    "function_definition",
    "lambda",
    # JS / TS
    "arrow_function",
    "function",
    "function_expression",
    "generator_function",
    "generator_function_declaration",
    "method_definition",
}
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _parser_for(path: Path):  # noqa: ANN202 - tree-sitter Parser
    suffix = path.suffix.lower()
    if suffix == ".go":
        from bizmap.detect.go_lang import _parser

        return _parser()
    if suffix == ".py":
        from bizmap.detect.python_lang import _parser

        return _parser()
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}:
        from bizmap.detect.js_lang import _parser_for as js_parser

        return js_parser(path)
    return None


def _walk(node: Any):
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(reversed(cur.children))


def _code_span(tree: Any, line: int) -> tuple[int, int] | None:
    """Span of the function that the node at ``line`` (1-based) stands for."""
    row = line - 1
    starting: list[Any] = []
    fns_on_line: list[Any] = []
    for node in _walk(tree.root_node):
        if node.start_point[0] > row or node.end_point[0] < row:
            continue
        if node.start_point[0] == row:
            starting.append(node)
            if node.type in _FN_TYPES:
                fns_on_line.append(node)
    if fns_on_line:
        best = max(fns_on_line, key=lambda n: n.end_point[0] - n.start_point[0])
        return best.start_point[0] + 1, best.end_point[0] + 1
    # `router.get(\n  '/x',\n  async (req, res) => {...})`: the literal starts
    # below the registration line; take the largest one inside that statement.
    for node in sorted(starting, key=lambda n: n.end_point[0] - n.start_point[0], reverse=True):
        inner = [n for n in _walk(node) if n.type in _FN_TYPES]
        if inner:
            best = max(inner, key=lambda n: n.end_point[0] - n.start_point[0])
            return node.start_point[0] + 1, best.end_point[0] + 1
    # anchored to a call, not a definition (`supabase.rpc(...)` inside a
    # handler): the line itself, so the enclosing handler still owns the rest
    return line, line


def _sql_span(lines: list[str], line: int) -> tuple[int, int] | None:
    """`CREATE FUNCTION ... AS $tag$ ... $tag$ ...;` starting at ``line``."""
    i = line - 1
    if i < 0 or i >= len(lines):
        return None
    tag: str | None = None
    for j in range(i, len(lines)):
        text = lines[j]
        if tag is None:
            m = _DOLLAR.search(text)
            if m:
                tag = m.group(0)
                rest = text[m.end():]
                if tag in rest:
                    tail = rest[rest.index(tag) + len(tag):]
                    if ";" in tail:
                        return line, j + 1
                    tag = ""  # closed on this line; wait for ';'
                continue
            if ";" in text:
                return line, j + 1
        elif tag == "":
            if ";" in text:
                return line, j + 1
        elif tag in text:
            tail = text[text.index(tag) + len(tag):]
            if ";" in tail:
                return line, j + 1
            tag = ""
    return line, len(lines)


def _load(cache: dict[str, tuple[list[str], Any]], root: Path, rel: str) -> tuple[list[str], Any]:
    if rel not in cache:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            cache[rel] = ([], None)
            return cache[rel]
        tree = None
        parser = _parser_for(path)
        if parser is not None:
            try:
                tree = parser.parse(text.encode("utf-8"))
            except Exception:  # pragma: no cover - parser failure
                tree = None
        cache[rel] = (text.splitlines(), tree)
    return cache[rel]


def fill_spans(graph: Graph, root: Path, cache: dict[str, tuple[list[str], Any]] | None = None) -> None:
    """Set ``end_line`` (and ``start_line`` when it differs from the evidence
    line) on function nodes that lack one. Files are read once each."""
    cache = {} if cache is None else cache
    for node in graph.nodes.values():
        if node.type != "function" or isinstance(node.attrs.get("end_line"), int):
            continue
        ev = node.attrs.get("evidence") or {}
        rel, line = ev.get("file"), ev.get("line")
        if not rel or not isinstance(line, int):
            continue
        path = root / str(rel)
        lines, tree = _load(cache, root, str(rel))
        if not lines:
            continue
        name = str(node.attrs.get("name") or node.id)
        span: tuple[int, int] | None
        if name.endswith(".module"):
            # top-level code: the whole file; real functions inside are
            # smaller spans and win in the innermost-span rule
            span = (1, len(lines))
        elif path.suffix.lower() == ".sql":
            span = _sql_span(lines, line)
        elif tree is not None:
            span = _code_span(tree, line)
        else:
            span = None
        if span is None:
            continue
        start, end = span
        if end < start:
            continue
        node.attrs["end_line"] = end
        if start != line:
            node.attrs["start_line"] = start


# ── factory members ──────────────────────────────────────────────────────────

_JS_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
_JS_FN = {"arrow_function", "function", "function_expression", "generator_function"}
_MOVABLE = {"calls_http", "calls", "reads", "writes", "enqueues"}


def _returned_objects(fn: Any) -> list[Any]:
    body = fn.child_by_field_name("body")
    if body is None:
        return []
    while body.type == "parenthesized_expression" and body.named_children:
        body = body.named_children[0]
    if body.type == "object":
        return [body]
    out = []
    if body.type == "statement_block":
        for stmt in body.named_children:
            if stmt.type != "return_statement" or not stmt.named_children:
                continue
            expr = stmt.named_children[0]
            while expr.type == "parenthesized_expression" and expr.named_children:
                expr = expr.named_children[0]
            if expr.type == "object":
                out.append(expr)
    return out


def _members(obj: Any) -> list[tuple[str, Any]]:
    out = []
    for child in obj.named_children:
        if child.type == "pair":
            key, value = child.child_by_field_name("key"), child.child_by_field_name("value")
            if key is not None and value is not None and value.type in _JS_FN:
                out.append((key.text.decode("utf-8", "replace").strip("'\"`"), value))
        elif child.type == "method_definition":
            name = child.child_by_field_name("name")
            if name is not None:
                out.append((name.text.decode("utf-8", "replace"), child))
    return out


class _OutIndex:
    """Outgoing edges by source, kept in sync while edges are moved."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.by_from: dict[str, dict[str, Any]] = {}
        for edge in graph.edges.values():
            self.by_from.setdefault(edge.from_id, {})[edge.id] = edge

    def out(self, node_id: str) -> list[Any]:
        return list((self.by_from.get(node_id) or {}).values())

    def add(self, etype: str, from_id: str, to_id: str, **kw: Any) -> None:
        edge = self.graph.add_edge(etype, from_id, to_id, **kw)
        self.by_from.setdefault(from_id, {})[edge.id] = edge

    def move(self, edge: Any, new_from: str) -> None:
        if edge.id not in self.graph.edges:
            return
        del self.graph.edges[edge.id]
        (self.by_from.get(edge.from_id) or {}).pop(edge.id, None)
        self.add(edge.type, new_from, edge.to_id, confidence=edge.confidence, evidence=edge.evidence, **edge.attrs)


def _fn_nodes_by_end(cache: dict[str, Any], rel: str, tree: Any) -> dict[int, list[Any]]:
    key = "\0fn_by_end:" + rel
    if key not in cache:
        idx: dict[int, list[Any]] = {}
        for cand in _walk(tree.root_node):
            if cand.type in _FN_TYPES:
                idx.setdefault(cand.end_point[0] + 1, []).append(cand)
        cache[key] = idx
    return cache[key]


def split_factory_members(
    graph: Graph, root: Path, cache: dict[str, Any] | None = None, index: _OutIndex | None = None
) -> None:
    """`createClient(http) { return { a: () => http.get(...), b() {...} } }`:
    each returned member becomes ``<factory>.<key>`` (a ``calls`` child of the
    factory) and takes the edges whose evidence is inside it, so a change to
    ``b`` reaches only ``b``'s routes."""
    cache = {} if cache is None else cache
    index = index or _OutIndex(graph)
    spans_by_file: dict[str, set[tuple[Any, Any]]] = {}
    for n in graph.nodes.values():
        if n.type == "function":
            nev = n.attrs.get("evidence") or {}
            if nev.get("file"):
                spans_by_file.setdefault(str(nev["file"]), set()).add(
                    (n.attrs.get("start_line") or nev.get("line"), n.attrs.get("end_line"))
                )
    for node in list(graph.nodes.values()):
        if node.type != "function" or node.attrs.get("language") != "javascript":
            continue
        ev = node.attrs.get("evidence") or {}
        rel, line, end = ev.get("file"), ev.get("line"), node.attrs.get("end_line")
        if not rel or not isinstance(line, int) or not isinstance(end, int):
            continue
        if Path(str(rel)).suffix.lower() not in _JS_SUFFIXES:
            continue
        name = str(node.attrs.get("name") or "")
        if not name or name.endswith(".module"):
            continue
        _lines, tree = _load(cache, root, str(rel))
        if tree is None:
            continue
        start = int(node.attrs.get("start_line") or line)
        fn = next(
            (c for c in _fn_nodes_by_end(cache, str(rel), tree).get(end, []) if start <= c.start_point[0] + 1 <= end),
            None,
        )
        if fn is None:
            continue
        members = [m for obj in _returned_objects(fn) for m in _members(obj)]
        if not members:
            continue
        outgoing = [e for e in index.out(node.id) if e.type in _MOVABLE]
        # members already measured as functions of their own keep their node
        taken = spans_by_file.get(str(rel), set()) - {(start, end), (line, end)}
        for key, mnode in members:
            m_start, m_end = mnode.start_point[0] + 1, mnode.end_point[0] + 1
            child_name = f"{name}.{key}"
            child_id = f"function:{child_name}"
            if child_id in graph.nodes or (m_start, m_end) in taken:
                continue
            graph.add_node(
                child_id,
                "function",
                name=child_name,
                language="javascript",
                end_line=m_end,
                parent=node.id,
                evidence={"file": rel, "line": m_start, "symbol": child_name},
            )
            spans_by_file.setdefault(str(rel), set()).add((m_start, m_end))
            index.add(
                "calls",
                node.id,
                child_id,
                evidence=Evidence(file=str(rel), line=m_start, symbol=name),
                via="returned_member",
            )
            for edge in outgoing:
                e_ev = edge.evidence
                if e_ev is None or e_ev.file != rel or not (m_start <= e_ev.line <= m_end):
                    continue
                index.move(edge, child_id)

# ── Go route closures ────────────────────────────────────────────────────────

_GO_DECLS = {"function_declaration", "method_declaration"}


def _go_route_literal(cache: dict[str, Any], rel: str, tree: Any, line: int) -> Any | None:
    """The `func(c echo.Context) error {...}` argument of the registration
    call that starts on ``line``."""
    key = "\0go_calls_by_row:" + rel
    if key not in cache:
        by_row: dict[int, list[Any]] = {}
        for node in _walk(tree.root_node):
            if node.type == "call_expression":
                by_row.setdefault(node.start_point[0], []).append(node)
        cache[key] = by_row
    for node in cache[key].get(line - 1, []):
        args = node.child_by_field_name("arguments")
        if args is None:
            continue
        lits = [a for a in args.named_children if a.type == "func_literal"]
        if lits:
            return lits[-1]
    return None


def _go_literal_suffix(decl: Any, lit: Any) -> str:
    """Go's closure naming: `func2` for the second literal directly in the
    declaration, `func2.1` for the first literal inside that one."""
    chain = []
    cur = lit
    while cur is not None and cur is not decl and cur.type not in _GO_DECLS:
        if cur.type == "func_literal":
            chain.append(cur)
        cur = cur.parent
    chain.reverse()
    parts: list[str] = []
    scope = decl
    for node in chain:
        siblings = []
        stack = list(reversed(scope.named_children)) if scope is not None else []
        while stack:
            cur = stack.pop()
            if cur.type == "func_literal":
                siblings.append(cur)
                continue  # its own literals belong to the next level
            stack.extend(reversed(cur.named_children))
        index = next((i for i, n in enumerate(siblings) if n.start_byte == node.start_byte), 0) + 1
        parts.append(str(index))
        scope = node
    return "func" + ".".join(parts)


def split_go_route_closures(
    graph: Graph, root: Path, cache: dict[str, Any] | None = None, index: _OutIndex | None = None
) -> None:
    """`e.GET("/x", func(c echo.Context) error {...})`: the closure becomes
    ``<enclosing>.funcN`` (Go's own naming), the route ``triggers`` it, and the
    edges whose evidence is inside it move off the enclosing function."""
    cache = {} if cache is None else cache
    index = index or _OutIndex(graph)
    triggered = {e.from_id for e in graph.edges.values() if e.type == "triggers"}
    go_fns: dict[tuple[str, int], Any] = {}
    for node in graph.nodes.values():
        if node.type == "function" and node.attrs.get("language") == "go":
            nev = node.attrs.get("evidence") or {}
            if nev.get("file") and isinstance(nev.get("line"), int):
                go_fns.setdefault((str(nev["file"]), int(nev["line"])), node)
    rollup_memo: dict[str, set[tuple[str, str]]] = {}
    for ep in list(graph.nodes.values()):
        if ep.type != "entrypoint" or ep.attrs.get("kind") != "http" or ep.id in triggered:
            continue
        ev = ep.attrs.get("evidence") or {}
        rel, line = ev.get("file"), ev.get("line")
        if not rel or not isinstance(line, int) or not str(rel).endswith(".go"):
            continue
        _lines, tree = _load(cache, root, str(rel))
        if tree is None:
            continue
        lit = _go_route_literal(cache, str(rel), tree, line)
        if lit is None:
            continue
        decl = lit.parent
        while decl is not None and decl.type not in _GO_DECLS:
            decl = decl.parent
        if decl is None:
            continue
        l_start, l_end = lit.start_point[0] + 1, lit.end_point[0] + 1
        owner = go_fns.get((str(rel), decl.start_point[0] + 1))
        if owner is None:
            continue
        owner_name = str(owner.attrs.get("name") or owner.id.split("function:", 1)[-1])
        name = f"{owner_name}.{_go_literal_suffix(decl, lit)}"
        fid = f"function:{name}"
        graph.add_node(
            fid,
            "function",
            name=name,
            language="go",
            end_line=l_end,
            parent=owner.id,
            evidence={"file": rel, "line": l_start, "symbol": name},
        )
        index.add("triggers", ep.id, fid, evidence=Evidence(file=str(rel), line=line, symbol=name))
        ep.attrs["symbol"] = name
        for edge in [e for e in index.out(owner.id) if e.type in _MOVABLE]:
            e_ev = edge.evidence
            if e_ev is None or e_ev.file != rel or not (l_start <= e_ev.line <= l_end):
                continue
            index.move(edge, fid)
        evidence = Evidence(file=str(rel), line=line, symbol=name)
        for etype, target in sorted(_table_hits(graph, index, fid, rollup_memo)):
            index.add(etype, ep.id, target, confidence="inferred", evidence=evidence)


def _table_hits(
    graph: Graph, index: _OutIndex, fid: str, memo: dict[str, set[tuple[str, str]]]
) -> set[tuple[str, str]]:
    """Table-level reads/writes ``fid`` (or what it calls) does: what the
    route gets rolled up, like named handlers, so report/why see them.
    ``memo`` caches each function's own (non-transitive) hits."""
    seen: set[str] = set()
    todo = [fid]
    hits: set[tuple[str, str]] = set()
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        own = memo.get(cur)
        callees: list[str] = []
        if own is None:
            own = set()
            for edge in index.out(cur):
                if edge.type in {"reads", "writes"}:
                    target = edge.to_id
                    if target.startswith("column:"):
                        target = "table:" + target.removeprefix("column:").split(".", 1)[0]
                    if target in graph.nodes:
                        own.add((edge.type, target))
            memo[cur] = own
        callees = [e.to_id for e in index.out(cur) if e.type == "calls"]
        hits |= own
        todo.extend(callees)
    return hits
