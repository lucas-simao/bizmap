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

from bizmap.graph import Graph

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
    return None


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


def fill_spans(graph: Graph, root: Path) -> None:
    """Set ``end_line`` (and ``start_line`` when it differs from the evidence
    line) on function nodes that lack one. Files are read once each."""
    cache: dict[str, tuple[list[str], Any]] = {}
    for node in graph.nodes.values():
        if node.type != "function" or isinstance(node.attrs.get("end_line"), int):
            continue
        ev = node.attrs.get("evidence") or {}
        rel, line = ev.get("file"), ev.get("line")
        if not rel or not isinstance(line, int):
            continue
        path = root / str(rel)
        if str(rel) not in cache:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                cache[str(rel)] = ([], None)
                continue
            tree = None
            parser = _parser_for(path)
            if parser is not None:
                try:
                    tree = parser.parse(text.encode("utf-8"))
                except Exception:  # pragma: no cover - parser failure
                    tree = None
            cache[str(rel)] = (text.splitlines(), tree)
        lines, tree = cache[str(rel)]
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
