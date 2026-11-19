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
ROUTE_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "all"}
CLIENT_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
ROUTER_FACTORIES = {"express", "Router", "fastify", "Fastify", "Hono", "Koa", "createApp"}
ROUTER_CTORS = {"Hono", "Koa", "Express"}
CLIENT_LIBS = {"axios", "fetch", "got", "ky", "ofetch", "superagent", "http", "https", "request"}
CLIENT_CREATE_KEYS = ("baseURL", "baseUrl", "prefixUrl", "prefix", "base")
SUPABASE_WRITE = {"insert", "upsert", "update", "delete"}
SUPABASE_READ = {"select", "maybeSingle", "single"}
PG_QUERY_METHODS = {"query", "exec", "execute"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_MEMBER = re.compile(
    r"^(?:process\.env|Bun\.env|import\.meta\.env)\.([A-Za-z_][A-Za-z0-9_]*)$"
)
_ENV_INDEX = re.compile(
    r"""^(?:process\.env|Bun\.env|import\.meta\.env)\[(['"])([A-Za-z_][A-Za-z0-9_]*)\1\]$"""
)


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


def _join_route(prefix: str, path: str) -> str:
    """Join a router prefix with a path or URL fragment."""
    if not prefix:
        if not path:
            return ""
        if path.startswith("/") or path.startswith("{") or "://" in path:
            return path
        return "/" + path
    if not path:
        return prefix
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if prefix.endswith("/") and path.startswith("/"):
        return prefix[:-1] + path
    if "://" in prefix and not path.startswith("/") and not path.startswith("{"):
        return prefix.rstrip("/") + "/" + path
    if not path.startswith("/") and not path.startswith("{") and not prefix.endswith("/"):
        return prefix.rstrip("/") + "/" + path
    return prefix.rstrip("/") + path


def _env_placeholder(raw: str) -> str | None:
    m = _ENV_MEMBER.match(raw.strip())
    if m:
        return "{" + m.group(1) + "}"
    m = _ENV_INDEX.match(raw.strip())
    if m:
        return "{" + m.group(2) + "}"
    return None


def _resolve_js_string(node: Node | None, consts: dict[str, str]) -> tuple[str | None, bool]:
    """Return (value, unresolved) for a string, template, concat, env, or const."""
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
    if node.type in {"member_expression", "subscript_expression"}:
        raw = text(node)
        if raw in consts:
            return consts[raw], "{" in consts[raw]
        env = _env_placeholder(raw)
        if env:
            return env, True
        if node.type == "subscript_expression":
            obj = node.child_by_field_name("object") or (
                node.named_children[0] if node.named_children else None
            )
            idx = node.child_by_field_name("index") or (
                node.named_children[1] if len(node.named_children) > 1 else None
            )
            if obj is not None and text(obj) in {"process.env", "Bun.env", "import.meta.env"}:
                key, _u = _resolve_js_string(idx, consts)
                if key:
                    return "{" + key + "}", True
        return None, False
    if node.type == "binary_expression":
        op_nodes = [c for c in node.children if not c.is_named]
        op = text(op_nodes[0]) if op_nodes else "+"
        if op != "+":
            return None, False
        left = node.child_by_field_name("left") or (
            node.named_children[0] if node.named_children else None
        )
        right = node.child_by_field_name("right") or (
            node.named_children[1] if len(node.named_children) > 1 else None
        )
        lv, lu = _resolve_js_string(left, consts)
        rv, ru = _resolve_js_string(right, consts)
        if lv is None and left is not None and left.type == "identifier":
            lv, lu = "{" + text(left) + "}", True
        if rv is None and right is not None and right.type == "identifier":
            rv, ru = "{" + text(right) + "}", True
        if lv is None and rv is None:
            return None, False
        return (lv or "") + (rv or ""), lu or ru or lv is None or rv is None
    if node.type == "parenthesized_expression" and node.named_children:
        return _resolve_js_string(node.named_children[0], consts)
    if node.type == "call_expression":
        callee = call_callee_name(node)
        recv = _receiver_ident(node)
        args = call_args(node)
        raw = text(node.child_by_field_name("function") or node)
        if callee in {"get", "getenv"} and (
            "env" in raw.lower() or recv in {"Deno", "process", "Bun"}
        ):
            if args:
                key, _u = _resolve_js_string(args[0], consts)
                if key:
                    return "{" + key.strip("{}") + "}", True
        if callee in consts:
            return consts[callee], "{" in consts[callee]
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


def _call_chain_inward(node: Node | None) -> list[Node]:
    """Walk ``new Hono().use().get()`` from the outer call to the root factory."""
    chain: list[Node] = []
    cur: Node | None = node
    seen = 0
    while cur is not None and seen < 32:
        seen += 1
        chain.append(cur)
        if cur.type == "call_expression":
            func = cur.child_by_field_name("function")
            if func is not None and func.type == "member_expression" and func.named_children:
                cur = func.named_children[0]
                continue
            break
        if cur.type == "new_expression":
            break
        if cur.type == "member_expression" and cur.named_children:
            cur = cur.named_children[0]
            continue
        break
    return chain


def _ctor_leaf(node: Node | None) -> str:
    if node is None or node.type != "new_expression":
        return ""
    ctor = node.child_by_field_name("constructor")
    if ctor is None or ctor.type == "type_arguments":
        ctor = next(
            (c for c in node.named_children if c.type not in {"type_arguments", "arguments"}),
            None,
        )
    if ctor is None:
        return ""
    raw = text(ctor)
    if "<" in raw:
        raw = raw.split("<", 1)[0]
    return raw.split(".")[-1].strip()


def _is_bare_router_factory(node: Node | None) -> bool:
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
        return _ctor_leaf(node) in ROUTER_CTORS
    return False


def _is_router_factory(node: Node | None) -> bool:
    """True when ``node`` is (or chains from) ``new Hono()`` / ``express()`` / ``Router()``."""
    if node is None:
        return False
    if _is_bare_router_factory(node):
        return True
    chain = _call_chain_inward(node)
    if not chain:
        return False
    return _is_bare_router_factory(chain[-1])


def _mount_method_prefix(call: Node, consts: dict[str, str]) -> str:
    if call.type != "call_expression":
        return ""
    method = call_callee_name(call)
    method_l = method.lower()
    args = call_args(call)
    if method in {"basePath", "basepath"} or method_l == "basepath":
        if args:
            val, _u = _resolve_js_string(args[0], consts)
            return val or ""
    if method_l in {"route", "group"} and args:
        val, _u = _resolve_js_string(args[0], consts)
        return val or ""
    return ""


def _router_prefix_of(value: Node, consts: dict[str, str], routers: dict[str, str]) -> str:
    """Prefix stored on an assigned router, including its own `.basePath()` / `.route()`."""
    prefix = _chain_prefix(value, consts, routers)
    extra = _mount_method_prefix(value, consts)
    return _join_route(prefix, extra) if extra else prefix


def _chain_prefix(call: Node, consts: dict[str, str], routers: dict[str, str]) -> str:
    """Prefix from ``.basePath('/v1')`` / ``.route('/x')`` / a named router receiver."""
    chain = _call_chain_inward(call)
    prefix = ""
    for n in reversed(chain[1:]):
        if n.type == "identifier":
            prefix = (routers.get(text(n)) or "") + prefix
            continue
        if n.type == "call_expression":
            method = call_callee_name(n)
            method_l = method.lower()
            args = call_args(n)
            if method in {"basePath", "basepath"} or method_l == "basepath":
                if args:
                    val, _u = _resolve_js_string(args[0], consts)
                    if val:
                        prefix = _join_route(prefix, val)
            elif method_l == "route" and args:
                val, _u = _resolve_js_string(args[0], consts)
                if val and (len(args) == 1 or args[0].type in {"string", "template_string", "identifier"}):
                    prefix = _join_route(prefix, val)
    return prefix


def _object_prop(obj: Node | None, keys: tuple[str, ...], consts: dict[str, str]) -> tuple[str | None, bool]:
    if obj is None or obj.type != "object":
        return None, False
    for child in obj.named_children:
        if child.type != "pair":
            continue
        key_n = child.child_by_field_name("key")
        val_n = child.child_by_field_name("value")
        key = text(key_n).strip("'\"") if key_n is not None else ""
        if key in keys:
            return _resolve_js_string(val_n, consts)
    return None, False


def _fn_param_names(node: Node | None) -> list[str]:
    if node is None:
        return []
    params = node.child_by_field_name("parameters")
    if params is None:
        for child in node.named_children:
            if child.type in {"formal_parameters", "parameters"}:
                params = child
                break
    names: list[str] = []
    if params is None:
        for child in node.named_children:
            if child.type == "identifier":
                names.append(text(child))
                break
            if child.type in {"required_parameter", "optional_parameter"}:
                ident = child.named_children[0] if child.named_children else None
                if ident is not None and ident.type == "identifier":
                    names.append(text(ident))
                break
        return names
    for child in params.named_children:
        if child.type == "identifier":
            names.append(text(child))
        elif child.type in {"required_parameter", "optional_parameter", "rest_parameter"}:
            ident = child.child_by_field_name("pattern") or (
                child.named_children[0] if child.named_children else None
            )
            if ident is not None and ident.type == "identifier":
                names.append(text(ident))
    return names


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
        clients_by_mod: dict[str, dict[str, str]] = {}
        wrappers_by_mod: dict[str, dict[str, tuple[str, str]]] = {}
        fn_nodes: dict[str, Node] = {}
        imports_by_mod: dict[str, dict[str, str]] = {}

        for _path, rel_file, module, tree, _src in parsed:
            consts = _collect_consts(tree)
            consts_by_mod[module] = consts
            routers, clients = _collect_receivers(tree, consts)
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
            wrappers_by_mod[module] = _collect_wrappers(tree, consts, clients)

        for _path, rel_file, module, tree, _src in parsed:
            consts = consts_by_mod.get(module) or {}
            routers = routers_by_mod.get(module) or {}
            clients = clients_by_mod.get(module) or {}
            wrappers = wrappers_by_mod.get(module) or {}
            imports = imports_by_mod.get(module) or {}
            self._scan_file(
                analysis,
                tree,
                rel_file,
                module,
                consts,
                routers,
                clients,
                wrappers,
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
        clients: dict[str, str],
        wrappers: dict[str, tuple[str, str]],
        imports: dict[str, str],
        fn_nodes: dict[str, Node],
    ) -> None:
        for node in walk(tree):
            if node.type != "call_expression":
                continue
            method = call_callee_name(node).lower()
            raw_method = call_callee_name(node)
            args = call_args(node)
            recv = _receiver_ident(node)
            chain_router = _is_router_factory(node)
            if method in HTTP_METHODS and args:
                path_val, path_unresolved = _resolve_js_string(args[0], consts)
                prefix = _chain_prefix(node, consts, routers)
                is_router = recv in routers or chain_router
                if is_router and method == "use":
                    continue
                if is_router and method == "route":
                    continue
                if is_router and method in ROUTE_HTTP_METHODS and path_val is not None and (
                    path_val.startswith("/") or path_val.startswith("{") or "://" in path_val
                ):
                    handler_node = args[-1] if len(args) >= 2 else None
                    handler_qual, handler_label = self._handler_symbol(
                        handler_node, module, imports
                    )
                    path_norm = _join_route(prefix, path_val)
                    if not (
                        path_norm.startswith("/") or path_norm.startswith("{") or "://" in path_norm
                    ):
                        path_norm = "/" + path_norm
                    if handler_qual is None and handler_label == "anonymous":
                        handler_qual = f"{module}.{method.upper()} {path_norm}"
                    route = JsRoute(
                        method=method.upper(),
                        path=path_norm,
                        handler=handler_qual or f"{module}.{handler_label}",
                        file=rel_file,
                        line=line_of(node),
                        unresolved=path_unresolved or ("{" in path_norm and "://" not in path_norm),
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
                if method in CLIENT_HTTP_METHODS:
                    if recv.lower() in {"map", "set", "weakmap", "weakset"}:
                        continue
                    if is_router:
                        continue
                    base = clients.get(recv) or ""
                    composed = _join_route(base, path_val) if path_val else base
                    if not composed or not _looks_like_http_path(composed):
                        continue
                    analysis.http_calls.append(
                        JsHttpCall(
                            method=method.upper(),
                            url=composed,
                            file=rel_file,
                            line=line_of(node),
                            symbol=_enclosing_symbol(node, module),
                            unresolved=path_unresolved or ("{" in composed),
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
                        symbol=_enclosing_symbol(node, module),
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
                            symbol=_enclosing_symbol(node, module),
                            unresolved=unresolved,
                        )
                    )
            wrap = wrappers.get(raw_method) or wrappers.get(method)
            if wrap and args:
                http_method, base = wrap
                path_val, path_unresolved = _resolve_js_string(args[0], consts)
                if path_val:
                    composed = _join_route(base, path_val)
                    if _looks_like_http_path(composed):
                        analysis.http_calls.append(
                            JsHttpCall(
                                method=http_method,
                                url=composed,
                                file=rel_file,
                                line=line_of(node),
                                symbol=_enclosing_symbol(node, module),
                                unresolved=path_unresolved or ("{" in composed),
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
            if method == "serve" and recv in {"", "Deno"}:
                self._record_serve(
                    analysis,
                    node,
                    args,
                    rel_file,
                    module,
                    consts,
                    imports,
                    fn_nodes,
                )

    def _record_serve(
        self,
        analysis: JsAnalysis,
        node: Node,
        args: list[Node],
        rel_file: str,
        module: str,
        consts: dict[str, str],
        imports: dict[str, str],
        fn_nodes: dict[str, Node],
    ) -> None:
        if not args:
            return
        handler_node = _unwrap_hof_handler(args[0])
        edge_name = _supabase_fn_name(rel_file)
        path = f"/functions/v1/{edge_name}" if edge_name else "/"
        handler_qual, handler_label = self._handler_symbol(handler_node, module, imports)
        symbol = handler_qual or f"{module}.serve"
        if handler_qual is None and handler_label == "anonymous":
            symbol = f"{module}.serve"
        analysis.functions.setdefault(symbol, (rel_file, line_of(node)))
        analysis.routes.append(
            JsRoute(
                method="POST",
                path=path,
                handler=symbol,
                file=rel_file,
                line=line_of(node),
                handler_qual=handler_qual,
            )
        )
        self._scan_handler(
            analysis,
            handler_node,
            symbol,
            rel_file,
            module,
            consts,
            imports,
            fn_nodes,
        )

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


def _unwrap_hof_handler(node: Node | None) -> Node | None:
    """Unwrap ``withAuth(async (req) => ...)`` / named handlers for ``serve(...)``."""
    cur = node
    for _ in range(8):
        if cur is None:
            return None
        if cur.type in {
            "arrow_function",
            "function",
            "function_expression",
            "function_declaration",
            "identifier",
        }:
            return cur
        if cur.type == "call_expression":
            args = call_args(cur)
            nxt = None
            for arg in args:
                if arg.type in {
                    "arrow_function",
                    "function",
                    "function_expression",
                    "identifier",
                    "call_expression",
                }:
                    nxt = arg
            cur = nxt
            continue
        if cur.type == "await_expression" and cur.named_children:
            cur = cur.named_children[0]
            continue
        if cur.type == "parenthesized_expression" and cur.named_children:
            cur = cur.named_children[0]
            continue
        break
    return cur


def _enclosing_symbol(node: Node, module: str) -> str:
    cur = node.parent
    while cur is not None:
        if cur.type in {"function_declaration", "method_definition", "function", "function_expression"}:
            name_n = cur.child_by_field_name("name")
            name = text(name_n) if name_n else ""
            if name:
                return f"{module}.{name}"
        if cur.type == "arrow_function":
            parent = cur.parent
            if parent is not None and parent.type == "variable_declarator":
                name = _declarator_name(parent)
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
        if val is not None:
            consts[name] = val
            _ = unresolved
    return consts


def _collect_receivers(tree: Node, consts: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    consts = consts or {}
    routers: dict[str, str] = {}
    clients: dict[str, str] = {}
    for node in walk(tree):
        if node.type != "variable_declarator":
            continue
        name = _declarator_name(node)
        value = node.child_by_field_name("value")
        if not name or value is None:
            continue
        raw = text(node)
        if _is_router_factory(value) or _typed_as_router(raw):
            routers[name] = _router_prefix_of(value, consts, routers)
            continue
        if value.type == "call_expression":
            callee = call_callee_name(value)
            recv = _receiver_ident(value)
            args = call_args(value)
            if callee in {"create"} and recv.lower() in CLIENT_LIBS:
                base, _u = _object_prop(args[0] if args else None, CLIENT_CREATE_KEYS, consts)
                clients[name] = base or ""
            elif callee in CLIENT_LIBS:
                base, _u = _object_prop(args[0] if args else None, CLIENT_CREATE_KEYS, consts)
                clients[name] = base or ""
        if value.type == "identifier" and text(value) in CLIENT_LIBS:
            clients[name] = ""
        if value.type == "new_expression" and _ctor_leaf(value) in ROUTER_CTORS:
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
        if recv in routers and method in {"group", "route", "router", "basepath"}:
            args = call_args(value)
            prefix, _u = _resolve_js_string(args[0], consts) if args else ("", False)
            routers[name] = _join_route(routers.get(recv) or "", prefix or "")
            if method == "basepath":
                routers[name] = _join_route(routers.get(recv) or "", prefix or "")
    # app.route('/api', sub) / app.use('/api', sub) mounts
    for node in walk(tree):
        if node.type != "call_expression":
            continue
        method = call_callee_name(node).lower()
        recv = _receiver_ident(node)
        if recv not in routers or method not in {"route", "use", "group"}:
            continue
        args = call_args(node)
        if len(args) < 2:
            continue
        prefix, _u = _resolve_js_string(args[0], consts)
        if not prefix or prefix in {"*", "/*"}:
            continue
        sub = args[1]
        sub_name = text(sub) if sub.type == "identifier" else ""
        if sub_name and (sub_name in routers or sub_name):
            routers[sub_name] = _join_route(routers.get(recv) or "", prefix)
    return routers, clients


def _collect_wrappers(
    tree: Node, consts: dict[str, str], clients: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """Functions that prepend a base const/env to a path argument."""
    wrappers: dict[str, tuple[str, str]] = {}

    def consider(name: str, fn_node: Node) -> None:
        params = _fn_param_names(fn_node)
        if not name or not params:
            return
        path_param = params[0]
        extra = dict(consts)
        extra[path_param] = ""
        for node in walk(fn_node):
            if node.type != "call_expression":
                continue
            method = call_callee_name(node).lower()
            args = call_args(node)
            recv = _receiver_ident(node)
            url_node = args[0] if args else None
            http_method = "GET"
            is_http = False
            if method == "fetch":
                is_http = True
                if len(args) > 1:
                    raw = text(args[1])
                    for m in HTTP_METHODS:
                        if f"method: '{m.upper()}'" in raw or f'method: "{m.upper()}"' in raw:
                            http_method = m.upper()
            elif method in CLIENT_HTTP_METHODS and (
                recv in clients or recv.lower() in CLIENT_LIBS
            ):
                is_http = True
                http_method = method.upper()
            if not is_http:
                continue
            url, _u = _resolve_js_string(url_node, extra)
            base = ""
            if recv in clients:
                base = clients[recv]
            if url:
                base = _join_route(base, url)
            if not base:
                continue
            wrappers[name] = (http_method, base.rstrip("/"))
            return

    for node in walk(tree):
        if node.type in {"function_declaration", "function"}:
            name_n = node.child_by_field_name("name")
            name = text(name_n) if name_n else ""
            consider(name, node)
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            name = _declarator_name(node)
            if name and value is not None and value.type in {"arrow_function", "function"}:
                consider(name, value)
    return wrappers


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
