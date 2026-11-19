"""JavaScript/TypeScript detector (Express/Fastify/Hono routes, fetch, supabase, pg)."""

from __future__ import annotations

import re
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
ROUTER_FACTORIES = {"express", "Router", "fastify", "Fastify", "Hono", "Koa", "createApp"}
ROUTER_CTORS = {"Hono", "Koa", "Express"}
CLIENT_LIBS = {"axios", "fetch", "got", "ky", "superagent", "http", "https", "request"}
SUPABASE_WRITE = {"insert", "upsert", "update", "delete"}
SUPABASE_READ = {"select", "maybeSingle", "single"}
PG_QUERY_METHODS = {"query", "exec", "execute"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def _declarator_name(node: Node) -> str:
    """Assigned identifier, even when TS annotations confuse the JS grammar."""
    name_n = node.child_by_field_name("name")
    if name_n is not None and name_n.type == "identifier":
        raw_name = text(name_n)
        # ``const r: Router = Router()`` may bind the name field to ``Router``.
        raw = text(node)
        left = raw.split("=", 1)[0]
        left = left.split(":")[0]
        for prefix in ("const ", "let ", "var "):
            if left.startswith(prefix):
                left = left[len(prefix) :]
        left = left.strip()
        if _IDENT.match(left) and left != raw_name:
            return left
        if _IDENT.match(raw_name):
            return raw_name
    raw = text(node)
    left = raw.split("=", 1)[0]
    left = left.split(":")[0]
    for prefix in ("const ", "let ", "var "):
        if left.startswith(prefix):
            left = left[len(prefix) :]
    left = left.strip()
    return left if _IDENT.match(left) else ""


def _resolve_js_string(node: Node | None, consts: dict[str, str]) -> tuple[str | None, bool]:
    """Return (value, unresolved) for a string, template, or resolvable const."""
    if node is None:
        return None, False
    if node.type in {"string", "string_fragment"}:
        return _unquote(text(node)), False
    if node.type == "template_string":
        parts: list[str] = []
        unresolved = False
        for child in node.children:
            if child.type == "string_fragment":
                parts.append(text(child))
            elif child.type == "template_substitution":
                inner = child.named_children[0] if child.named_children else None
                piece, hole = _resolve_substitution(inner, consts)
                parts.append(piece)
                unresolved = unresolved or hole
        joined = "".join(parts)
        return (joined if joined else None), unresolved
    if node.type == "identifier":
        name = text(node)
        if name in consts:
            return consts[name], "{" in consts[name]
        return None, False
    if node.type == "member_expression":
        raw = text(node)
        if raw in consts:
            return consts[raw], "{" in consts[raw]
        return None, False
    return None, False


def _resolve_substitution(inner: Node | None, consts: dict[str, str]) -> tuple[str, bool]:
    if inner is None:
        return "{expr}", True
    if inner.type == "identifier":
        name = text(inner)
        if name in consts:
            return consts[name], False
        return "{" + name + "}", True
    if inner.type == "call_expression":
        callee = call_callee_name(inner)
        args = call_args(inner)
        if callee in consts:
            # ``base(id)`` with resolvable base const — keep the base, drop args.
            base = consts[callee].rstrip("/")
            return base, False
        arg_bits = ",".join(text(a) for a in args)
        return "{" + f"{callee}({arg_bits})" + "}", True
    if inner.type in {"string", "template_string"}:
        val, unresolved = _resolve_js_string(inner, consts)
        return val or ("{" + text(inner) + "}"), unresolved
    return "{" + text(inner) + "}", True


def _looks_like_http_path(value: str | None) -> bool:
    if not value:
        return False
    if value.startswith("/") or "://" in value:
        return True
    if value.startswith("{") and ("/" in value or "://" in value or value.endswith("}")):
        # ``{BASE}/x`` or ``{base(id)}/x`` — path-like. Bare ``{key}`` is not.
        return "/" in value or "://" in value
    return "/" in value


def _is_router_factory(node: Node | None) -> bool:
    if node is None:
        return False
    if node.type == "call_expression":
        name = call_callee_name(node)
        if name in ROUTER_FACTORIES:
            return True
        func = node.child_by_field_name("function")
        if func is not None and func.type == "member_expression":
            # express.Router()
            if text(func).endswith(".Router") or call_callee_name(node) == "Router":
                return True
        return False
    if node.type == "new_expression":
        ctor = node.child_by_field_name("constructor") or (
            node.named_children[0] if node.named_children else None
        )
        return bool(ctor is not None and text(ctor).split(".")[-1] in ROUTER_CTORS)
    return False


def _receiver_ident(call: Node) -> str:
    func = call.child_by_field_name("function")
    if func is None:
        return ""
    if func.type in {"member_expression", "attribute"}:
        obj = func.named_children[0] if func.named_children else None
        if obj is None:
            return ""
        if obj.type == "identifier":
            return text(obj)
        if obj.type == "member_expression":
            return text(obj.named_children[0]) if obj.named_children else text(obj)
        return text(obj)
    return ""


def _typed_as_router(raw: str) -> bool:
    lower = raw.lower()
    return any(
        token in lower
        for token in (
            ": express",
            ":express",
            "express.application",
            "express.router",
            ": router",
            ":router",
            ": fastify",
            ": hono",
            ": application",
        )
    )


@dataclass
class JsRoute:
    method: str
    path: str
    handler: str
    file: str
    line: int
    unresolved: bool = False
    handler_qual: str | None = None


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
class JsAccess:
    kind: str
    table: str
    file: str
    line: int
    symbol: str
    target_kind: str = "table"


@dataclass
class JsCall:
    caller: str
    callee: str
    file: str
    line: int


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
    accesses: list[JsAccess] = field(default_factory=list)
    table_accesses: list[JsTableAccess] = field(default_factory=list)
    sql_sites: list[JsSqlSite] = field(default_factory=list)
    calls: list[JsCall] = field(default_factory=list)


def _supabase_fn_name(rel_file: str) -> str | None:
    parts = rel_file.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part == "functions" and i > 0 and parts[i - 1] == "supabase" and i + 1 < len(parts):
            name = parts[i + 1]
            if name and name not in {"src", "lib"}:
                return name
    return None


class JsAnalyzer:
    def __init__(self, rules=None) -> None:
        self.rules = rules or []

    def analyze(self, files: list[Path], root: Path) -> JsAnalysis:
        analysis = JsAnalysis()
        parsed: list[tuple[Path, str, str, Node, bytes]] = []
        for path in files:
            parser = _parser_for(path)
            if parser is None:
                continue
            source = path.read_bytes()
            tree = parser.parse(source).root_node
            rel_file = rel(path, root)
            module = rel_file.replace("/", ".").rsplit(".", 1)[0]
            parsed.append((path, rel_file, module, tree, source))

        consts_by_mod: dict[str, dict[str, str]] = {}
        routers_by_mod: dict[str, dict[str, str]] = {}
        clients_by_mod: dict[str, set[str]] = {}
        fn_nodes: dict[str, Node] = {}
        imports_by_mod: dict[str, dict[str, str]] = {}

        for _path, rel_file, module, tree, _src in parsed:
            consts = _collect_consts(tree)
            consts_by_mod[module] = consts
            routers, clients = _collect_receivers(tree)
            routers_by_mod[module] = routers
            clients_by_mod[module] = clients
            imports_by_mod[module] = _collect_imports(tree, module)
            for node in walk(tree):
                if node.type in {"function_declaration", "method_definition", "function"}:
                    name_n = node.child_by_field_name("name")
                    name = text(name_n) if name_n else ""
                    if name:
                        qual = f"{module}.{name}"
                        analysis.functions[qual] = (rel_file, line_of(node))
                        fn_nodes[qual] = node
                if node.type == "variable_declarator":
                    # const save = () => { ... } / const save = function () {}
                    value = node.child_by_field_name("value")
                    name = _declarator_name(node)
                    if name and value is not None and value.type in {
                        "arrow_function",
                        "function",
                    }:
                        qual = f"{module}.{name}"
                        analysis.functions[qual] = (rel_file, line_of(node))
                        fn_nodes[qual] = value

        for _path, rel_file, module, tree, _src in parsed:
            consts = consts_by_mod.get(module) or {}
            routers = routers_by_mod.get(module) or {}
            clients = clients_by_mod.get(module) or set()
            imports = imports_by_mod.get(module) or {}
            self._scan_file(
                analysis,
                tree,
                rel_file,
                module,
                consts,
                routers,
                clients,
                imports,
                fn_nodes,
            )
        return analysis

    def _scan_file(
        self,
        analysis: JsAnalysis,
        tree: Node,
        rel_file: str,
        module: str,
        consts: dict[str, str],
        routers: dict[str, str],
        clients: set[str],
        imports: dict[str, str],
        fn_nodes: dict[str, Node],
    ) -> None:
        for node in walk(tree):
            if node.type != "call_expression":
                continue
            method = call_callee_name(node).lower()
            args = call_args(node)
            recv = _receiver_ident(node)
            if method in HTTP_METHODS and args:
                path_val, path_unresolved = _resolve_js_string(args[0], consts)
                is_router = recv in routers
                if is_router and path_val is not None and (
                    path_val.startswith("/") or path_val.startswith("{") or "://" in path_val
                ):
                    if method == "use":
                        continue
                    handler_node = args[-1] if len(args) >= 2 else None
                    handler_qual, handler_label = self._handler_symbol(
                        handler_node, module, imports
                    )
                    path_norm = (
                        path_val
                        if path_val.startswith("/") or path_val.startswith("{")
                        else "/" + path_val
                    )
                    if handler_qual is None and handler_label == "anonymous":
                        handler_qual = f"{module}.{method.upper()} {path_norm}"
                    route = JsRoute(
                        method=method.upper(),
                        path=path_norm,
                        handler=handler_qual or f"{module}.{handler_label}",
                        file=rel_file,
                        line=line_of(node),
                        unresolved=path_unresolved,
                        handler_qual=handler_qual,
                    )
                    analysis.routes.append(route)
                    self._scan_handler(
                        analysis,
                        handler_node,
                        handler_qual or route.handler,
                        rel_file,
                        module,
                        consts,
                        imports,
                        fn_nodes,
                    )
                    continue
                # Outbound client call (axios.get, client.get, api.post, …)
                if method in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    if recv.lower() in {"map", "set", "weakmap", "weakset"}:
                        continue
                    if path_val is None or not _looks_like_http_path(path_val):
                        continue
                    if is_router:
                        continue
                    analysis.http_calls.append(
                        JsHttpCall(
                            method=method.upper(),
                            url=path_val,
                            file=rel_file,
                            line=line_of(node),
                            symbol=f"{module}.{recv + '.' if recv else ''}{method}",
                            unresolved=path_unresolved or ("{" in path_val),
                        )
                    )
                    continue
            if method == "fetch" and args:
                url, unresolved = _resolve_js_string(args[0], consts)
                if not url:
                    continue
                http_method = "GET"
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
                        unresolved=unresolved or ("{" in url and not url.startswith("/{")),
                    )
                )
            if method in CLIENT_LIBS and method != "fetch" and args:
                url, unresolved = _resolve_js_string(args[0], consts)
                if url and _looks_like_http_path(url):
                    analysis.http_calls.append(
                        JsHttpCall(
                            method="GET",
                            url=url,
                            file=rel_file,
                            line=line_of(node),
                            symbol=f"{module}.{method}",
                            unresolved=unresolved,
                        )
                    )
            if method == "rpc" and args:
                name, _u = _resolve_js_string(args[0], consts)
                if name:
                    analysis.rpc_calls.append(
                        JsRpcCall(name=name, file=rel_file, line=line_of(node), symbol=f"{module}.rpc")
                    )
            if method in PG_QUERY_METHODS and args:
                sql, unresolved = _resolve_js_string(args[0], consts)
                if sql and looks_like_sql(sql):
                    analysis.sql_sites.append(
                        JsSqlSite(
                            symbol=_enclosing_symbol(node, module),
                            sql=sql,
                            file=rel_file,
                            line=line_of(node),
                            dynamic=unresolved or "{" in sql,
                        )
                    )
            self._maybe_supabase(analysis, node, method, args, rel_file, module, consts)
        edge_name = _supabase_fn_name(rel_file)
        if edge_name and any(
            n.type == "call_expression" and call_callee_name(n).lower() in {"serve"}
            for n in walk(tree)
        ):
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

    def _handler_symbol(
        self, handler: Node | None, module: str, imports: dict[str, str]
    ) -> tuple[str | None, str]:
        if handler is None:
            return None, "anonymous"
        if handler.type == "identifier":
            name = text(handler)
            if name in imports:
                return imports[name], name
            return f"{module}.{name}", name
        if handler.type in {"arrow_function", "function"}:
            return None, "anonymous"
        if handler.type == "member_expression":
            raw = text(handler)
            return f"{module}.{raw}", raw[:80]
        return None, text(handler)[:80]

    def _scan_handler(
        self,
        analysis: JsAnalysis,
        handler: Node | None,
        symbol: str,
        rel_file: str,
        module: str,
        consts: dict[str, str],
        imports: dict[str, str],
        fn_nodes: dict[str, Node],
    ) -> None:
        body: Node | None = handler
        if handler is not None and handler.type == "identifier":
            name = text(handler)
            qual = imports.get(name) or f"{module}.{name}"
            body = fn_nodes.get(qual)
            analysis.calls.append(
                JsCall(caller=symbol, callee=qual, file=rel_file, line=line_of(handler))
            )
        if body is None:
            return
        for node in walk(body):
            if node.type != "call_expression":
                continue
            method = call_callee_name(node).lower()
            args = call_args(node)
            self._maybe_supabase(analysis, node, method, args, rel_file, module, consts, symbol=symbol)
            if method == "rpc" and args:
                name, _u = _resolve_js_string(args[0], consts)
                if name:
                    analysis.rpc_calls.append(
                        JsRpcCall(name=name, file=rel_file, line=line_of(node), symbol=symbol)
                    )
            if node.child_by_field_name("function") and node.child_by_field_name("function").type == "identifier":
                callee = text(node.child_by_field_name("function"))
                if callee in imports:
                    analysis.calls.append(
                        JsCall(
                            caller=symbol,
                            callee=imports[callee],
                            file=rel_file,
                            line=line_of(node),
                        )
                    )
                elif f"{module}.{callee}" in fn_nodes:
                    analysis.calls.append(
                        JsCall(
                            caller=symbol,
                            callee=f"{module}.{callee}",
                            file=rel_file,
                            line=line_of(node),
                        )
                    )

    def _maybe_supabase(
        self,
        analysis: JsAnalysis,
        node: Node,
        method: str,
        args: list[Node],
        rel_file: str,
        module: str,
        consts: dict[str, str],
        symbol: str | None = None,
    ) -> None:
        table = _supabase_table(node, consts)
        if not table:
            return
        kind = None
        if method in SUPABASE_WRITE:
            kind = "write"
        elif method in SUPABASE_READ:
            kind = "read"
        if kind is None:
            return
        sym = symbol or _enclosing_symbol(node, module)
        analysis.accesses.append(
            JsAccess(
                kind=kind,
                table=table,
                file=rel_file,
                line=line_of(node),
                symbol=sym,
                target_kind="table",
            )
        )
        analysis.table_accesses.append(
            JsTableAccess(
                symbol=sym,
                kind=kind,
                table=table,
                file=rel_file,
                line=line_of(node),
            )
        )


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


def _collect_consts(tree: Node) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in walk(tree):
        if node.type != "variable_declarator":
            continue
        name = _declarator_name(node)
        value = node.child_by_field_name("value")
        if not name or value is None:
            continue
        val, unresolved = _resolve_js_string(value, consts)
        if val is not None and not unresolved:
            consts[name] = val
    return consts


def _collect_receivers(tree: Node) -> tuple[dict[str, str], set[str]]:
    routers: dict[str, str] = {}
    clients: set[str] = set()
    for node in walk(tree):
        if node.type != "variable_declarator":
            continue
        name = _declarator_name(node)
        value = node.child_by_field_name("value")
        if not name or value is None:
            continue
        raw = text(node)
        if _is_router_factory(value) or _typed_as_router(raw):
            routers[name] = ""
            continue
        if value.type == "call_expression":
            callee = call_callee_name(value)
            recv = _receiver_ident(value)
            if callee in {"create"} and recv.lower() in CLIENT_LIBS:
                clients.add(name)
            if callee in CLIENT_LIBS:
                clients.add(name)
        if value.type == "identifier" and text(value) in CLIENT_LIBS:
            clients.add(name)
        if value.type == "new_expression":
            ctor = value.child_by_field_name("constructor") or (
                value.named_children[0] if value.named_children else None
            )
            ctor_name = text(ctor) if ctor is not None else ""
            if ctor_name.split(".")[-1] in ROUTER_CTORS:
                routers[name] = ""
    # Nested groups: const admin = app.group("/admin") / app.route("/admin")
    for node in walk(tree):
        if node.type != "variable_declarator":
            continue
        name = _declarator_name(node)
        value = node.child_by_field_name("value")
        if not name or value is None or value.type != "call_expression":
            continue
        method = call_callee_name(value).lower()
        recv = _receiver_ident(value)
        if recv in routers and method in {"group", "route", "router"}:
            args = call_args(value)
            prefix, _u = _resolve_js_string(args[0], {}) if args else ("", False)
            routers[name] = (routers.get(recv) or "") + (prefix or "")
    return routers, clients


def _collect_imports(tree: Node, module: str) -> dict[str, str]:
    """local name → module.original (best-effort ES import)."""
    out: dict[str, str] = {}
    for node in walk(tree):
        if node.type not in {"import_statement", "lexical_declaration"}:
            continue
        raw = text(node)
        if "from" not in raw and "require(" not in raw:
            continue
        src = ""
        m = re.search(r"""from\s+['"]([^'"]+)['"]""", raw)
        if m:
            src = m.group(1)
        else:
            m = re.search(r"""require\(\s*['"]([^'"]+)['"]""", raw)
            if m:
                src = m.group(1)
        if not src:
            continue
        src_mod = _spec_to_module(src, module)
        for ident in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", raw.split("from")[0] if "from" in raw else raw):
            if ident in {"import", "const", "let", "var", "from", "as", "require"}:
                continue
            out[ident] = f"{src_mod}.{ident}"
    return out


def _spec_to_module(spec: str, current: str) -> str:
    spec = spec.strip()
    if spec.startswith("."):
        parts = current.split(".")
        rest = spec
        while rest.startswith("../"):
            parts = parts[:-1]
            rest = rest[3:]
        if rest.startswith("./"):
            rest = rest[2:]
        rest = rest.rsplit(".", 1)[0]
        bits = [p for p in rest.split("/") if p and p != "."]
        return ".".join([*parts[:-1], *bits]) if bits else ".".join(parts[:-1]) or current
    return spec.replace("/", ".")


def _supabase_table(call: Node, consts: dict[str, str]) -> str | None:
    cur: Node | None = call
    while cur is not None:
        if cur.type == "call_expression" and call_callee_name(cur) == "from":
            args = call_args(cur)
            if args:
                val, unresolved = _resolve_js_string(args[0], consts)
                if val and not unresolved:
                    return val
                return "unknown"
        func = cur.child_by_field_name("function") if cur.type == "call_expression" else None
        if func is not None and func.type == "member_expression" and func.named_children:
            cur = func.named_children[0]
            continue
        if cur.type == "member_expression" and cur.named_children:
            cur = cur.named_children[0]
            continue
        break
    return None
