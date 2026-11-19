"""JavaScript/TypeScript detector (Express/Fastify/Hono, fetch, supabase, pg)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Node, Parser

from bizmap.detect.tsutil import call_args, call_callee_name, line_of, text, walk
from bizmap.files import rel
from bizmap.sql_analyze import looks_like_sql

try:
    import tree_sitter_javascript as tsjs

    _JS_LANGUAGE = Language(tsjs.language())
except Exception:  # pragma: no cover - optional dep
    _JS_LANGUAGE = None

try:
    import tree_sitter_typescript as tsts

    _TS_LANGUAGE = Language(tsts.language_typescript())
    _TSX_LANGUAGE = Language(tsts.language_tsx())
except Exception:  # pragma: no cover - optional dep
    _TS_LANGUAGE = None
    _TSX_LANGUAGE = None

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "all", "use"}
SUPABASE_WRITE = {"insert", "update", "delete", "upsert"}
SUPABASE_READ = {"select"}
PG_QUERY_METHODS = {"query", "exec", "execute"}


def _parser_for(path: Path) -> Parser | None:
    suffix = path.suffix.lower()
    if suffix in {".ts", ".mts", ".cts"} and _TS_LANGUAGE is not None:
        return Parser(_TS_LANGUAGE)
    if suffix in {".tsx"} and _TSX_LANGUAGE is not None:
        return Parser(_TSX_LANGUAGE)
    if _JS_LANGUAGE is not None:
        return Parser(_JS_LANGUAGE)
    if _TS_LANGUAGE is not None:
        return Parser(_TS_LANGUAGE)
    return None


def _unquote(raw: str) -> str:
    s = raw.strip()
    if s.startswith(("f", "r")):
        s = s[1:]
    if len(s) >= 2 and s[0] in {"'", '"', "`"} and s[-1] == s[0]:
        return s[1:-1]
    return raw


def _string(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in {"string", "template_string", "string_fragment"}:
        if node.type == "template_string":
            if any(c.type == "template_substitution" for c in node.named_children):
                parts: list[str] = []
                for child in node.children:
                    if child.type == "string_fragment":
                        parts.append(text(child))
                    elif child.type == "template_substitution":
                        inner = child.named_children[0] if child.named_children else None
                        parts.append("{" + (text(inner) if inner is not None else "expr") + "}")
                joined = "".join(parts)
                return joined if joined else None
            return _unquote(text(node))
        return _unquote(text(node))
    if node.type == "identifier":
        return "{" + text(node) + "}"
    return None


def _supabase_fn_name(rel_file: str) -> str | None:
    parts = rel_file.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part == "functions" and i > 0 and parts[i - 1] == "supabase" and i + 1 < len(parts):
            name = parts[i + 1]
            if name and name not in {"src", "lib"}:
                return name
    return None


def _chain_from_table(call: Node) -> str | None:
    """Walk ``x.from('t').select()`` and return the table name."""
    cur: Node | None = call
    while cur is not None:
        if cur.type == "call_expression" and call_callee_name(cur) == "from":
            args = call_args(cur)
            if args:
                name = _string(args[0])
                if name:
                    return name
        func = cur.child_by_field_name("function") if cur.type == "call_expression" else None
        if func is not None and func.type == "member_expression" and func.named_children:
            cur = func.named_children[0]
            continue
        if cur.type == "member_expression" and cur.named_children:
            cur = cur.named_children[0]
            continue
        break
    return None


def _enclosing_symbol(node: Node, module: str) -> str:
    cur = node.parent
    while cur is not None:
        if cur.type in {"function_declaration", "method_definition", "function"}:
            name_n = cur.child_by_field_name("name")
            name = text(name_n) if name_n else ""
            if name:
                return f"{module}.{name}"
        cur = cur.parent
    return f"{module}.module"


@dataclass
class JsRoute:
    method: str
    path: str
    handler: str
    file: str
    line: int
    unresolved: bool = False


@dataclass
class JsHttpCall:
    method: str
    url: str
    file: str
    line: int
    symbol: str
    unresolved: bool = False


@dataclass
class JsRpcCall:
    name: str
    file: str
    line: int
    symbol: str


@dataclass
class JsSqlSite:
    symbol: str
    sql: str
    file: str
    line: int
    dynamic: bool = False


@dataclass
class JsTableAccess:
    symbol: str
    kind: str
    table: str
    file: str
    line: int


@dataclass
class JsAnalysis:
    routes: list[JsRoute] = field(default_factory=list)
    http_calls: list[JsHttpCall] = field(default_factory=list)
    rpc_calls: list[JsRpcCall] = field(default_factory=list)
    functions: dict[str, tuple[str, int]] = field(default_factory=dict)
    sql_sites: list[JsSqlSite] = field(default_factory=list)
    table_accesses: list[JsTableAccess] = field(default_factory=list)


class JsAnalyzer:
    def __init__(self, rules=None) -> None:
        self.rules = rules or []

    def analyze(self, files: list[Path], root: Path) -> JsAnalysis:
        analysis = JsAnalysis()
        for path in files:
            parser = _parser_for(path)
            if parser is None:
                continue
            source = path.read_bytes()
            tree = parser.parse(source).root_node
            rel_file = rel(path, root)
            module = rel_file.replace("/", ".").rsplit(".", 1)[0]
            edge_name = _supabase_fn_name(rel_file)
            saw_serve = False
            for node in walk(tree):
                if node.type in {
                    "function_declaration",
                    "method_definition",
                    "arrow_function",
                    "function",
                    "function_expression",
                }:
                    name_n = node.child_by_field_name("name")
                    name = text(name_n) if name_n else ""
                    if name:
                        analysis.functions[f"{module}.{name}"] = (rel_file, line_of(node))
                if node.type != "call_expression":
                    continue
                method = call_callee_name(node).lower()
                args = call_args(node)
                symbol = _enclosing_symbol(node, module)

                if method in HTTP_METHODS and args:
                    path_val = _string(args[0])
                    if path_val is None:
                        continue
                    if not (
                        path_val.startswith("/")
                        or path_val.startswith("{")
                        or path_val.startswith("http")
                    ):
                        continue
                    unresolved = path_val.startswith("{") or (
                        "{" in path_val and args and "${" in text(args[0])
                    )
                    handler = "anonymous"
                    if len(args) >= 2:
                        handler = text(args[-1])[:80]
                    analysis.routes.append(
                        JsRoute(
                            method="USE" if method == "use" else method.upper(),
                            path=path_val
                            if path_val.startswith("/") or path_val.startswith("{")
                            else "/" + path_val,
                            handler=f"{module}.{handler}",
                            file=rel_file,
                            line=line_of(node),
                            unresolved=unresolved,
                        )
                    )
                if method == "fetch" and args:
                    url = _string(args[0])
                    if not url:
                        continue
                    http_method = "GET"
                    unresolved = "{" in url and not url.startswith("/{")
                    if len(args) > 1:
                        raw = text(args[1])
                        for m in HTTP_METHODS:
                            if (
                                f"method: '{m.upper()}'" in raw
                                or f'method: "{m.upper()}"' in raw
                                or f"method: '{m}'" in raw
                            ):
                                http_method = m.upper()
                    analysis.http_calls.append(
                        JsHttpCall(
                            method=http_method,
                            url=url,
                            file=rel_file,
                            line=line_of(node),
                            symbol=f"{module}.fetch",
                            unresolved=unresolved,
                        )
                    )
                if method == "rpc" and args:
                    name = _string(args[0])
                    if name:
                        analysis.rpc_calls.append(
                            JsRpcCall(
                                name=name,
                                file=rel_file,
                                line=line_of(node),
                                symbol=f"{module}.rpc",
                            )
                        )
                if method in SUPABASE_READ | SUPABASE_WRITE:
                    table = _chain_from_table(node)
                    if table:
                        kind = "read" if method in SUPABASE_READ else "write"
                        analysis.table_accesses.append(
                            JsTableAccess(
                                symbol=symbol,
                                kind=kind,
                                table=table,
                                file=rel_file,
                                line=line_of(node),
                            )
                        )
                if method in PG_QUERY_METHODS and args:
                    sql = _string(args[0])
                    if sql and looks_like_sql(sql):
                        analysis.sql_sites.append(
                            JsSqlSite(
                                symbol=symbol,
                                sql=sql,
                                file=rel_file,
                                line=line_of(node),
                                dynamic="{" in sql,
                            )
                        )
                if method in {"serve"} and edge_name:
                    saw_serve = True

            if edge_name and saw_serve:
                path = f"/functions/v1/{edge_name}"
                handler = f"{module}.serve"
                analysis.routes.append(
                    JsRoute(
                        method="POST",
                        path=path,
                        handler=handler,
                        file=rel_file,
                        line=1,
                    )
                )
                analysis.functions[handler] = (rel_file, 1)
        return analysis
