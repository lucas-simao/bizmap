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


def split_factory_members(
    graph: Graph, root: Path, cache: dict[str, tuple[list[str], Any]] | None = None
) -> None:
    """`createClient(http) { return { a: () => http.get(...), b() {...} } }`:
    each returned member becomes ``<factory>.<key>`` (a ``calls`` child of the
    factory) and takes the edges whose evidence is inside it, so a change to
    ``b`` reaches only ``b``'s routes."""
    cache = {} if cache is None else cache
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
        fn = None
        for cand in _walk(tree.root_node):
            if (
                cand.type in _FN_TYPES
                and cand.end_point[0] + 1 == end
                and start <= cand.start_point[0] + 1 <= end
            ):
                fn = cand
                break
        if fn is None:
            continue
        members = [m for obj in _returned_objects(fn) for m in _members(obj)]
        if not members:
            continue
        outgoing = [e for e in graph.edges.values() if e.from_id == node.id and e.type in _MOVABLE]
        # members already measured as functions of their own (method
        # definitions the detector named) keep their node
        taken = {
            ((n.attrs.get("start_line") or (n.attrs.get("evidence") or {}).get("line")), n.attrs.get("end_line"))
            for n in graph.nodes.values()
            if n.type == "function" and (n.attrs.get("evidence") or {}).get("file") == rel and n.id != node.id
        }
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
            graph.add_edge(
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
                if edge.id not in graph.edges:
                    continue
                del graph.edges[edge.id]
                graph.add_edge(
                    edge.type,
                    child_id,
                    edge.to_id,
                    confidence=edge.confidence,
                    evidence=e_ev,
                    **edge.attrs,
                )


# ── Go route closures ────────────────────────────────────────────────────────

_GO_DECLS = {"function_declaration", "method_declaration"}


def _go_route_literal(tree: Any, line: int) -> Any | None:
    """The `func(c echo.Context) error {...}` argument of the registration
    call that starts on ``line``."""
    row = line - 1
    for node in _walk(tree.root_node):
        if node.type != "call_expression" or node.start_point[0] != row:
            continue
        args = node.child_by_field_name("arguments")
        if args is None:
            continue
        lits = [a for a in args.named_children if a.type == "func_literal"]
        if lits:
            return lits[-1]
    return None


def split_go_route_closures(
    graph: Graph, root: Path, cache: dict[str, tuple[list[str], Any]] | None = None
) -> None:
    """`e.GET("/x", func(c echo.Context) error {...})`: the closure becomes
    ``<enclosing>.funcN`` (Go's own naming), the route ``triggers`` it, and the
    edges whose evidence is inside it move off the enclosing function."""
    cache = {} if cache is None else cache
    triggered = {e.from_id for e in graph.edges.values() if e.type == "triggers"}
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
        lit = _go_route_literal(tree, line)
        if lit is None:
            continue
        decl = lit.parent
        while decl is not None and decl.type not in _GO_DECLS:
            decl = decl.parent
        if decl is None:
            continue
        l_start, l_end = lit.start_point[0] + 1, lit.end_point[0] + 1
        d_start = decl.start_point[0] + 1
        owner = None
        for node in graph.nodes.values():
            if node.type != "function" or node.attrs.get("language") != "go":
                continue
            nev = node.attrs.get("evidence") or {}
            if nev.get("file") == rel and nev.get("line") == d_start:
                owner = node
                break
        if owner is None:
            continue
        lits = [n for n in _walk(decl) if n.type == "func_literal"]
        lits.sort(key=lambda n: n.start_byte)
        index = next((i for i, n in enumerate(lits) if n.start_byte == lit.start_byte), 0) + 1
        owner_name = str(owner.attrs.get("name") or owner.id.split("function:", 1)[-1])
        name = f"{owner_name}.func{index}"
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
        graph.add_edge(
            "triggers",
            ep.id,
            fid,
            evidence=Evidence(file=str(rel), line=line, symbol=name),
        )
        ep.attrs["symbol"] = name
        for edge in [e for e in graph.edges.values() if e.from_id == owner.id and e.type in _MOVABLE]:
            e_ev = edge.evidence
            if e_ev is None or e_ev.file != rel or not (l_start <= e_ev.line <= l_end):
                continue
            del graph.edges[edge.id]
            graph.add_edge(edge.type, fid, edge.to_id, confidence=edge.confidence, evidence=e_ev, **edge.attrs)
