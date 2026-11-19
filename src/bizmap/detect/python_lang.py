from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspy
from tree_sitter import Language, Node, Parser

from bizmap.detect.rules import Rule, import_matches, method_matches
from bizmap.detect.tsutil import call_args, call_callee_name, line_of, text, walk
from bizmap.files import rel
from bizmap.graph import Confidence, Evidence
from bizmap.sql_analyze import looks_like_sql

_PY_LANGUAGE = Language(tspy.language())

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _parser() -> Parser:
    return Parser(_PY_LANGUAGE)


def _unquote(raw: str) -> str:
    if len(raw) >= 6 and raw[:3] in {'"""', "'''"} and raw[-3:] == raw[:3]:
        return raw[3:-3]
    if len(raw) >= 2 and raw[0] in {'"', "'"} and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def _string_value(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return _unquote(text(node))
    if node.type == "concatenated_string":
        parts = [_string_value(c) for c in node.named_children]
        if all(p is not None for p in parts):
            return "".join(parts)  # type: ignore[arg-type]
        return "".join(p or "" for p in parts)
    if node.type == "binary_operator" and text(node.child_by_field_name("operator") or node) == "+":
        left = _string_value(node.child_by_field_name("left"))
        right = _string_value(node.child_by_field_name("right"))
        if left is not None and right is not None:
            return left + right
        if left or right:
            return (left or "") + (right or "")
    return None


def _docstring(fn_node: Node) -> str | None:
    body = fn_node.child_by_field_name("body")
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children:
        inner = first.named_children[0]
        if inner.type == "string":
            return _unquote(text(inner)).strip() or None
    return None


def _preceding_hash_comment(fn_node: Node, source: bytes) -> str | None:
    start = fn_node.start_byte
    before = source[:start].decode("utf-8", errors="replace")
    lines = before.splitlines()
    comments: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            if comments:
                break
            continue
        if stripped.startswith("#"):
            comments.append(stripped.lstrip("#").strip())
            continue
        break
    comments.reverse()
    return " ".join(comments) if comments else None


def _imports(root: Node) -> list[str]:
    found: list[str] = []
    for node in walk(root):
        if node.type == "import_statement":
            found.append(text(node))
        elif node.type == "import_from_statement":
            found.append(text(node))
    return found


def _module_name(rel_file: str) -> str:
    path = rel_file[:-3] if rel_file.endswith(".py") else rel_file
    return path.replace("/", ".").replace("\\", ".")


@dataclass
class PyFunction:
    module: str
    name: str
    file: str
    line: int
    why: str | None
    node: Node

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.name}"


@dataclass
class PyRoute:
    method: str
    path: str
    handler: PyFunction
    evidence: Evidence


@dataclass
class PySqlSite:
    function: PyFunction
    sql: str
    dynamic: bool
    evidence: Evidence


@dataclass
class PyAnalysis:
    functions: dict[str, PyFunction] = field(default_factory=dict)
    routes: list[PyRoute] = field(default_factory=list)
    sql_sites: list[PySqlSite] = field(default_factory=list)
    calls: list[tuple[str, str, Evidence, Confidence]] = field(default_factory=list)


class PythonAnalyzer:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = [r for r in rules if r.language == "python" and not r.is_stub]
        self._parser = _parser()

    def analyze(self, files: list[Path], root: Path) -> PyAnalysis:
        analysis = PyAnalysis()
        parsed: list[tuple[Path, str, bytes, Node, list[str], list[PyFunction]]] = []
        for path in files:
            source = path.read_bytes()
            tree = self._parser.parse(source).root_node
            rel_file = rel(path, root)
            module = _module_name(rel_file)
            imports = _imports(tree)
            fns: list[PyFunction] = []
            for node in walk(tree):
                if node.type != "function_definition":
                    continue
                name_node = node.child_by_field_name("name")
                name = text(name_node) or "unknown"
                why = _docstring(node) or _preceding_hash_comment(node, source)
                fn = PyFunction(
                    module=module,
                    name=name,
                    file=rel_file,
                    line=line_of(node),
                    why=why,
                    node=node,
                )
                fns.append(fn)
                analysis.functions[fn.qualname] = fn
            parsed.append((path, rel_file, source, tree, imports, fns))

        fastapi_rule = next((r for r in self.rules if r.framework == "fastapi"), None)
        sql_rules = [r for r in self.rules if "sql" in r.produce]

        for _path, rel_file, _source, tree, imports, fns in parsed:
            enclosing = {id(fn.node): fn for fn in fns}
            # decorated_definition wraps function_definition
            for node in walk(tree):
                if node.type == "decorated_definition":
                    fn_node = next((c for c in node.named_children if c.type == "function_definition"), None)
                    if fn_node is None:
                        continue
                    fn = enclosing.get(id(fn_node))
                    if fn is None:
                        continue
                    for child in node.named_children:
                        if child.type != "decorator":
                            continue
                        call = child.named_children[0] if child.named_children else None
                        if call is None:
                            continue
                        # @app.get(...) is a call; @decorator without args is identifier
                        target = call if call.type == "call" else None
                        if target is None:
                            continue
                        method = call_callee_name(target).lower()
                        if fastapi_rule and method_matches(
                            method, (fastapi_rule.detect.get("decorators") or {}).get("method")
                        ):
                            if import_matches(imports, fastapi_rule.when):
                                args = call_args(target)
                                path_idx = int(
                                    ((fastapi_rule.detect.get("decorators") or {}).get("arguments") or {})
                                    .get("path", {})
                                    .get("index", 0)
                                )
                                path_val = _string_value(args[path_idx] if len(args) > path_idx else None)
                                if path_val is not None:
                                    analysis.routes.append(
                                        PyRoute(
                                            method=method.upper(),
                                            path=path_val if path_val.startswith("/") else "/" + path_val,
                                            handler=fn,
                                            evidence=Evidence(
                                                file=rel_file,
                                                line=line_of(target),
                                                symbol=fn.qualname,
                                            ),
                                        )
                                    )

            by_local = {fn.name: fn for fn in fns}
            for fn in fns:
                for node in walk(fn.node):
                    if node.type != "call":
                        continue
                    method = call_callee_name(node)
                    args = call_args(node)
                    evidence = Evidence(file=rel_file, line=line_of(node), symbol=fn.qualname)
                    # same-module call graph
                    if method in by_local and by_local[method].qualname != fn.qualname:
                        analysis.calls.append(
                            (fn.qualname, by_local[method].qualname, evidence, Confidence.INFERRED)
                        )
                    else:
                        # unique across project
                        matches = [f for f in analysis.functions.values() if f.name == method]
                        if len(matches) == 1 and matches[0].qualname != fn.qualname:
                            analysis.calls.append(
                                (fn.qualname, matches[0].qualname, evidence, Confidence.INFERRED)
                            )

                    for rule in sql_rules:
                        if not method_matches(method, (rule.detect.get("calls") or {}).get("method")):
                            continue
                        if not import_matches(imports, rule.when):
                            continue
                        idx = int((rule.detect.get("calls") or {}).get("arguments", {}).get("sql", {}).get("index", 0))
                        sql_node = args[idx] if len(args) > idx else None
                        sql_val = _string_value(sql_node)
                        dynamic = sql_val is None
                        if sql_val is None and sql_node is not None:
                            # identifier / concat with holes
                            sql_val = text(sql_node).strip("\"'")
                        if sql_val and (looks_like_sql(sql_val) or dynamic):
                            analysis.sql_sites.append(
                                PySqlSite(
                                    function=fn,
                                    sql=sql_val,
                                    dynamic=dynamic or not looks_like_sql(sql_val),
                                    evidence=Evidence(
                                        file=rel_file, line=line_of(node), symbol=fn.qualname
                                    ),
                                )
                            )

                    # Generic harvest: any string literal argument that looks like SQL.
                    for arg in args:
                        sql_val = _string_value(arg)
                        if sql_val and looks_like_sql(sql_val):
                            analysis.sql_sites.append(
                                PySqlSite(
                                    function=fn,
                                    sql=sql_val,
                                    dynamic=False,
                                    evidence=Evidence(
                                        file=rel_file, line=line_of(node), symbol=fn.qualname
                                    ),
                                )
                            )

        # de-dupe sql sites
        unique: dict[tuple, PySqlSite] = {}
        for site in analysis.sql_sites:
            key = (site.function.qualname, site.sql, site.evidence.line)
            unique[key] = site
        analysis.sql_sites = list(unique.values())
        return analysis
