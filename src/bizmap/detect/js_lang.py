"""JavaScript/TypeScript detector (Express/Fastify/Hono routes, fetch, supabase, pg)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Node, Parser

from bizmap.detect.tsutil import (
    call_args,
    call_callee_name,
    line_of,
    text,
    unwrap_callee_expr,
    walk,
)
from bizmap.files import SKIP_DIR_NAMES, rel
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
CLIENT_REQUEST_METHODS = CLIENT_HTTP_METHODS | {"request"}
HTTP_PATH_PARAM_NAMES = {"url", "path", "endpoint", "route", "pathname", "uri"}
HTTP_METHOD_PARAM_NAMES = {"method", "verb"}
WRAPPER_KEEP_HOLES = {
    "base",
    "baseurl",
    "prefixurl",
    "prefix",
    "origin",
    "host",
    "client",
    "http",
    "api",
    "transport",
    "session",
    "instance",
}
ROUTER_FACTORIES = {"express", "Router", "fastify", "Fastify", "Hono", "Koa", "createApp"}
ROUTER_CTORS = {"Hono", "Koa", "Express"}
CLIENT_LIBS = {"axios", "fetch", "got", "ky", "ofetch", "superagent", "http", "https", "request"}
CLIENT_CREATE_KEYS = ("baseURL", "baseUrl", "prefixUrl", "prefix", "base")
NON_HTTP_RECEIVERS = {
    "map",
    "set",
    "weakmap",
    "weakset",
    "headers",
    "urlsearchparams",
    "searchparams",
}
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
    if path in {"/", ""}:
        if not prefix:
            return path or "/"
        return prefix.rstrip("/") or "/"
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
        leaf = raw.rsplit(".", 1)[-1]
        if leaf in consts and (leaf[:1].isupper() or _ENV_LIKE.match(leaf)):
            return consts[leaf], "{" in consts[leaf]
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
        helper = _apply_path_helper(node, consts)
        if helper is not None:
            return helper, True
        unwrapped = _transparent_arg(node)
        if unwrapped is not None:
            if unwrapped.type == "identifier" and text(unwrapped) not in consts:
                return "{" + text(unwrapped) + "}", True
            if unwrapped.type == "member_expression":
                val, _u = _resolve_js_string(unwrapped, consts)
                if val is None:
                    return "{" + text(unwrapped).rsplit(".", 1)[-1] + "}", True
                return val, _u
            return _resolve_js_string(unwrapped, consts)
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
    if inner.type in {"member_expression", "subscript_expression"}:
        val, unresolved = _resolve_js_string(inner, consts)
        if val:
            return val, unresolved
        return "{" + text(inner) + "}", True
    if inner.type == "call_expression":
        helper = _apply_path_helper(inner, consts)
        if helper is not None:
            return helper, True
        unwrapped = _transparent_arg(inner)
        if unwrapped is not None:
            return _resolve_substitution(unwrapped, consts)
        callee = call_callee_name(inner)
        args = call_args(inner)
        val, unresolved = _resolve_js_string(inner, consts)
        if val:
            return val, unresolved
        if callee in consts:
            # ``base(id)`` with resolvable base const — keep the base, drop args.
            base = consts[callee].rstrip("/")
            return base, False
        arg_bits = ",".join(text(a) for a in args)
        return "{" + f"{callee}({arg_bits})" + "}", True
    if inner.type in {"string", "template_string"}:
        val, unresolved = _resolve_js_string(inner, consts)
        return val or ("{" + text(inner) + "}"), unresolved
    val, unresolved = _resolve_js_string(inner, consts)
    if val:
        return val, unresolved
    return "{" + text(inner) + "}", True


_HTTP_PATH_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_LIKE = re.compile(r"^[A-Z][A-Z0-9_]+$")


def _normalize_http_url(url: str) -> str:
    """Turn template holes like ``{id}`` into ``:id``; keep ``{VITE_API_URL}`` / ``{base}``."""

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.lower() in WRAPPER_KEEP_HOLES:
            return match.group(0)
        if _ENV_LIKE.match(name):
            return match.group(0)
        return ":" + name

    return _HTTP_PATH_PARAM.sub(repl, url)


def _js_call_is_interceptor(call: Node) -> bool:
    raw = text(call.child_by_field_name("function") or call).lower()
    return ".interceptors." in raw


def _url_uses_enclosing_path_param(url: str, params: list[str]) -> bool:
    """True when the URL is the enclosing wrapper's path argument (``BASE + url``).

    Resource ids (``get(id) => client.get(`/orders/${id}`)``) are not path
    wrappers — those calls must be recorded on the method itself.
    """
    for name in params:
        if name.lower() not in HTTP_PATH_PARAM_NAMES:
            continue
        if f":{name}" in url or "{" + name + "}" in url:
            return True
    return False


def _js_client_url_and_method(
    method: str, args: list[Node], consts: dict[str, str]
) -> tuple[str | None, bool, str]:
    """Path + HTTP method for ``.get(url)`` or ``.request({method, url})``."""
    http_method = method.upper() if method != "request" else "POST"
    path_val: str | None = None
    unresolved = False
    if not args:
        return None, False, http_method
    first = args[0]
    first = unwrap_callee_expr(first) or first
    if first.type == "object":
        mnode = _object_prop_node(first, ("method", "httpMethod", "http_method"))
        unode = _object_prop_node(first, ("url", "path", "href"))
        if mnode is not None:
            mval, _u = _resolve_js_string(mnode, consts)
            if mval:
                http_method = mval.upper()
        if unode is not None:
            path_val, unresolved = _resolve_js_string(unode, consts)
        return path_val, unresolved, http_method
    if method == "request" and len(args) >= 2:
        mval, _u = _resolve_js_string(first, consts)
        if mval:
            http_method = mval.upper()
        path_val, unresolved = _resolve_js_string(args[1], consts)
        return path_val, unresolved, http_method
    path_val, unresolved = _resolve_js_string(first, consts)
    return path_val, unresolved, http_method


def _apply_yaml_js_consts(consts: dict[str, str], constants: dict[str, str]) -> None:
    for key, val in (constants or {}).items():
        consts.setdefault(str(key), str(val))
        if "." in str(key):
            consts.setdefault(str(key).rsplit(".", 1)[-1], str(val))


def _create_call_base(node: Node | None, consts: dict[str, str]) -> str | None:
    """Base URL from ``axios.create({baseURL})`` / ``ky.create({prefixUrl})``."""
    if node is None:
        return None
    node = unwrap_callee_expr(node) or node
    if node.type != "call_expression":
        return None
    callee = call_callee_name(node)
    recv = _receiver_ident(node)
    if callee == "create" and recv.lower() in CLIENT_LIBS:
        args = call_args(node)
        base, _u = _object_prop(args[0] if args else None, CLIENT_CREATE_KEYS, consts)
        return base or ""
    if callee in CLIENT_LIBS:
        args = call_args(node)
        base, _u = _object_prop(args[0] if args else None, CLIENT_CREATE_KEYS, consts)
        return base or ""
    return None


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
    while cur is not None and seen < 512:
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
        # `.route('/x', child)` is a mount, not the parent's own prefix.
        if len(args) >= 2:
            return ""
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
            elif method_l == "route" and args and len(args) < 2:
                val, _u = _resolve_js_string(args[0], consts)
                if val and args[0].type in {"string", "template_string", "identifier"}:
                    prefix = _join_route(prefix, val)
    return prefix


def _unwrap_object_node(obj: Node | None) -> Node | None:
    cur = unwrap_callee_expr(obj) or obj
    if cur is not None and cur.type == "object":
        return cur
    return obj if obj is not None and obj.type == "object" else cur


def _object_prop_node(obj: Node | None, keys: tuple[str, ...]) -> Node | None:
    obj = _unwrap_object_node(obj)
    if obj is None or obj.type != "object":
        return None
    want = {k.lower() for k in keys}
    for child in obj.named_children:
        if child.type == "pair":
            key_n = child.child_by_field_name("key")
            val_n = child.child_by_field_name("value")
            key = text(key_n).strip("'\"") if key_n is not None else ""
            if key.lower() in want:
                return val_n
        elif child.type in {
            "shorthand_property_identifier",
            "shorthand_property_identifier_pattern",
        }:
            if text(child).lower() in want:
                return child
    return None


def _object_prop(obj: Node | None, keys: tuple[str, ...], consts: dict[str, str]) -> tuple[str | None, bool]:
    val_n = _object_prop_node(obj, keys)
    if val_n is None:
        return None, False
    return _resolve_js_string(val_n, consts)


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

    def _param_ident(child: Node) -> str:
        ident = child.child_by_field_name("pattern") or child.child_by_field_name("name")
        if ident is not None and ident.type == "identifier":
            return text(ident)
        for inner in child.named_children:
            if inner.type == "identifier":
                return text(inner)
            if inner.type in {"required_parameter", "optional_parameter"}:
                found = _param_ident(inner)
                if found:
                    return found
        return ""

    if params is None:
        for child in node.named_children:
            if child.type == "identifier":
                names.append(text(child))
                break
            if child.type in {"required_parameter", "optional_parameter"}:
                ident = _param_ident(child)
                if ident:
                    names.append(ident)
                break
        return names
    for child in params.named_children:
        if child.type == "identifier":
            names.append(text(child))
        elif child.type in {"required_parameter", "optional_parameter", "rest_parameter"}:
            ident = _param_ident(child)
            if ident:
                names.append(ident)
    return names


def _fn_param_entries(node: Node | None) -> list[tuple[str, str]]:
    """Return (name, type_text) for each parameter, including TS annotations."""
    if node is None:
        return []
    params = node.child_by_field_name("parameters")
    if params is None:
        for child in node.named_children:
            if child.type in {"formal_parameters", "parameters"}:
                params = child
                break
    entries: list[tuple[str, str]] = []

    def _one(child: Node) -> tuple[str, str]:
        ident = child.child_by_field_name("pattern") or child.child_by_field_name("name")
        name = ""
        if ident is not None and ident.type == "identifier":
            name = text(ident)
        else:
            for inner in child.named_children:
                if inner.type == "identifier":
                    name = text(inner)
                    break
                if inner.type in {"required_parameter", "optional_parameter"}:
                    found_name, _t = _one(inner)
                    if found_name:
                        return found_name, _t
        type_n = child.child_by_field_name("type")
        type_txt = text(type_n).lstrip(": ").strip() if type_n is not None else ""
        return name, type_txt

    if params is None:
        for child in node.named_children:
            if child.type == "identifier":
                entries.append((text(child), ""))
                break
            if child.type in {"required_parameter", "optional_parameter"}:
                name, typ = _one(child)
                if name:
                    entries.append((name, typ))
                break
        return entries
    for child in params.named_children:
        if child.type == "identifier":
            entries.append((text(child), ""))
        elif child.type in {"required_parameter", "optional_parameter", "rest_parameter"}:
            name, typ = _one(child)
            if name:
                entries.append((name, typ))
    return entries


_HTTP_CLIENT_TYPE = re.compile(
    r"\b(AxiosInstance|KyInstance|AxiosStatic|Axios)\b"
)
_TYPEOF_CLIENT = re.compile(r"\btypeof\s+([A-Za-z_][A-Za-z0-9_]*)")


def _type_is_http_client(type_text: str, clients: dict[str, str] | None = None) -> bool:
    """True for AxiosInstance, Pick/Partial of it, KyInstance, or typeof a client."""
    if not type_text:
        return False
    compact = re.sub(r"\s+", "", type_text)
    if _HTTP_CLIENT_TYPE.search(compact) or _HTTP_CLIENT_TYPE.search(type_text):
        return True
    m = _TYPEOF_CLIENT.search(type_text)
    if m:
        ident = m.group(1)
        if clients is not None and ident in clients:
            return True
        if ident.lower() in CLIENT_LIBS:
            return True
    return False


def _assigned_name(node: Node | None) -> str:
    """Identifier of ``const r = <node>`` / ``export const r = <node>``."""
    cur = node
    depth = 0
    while cur is not None and depth < 512:
        if cur.type == "variable_declarator":
            return _declarator_name(cur)
        cur = cur.parent
        depth += 1
    return ""


def _enclosing_js_params(node: Node | None) -> list[str]:
    """Parameter names of every enclosing function/arrow (for nested factories)."""
    names: list[str] = []
    cur = node.parent if node is not None else None
    depth = 0
    while cur is not None and depth < 48:
        if cur.type in {
            "function_declaration",
            "function",
            "function_expression",
            "arrow_function",
            "method_definition",
        }:
            names.extend(_fn_param_names(cur))
        cur = cur.parent
        depth += 1
    return names


def _receiver_ident(call: Node) -> str:
    func = call.child_by_field_name("function")
    func = unwrap_callee_expr(func) or func
    if func is None:
        return ""
    if func.type in {"member_expression", "attribute"}:
        obj = func.named_children[0] if func.named_children else None
        if obj is None:
            return ""
        if obj.type == "identifier":
            return text(obj)
        if obj.type in {"member_expression", "attribute"}:
            raw = text(obj)
            if raw.startswith("this.") or raw.startswith("self."):
                return raw
            return text(obj.named_children[0]) if obj.named_children else raw
        if obj.type == "this":
            return "this"
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
    client_unknown: bool = False


@dataclass
class JsHttpWrapper:
    method: str
    base: str
    path_index: int = 0
    method_index: int | None = None


@dataclass
class JsClientFactory:
    base: str
    params: list[str] = field(default_factory=list)


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
class JsRouteLiteral:
    """A string/template that looks like an API path, wherever it appears.

    Linked to backend routes by path (``via: route_literal``) when the client
    that sends it could not be traced through factories / DI / wrappers.
    """

    path: str
    method: str | None
    file: str
    line: int
    symbol: str
    same_repo: bool = False


@dataclass
class JsAnalysis:
    routes: list[JsRoute] = field(default_factory=list)
    http_calls: list[JsHttpCall] = field(default_factory=list)
    route_literals: list[JsRouteLiteral] = field(default_factory=list)
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

    def analyze(
        self,
        files: list[Path],
        root: Path,
        constants: dict[str, str] | None = None,
    ) -> JsAnalysis:
        analysis = JsAnalysis()
        yaml_constants = constants or {}
        path_aliases = _ts_path_aliases(root)
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
        base_routers_by_mod: dict[str, dict[str, str]] = {}
        clients_by_mod: dict[str, dict[str, str]] = {}
        wrappers_by_mod: dict[str, dict[str, JsHttpWrapper]] = {}
        fn_nodes: dict[str, Node] = {}
        imports_by_mod: dict[str, dict[str, str]] = {}
        import_specs_by_mod: dict[str, dict[str, tuple[str, str]]] = {}
        exports_by_mod: dict[str, dict[str, str]] = {}
        factories_by_mod: dict[str, dict[str, JsClientFactory]] = {}
        class_ctors_by_mod: dict[str, dict[str, list[str]]] = {}
        trees_by_mod: dict[str, Node] = {}
        mounts: list[tuple[str, str, str, str, str]] = []

        for _path, rel_file, module, tree, _src in parsed:
            consts = _collect_consts(tree)
            _apply_yaml_js_consts(consts, yaml_constants)
            consts.update(_collect_path_helpers(tree, consts))
            consts_by_mod[module] = consts
            routers, clients = _collect_receivers(tree, consts)
            routers_by_mod[module] = routers
            base_routers_by_mod[module] = dict(routers)
            clients_by_mod[module] = clients
            imports, specs = _collect_imports(tree, module, path_aliases)
            imports_by_mod[module] = imports
            import_specs_by_mod[module] = specs
            exports_by_mod[module] = _collect_exports(tree)
            factories_by_mod[module] = _collect_client_factories(tree, consts)
            class_ctors_by_mod[module] = _collect_class_ctors(tree)
            trees_by_mod[module] = tree
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

        _import_path_helpers(consts_by_mod, import_specs_by_mod)
        for _path, _rel_file, module, tree, _src in parsed:
            mounts.extend(
                _collect_cross_mounts(
                    tree,
                    module,
                    consts_by_mod.get(module) or {},
                    routers_by_mod.get(module) or {},
                    import_specs_by_mod.get(module) or {},
                    exports_by_mod,
                )
            )
        _propagate_router_mounts(routers_by_mod, base_routers_by_mod, mounts)
        _propagate_client_bases(
            clients_by_mod,
            import_specs_by_mod,
            exports_by_mod,
            factories_by_mod,
            trees_by_mod,
            consts_by_mod,
            class_ctors_by_mod,
        )
        for module, tree in trees_by_mod.items():
            wrappers_by_mod[module] = _collect_wrappers(
                tree, consts_by_mod.get(module) or {}, clients_by_mod.get(module) or {}
            )

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
            _collect_route_literals(analysis, tree, rel_file, module, consts, routers)
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
        wrappers: dict[str, JsHttpWrapper],
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
            wrap = wrappers.get(raw_method) or wrappers.get(method)
            if wrap and args and not recv:
                self._record_wrapper_call(
                    analysis, node, wrap, args, consts, rel_file, module
                )
                continue
            if method in HTTP_METHODS and args:
                path_val, path_unresolved = _resolve_js_string(args[0], consts)
                assigned = _assigned_name(node)
                if assigned and assigned in routers:
                    prefix = routers[assigned]
                else:
                    prefix = _chain_prefix(node, consts, routers)
                is_router = recv in routers or chain_router or bool(
                    assigned and assigned in routers
                )
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
                # Outbound client call — known clients, or a factory param
                # (`makeApi = (client) => ({ list: () => client.get('/x') })`).
                if method in CLIENT_HTTP_METHODS:
                    if is_router:
                        continue
                    if _js_call_is_interceptor(node):
                        continue
                    if self._record_client_http(
                        analysis,
                        node,
                        method,
                        args,
                        recv,
                        clients,
                        consts,
                        rel_file,
                        module,
                        wrappers,
                    ):
                        continue
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
            if method == "request" and recv and args:
                if not _js_call_is_interceptor(node):
                    self._record_client_http(
                        analysis,
                        node,
                        method,
                        args,
                        recv,
                        clients,
                        consts,
                        rel_file,
                        module,
                        wrappers,
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
        if _is_serve_options(args[0]):
            return
        handler_node = _unwrap_hof_handler(args[0])
        if handler_node is None:
            return
        if handler_node.type in {"member_expression", "attribute"}:
            # ``app.fetch`` on a Hono/Express app is not a Deno/Edge handler.
            return
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

    def _record_wrapper_call(
        self,
        analysis: JsAnalysis,
        node: Node,
        wrap: JsHttpWrapper,
        args: list[Node],
        consts: dict[str, str],
        rel_file: str,
        module: str,
    ) -> None:
        path_node = args[wrap.path_index] if wrap.path_index < len(args) else None
        path_val, path_unresolved = _resolve_js_string(path_node, consts)
        http_method = wrap.method or "GET"
        if wrap.method_index is not None and wrap.method_index < len(args):
            mval, _u = _resolve_js_string(args[wrap.method_index], consts)
            if mval:
                http_method = mval.upper()
        if not path_val:
            return
        composed = _normalize_http_url(_join_route(wrap.base, path_val))
        if not _looks_like_http_path(composed):
            return
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

    def _record_client_http(
        self,
        analysis: JsAnalysis,
        node: Node,
        method: str,
        args: list[Node],
        recv: str,
        clients: dict[str, str],
        consts: dict[str, str],
        rel_file: str,
        module: str,
        wrappers: dict[str, JsHttpWrapper],
    ) -> bool:
        """Record axios/ky ``.get/.request`` (and unknown transports). Return True if handled."""
        enclosing = _enclosing_symbol(node, module)
        local_name = enclosing.rsplit(".", 1)[-1] if enclosing else ""
        if local_name in wrappers:
            # Inner ``api.get(BASE + url)`` of a local wrapper — call site records it.
            return True
        path_val, path_unresolved, http_method = _js_client_url_and_method(
            method, args, consts
        )
        param_names = _enclosing_js_params(node)
        if path_val and _url_uses_enclosing_path_param(path_val, param_names):
            return True
        typed_http = _enclosing_typed_http_param(node, recv, clients)
        param_client = bool(
            recv
            and (
                (recv in param_names and _looks_like_http_path(path_val))
                or typed_http
            )
        )
        known = _is_known_http_client(recv, clients, node) or typed_http
        client_unknown = False
        if not known and not param_client:
            if method in CLIENT_REQUEST_METHODS and recv and _looks_like_http_path(path_val):
                client_unknown = True
            elif method == "request" and recv:
                client_unknown = True
            else:
                return False
        base = clients.get(recv) or ""
        if not base and recv.startswith(("this.", "self.")):
            field = recv.split(".", 1)[-1]
            base = clients.get(field) or clients.get(f"this.{field}") or ""
        if not base and param_client:
            bound = ""
            leaf = recv.split(".")[-1]
            for key, val in clients.items():
                if key == leaf or key.endswith("." + leaf):
                    bound = val
                    break
            base = bound or ("{" + leaf + "}" if not bound else bound)
            if not bound:
                client_unknown = True
        composed = _join_route(base, path_val) if path_val else base
        if not composed or not _looks_like_http_path(composed):
            if (param_client or client_unknown) and path_val:
                hole = recv.split(".")[-1] if recv else "client"
                composed = _join_route("{" + hole + "}", path_val)
            else:
                return False
        composed = _normalize_http_url(composed)
        if _url_uses_enclosing_path_param(composed, param_names):
            return True
        analysis.http_calls.append(
            JsHttpCall(
                method=http_method,
                url=composed,
                file=rel_file,
                line=line_of(node),
                symbol=enclosing,
                unresolved=path_unresolved or ("{" in composed) or client_unknown,
                client_unknown=client_unknown,
            )
        )
        return True

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


def _is_serve_options(node: Node | None) -> bool:
    """True for ``serve({ fetch: app.fetch, port })`` / ``{ fetch: app.fetch }``."""
    if node is None:
        return False
    if node.type in {"object", "object_literal"}:
        raw = text(node)
        return "fetch" in raw or "port" in raw
    return False


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
            if method == "basepath":
                routers[name] = _join_route(routers.get(recv) or "", prefix or "")
            else:
                # Child routers get their mount prefix during iterative propagation
                # so nested / cross-file parents can compose first.
                routers.setdefault(name, "")
    return routers, clients


def _path_param_index(params: list[str]) -> int | None:
    for i, name in enumerate(params):
        if name.lower() in HTTP_PATH_PARAM_NAMES:
            return i
    if params and params[0].lower() in HTTP_METHOD_PARAM_NAMES and len(params) > 1:
        return 1
    return None


def _method_param_index(params: list[str]) -> int | None:
    for i, name in enumerate(params):
        if name.lower() in HTTP_METHOD_PARAM_NAMES:
            return i
    return None


def _collect_wrappers(
    tree: Node, consts: dict[str, str], clients: dict[str, str]
) -> dict[str, JsHttpWrapper]:
    """Functions that prepend a base const/env to a path argument."""
    wrappers: dict[str, JsHttpWrapper] = {}

    def consider(name: str, fn_node: Node) -> None:
        params = _fn_param_names(fn_node)
        if not name or not params:
            return
        path_i = _path_param_index(params)
        if path_i is None:
            return
        method_i = _method_param_index(params)
        path_param = params[path_i]
        extra = dict(consts)
        extra[path_param] = ""
        # Implicit arrow body ``(url) => api.get(BASE + url)``.
        body = fn_node.child_by_field_name("body")
        candidates: list[Node] = []
        if body is not None and body.type == "call_expression":
            candidates.append(body)
        for node in walk(fn_node):
            if node.type == "call_expression":
                candidates.append(node)
        seen: set[int] = set()
        for node in candidates:
            if id(node) in seen:
                continue
            seen.add(id(node))
            method = call_callee_name(node).lower()
            args = call_args(node)
            recv = _receiver_ident(node)
            if _js_call_is_interceptor(node):
                continue
            http_method = "GET"
            is_http = False
            url_node: Node | None = None
            if method == "fetch":
                is_http = True
                if args:
                    url_node = args[0]
                if len(args) > 1:
                    raw = text(args[1])
                    for m in HTTP_METHODS:
                        if f"method: '{m.upper()}'" in raw or f'method: "{m.upper()}"' in raw:
                            http_method = m.upper()
            elif method == "request" and (recv in clients or recv.lower() in CLIENT_LIBS or recv):
                is_http = True
                path_val, _u, http_method = _js_client_url_and_method(method, args, extra)
                url_node = None
                if path_val:
                    extra_url = path_val
                    base = clients.get(recv) or ""
                    composed = _join_route(base, extra_url) if extra_url else base
                    if composed:
                        wrappers[name] = JsHttpWrapper(
                            method=http_method if method_i is None else "",
                            base=composed.rstrip("/"),
                            path_index=path_i,
                            method_index=method_i,
                        )
                        return
            elif method in CLIENT_HTTP_METHODS and (
                recv in clients or recv.lower() in CLIENT_LIBS or recv
            ):
                is_http = True
                http_method = method.upper()
                url_node = args[0] if args else None
            if not is_http:
                continue
            url, _u = _resolve_js_string(url_node, extra) if url_node is not None else (None, False)
            base = ""
            if recv in clients:
                base = clients[recv]
            if url:
                base = _join_route(base, url)
            if not base:
                continue
            wrappers[name] = JsHttpWrapper(
                method=http_method if method_i is None else "",
                base=base.rstrip("/"),
                path_index=path_i,
                method_index=method_i,
            )
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


def _is_known_http_client(recv: str, clients: dict[str, str], call: Node | None = None) -> bool:
    """True for fetch/axios/ky/ofetch/got/superagent (and instances), not Headers/Map."""
    if call is not None and _receiver_is_non_http(call):
        return False
    if not recv:
        return False
    low = recv.lower()
    if low in NON_HTTP_RECEIVERS:
        return False
    if recv in clients:
        return True
    if "." in recv:
        field = recv.rsplit(".", 1)[-1]
        if recv.startswith("this.") or recv.startswith("self."):
            if field in clients or f"this.{field}" in clients or f"self.{field}" in clients:
                return True
            if field.lower() in {"client", "api", "http", "ky", "axios", "instance", "transport"}:
                return True
        if field.lower() in CLIENT_LIBS:
            return True
    return low in CLIENT_LIBS


def _receiver_is_non_http(call: Node) -> bool:
    func = call.child_by_field_name("function")
    raw = text(func).lower() if func is not None else ""
    if any(
        token in raw
        for token in (
            ".headers.",
            ".searchparams.",
            ".params.",
            "headers.",
            "searchparams.",
        )
    ):
        return True
    if raw.endswith(".headers") or raw.endswith(".searchparams") or raw.endswith(".params"):
        return True
    return False


def _collect_imports(
    tree: Node,
    module: str,
    path_aliases: list[tuple[str, list[str]]] | None = None,
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Return (local→qualname, local→(src_module, orig_name))."""
    out: dict[str, str] = {}
    specs: dict[str, tuple[str, str]] = {}
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
        src_mod = _spec_to_module(src, module, path_aliases)
        head = raw.split("from")[0] if "from" in raw else raw
        mixed = re.search(
            r"import\s+([A-Za-z_][A-Za-z0-9_]*)\s*,\s*\{([^}]+)\}",
            head,
        )
        named = re.search(r"import\s*\{([^}]+)\}", head)
        default = re.search(r"import\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", head.strip())
        if not default:
            default = re.search(r"import\s+([A-Za-z_][A-Za-z0-9_]*)\s+from", raw)
        if mixed:
            local = mixed.group(1)
            specs[local] = (src_mod, "default")
            out[local] = f"{src_mod}.default"
            named_blob = mixed.group(2)
        else:
            named_blob = named.group(1) if named else ""
            if default and not named:
                local = default.group(1)
                if local not in {"import", "type"}:
                    specs[local] = (src_mod, "default")
                    out[local] = f"{src_mod}.{local}"
        if named_blob:
            for part in named_blob.split(","):
                part = part.strip()
                if not part or part == "type":
                    continue
                if " as " in part:
                    orig, alias = re.split(r"\s+as\s+", part, maxsplit=1)
                    orig, alias = orig.strip(), alias.strip()
                    if orig.startswith("type "):
                        orig = orig[5:].strip()
                    specs[alias] = (src_mod, orig)
                    out[alias] = f"{src_mod}.{orig}"
                else:
                    orig = part.replace("type ", "").strip()
                    specs[orig] = (src_mod, orig)
                    out[orig] = f"{src_mod}.{orig}"
        if "require(" in raw:
            mreq = re.search(r"(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", raw)
            if mreq:
                local = mreq.group(1)
                specs.setdefault(local, (src_mod, "default"))
                out.setdefault(local, f"{src_mod}.{local}")
    return out, specs


def _collect_exports(tree: Node) -> dict[str, str]:
    """exported name → local identifier (``default`` for default export)."""
    out: dict[str, str] = {}
    for node in walk(tree):
        raw = text(node)
        if node.type not in {"export_statement", "expression_statement", "lexical_declaration"}:
            if not raw.strip().startswith("export ") and "module.exports" not in raw:
                continue
        m = re.search(r"export\s+default\s+([A-Za-z_][A-Za-z0-9_]*)", raw)
        if m:
            out["default"] = m.group(1)
            out.setdefault(m.group(1), m.group(1))
        m = re.search(
            r"export\s+(?:const|let|var|function|class|async\s+function)\s+([A-Za-z_][A-Za-z0-9_]*)",
            raw,
        )
        if m:
            out[m.group(1)] = m.group(1)
        m = re.search(r"export\s*\{([^}]+)\}", raw)
        if m:
            for part in m.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                if " as " in part:
                    orig, alias = re.split(r"\s+as\s+", part, maxsplit=1)
                    out[alias.strip()] = orig.strip()
                else:
                    out[part] = part
        m = re.search(r"module\.exports\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", raw)
        if m:
            out["default"] = m.group(1)
            out.setdefault(m.group(1), m.group(1))
    return out


def _resolve_imported_name(
    local: str,
    specs: dict[str, tuple[str, str]],
    exports_by_mod: dict[str, dict[str, str]],
) -> tuple[str, str] | None:
    if local not in specs:
        return None
    src_mod, orig = specs[local]
    exports = exports_by_mod.get(src_mod) or {}
    if orig == "default":
        mapped = exports.get("default") or exports.get(local)
        if mapped:
            return src_mod, mapped
        if len(exports) == 1:
            return src_mod, exports[sorted(exports)[0]]
        return src_mod, local
    mapped = exports.get(orig) or orig
    return src_mod, mapped


def _collect_cross_mounts(
    tree: Node,
    module: str,
    consts: dict[str, str],
    routers: dict[str, str],
    specs: dict[str, tuple[str, str]],
    exports_by_mod: dict[str, dict[str, str]],
) -> list[tuple[str, str, str, str, str]]:
    """(parent_mod, parent_recv, prefix, child_mod, child_router_name)."""
    mounts: list[tuple[str, str, str, str, str]] = []
    for node in walk(tree):
        if node.type != "call_expression":
            continue
        method = call_callee_name(node).lower()
        if method not in {"route", "use", "group"}:
            continue
        recv = _receiver_ident(node)
        parent = recv if recv in routers else _assigned_name(node)
        if parent not in routers:
            continue
        args = call_args(node)
        if len(args) < 2:
            continue
        prefix, _u = _resolve_js_string(args[0], consts)
        if not prefix or prefix in {"*", "/*"}:
            continue
        sub = args[1]
        sub_name = text(sub) if sub.type == "identifier" else ""
        if not sub_name:
            continue
        resolved = _resolve_imported_name(sub_name, specs, exports_by_mod)
        if resolved:
            child_mod, child_name = resolved
        elif sub_name in routers:
            child_mod, child_name = module, sub_name
        else:
            child_mod, child_name = module, sub_name
        mounts.append((module, parent, prefix, child_mod, child_name))
    # ``const admin = app.group('/admin')`` / ``const x = parent.route('/x')``
    for node in walk(tree):
        if node.type != "variable_declarator":
            continue
        name = _declarator_name(node)
        value = node.child_by_field_name("value")
        if not name or value is None or value.type != "call_expression":
            continue
        method = call_callee_name(value).lower()
        if method not in {"group", "route", "router"}:
            continue
        recv = _receiver_ident(value)
        parent = recv if recv in routers else ""
        if parent not in routers:
            continue
        args = call_args(value)
        if len(args) != 1:
            continue
        prefix, _u = _resolve_js_string(args[0], consts)
        if not prefix or prefix in {"*", "/*"}:
            continue
        mounts.append((module, parent, prefix, module, name))
    return mounts


def _propagate_router_mounts(
    routers_by_mod: dict[str, dict[str, str]],
    base_routers_by_mod: dict[str, dict[str, str]],
    mounts: list[tuple[str, str, str, str, str]],
) -> None:
    if not mounts:
        return
    for _ in range(64):
        changed = False
        for parent_mod, parent_recv, prefix, child_mod, child_name in mounts:
            parent_prefix = (routers_by_mod.get(parent_mod) or {}).get(parent_recv) or ""
            local_prefix = (base_routers_by_mod.get(child_mod) or {}).get(child_name) or ""
            new = _join_route(_join_route(parent_prefix, prefix), local_prefix)
            child_map = routers_by_mod.setdefault(child_mod, {})
            cur = child_map.get(child_name) or ""
            if new and new != cur:
                child_map[child_name] = new
                changed = True
        if not changed:
            break


def _collect_class_ctors(tree: Node) -> dict[str, list[str]]:
    """class Foo { constructor(private client: AxiosInstance) {} } → {Foo: [client]}."""
    out: dict[str, list[str]] = {}
    for node in walk(tree):
        if node.type not in {"class_declaration", "class"}:
            continue
        name_n = node.child_by_field_name("name")
        name = text(name_n) if name_n else ""
        if not name:
            continue
        for child in walk(node):
            if child.type not in {"method_definition", "method_signature", "function"}:
                continue
            meth_n = child.child_by_field_name("name")
            meth = text(meth_n) if meth_n else ""
            if meth != "constructor" and not text(child).lstrip().startswith("constructor"):
                continue
            params = _fn_param_names(child)
            if params:
                out[name] = params
            break
    return out


def _collect_client_factories(tree: Node, consts: dict[str, str]) -> dict[str, JsClientFactory]:
    """Functions that return ``axios.create({baseURL})`` / a local instance with interceptors."""
    factories: dict[str, JsClientFactory] = {}

    def consider(name: str, fn_node: Node) -> None:
        if not name:
            return
        params = _fn_param_names(fn_node)
        local_bases: dict[str, str] = {}
        for node in walk(fn_node):
            if node.type != "variable_declarator":
                continue
            dname = _declarator_name(node)
            value = node.child_by_field_name("value")
            if not dname or value is None:
                continue
            base = _create_call_base(value, consts)
            if base is not None:
                local_bases[dname] = base
        body = fn_node.child_by_field_name("body")
        if body is not None:
            implicit = _create_call_base(body, consts)
            if implicit:
                factories[name] = JsClientFactory(base=implicit, params=params)
                return
            if body.type == "identifier" and text(body) in local_bases:
                factories[name] = JsClientFactory(base=local_bases[text(body)], params=params)
                return
        for node in walk(fn_node):
            if node.type not in {"return_statement", "return"}:
                continue
            expr = node.named_children[0] if node.named_children else None
            if expr is None:
                continue
            expr = unwrap_callee_expr(expr) or expr
            base = _create_call_base(expr, consts)
            if base:
                factories[name] = JsClientFactory(base=base, params=params)
                return
            if expr.type == "identifier" and text(expr) in local_bases:
                factories[name] = JsClientFactory(base=local_bases[text(expr)], params=params)
                return

    for node in walk(tree):
        if node.type in {"function_declaration", "function", "method_definition"}:
            name_n = node.child_by_field_name("name")
            consider(text(name_n) if name_n else "", node)
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            name = _declarator_name(node)
            if name and value is not None and value.type in {"arrow_function", "function"}:
                consider(name, value)
    return factories


def _propagate_client_bases(
    clients_by_mod: dict[str, dict[str, str]],
    import_specs_by_mod: dict[str, dict[str, tuple[str, str]]],
    exports_by_mod: dict[str, dict[str, str]],
    factories_by_mod: dict[str, dict[str, str]],
    trees_by_mod: dict[str, Node],
    consts_by_mod: dict[str, dict[str, str]],
    class_ctors_by_mod: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    fn_params_by_mod: dict[str, dict[str, list[str]]] = {}
    for module, tree in trees_by_mod.items():
        consts = consts_by_mod.get(module) or {}
        params: dict[str, list[str]] = {}
        for node in walk(tree):
            if node.type in {"function_declaration", "function", "method_definition"}:
                name_n = node.child_by_field_name("name")
                name = text(name_n) if name_n else ""
                if name:
                    params[name] = _fn_param_names(node)
            if node.type == "variable_declarator":
                value = node.child_by_field_name("value")
                name = _declarator_name(node)
                if name and value is not None and value.type in {"arrow_function", "function"}:
                    params[name] = _fn_param_names(value)
        fn_params_by_mod[module] = params
        _ = consts  # reserved for future const-based bases

    class_ctors_by_mod = class_ctors_by_mod or {}

    def _fn_params_for(module: str, local_name: str) -> tuple[str, list[str]] | None:
        local_params = fn_params_by_mod.get(module) or {}
        if local_name in local_params:
            return module, local_params[local_name]
        local_ctors = class_ctors_by_mod.get(module) or {}
        if local_name in local_ctors:
            return module, local_ctors[local_name]
        specs = import_specs_by_mod.get(module) or {}
        if local_name not in specs:
            return None
        src_mod, orig = specs[local_name]
        exports = exports_by_mod.get(src_mod) or {}
        key = exports.get("default") if orig == "default" else exports.get(orig, orig)
        src_params = fn_params_by_mod.get(src_mod) or {}
        src_ctors = class_ctors_by_mod.get(src_mod) or {}
        if key and key in src_params:
            return src_mod, src_params[key]
        if orig in src_params:
            return src_mod, src_params[orig]
        if key and key in src_ctors:
            return src_mod, src_ctors[key]
        if orig in src_ctors:
            return src_mod, src_ctors[orig]
        return None

    def _bind_client_args(
        module: str,
        clients: dict[str, str],
        node: Node,
        callee: str,
    ) -> bool:
        resolved = _fn_params_for(module, callee)
        if not resolved:
            return False
        dest_mod, params = resolved
        dest_clients = clients_by_mod.setdefault(dest_mod, {})
        args = call_args(node)
        changed = False
        for i, arg in enumerate(args):
            if i >= len(params) or arg.type != "identifier":
                continue
            src = text(arg)
            base = clients.get(src)
            if base is None:
                base = dest_clients.get(src)
            if base is None:
                continue
            pname = params[i]
            for key in (pname, f"this.{pname}", f"self.{pname}"):
                if dest_clients.get(key) != base:
                    dest_clients[key] = base
                    changed = True
                if clients.get(key) != base:
                    clients[key] = base
                    changed = True
        return changed

    for _ in range(8):
        changed = False
        for module, specs in import_specs_by_mod.items():
            dest = clients_by_mod.setdefault(module, {})
            for local, (src_mod, orig) in specs.items():
                src_clients = clients_by_mod.get(src_mod) or {}
                exports = exports_by_mod.get(src_mod) or {}
                key = exports.get("default") if orig == "default" else exports.get(orig, orig)
                if orig == "default" and not key:
                    key = local
                base = src_clients.get(key or "") if key else None
                if base is None and orig != "default":
                    base = src_clients.get(orig)
                if base is None and orig == "default" and len(src_clients) == 1:
                    base = src_clients[sorted(src_clients)[0]]
                if base is not None and dest.get(local) != base:
                    dest[local] = base
                    changed = True
            for local, (src_mod, orig) in specs.items():
                src_factories = factories_by_mod.get(src_mod) or {}
                key = orig if orig != "default" else (exports_by_mod.get(src_mod) or {}).get(
                    "default", local
                )
                if key in src_factories:
                    factories_by_mod.setdefault(module, {})[local] = src_factories[key]
        for module, tree in trees_by_mod.items():
            clients = clients_by_mod.setdefault(module, {})
            factories = factories_by_mod.get(module) or {}
            consts = consts_by_mod.get(module) or {}
            for node in walk(tree):
                if node.type in {
                    "function_declaration",
                    "function",
                    "function_expression",
                    "arrow_function",
                    "method_definition",
                }:
                    for pname, typ in _fn_param_entries(node):
                        if not _type_is_http_client(typ, clients):
                            continue
                        base = ""
                        m = _TYPEOF_CLIENT.search(typ or "")
                        if m and m.group(1) in clients:
                            base = clients[m.group(1)]
                        if pname not in clients:
                            clients[pname] = base
                            changed = True
                        elif base and clients.get(pname) != base:
                            clients[pname] = base
                            changed = True
            for node in walk(tree):
                if node.type != "variable_declarator":
                    continue
                name = _declarator_name(node)
                value = node.child_by_field_name("value")
                if not name or value is None or value.type != "call_expression":
                    continue
                callee = call_callee_name(value)
                factory = factories.get(callee)
                if factory is None:
                    recv = _receiver_ident(value)
                    factory = factories.get(recv)
                if factory is not None:
                    base = _instantiate_factory(factory, value, consts)
                    if clients.get(name) != base:
                        clients[name] = base
                        changed = True
            for node in walk(tree):
                if node.type == "call_expression":
                    if _bind_client_args(module, clients, node, call_callee_name(node)):
                        changed = True
                elif node.type == "new_expression":
                    if _bind_client_args(module, clients, node, _ctor_leaf(node)):
                        changed = True
        if not changed:
            break



def _spec_to_module(
    spec: str,
    current: str,
    path_aliases: list[tuple[str, list[str]]] | None = None,
) -> str:
    spec = spec.strip()
    if spec.startswith("."):
        parts = current.split(".")
        rest = spec
        while rest.startswith("../"):
            parts = parts[:-1]
            rest = rest[3:]
        if rest.startswith("./"):
            rest = rest[2:]
        rest = _strip_js_ext(rest)
        bits = [p for p in rest.split("/") if p and p != "."]
        return ".".join([*parts[:-1], *bits]) if bits else ".".join(parts[:-1]) or current
    mapped = _map_ts_alias(spec, path_aliases or [])
    if mapped:
        return mapped
    if spec.startswith("@/"):
        rest = _strip_js_ext(spec[2:])
        bits = [p for p in rest.split("/") if p]
        return ".".join(["src", *bits]) if bits else "src"
    return spec.replace("/", ".")


_JS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _strip_js_ext(path: str) -> str:
    raw = path.replace("\\", "/")
    for ext in _JS_EXTS:
        if raw.endswith(ext):
            return raw[: -len(ext)]
    return raw


def _path_to_module(rel_posix: str) -> str:
    rel_posix = _strip_js_ext(rel_posix.replace("\\", "/").lstrip("./"))
    bits = [p for p in rel_posix.split("/") if p and p != "."]
    return ".".join(bits)


def _apply_ts_pattern(spec: str, pattern: str, target: str) -> str | None:
    if "*" not in pattern:
        return target if spec == pattern else None
    pre, _, post = pattern.partition("*")
    if not spec.startswith(pre):
        return None
    if post and not spec.endswith(post):
        return None
    star = spec[len(pre) : len(spec) - len(post) if post else None]
    if "*" in target:
        tpre, _, tpost = target.partition("*")
        return tpre + star + tpost
    return target


def _map_ts_alias(spec: str, aliases: list[tuple[str, list[str]]]) -> str | None:
    if not aliases:
        return None
    for pattern, targets in sorted(aliases, key=lambda item: -len(item[0])):
        for target in targets:
            mapped = _apply_ts_pattern(spec, pattern, target)
            if mapped:
                return _path_to_module(mapped)
    return None


def _parse_jsonc(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    text = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    lines: list[str] = []
    for line in text.splitlines():
        buf: list[str] = []
        in_str = False
        escaped = False
        i = 0
        while i < len(line):
            ch = line[i]
            if escaped:
                buf.append(ch)
                escaped = False
                i += 1
                continue
            if in_str and ch == "\\":
                buf.append(ch)
                escaped = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
                buf.append(ch)
                i += 1
                continue
            if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break
            buf.append(ch)
            i += 1
        lines.append("".join(buf))
    text = "\n".join(lines)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def _merge_tsconfig(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, val in overlay.items():
        if key == "compilerOptions" and isinstance(val, dict):
            opts = dict(out.get("compilerOptions") or {})
            overlay_opts = dict(val)
            paths = dict(opts.get("paths") or {})
            paths.update(overlay_opts.get("paths") or {})
            opts.update(overlay_opts)
            if paths:
                opts["paths"] = paths
            out["compilerOptions"] = opts
        else:
            out[key] = val
    return out


def _load_tsconfig(path: Path, seen: set[Path] | None = None) -> dict:
    seen = seen or set()
    path = path.resolve()
    if path in seen or not path.is_file():
        return {}
    seen.add(path)
    try:
        data = _parse_jsonc(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    merged: dict = {}
    ext = data.get("extends")
    exts = ext if isinstance(ext, list) else ([ext] if ext else [])
    for item in exts:
        if not item:
            continue
        raw = str(item)
        if raw.startswith("@"):
            continue
        parent = (path.parent / raw).resolve()
        if not parent.exists() and not raw.endswith(".json"):
            parent = (path.parent / (raw + ".json")).resolve()
        if parent.exists():
            merged = _merge_tsconfig(merged, _load_tsconfig(parent, seen))
    return _merge_tsconfig(merged, data)


def _join_ts_base(base_url: str, target: str) -> str:
    target = target.replace("\\", "/").lstrip("/")
    base = (base_url or ".").replace("\\", "/").rstrip("/")
    if base in {".", "", "./"}:
        return target
    if target.startswith("./"):
        target = target[2:]
    return base + "/" + target


def _ts_path_aliases(root: Path) -> list[tuple[str, list[str]]]:
    """compilerOptions.paths from tsconfig.json / jsconfig.json (with extends)."""
    configs: list[Path] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for name in filenames:
            if name in {"tsconfig.json", "jsconfig.json"} or (
                name.startswith("tsconfig.") and name.endswith(".json")
            ):
                configs.append(Path(dirpath) / name)
    aliases: list[tuple[str, list[str]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for cfg_path in sorted(configs):
        data = _load_tsconfig(cfg_path)
        opts = data.get("compilerOptions") if isinstance(data, dict) else None
        if not isinstance(opts, dict):
            continue
        base_url = str(opts.get("baseUrl") or ".")
        paths = opts.get("paths") or {}
        if not isinstance(paths, dict):
            continue
        for pattern, targets in paths.items():
            if isinstance(targets, str):
                target_list = [targets]
            elif isinstance(targets, list):
                target_list = [str(t) for t in targets if t]
            else:
                continue
            joined = [_join_ts_base(base_url, t) for t in target_list]
            key = (str(pattern), tuple(joined))
            if key in seen:
                continue
            seen.add(key)
            aliases.append((str(pattern), joined))
    return aliases


def _instantiate_factory(
    factory: JsClientFactory,
    call: Node,
    consts: dict[str, str],
) -> str:
    """Substitute call-site args into a factory's ``{param}`` baseURL template."""
    base = factory.base or ""
    args = call_args(call)
    for i, pname in enumerate(factory.params):
        if i >= len(args):
            break
        val, _u = _resolve_js_string(args[i], consts)
        if not val:
            continue
        hole = "{" + pname + "}"
        if hole in base:
            base = base.replace(hole, val.rstrip("/"))
    return base


def _enclosing_typed_http_param(
    node: Node | None,
    recv: str,
    clients: dict[str, str],
) -> bool:
    if not recv:
        return False
    cur = node.parent if node is not None else None
    depth = 0
    while cur is not None and depth < 48:
        if cur.type in {
            "function_declaration",
            "function",
            "function_expression",
            "arrow_function",
            "method_definition",
        }:
            for name, typ in _fn_param_entries(cur):
                if name == recv and _type_is_http_client(typ, clients):
                    return True
        cur = cur.parent
        depth += 1
    return False


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


# ---------------------------------------------------------------------------
# Route literals: API-looking path strings, linked to routes by path.
# ---------------------------------------------------------------------------

# Object keys / JSX attributes whose string values are front-end navigation,
# assets, or route *definitions* — never an outbound API call.
_LITERAL_SKIP_KEYS = {
    "path",
    "to",
    "href",
    "redirect",
    "redirectto",
    "pathname",
    "from",
    "link",
    "as",
    "src",
    "icon",
    "image",
    "asset",
    "component",
    "element",
    "pattern",
    "matcher",
    "source",
    "destination",
}
# Callees whose string arguments are navigation, matching, logging, or
# route registration rather than an HTTP request.
_LITERAL_SKIP_CALLS = {
    "navigate",
    "navigateto",
    "push",
    "replace",
    "redirect",
    "assign",
    "open",
    "require",
    "import",
    "prefetch",
    "revalidatepath",
    "revalidatetag",
    "usematch",
    "matchpath",
    "matchroute",
    "creatematcher",
    "createroute",
    "createfileroute",
    "route",
    "use",
    "basepath",
    "mount",
    "register",
    "group",
    "on",
    "static",
    "sendfile",
    "describe",
    "it",
    "test",
    "log",
    "warn",
    "error",
    "info",
    "debug",
    "startswith",
    "endswith",
    "includes",
    "indexof",
    "match",
    "split",
    "t",
    "translate",
}
_ASSET_EXT = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,4}$")
_SEGMENT_OK = re.compile(r"^[A-Za-z0-9_.~@:{}\-]+$")
_METHOD_IN_OPTIONS = re.compile(r"""method\s*:\s*['"`]([A-Za-z]+)['"`]""")
_HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _is_plus(node: Node) -> bool:
    ops = [c for c in node.children if not c.is_named]
    return bool(ops) and text(ops[0]) == "+"


def _literal_top(node: Node) -> Node:
    cur = node
    while cur.parent is not None and (
        (cur.parent.type == "binary_expression" and _is_plus(cur.parent))
        or cur.parent.type == "parenthesized_expression"
    ):
        cur = cur.parent
    return cur


def _is_route_registration(call: Node, routers: dict[str, str]) -> bool:
    recv = _receiver_ident(call)
    assigned = _assigned_name(call)
    return bool(
        recv in routers
        or _is_router_factory(call)
        or (assigned and assigned in routers)
    )


def _literal_is_excluded(top: Node, routers: dict[str, str]) -> bool:
    parent = top.parent
    if parent is None:
        return True
    ptype = parent.type
    if ptype in {
        "import_statement",
        "export_statement",
        "jsx_attribute",
        "switch_case",
        "array",
        "binary_expression",  # comparisons (`===`, `!==`, `in`)
        "property_identifier",
        "type_annotation",
        "literal_type",
    }:
        return True
    if ptype == "jsx_expression" and parent.parent is not None and parent.parent.type == "jsx_attribute":
        return True
    if ptype == "pair":
        key = parent.child_by_field_name("key")
        if key is None or key == top or key.start_byte == top.start_byte:
            return True
        if _unquote(text(key)).lower() in _LITERAL_SKIP_KEYS:
            return True
    if ptype in {"assignment_expression", "augmented_assignment_expression"}:
        left = parent.child_by_field_name("left")
        ltxt = text(left).lower() if left is not None else ""
        if ltxt.endswith("href") or ltxt.endswith("location") or ltxt.endswith("pathname"):
            return True
    if ptype == "arguments" and parent.parent is not None and parent.parent.type in {
        "call_expression",
        "new_expression",
    }:
        call = parent.parent
        name = call_callee_name(call).lower()
        if name in _LITERAL_SKIP_CALLS:
            return True
        if name in ROUTE_HTTP_METHODS and _is_route_registration(call, routers):
            return True
        if call.type == "new_expression" and name in {"regexp", "urlpattern", "url"}:
            return name != "url"
    return False


def _literal_method_hint(top: Node, consts: dict[str, str]) -> str | None:
    parent = top.parent
    if parent is None:
        return None
    if parent.type == "arguments" and parent.parent is not None and parent.parent.type == "call_expression":
        call = parent.parent
        name = call_callee_name(call).lower()
        if name in CLIENT_HTTP_METHODS:
            return name.upper()
        args = call_args(call)
        if name == "fetch":
            if len(args) > 1:
                m = _METHOD_IN_OPTIONS.search(text(args[1]))
                if m and m.group(1).upper() in _HTTP_VERBS:
                    return m.group(1).upper()
            return "GET"
        for arg in args:
            if arg.start_byte == top.start_byte:
                continue
            if arg.type == "string":
                verb = _unquote(text(arg)).upper()
                if verb in _HTTP_VERBS:
                    return verb
            if arg.type == "object":
                m = _METHOD_IN_OPTIONS.search(text(arg))
                if m and m.group(1).upper() in _HTTP_VERBS:
                    return m.group(1).upper()
        return None
    if parent.type == "pair":
        key = parent.child_by_field_name("key")
        if key is not None and _unquote(text(key)).lower() in {"url", "endpoint", "uri"}:
            obj = parent.parent
            if obj is not None:
                m = _METHOD_IN_OPTIONS.search(text(obj))
                if m and m.group(1).upper() in _HTTP_VERBS:
                    return m.group(1).upper()
    return None


def _clean_route_literal(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw or any(ch.isspace() for ch in raw) or "*" in raw or raw.startswith("//"):
        return None
    if "://" in raw:
        if not raw.lower().startswith(("http://", "https://", "{")):
            return None
    elif not (raw.startswith("/") or (raw.startswith("{") and "/" in raw)):
        return None
    raw = _normalize_http_url(raw)
    if "://" in raw:
        after = raw.split("://", 1)[1]
        raw = "/" + after.split("/", 1)[1] if "/" in after else "/"
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in raw.split("/") if p]
    while parts and (parts[0].startswith("{") or parts[0].startswith(":")):
        parts.pop(0)
    if not parts:
        return None
    literal = [p for p in parts if not (p.startswith(":") or p.startswith("{"))]
    if not literal:
        return None
    if any(not _SEGMENT_OK.match(p) for p in parts):
        return None
    if _ASSET_EXT.search(parts[-1]) and not parts[-1].startswith(":"):
        return None
    return "/" + "/".join(parts)


def _is_helper_return(top: Node) -> bool:
    """True when ``top`` is the whole returned value of a function whose only
    job is to return it (`(id) => `/sites/${id}``, `function p() { return '/x' }`)."""
    cur = top.parent
    while cur is not None and cur.type in {"parenthesized_expression", "return_statement", "statement_block"}:
        cur = cur.parent
    if cur is None or cur.type not in {
        "arrow_function",
        "function",
        "function_expression",
        "function_declaration",
        "method_definition",
    }:
        return False
    expr = _helper_body_expr(cur)
    return expr is not None and (expr.start_byte, expr.end_byte) == (top.start_byte, top.end_byte)


def _collect_route_literals(
    analysis: JsAnalysis,
    tree: Node,
    rel_file: str,
    module: str,
    consts: dict[str, str],
    routers: dict[str, str],
) -> None:
    seen_tops: set[tuple[int, int]] = set()
    seen: set[tuple[str, str, str | None]] = set()
    for node in walk(tree):
        if node.type in {"string", "template_string"}:
            pass
        elif node.type == "call_expression" and _apply_path_helper(node, consts) is not None:
            # `api.get(site(id))`: the helper call is the path at this call site
            pass
        else:
            continue
        top = _literal_top(node)
        key = (top.start_byte, top.end_byte)
        if key in seen_tops:
            continue
        seen_tops.add(key)
        if _literal_is_excluded(top, routers):
            continue
        if _is_helper_return(top):
            # A function that only returns a path never sends a request: the
            # origin is whoever calls the client with it.
            continue
        value, _unresolved = _resolve_js_string(top, consts)
        path = _clean_route_literal(value)
        if path is None:
            continue
        method = _literal_method_hint(top, consts)
        # Bare `fetch('/x')` is relative to the page origin: same repo only.
        same_repo = bool(
            value
            and value.startswith("/")
            and top.parent is not None
            and top.parent.type == "arguments"
            and top.parent.parent is not None
            and call_callee_name(top.parent.parent).lower() == "fetch"
        )
        symbol = _enclosing_symbol(top, module)
        dedupe = (symbol, path, method)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        analysis.route_literals.append(
            JsRouteLiteral(
                path=path,
                method=method,
                file=rel_file,
                line=line_of(top),
                symbol=symbol,
                same_repo=same_repo,
            )
        )



# ---------------------------------------------------------------------------
# Pure path helpers: `const site = (id) => `/sites/${id}`` / `function seg(id)
# { return '/teams/' + id }`. Calls inside client URLs are evaluated with the
# caller's argument names (`site(siteId)` → `/sites/{siteId}` → `/sites/:siteId`).
# Stored in the module's consts under `name()` (template) and `name()#params`.
# ---------------------------------------------------------------------------

_HOLE = re.compile(r"\{([^{}]+)\}")


def _helper_body_expr(fn: Node) -> Node | None:
    body = fn.child_by_field_name("body")
    if body is None:
        return None
    if body.type in {"template_string", "string", "binary_expression", "parenthesized_expression"}:
        return body
    if body.type == "statement_block":
        stmts = [c for c in body.named_children if c.type != "comment"]
        if len(stmts) == 1 and stmts[0].type == "return_statement" and stmts[0].named_children:
            return stmts[0].named_children[0]
    return None


_TRANSPARENT_FUNCS = {"encodeURIComponent", "encodeURI", "String", "Number", "escape"}
_TRANSPARENT_METHODS = {"toString", "trim", "toLowerCase", "toUpperCase", "valueOf"}


def _transparent_arg(call: Node) -> Node | None:
    """`encodeURIComponent(x)` / `String(x)` / `x.toString()` → the node for `x`."""
    func = call.child_by_field_name("function")
    if func is None:
        return None
    args = call_args(call)
    if func.type == "identifier" and text(func) in _TRANSPARENT_FUNCS and len(args) == 1:
        return args[0]
    if func.type == "member_expression" and not args:
        prop = func.child_by_field_name("property")
        obj = func.child_by_field_name("object")
        if prop is not None and obj is not None and text(prop) in _TRANSPARENT_METHODS:
            return obj
    return None


def _helper_candidates(tree: Node):
    """Yield (helper name, function node), including object members (`paths.site`)."""
    for node in walk(tree):
        if node.type == "variable_declarator":
            name = _declarator_name(node)
            value = node.child_by_field_name("value")
            if not name or value is None:
                continue
            if value.type in {"arrow_function", "function", "function_expression"}:
                yield name, value
            elif value.type == "object":
                for member in value.named_children:
                    if member.type == "pair":
                        key = member.child_by_field_name("key")
                        val = member.child_by_field_name("value")
                        if key is not None and val is not None and val.type in {
                            "arrow_function",
                            "function",
                            "function_expression",
                        }:
                            yield f"{name}.{_unquote(text(key))}", val
                    elif member.type == "method_definition":
                        key = member.child_by_field_name("name")
                        if key is not None:
                            yield f"{name}.{text(key)}", member
        elif node.type == "function_declaration":
            ident = node.child_by_field_name("name")
            if ident is not None:
                yield text(ident), node


def _collect_path_helpers(tree: Node, consts: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, fn in _helper_candidates(tree):
        if not name or fn is None:
            continue
        expr = _helper_body_expr(fn)
        if expr is None:
            continue
        params = _fn_param_names(fn)
        scope = {k: v for k, v in consts.items() if k not in params}
        scope.update(out)  # helpers defined earlier in the file may compose
        value, _unresolved = _resolve_js_string(expr, scope)
        if not value or not value.startswith("/") or any(ch.isspace() for ch in value):
            continue
        # Pure: the only holes are the helper's own parameters.
        if not set(_HOLE.findall(value)) <= set(params):
            continue
        out[f"{name}()"] = value
        out[f"{name}()#params"] = ",".join(params)
    return out


def _import_path_helpers(
    consts_by_mod: dict[str, dict[str, str]],
    import_specs_by_mod: dict[str, dict[str, tuple[str, str]]],
) -> None:
    for module, specs in import_specs_by_mod.items():
        consts = consts_by_mod.setdefault(module, {})
        for local, (src_mod, orig) in specs.items():
            src = consts_by_mod.get(src_mod) or {}
            key = f"{orig}()"
            if key in src and f"{local}()" not in consts:
                consts[f"{local}()"] = src[key]
                consts[f"{local}()#params"] = src.get(f"{key}#params", "")
            # object helpers: `import { paths } from '@/paths'` → paths.site()
            prefix = f"{orig}."
            for skey, sval in src.items():
                if skey.startswith(prefix) and (skey.endswith("()") or skey.endswith("()#params")):
                    consts.setdefault(f"{local}.{skey[len(prefix):]}", sval)


def _apply_path_helper(call: Node, consts: dict[str, str]) -> str | None:
    func = call.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "identifier":
        key = f"{text(func)}()"
    elif func.type == "member_expression":
        obj = func.child_by_field_name("object")
        prop = func.child_by_field_name("property")
        if obj is None or prop is None or obj.type != "identifier":
            return None
        key = f"{text(obj)}.{text(prop)}()"
    else:
        return None
    template = consts.get(key)
    if template is None:
        return None
    params = [p for p in (consts.get(f"{key}#params") or "").split(",") if p]
    args = call_args(call)
    for i, param in enumerate(params):
        rep = "{" + param + "}"
        if i < len(args):
            arg = args[i]
            inner_arg = _transparent_arg(arg) if arg.type == "call_expression" else None
            if inner_arg is not None:
                arg = inner_arg
            if arg.type == "identifier":
                rep = "{" + text(arg) + "}"
            elif arg.type == "member_expression":
                rep = "{" + text(arg).rsplit(".", 1)[-1] + "}"
            else:
                val, _u = _resolve_js_string(arg, consts)
                if val and "{" not in val and "/" not in val:
                    rep = val
        template = template.replace("{" + param + "}", rep)
    return template
