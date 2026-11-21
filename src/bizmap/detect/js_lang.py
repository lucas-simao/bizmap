"""JavaScript/TypeScript detector (Express/Fastify/Hono routes, fetch, supabase, pg)."""

from __future__ import annotations

import gc
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Node, Parser

from bizmap import progress
from bizmap.detect.tsutil import (
    JS_TYPED_WALK_TYPES as JS_TYPES,
)
from bizmap.detect.tsutil import (
    call_args,
    call_callee_name,
    clear_parent_index,
    clear_typed_walks,
    flatten_plus,
    index_deep_tree,
    line_of,
    parent_of,
    text,
    typed_walk,
    unwrap_callee_expr,
    walk,
)
from bizmap.files import SKIP_DIR_NAMES, rel
from bizmap.sql_analyze import looks_like_sql
from bizmap.urlclean import clean_method, clean_url

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
        head, _sep, leaf2 = raw.partition(".")
        if _sep and f"{head}.*env" in consts and "." not in leaf2:
            return "{" + leaf2 + "}", True
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
        operands = flatten_plus(node, {"binary_expression"})
        if not operands:
            return None, False

        def leaf(operand: Node) -> tuple[str | None, bool]:
            value, unresolved = _resolve_js_string(operand, consts)
            if value is None and operand.type == "identifier":
                return "{" + text(operand) + "}", True
            return value, unresolved

        # left fold, same result as the left-associative pairwise evaluation
        acc, acc_u = leaf(operands[0])
        for operand in operands[1:]:
            value, unresolved = leaf(operand)
            if acc is None and value is None:
                acc, acc_u = None, False
            else:
                acc, acc_u = (acc or "") + (value or ""), acc_u or unresolved or acc is None or value is None
        return acc, acc_u
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


_PER_CALL_BASE_KEYS = ("baseURL", "baseUrl", "prefixUrl")
_CONFIG_ARG_INDEX = {"get": 1, "delete": 1, "head": 1, "options": 1, "post": 2, "put": 2, "patch": 2}


def _per_call_base(method: str, args: list[Node], consts: dict[str, str]) -> str | None:
    """`http.post(url, body, { baseURL: NEXT })` / `http.get(url, { baseURL })` /
    `http.request({ baseURL, url })` → the call's own base (unknown → `{baseURL}`)."""
    candidates: list[Node] = []
    idx = _CONFIG_ARG_INDEX.get(method)
    if idx is not None and len(args) > idx:
        candidates.append(args[idx])
    if method == "request" and args:
        candidates.append(args[0])
    for arg in candidates:
        node = _object_prop_node(arg, _PER_CALL_BASE_KEYS)
        if node is None:
            continue
        value, _u = _resolve_js_string(node, consts)
        return value if value else "{baseURL}"
    return None


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
    if val_n.type in {"shorthand_property_identifier", "shorthand_property_identifier_pattern"}:
        # `{ baseURL }`: the variable of that name
        name = text(val_n)
        return (consts[name], "{" in consts[name]) if name in consts else (None, False)
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
        cur = parent_of(cur)
        depth += 1
    return ""


def _enclosing_js_params(node: Node | None) -> list[str]:
    """Parameter names of every enclosing function/arrow (for nested factories)."""
    names: list[str] = []
    cur = parent_of(node) if node is not None else None
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
        cur = parent_of(cur)
        depth += 1
    return names


def _route_chain(call: Node, consts: dict[str, str]) -> tuple[str, str | None] | None:
    """`router.route('/x').get(h).post(h2)`: (root receiver, '/x') for each
    method call of the chain, else None."""
    func = call.child_by_field_name("function")
    obj = func.child_by_field_name("object") if func is not None and func.type == "member_expression" else None
    path: str | None = None
    found = False
    while obj is not None and obj.type == "call_expression":
        fn = obj.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return None
        prop = fn.child_by_field_name("property")
        if prop is not None and text(prop) == "route":
            args = call_args(obj)
            if len(args) != 1:
                # Hono `.route('/inv', subApp)` mounts a sub-app (2 args); only
                # Express `.route('/x')` (1 arg) opens a method chain
                return None
            path, _u = _resolve_js_string(args[0], consts)
            found = True
            root = fn.child_by_field_name("object")
            while root is not None and root.type in {"member_expression", "parenthesized_expression"}:
                if root.type == "parenthesized_expression":
                    root = root.named_children[0] if root.named_children else None
                    continue
                break
            if root is None:
                return None
            return (text(root) if root.type == "identifier" else text(root)), path
        obj = fn.child_by_field_name("object")
    return ("", path) if found else None


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
    # `listHandler` / `h.cancelHandler` as written, for imports resolved once
    # every file is indexed (default exports, namespaces, barrels)
    handler_ref: tuple[str, ...] | None = None
    module: str | None = None
    # lines of an inline (anonymous) handler, to give it the SQL inside
    span: tuple[int, int] | None = None


@dataclass
class JsHttpCall:
    method: str
    url: str
    file: str
    line: int
    symbol: str
    unresolved: bool = False
    client_unknown: bool = False

    def __post_init__(self) -> None:
        self.url = clean_url(self.url)
        self.method = clean_method(self.method)


@dataclass
class JsHttpWrapper:
    method: str
    base: str
    path_index: int = 0
    method_index: int | None = None
    # `read(url) => api.get(url)` inside a factory: the call site is recorded
    # as a call on that client (its bases come from the factory instances)
    recv: str = ""


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
    # resolved by name: a function in scope or imported by name (`exact`);
    # members, factories and callbacks stay inferred
    exact: bool = False


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
    # the literal is the whole return value of a path helper (or the value of a
    # module-level const); scan moves it to direct callers / users
    helper: bool = False
    # the literal carried its own base (`${API}/x`, `https://h/x`): match whole
    explicit_base: bool = False
    base_hole: str | None = None
    # concrete base every caller passes for `base_hole` (see _bind_param_bases)
    bound_base: str | None = None
    # helper defined as an object property (`paths.site`), called as obj.key()
    member_helper: bool = False


@dataclass
class JsHelperCall:
    """A call site `getItem → itemPath(id)` (callee qualname resolved via imports)."""

    caller: str
    callee: str
    file: str
    line: int
    method: str | None = None
    args: list[str | None] = field(default_factory=list)
    # the call / reference is the whole argument of a call (`http.get(itemPath(id))`)
    direct: bool = False
    kind: str = "call"  # "call" (helper()) or "ref" (module const used by name)


@dataclass
class JsAnalysis:
    routes: list[JsRoute] = field(default_factory=list)
    http_calls: list[JsHttpCall] = field(default_factory=list)
    route_literals: list[JsRouteLiteral] = field(default_factory=list)
    helper_calls: list[JsHelperCall] = field(default_factory=list)
    rpc_calls: list[JsRpcCall] = field(default_factory=list)
    functions: dict[str, tuple[str, int]] = field(default_factory=dict)
    function_ends: dict[str, int] = field(default_factory=dict)
    function_why: dict[str, str] = field(default_factory=dict)
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
        """Analyse the files with the garbage collector's thresholds raised
        (as in the Go phase: many short-lived objects next to the trees)."""
        old = gc.get_threshold()
        gc.set_threshold(max(old[0], 50_000), max(old[1], 50), max(old[2], 100))
        try:
            return self._analyze(files, root, constants)
        finally:
            clear_typed_walks()
            gc.set_threshold(*old)

    def _analyze(
        self,
        files: list[Path],
        root: Path,
        constants: dict[str, str] | None,
    ) -> JsAnalysis:
        analysis = JsAnalysis()
        _REEXPORTS.clear()
        _LITERAL_TOP.clear()
        _SCOPE_BINDINGS.clear()
        _ENCLOSING_SYMBOL.clear()
        _CLIENT_BASES.clear()
        clear_typed_walks()
        clear_parent_index()
        yaml_constants = constants or {}
        path_aliases = _ts_path_aliases(root)
        parsed: list[tuple[Path, str, str, Node, bytes]] = []
        for parsed_n, path in enumerate(files):
            progress.counter("js/ts: parsing", parsed_n, len(files))
            parser = _parser_for(path)
            if parser is None:
                continue
            source = path.read_bytes()
            tree = parser.parse(source).root_node
            index_deep_tree(tree)
            rel_file = rel(path, root)
            module = rel_file.replace("/", ".").rsplit(".", 1)[0]
            parsed.append((path, rel_file, module, tree, source))
        _KNOWN_MODULES.clear()
        _KNOWN_MODULES.update(_path_to_module(p[1]) for p in parsed)
        _MODULE_DIRS.clear()
        _MODULE_DIRS.update({p[2]: tuple(b for b in p[1].split("/")[:-1] if b and b != ".") for p in parsed})

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
        pending_import_calls: list[tuple[str, tuple[str, str], str, int]] = []
        # `X.m(...)` on an object (or factory instance) declared here or imported:
        # (caller, module the name lives in, name, member, file, line)
        pending_member_calls: list[tuple[str, str, str, str, str, int]] = []
        # imported functions passed by reference (`queryFn: listWidgets`): inferred
        pending_ref_imports: list[tuple[str, tuple[str, str], str, int]] = []
        instances_by_mod: dict[str, dict[str, str]] = {}

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
            _REEXPORTS[module] = _collect_reexports(tree, module, path_aliases)
            factories_by_mod[module] = _collect_client_factories(tree, consts)
            class_ctors_by_mod[module] = _collect_class_ctors(tree)
            trees_by_mod[module] = tree
            instances_by_mod[module] = _collect_instances(tree)
            file_quals: list[str] = []  # functions this file declares (fn_nodes keeps growing)
            for node in typed_walk(tree, _DECL_TYPES, JS_TYPES):
                # declarations only: a named function *expression* is registered
                # through its binding below (its own name is not in scope here)
                if node.type in {"function_declaration", "generator_function_declaration", "method_definition"}:
                    name_n = node.child_by_field_name("name")
                    name = text(name_n) if name_n else ""
                    if name:
                        qual = _fn_qual(node, module) or f"{module}.{name}"
                        analysis.functions[qual] = (rel_file, line_of(node))
                        analysis.function_ends[qual] = node.end_point[0] + 1
                        fn_nodes[qual] = node
                        file_quals.append(qual)
                        why = _js_why(node)
                        if why:
                            analysis.function_why[qual] = why
                        # `return { stock(sku) {...} }`: a method member
                        if node.type == "method_definition" and _is_returned_object(parent_of(node)):
                            parent_qual = qual.rsplit(".", 1)[0]
                            if parent_qual != module:
                                analysis.calls.append(
                                    JsCall(caller=parent_qual, callee=qual, file=rel_file, line=line_of(node))
                                )
                if node.type == "variable_declarator":
                    # const save = () => { ... } / const save = function () {}
                    value = node.child_by_field_name("value")
                    name = _declarator_name(node)
                    if name and value is not None and value.type in {
                        "arrow_function",
                        "function",
                        "function_expression",
                        "generator_function",
                    }:
                        qual = _fn_qual(value, module) or f"{module}.{name}"
                        analysis.functions[qual] = (rel_file, line_of(node))
                        analysis.function_ends[qual] = node.end_point[0] + 1
                        fn_nodes[qual] = value
                        file_quals.append(qual)
                        why = _js_why(value)
                        if why:
                            analysis.function_why[qual] = why
                if node.type == "pair":
                    value = node.child_by_field_name("value")
                    if value is not None and value.type in {
                        "arrow_function",
                        "function",
                        "function_expression",
                        "generator_function",
                    }:
                        qual = _fn_qual(value, module)
                        parent_qual = qual.rsplit(".", 1)[0] if qual else None
                        if qual and _object_member_key(value) and not _returned_member_key(value):
                            # `export const X = { m: (..) => .. }`: X.m is a function
                            analysis.functions[qual] = (rel_file, line_of(value))
                            analysis.function_ends[qual] = value.end_point[0] + 1
                            fn_nodes[qual] = value
                            file_quals.append(qual)
                            why = _js_why(value)
                            if why:
                                analysis.function_why[qual] = why
                        elif qual and parent_qual and parent_qual != module and _returned_member_key(value):
                            analysis.functions[qual] = (rel_file, line_of(value))
                            analysis.function_ends[qual] = value.end_point[0] + 1
                            fn_nodes[qual] = value
                            file_quals.append(qual)
                            why = _js_why(value)
                            if why:
                                analysis.function_why[qual] = why
                            # the factory reaches what its members call
                            analysis.calls.append(
                                JsCall(caller=parent_qual, callee=qual, file=rel_file, line=line_of(value))
                            )
            # calls to helpers declared in an enclosing function's scope
            fn_by_span = {
                (fn_nodes[q].start_byte, fn_nodes[q].end_byte): q
                for q in file_quals
                if q.startswith(module + ".") and analysis.functions.get(q, ("",))[0] == rel_file
            }
            linker = _CallLinker(
                module, rel_file, specs, fn_nodes, fn_by_span, analysis.calls,
                pending_import_calls, pending_ref_imports, pending_member_calls,
            )
            for node in typed_walk(tree, frozenset({"call_expression", "jsx_attribute"}), JS_TYPES):
                if node.type == "jsx_attribute":
                    # `onClick={rename}` / `onSubmit={form.save}`
                    attr = node.named_children[0] if node.named_children else None
                    value = node.named_children[-1] if len(node.named_children) > 1 else None
                    if attr is not None and text(attr).startswith("on") and value is not None:
                        if value.type == "jsx_expression" and value.named_children:
                            linker.ref(value.named_children[0], node)
                    continue
                args = node.child_by_field_name("arguments")
                for arg in args.named_children if args is not None else ():
                    if arg.type in {"identifier", "member_expression"}:
                        linker.ref(arg, node)
                    elif arg.type == "object":
                        for pair in arg.named_children:
                            if pair.type == "pair":
                                linker.ref(pair.child_by_field_name("value"), node)
                fn_n = node.child_by_field_name("function")
                if fn_n is not None and fn_n.type == "member_expression":
                    obj, prop = fn_n.child_by_field_name("object"), fn_n.child_by_field_name("property")
                    if obj is not None and prop is not None and obj.type == "identifier":
                        caller = _enclosing_symbol(node, module)
                        if not caller.endswith(".module"):
                            linker.member(node, obj, prop, caller)
                    continue
                if fn_n is None or fn_n.type != "identifier":
                    continue
                caller = _enclosing_symbol(node, module)
                if caller.endswith(".module"):
                    continue
                linker.name(node, text(fn_n), caller, exact=True)
            wrappers_by_mod[module] = _collect_wrappers(tree, consts, clients)

        # imported functions: keep calls whose target is a scanned function
        # (`import { helperTop } from './a'`, default exports via `exports`)
        known_mods = set(trees_by_mod)
        for caller, (src_mod, orig), call_file, call_line in pending_import_calls:
            target = _imported_function(src_mod, orig, exports_by_mod, known_mods, fn_nodes)
            if target and target != caller:
                analysis.calls.append(
                    JsCall(caller=caller, callee=target, file=call_file, line=call_line, exact=True)
                )

        for caller, (src_mod, orig), call_file, call_line in pending_ref_imports:
            target = _imported_function(src_mod, orig, exports_by_mod, known_mods, fn_nodes)
            if target and target != caller:
                analysis.calls.append(JsCall(caller=caller, callee=target, file=call_file, line=call_line))
        for caller, src_mod, name, member, call_file, call_line in pending_member_calls:
            target = _member_function(
                src_mod, name, member, exports_by_mod, known_mods, fn_nodes, instances_by_mod, import_specs_by_mod
            )
            if target and target != caller:
                analysis.calls.append(
                    JsCall(caller=caller, callee=target, file=call_file, line=call_line, exact=False)
                )

        _import_path_helpers(consts_by_mod, import_specs_by_mod)
        # Helpers built from imported helpers (`site = (id) => `${base()}/sites/${id}``)
        # resolve once the imports are in scope; one more round covers one hop.
        for _round in range(2):
            for _p, _rf, module, tree, _src in parsed:
                consts = consts_by_mod.get(module) or {}
                for key, value in _collect_path_helpers(tree, consts).items():
                    # later rounds see imported helpers: their value is more
                    # complete (`{base()}/x` → `{VITE_API_URL}/api/v1/shop/x`)
                    consts[key] = value
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
        _resolve_specs_through_barrels(import_specs_by_mod, exports_by_mod, set(trees_by_mod))
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
        _import_wrappers(wrappers_by_mod, import_specs_by_mod, exports_by_mod, set(trees_by_mod))

        for scan_n, (_path, rel_file, module, tree, _src) in enumerate(parsed):
            progress.counter("js/ts: analysing", scan_n, len(parsed))
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
            _collect_route_literals(
                analysis, tree, rel_file, module, consts, routers, imports_by_mod.get(module) or {}
            )
        _resolve_route_handlers(analysis, import_specs_by_mod, exports_by_mod, set(trees_by_mod), fn_nodes)
        _attribute_inline_handler_sites(analysis)
        _bind_param_bases(analysis, fn_nodes)
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
        for node in typed_walk(tree, frozenset({"call_expression"}), JS_TYPES):
            method = call_callee_name(node).lower()
            raw_method = call_callee_name(node)
            args = call_args(node)
            recv = _receiver_ident(node)
            chain_router = _is_router_factory(node)
            wrap = wrappers.get(raw_method) or wrappers.get(method)
            if wrap and args and not recv:
                if wrap.recv:
                    if wrap.path_index < len(args):
                        self._record_client_http(
                            analysis,
                            node,
                            wrap.method.lower() or "get",
                            [args[wrap.path_index]],
                            wrap.recv,
                            clients,
                            consts,
                            rel_file,
                            module,
                            wrappers,
                        )
                    continue
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
                # Express `router.route('/x').get(h).post(h2)`: the path comes from
                # `.route()`, each method call takes only the handler
                chained = _route_chain(node, consts) if method in ROUTE_HTTP_METHODS else None
                if chained is not None and chained[1] and (chained[0] in routers or is_router):
                    path_val, path_unresolved = chained[1], False
                    prefix = routers.get(chained[0], prefix) if chained[0] in routers else prefix
                    is_router = True
                    args = [*args[:0], None, *args] if len(args) == 1 else args  # handler = args[-1]
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
                    handler_ref: tuple[str, ...] | None = None
                    if handler_node is not None and handler_node.type == "identifier":
                        handler_ref = ("ident", text(handler_node))
                    elif handler_node is not None and handler_node.type == "member_expression":
                        h_obj = handler_node.child_by_field_name("object")
                        h_prop = handler_node.child_by_field_name("property")
                        if h_obj is not None and h_prop is not None and h_obj.type == "identifier":
                            handler_ref = ("member", text(h_obj), text(h_prop))
                    route = JsRoute(
                        method=method.upper(),
                        path=path_norm,
                        handler=handler_qual or f"{module}.{handler_label}",
                        file=rel_file,
                        line=line_of(node),
                        unresolved=path_unresolved or ("{" in path_norm and "://" not in path_norm),
                        handler_qual=handler_qual,
                        handler_ref=handler_ref,
                        module=module,
                        span=(
                            (handler_node.start_point[0] + 1, handler_node.end_point[0] + 1)
                            if handler_node is not None
                            and handler_node.type in {"arrow_function", "function", "function_expression"}
                            else None
                        ),
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
        override = _per_call_base(method, args, consts)
        if override is not None:
            # axios/ky config `{ baseURL }` on the call wins over the instance
            base = override
        if path_val is None and args and (known or param_client or client_unknown):
            # The path expression could not be evaluated (opaque helper,
            # variable). Record the call with an explicit hole instead of
            # dropping it: it becomes `unresolved_http` (`path_dynamic`).
            expr = re.sub(r"[\s{}]+", " ", text(args[0])).strip()[:60] or "path"
            path_val = "/{" + (expr if "(" in expr else expr + "()") + "}"
            path_unresolved = True
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
        if override is None and path_val and base and base == clients.get(recv):
            # the same client parameter given another base elsewhere: the
            # call goes out through that base too
            for other in _other_client_bases(module, recv, base):
                url = _normalize_http_url(_join_route(other, path_val))
                if url != composed and _looks_like_http_path(url):
                    analysis.http_calls.append(
                        JsHttpCall(
                            method=http_method,
                            url=url,
                            file=rel_file,
                            line=line_of(node),
                            symbol=enclosing,
                            unresolved=path_unresolved or ("{" in url),
                            client_unknown=False,
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
        for node in typed_walk(body, frozenset({"call_expression"}), JS_TYPES):
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


_NAMED_FN_TYPES = {
    "function_declaration",
    "generator_function_declaration",
    "method_definition",
    "function",
    "function_expression",
    "generator_function",
}


def _js_why_anchor(fn: Node) -> Node:
    """The statement a doc comment sits above: `export function f`,
    `export const f = () =>`, a class method or an object member."""
    cur = fn
    if parent_of(cur) is not None and parent_of(cur).type in {"variable_declarator", "pair"}:
        cur = parent_of(cur)
    if cur.type == "variable_declarator" and parent_of(cur) is not None:
        cur = parent_of(cur)  # lexical_declaration / variable_declaration
    if parent_of(cur) is not None and parent_of(cur).type == "export_statement":
        cur = parent_of(cur)
    return cur


_JS_DIRECTIVE = re.compile(
    r"^(?:eslint[- ]|eslint$|prettier-ignore|istanbul\s+ignore|c8\s+ignore|v8\s+ignore|biome-ignore|"
    r"@ts-|ts-(?:ignore|expect-error|nocheck|check)|jshint|jscs:|global\s|noinspection|"
    r"(?:TODO|FIXME|HACK|XXX)\b)",
    re.IGNORECASE,
)
_JS_LICENSE = re.compile(
    r"(?i)\bcopyright\b|\bspdx-license-identifier\b|\blicensed\s+under\b|"
    r"\ball\s+rights\s+reserved\b|\(c\)\s*\d{4}|©"
)


def _comment_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        line = re.sub(r"^/\*[*!]?", "", line)
        line = re.sub(r"\*/$", "", line)
        line = re.sub(r"^//+", "", line)
        line = re.sub(r"^\*+", "", line).strip()
        if line:
            lines.append(line)
    return lines


def _is_overload_signature(node: Node | None) -> bool:
    if node is not None and node.type == "export_statement":
        node = node.named_children[0] if node.named_children else None
    return node is not None and node.type == "function_signature"


def _js_why(fn: Node) -> str | None:
    """`/** Marks an order as paid. */` or `// ...` lines right above the
    function (a blank line breaks the link). JSDoc `@param`-style tag lines,
    tool directives (`eslint-…`, `@ts-…`, `TODO`…) and license headers are
    dropped; a comment trailing code on its line belongs to that code. For a
    TypeScript overload, the doc above the first signature is used."""
    anchor = _js_why_anchor(fn)
    # `export function f(a: number): number;` overloads sit above the body
    first = anchor
    while _is_overload_signature(first.prev_named_sibling) and first.prev_named_sibling.end_point[0] >= first.start_point[0] - 1:
        first = first.prev_named_sibling
    comments: list[Node] = []
    prev = first.prev_sibling
    expect_row = first.start_point[0]
    while prev is not None and prev.type == "comment":
        if prev.end_point[0] < expect_row - 1:
            break  # blank line between the comment and what follows it
        before = prev.prev_sibling
        if before is not None and before.end_point[0] == prev.start_point[0] and before.type != "comment":
            break  # `const limit = 5; // max retries`: it documents that line
        comments.append(prev)
        expect_row = prev.start_point[0]
        prev = before
    if not comments:
        return None
    comments.reverse()
    lines: list[str] = []
    for comment in comments:
        raw = text(comment)
        if raw.startswith("/*!") or _JS_LICENSE.search(raw):
            continue  # license / banner header
        for line in _comment_lines(raw):
            if line.startswith("@") or _JS_DIRECTIVE.match(line):
                continue
            lines.append(line)
    why = " ".join(lines).strip()
    return why or None


def _fn_own_name(cur: Node) -> str | None:
    """Name a function-like node is known by, or None when anonymous.

    A function *expression* is known by its binding (`const topNamed =
    function topN() {}` is `topNamed`): its own name `topN` is only visible
    inside itself."""
    if cur.type in {"function", "function_expression", "generator_function"}:
        parent = parent_of(cur)
        if parent is not None and parent.type == "variable_declarator":
            return _declarator_name(parent) or None
        key = _returned_member_key(cur) or _object_member_key(cur)
        if key:
            return key
    if cur.type in _NAMED_FN_TYPES:
        name_n = cur.child_by_field_name("name")
        if name_n is not None and text(name_n):
            return text(name_n)
    if cur.type in {"arrow_function", "function", "function_expression"}:
        parent = parent_of(cur)
        if parent is not None and parent.type == "variable_declarator":
            return _declarator_name(parent) or None
        key = _returned_member_key(cur) or _object_member_key(cur)
        if key:
            return key
    return None


_OBJECT_WRAPPERS = frozenset({"parenthesized_expression", "as_expression", "satisfies_expression"})


def _declared_object_name(obj: Node | None) -> str | None:
    """`X` for the object of a module-level `[export] const X = { ... }`
    (also `{ ... } as const` / `satisfies T`): an object of methods, as an
    API client module is (`export const widgetsHttpClient = { list: ... }`)."""
    if obj is None or obj.type != "object":
        return None
    up = parent_of(obj)
    while up is not None and up.type in _OBJECT_WRAPPERS:
        up = parent_of(up)
    if up is None or up.type != "variable_declarator":
        return None
    decl = parent_of(up)
    if decl is None or decl.type not in {"lexical_declaration", "variable_declaration"}:
        return None
    top = parent_of(decl)
    if top is not None and top.type == "export_statement":
        top = parent_of(top)
    if top is None or top.type != "program":
        return None
    name = _declarator_name(up)
    return name if name and name.isidentifier() else None


def _object_member_key(fn: Node) -> str | None:
    """`list` for the function in `const X = { list: (p) => ... }` at module
    level (also `list() {}` / `list: function () {}`): a member of an object of
    methods, known as `X.list`."""
    if fn.type == "method_definition":
        obj = parent_of(fn)
        key = fn.child_by_field_name("name")
    else:
        pair = parent_of(fn)
        if pair is None or pair.type != "pair" or pair.child_by_field_name("value") != fn:
            return None
        obj = parent_of(pair)
        key = pair.child_by_field_name("key")
    if _declared_object_name(obj) is None:
        return None
    name = _unquote(text(key)) if key is not None else ""
    return name if name.isidentifier() else None


def _returned_member_key(fn: Node) -> str | None:
    """`one` for the function in `return { one: (id) => ... }` (or an arrow
    body `=> ({ one: ... })`) of a named function: a factory member."""
    pair = parent_of(fn)
    if pair is None or pair.type != "pair" or pair.child_by_field_name("value") != fn:
        return None
    if not _is_returned_object(parent_of(pair)):
        return None
    key = pair.child_by_field_name("key")
    name = _unquote(text(key)) if key is not None else ""
    return name if name.isidentifier() else None


def _is_returned_object(obj: Node | None) -> bool:
    """`return { ... }` or an arrow body `=> ({ ... })`."""
    if obj is None or obj.type != "object":
        return False
    up = parent_of(obj)
    while up is not None and up.type == "parenthesized_expression":
        up = parent_of(up)
    if up is None:
        return False
    return up.type == "return_statement" or (
        up.type == "arrow_function" and up.child_by_field_name("body") is not None
    )


def _fn_qual(fn: Node, module: str) -> str | None:
    """`<module>.<outer>.<inner>`: a declaration nested in functions is
    qualified by every named function around it, so two factories can each
    have their own `build` helper. Top-level ones stay `<module>.<name>`."""
    own = _fn_own_name(fn)
    if not own:
        return None
    chain = [own]
    cur = parent_of(fn)
    while cur is not None:
        if cur.type in _NAMED_FN_TYPES or cur.type == "arrow_function":
            name = _fn_own_name(cur)
            if name and cur.type != "method_definition":
                chain.append(name)
        elif cur.type == "object":
            obj_name = _declared_object_name(cur)
            if obj_name:
                chain.append(obj_name)  # `const X = { m: ... }` -> X.m
        cur = parent_of(cur)
    return ".".join([module, *reversed(chain)])


_DECL_TYPES = frozenset(
    {"function_declaration", "generator_function_declaration", "method_definition", "variable_declarator", "pair"}
)
_FN_OR_DECL = frozenset({"function_declaration", "function", "method_definition", "variable_declarator"})
_FN_NODES = frozenset(
    {"function_declaration", "function", "function_expression", "arrow_function", "method_definition"}
)
_EXPORT_NODES = frozenset({"export_statement", "expression_statement", "lexical_declaration"})
_ENCLOSING_SYMBOL: dict[tuple[str, int, int, int], str] = {}


def _enclosing_symbol(node: Node, module: str) -> str:
    """Qualified name of the nearest named function above ``node``. Memoized
    for every node on the climb (cleared in `analyze`), so the many calls of
    one long function do not each climb to it."""
    path: list[tuple[str, int, int, int]] = []
    cur = parent_of(node)
    found = f"{module}.module"
    while cur is not None:
        key = (module, cur.id, cur.start_byte, cur.end_byte)
        hit = _ENCLOSING_SYMBOL.get(key)
        if hit is not None:
            found = hit
            break
        path.append(key)
        if cur.type in _NAMED_FN_TYPES or cur.type == "arrow_function":
            qual = _fn_qual(cur, module)
            if qual:
                found = qual
                break
        cur = parent_of(cur)
    for key in path:
        _ENCLOSING_SYMBOL[key] = found
    return found


_FN_SCOPE_TYPES = {
    "function_declaration",
    "generator_function_declaration",
    "function",
    "function_expression",
    "generator_function",
    "arrow_function",
    "method_definition",
}


def _pattern_names(node: Node | None, out: set[str]) -> None:
    """Names bound by a parameter / declarator pattern (`a`, `{ a, b: c }`,
    `[a, ...b]`, `a = 1`), not the default values."""
    if node is None:
        return
    t = node.type
    if t in {"identifier", "shorthand_property_identifier_pattern"}:
        out.add(text(node))
    elif t == "pair_pattern":
        _pattern_names(node.child_by_field_name("value"), out)
    elif t in {"assignment_pattern", "object_assignment_pattern"}:
        _pattern_names(node.child_by_field_name("left"), out)
    elif t in {"required_parameter", "optional_parameter"}:
        _pattern_names(node.child_by_field_name("pattern"), out)
    elif t in {
        "object_pattern",
        "array_pattern",
        "rest_pattern",
        "formal_parameters",
    }:
        for child in node.named_children:
            _pattern_names(child, out)


_SCOPE_BINDINGS: dict[tuple[int, int, int], frozenset[str]] = {}


def _scope_bindings(scope: Node) -> frozenset[str]:
    """Names a function (or catch clause) binds itself: parameters and the
    `let`/`var`/`const` declarations in its body, nested functions excluded.
    Memoized per scope (cleared in `analyze`): a function with many calls
    asks for the same scope once per call."""
    key = (scope.id, scope.start_byte, scope.end_byte)
    hit = _SCOPE_BINDINGS.get(key)
    if hit is None:
        hit = _SCOPE_BINDINGS[key] = frozenset(_compute_scope_bindings(scope))
    return hit


def _compute_scope_bindings(scope: Node) -> set[str]:
    names: set[str] = set()
    if scope.type == "catch_clause":
        _pattern_names(scope.child_by_field_name("parameter"), names)
        return names
    params = scope.child_by_field_name("parameters")
    if params is not None:
        _pattern_names(params, names)
    else:
        single = scope.child_by_field_name("parameter")  # `x => ...`
        _pattern_names(single, names)
    body = scope.child_by_field_name("body")
    if body is None:
        return names
    stack = list(body.named_children) if body.type == "statement_block" else []
    while stack:
        cur = stack.pop()
        if cur.type in _FN_SCOPE_TYPES or cur.type == "class_declaration":
            continue
        if cur.type == "variable_declarator":
            _pattern_names(cur.child_by_field_name("name"), names)
        elif cur.type in {"for_in_statement"}:
            _pattern_names(cur.child_by_field_name("left"), names)
        stack.extend(cur.named_children)
    return names


def _binding_scope(call: Node, name: str) -> Node | None:
    """Innermost function / catch clause between the call and module scope
    that binds ``name`` (parameter, `let`/`var`/`const`, `catch (name)`)."""
    cur = parent_of(call)
    while cur is not None and cur.type != "program":
        if cur.type in _FN_SCOPE_TYPES or cur.type == "catch_clause":
            if name in _scope_bindings(cur):
                return cur
        cur = parent_of(cur)
    return None


def _locally_shadowed(call: Node, name: str) -> bool:
    return _binding_scope(call, name) is not None


def _bound_function(scope: Node, name: str) -> Node | None:
    """The arrow/function a `const name = () => {}` in ``scope``'s own body
    binds (nested functions excluded), else None."""
    if scope.type == "catch_clause":
        return None
    body = scope.child_by_field_name("body")
    if body is None or body.type != "statement_block":
        return None
    stack = list(body.named_children)
    while stack:
        cur = stack.pop()
        if cur.type in _FN_SCOPE_TYPES or cur.type == "class_declaration":
            continue
        if cur.type == "variable_declarator":
            nm = cur.child_by_field_name("name")
            value = cur.child_by_field_name("value")
            if nm is not None and nm.type == "identifier" and text(nm) == name:
                if value is not None and value.type in {
                    "arrow_function",
                    "function",
                    "function_expression",
                    "generator_function",
                }:
                    return value
                return None
        stack.extend(cur.named_children)
    return None


def _scoped_callee(caller: str, callee: str, module: str, fn_nodes: dict[str, Node]) -> str | None:
    """`build(id)` inside `<module>.createA.one`: the nearest enclosing scope
    that declares `build` (`<module>.createA.build`), nested scopes only."""
    if not caller.startswith(module + ".") or caller.endswith(".module"):
        return None
    parts = caller[len(module) + 1 :].split(".")
    for depth in range(len(parts), 0, -1):
        cand = ".".join([module, *parts[:depth], callee])
        if cand in fn_nodes and cand != caller:
            return cand
    return None


_ENV_OBJECTS = {"import.meta.env", "process.env", "Bun.env", "Deno.env"}


def _collect_consts(tree: Node) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in typed_walk(tree, frozenset({"variable_declarator"}), JS_TYPES):
        value = node.child_by_field_name("value")
        name_node = node.child_by_field_name("name")
        if value is None or name_node is None:
            continue
        vtext = text(value).strip()
        if name_node.type == "object_pattern" and vtext in _ENV_OBJECTS:
            # const { VITE_API_URL, API: base } = import.meta.env
            for prop in name_node.named_children:
                if prop.type == "shorthand_property_identifier_pattern":
                    consts[text(prop)] = "{" + text(prop) + "}"
                elif prop.type == "pair_pattern":
                    key = prop.child_by_field_name("key")
                    val = prop.child_by_field_name("value")
                    if key is not None and val is not None and val.type == "identifier":
                        consts[text(val)] = "{" + _unquote(text(key)) + "}"
            continue
        name = _declarator_name(node)
        if not name:
            continue
        if vtext in _ENV_OBJECTS:
            # const env = import.meta.env → env.X is an env placeholder
            consts[f"{name}.*env"] = "1"
            continue
        if value.type == "object":
            # export const config = { apiUrl: import.meta.env.VITE_API_URL }
            for pair in value.named_children:
                if pair.type != "pair":
                    continue
                key = pair.child_by_field_name("key")
                pval = pair.child_by_field_name("value")
                if key is None or pval is None:
                    continue
                v, _u = _resolve_js_string(_unwrap_fallback(pval), consts)
                if v is not None:
                    consts[f"{name}.{_unquote(text(key))}"] = v
            continue
        # `import.meta.env.X as string | undefined`, `raw ?? ''`: the env
        val, unresolved = _resolve_js_string(_unwrap_fallback(value), consts)
        if val is not None:
            consts[name] = val
            _ = unresolved
    return consts


def _collect_receivers(tree: Node, consts: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    consts = consts or {}
    routers: dict[str, str] = {}
    clients: dict[str, str] = {}
    for node in typed_walk(tree, frozenset({"variable_declarator"}), JS_TYPES):
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
    for node in typed_walk(tree, frozenset({"variable_declarator"}), JS_TYPES):
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
        for node in typed_walk(fn_node, frozenset({"call_expression"}), JS_TYPES):
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
                # `http.request({ baseURL: V2, url })`: the request's own base
                # wins over the instance's
                override = _per_call_base(method, args, extra)
                if path_val or override is not None:
                    extra_url = path_val
                    base = override if override is not None else (clients.get(recv) or "")
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
                if recv and recv not in clients and method in CLIENT_HTTP_METHODS and url_node is not None:
                    if text(url_node) == path_param and _is_outer_fn_param(fn_node, recv):
                        wrappers[name] = JsHttpWrapper(
                            method=http_method, base="", path_index=path_i, method_index=method_i, recv=recv
                        )
                        return
                continue
            wrappers[name] = JsHttpWrapper(
                method=http_method if method_i is None else "",
                base=base.rstrip("/"),
                path_index=path_i,
                method_index=method_i,
            )
            return

    for node in typed_walk(tree, frozenset({"function_declaration", "function", "variable_declarator"}), JS_TYPES):
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


def _is_outer_fn_param(fn_node: Node, name: str) -> bool:
    """`name` is a parameter of a function enclosing ``fn_node`` (a factory)."""
    cur = parent_of(fn_node)
    while cur is not None:
        if cur.type in {"arrow_function", "function", "function_declaration", "function_expression", "method_definition"}:
            if name in _fn_param_names(cur):
                return True
        cur = parent_of(cur)
    return False


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
    path_aliases: list | None = None,
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Return (local→qualname, local→(src_module, orig_name))."""
    out: dict[str, str] = {}
    specs: dict[str, tuple[str, str]] = {}
    for node in typed_walk(tree, frozenset({"import_statement", "lexical_declaration"}), JS_TYPES):
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
        star = re.search(r"import\s+\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)", head)
        if star:
            specs[star.group(1)] = (src_mod, "*")
            out[star.group(1)] = f"{src_mod}.*"
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
            mdes = re.search(r"(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\(", raw)
            if mdes:
                # const { a, b: c } = require('./lib')
                for part in mdes.group(1).split(","):
                    part = part.strip()
                    if not part:
                        continue
                    orig, _colon, alias = part.partition(":")
                    orig, alias = orig.strip(), (alias.strip() or orig.strip())
                    specs[alias] = (src_mod, orig)
                    out[alias] = f"{src_mod}.{orig}"
            mreq = re.search(r"(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", raw)
            if mreq:
                local = mreq.group(1)
                specs.setdefault(local, (src_mod, "default"))
                out.setdefault(local, f"{src_mod}.{local}")
    return out, specs


def _collect_exports(tree: Node) -> dict[str, str]:
    """exported name → local identifier (``default`` for default export).

    Only statements can export: the file itself and its export / expression /
    declaration statements are read (reading every node's text was quadratic)."""
    out: dict[str, str] = {}
    for node in [tree, *typed_walk(tree, _EXPORT_NODES, JS_TYPES)]:
        raw = text(node)
        if node.type not in {"export_statement", "expression_statement", "lexical_declaration"}:
            if not raw.strip().startswith("export ") and "module.exports" not in raw:
                continue
        m = re.search(
            r"export\s+default\s+(?:async\s+)?(?:function\*?\s+(?=[A-Za-z_])|class\s+(?=[A-Za-z_]))?([A-Za-z_][A-Za-z0-9_]*)",
            raw,
        )
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
        m = re.search(r"module\.exports\s*=\s*\{([^}]*)\}", raw)
        if m:
            # module.exports = { a, b: c }
            for part in m.group(1).split(","):
                part = part.strip()
                if not part:
                    continue
                key, _colon, value = part.partition(":")
                key, value = key.strip(), (value.strip() or key.strip())
                if key.isidentifier() and value.isidentifier():
                    out[key] = value
        for mm in re.finditer(r"(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)", raw):
            out[mm.group(1)] = mm.group(2)
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
    for node in typed_walk(tree, frozenset({"call_expression"}), JS_TYPES):
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
    for node in typed_walk(tree, frozenset({"variable_declarator"}), JS_TYPES):
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
    for node in typed_walk(tree, frozenset({"class_declaration", "class"}), JS_TYPES):
        name_n = node.child_by_field_name("name")
        name = text(name_n) if name_n else ""
        if not name:
            continue
        for child in typed_walk(node, frozenset({"method_definition", "method_signature", "function"}), JS_TYPES):
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
        # strings built in the body (`const baseURL = `${baseUrl}/api/v1``):
        # `axios.create({ baseURL })` reads them; parameters stay `{param}` holes
        scope = dict(consts)
        for node in typed_walk(fn_node, frozenset({"variable_declarator"}), JS_TYPES):
            dname = _declarator_name(node)
            value = node.child_by_field_name("value")
            if not dname or value is None:
                continue
            if value.type in {"template_string", "string", "binary_expression"}:
                built, _u = _resolve_js_string(value, scope)
                if built:
                    scope[dname] = built
                continue
            base = _create_call_base(value, scope)
            if base is not None:
                local_bases[dname] = base
        body = fn_node.child_by_field_name("body")
        if body is not None:
            implicit = _create_call_base(body, scope)
            if implicit:
                factories[name] = JsClientFactory(base=implicit, params=params)
                return
            if body.type == "identifier" and text(body) in local_bases:
                factories[name] = JsClientFactory(base=local_bases[text(body)], params=params)
                return
        for node in typed_walk(fn_node, frozenset({"return_statement", "return"}), JS_TYPES):
            expr = node.named_children[0] if node.named_children else None
            if expr is None:
                continue
            expr = unwrap_callee_expr(expr) or expr
            base = _create_call_base(expr, scope)
            if base:
                factories[name] = JsClientFactory(base=base, params=params)
                return
            if expr.type == "identifier" and text(expr) in local_bases:
                factories[name] = JsClientFactory(base=local_bases[text(expr)], params=params)
                return

    for node in typed_walk(tree, _FN_OR_DECL, JS_TYPES):
        if node.type in {"function_declaration", "function", "method_definition"}:
            name_n = node.child_by_field_name("name")
            consider(text(name_n) if name_n else "", node)
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            name = _declarator_name(node)
            if name and value is not None and value.type in {"arrow_function", "function"}:
                consider(name, value)
    return factories


def _final_source(
    mod: str,
    name: str,
    exports_by_mod: dict[str, dict[str, str]],
    known: set[str],
    depth: int = 0,
) -> tuple[str, str] | None:
    """Module that actually defines `name`, following `index` files and
    `export { a as b } from` / `export * from` re-exports."""
    if depth > 6:
        return None
    for cand in (mod, f"{mod}.index"):
        if cand not in known:
            continue
        named, stars = _REEXPORTS.get(cand, ({}, []))
        if name in named:
            nmod, orig = named[name]
            return _final_source(nmod, orig, exports_by_mod, known, depth + 1) or (nmod, orig)
        if name in (exports_by_mod.get(cand) or {}) or not stars:
            return cand, name
        for star in stars:
            hit = _final_source(star, name, exports_by_mod, known, depth + 1)
            if hit and name in (exports_by_mod.get(hit[0]) or {}):
                return hit
        return cand, name
    return None


def _export_sources(
    mod: str,
    name: str,
    exports_by_mod: dict[str, dict[str, str]],
    known: set[str],
    depth: int = 0,
) -> set[tuple[str, str]]:
    """Every (module, local name) ``name`` can come from when imported from
    ``mod``: its own export, `export { a as b } from`, or each `export *`."""
    if depth > 6:
        return set()
    for cand in (mod, f"{mod}.index"):
        if cand not in known:
            continue
        exports = exports_by_mod.get(cand) or {}
        named, stars = _REEXPORTS.get(cand, ({}, []))
        # `export { a as b } from './x'` also lands in the barrel's own export
        # map (b -> a); the re-export says where it really lives
        if name in named:
            nmod, orig = named[name]
            return _export_sources(nmod, orig, exports_by_mod, known, depth + 1) or {(nmod, orig)}
        if name in exports:
            return {(cand, exports[name])}
        found: set[tuple[str, str]] = set()
        for star in stars:
            if name == "default":
                continue  # `export *` never re-exports the default
            found |= _export_sources(star, name, exports_by_mod, known, depth + 1)
        return found
    return set()


def _attribute_inline_handler_sites(analysis: JsAnalysis) -> None:
    """SQL / table / collection accesses inside an inline route handler
    (`app.post('/x', async (req, res) => {...})`) belong to that handler, not
    to `<module>.module`: every site, so two handlers touching the same column
    each get their edge."""
    spans: dict[str, list[tuple[int, int, str]]] = {}
    for route in analysis.routes:
        if route.span and route.method != "USE":
            spans.setdefault(route.file, []).append((route.span[0], route.span[1], route.handler))
    if not spans:
        return
    for items in (analysis.sql_sites, analysis.table_accesses, analysis.accesses, analysis.rpc_calls):
        for item in items:
            symbol = getattr(item, "symbol", "")
            if not isinstance(symbol, str) or not symbol.endswith(".module"):
                continue
            inside = [sp for sp in spans.get(getattr(item, "file", ""), []) if sp[0] <= getattr(item, "line", 0) <= sp[1]]
            if inside:
                item.symbol = min(inside, key=lambda sp: sp[1] - sp[0])[2]


def _resolve_route_handlers(
    analysis: JsAnalysis,
    import_specs_by_mod: dict[str, dict[str, tuple[str, str]]],
    exports_by_mod: dict[str, dict[str, str]],
    known: set[str],
    fn_nodes: dict[str, Node],
) -> None:
    """`app.get('/orders', listHandler)` with `import listHandler from` (a
    default export) or `h.cancelHandler` with `import * as h`: point the
    route at the scanned function, so it gets its tables and `why`."""
    renames: dict[str, str] = {}
    for route in analysis.routes:
        if not route.handler_ref or not route.module or (route.handler_qual and route.handler_qual in fn_nodes):
            continue
        specs = import_specs_by_mod.get(route.module) or {}
        target = None
        if route.handler_ref[0] == "ident":
            spec = specs.get(route.handler_ref[1])
            if spec:
                target = _imported_function(spec[0], spec[1], exports_by_mod, known, fn_nodes)
        elif route.handler_ref[0] == "member":
            spec = specs.get(route.handler_ref[1])
            if spec and spec[1] == "*":
                target = _imported_function(spec[0], route.handler_ref[2], exports_by_mod, known, fn_nodes)
        if target:
            old = route.handler
            renames[old] = target
            route.handler = target
            route.handler_qual = target
    if renames:
        analysis.calls[:] = [
            JsCall(
                caller=renames.get(c.caller, c.caller),
                callee=renames.get(c.callee, c.callee),
                file=c.file,
                line=c.line,
            )
            for c in analysis.calls
            if renames.get(c.caller, c.caller) != renames.get(c.callee, c.callee)
        ]


def _import_wrappers(
    wrappers_by_mod: dict[str, dict[str, JsHttpWrapper]],
    import_specs_by_mod: dict[str, dict[str, tuple[str, str]]],
    exports_by_mod: dict[str, dict[str, str]],
    known: set[str],
) -> None:
    """`import { getJSON } from '../lib/http'`: an HTTP wrapper defined in
    another module (`getJSON(path) => http.get(path)`) is followed at the
    importing module's call sites too."""
    own = {mod: dict(ws) for mod, ws in wrappers_by_mod.items()}
    for module, specs in import_specs_by_mod.items():
        local = wrappers_by_mod.setdefault(module, {})
        for name, (src_mod, orig) in specs.items():
            if name in local:
                continue
            name_in_src = "default" if orig == "require" else orig
            sources = _export_sources(src_mod, name_in_src, exports_by_mod, known)
            wrap = None
            if len(sources) == 1:
                mod, loc = next(iter(sources))
                wrap = (own.get(mod) or {}).get(loc)
            elif not sources:
                for mod in (src_mod, f"{src_mod}.index"):
                    wrap = (own.get(mod) or {}).get(name_in_src)
                    if wrap is not None:
                        break
            if wrap is not None and not wrap.recv:  # a factory's own helper stays local
                local[name] = wrap


def _imported_function(
    src_mod: str,
    orig: str,
    exports_by_mod: dict[str, dict[str, str]],
    known: set[str],
    fn_nodes: dict[str, Node],
) -> str | None:
    """Scanned function an import names: through `index` barrels,
    `export { a } from` / `export * from`, `export default <identifier>` and
    CommonJS exports. A name two `export *` sources both provide is
    ambiguous (TypeScript does not export it): no target."""
    if orig == "require":
        orig = "default"
    sources = _export_sources(src_mod, orig, exports_by_mod, known)
    targets = {f"{m}.{local}" for m, local in sources if f"{m}.{local}" in fn_nodes}
    if len(sources) > 1:
        return None
    if len(targets) == 1:
        return next(iter(targets))
    if not sources:
        for mod in (src_mod, f"{src_mod}.index"):
            if f"{mod}.{orig}" in fn_nodes:
                return f"{mod}.{orig}"
    return None


class _CallLinker:
    """Calls one file makes: by name (`listMembers(id)`), on an object or
    factory instance (`client.list(p)`), and functions passed by reference
    (`queryFn: listWidgets`, `mutationFn: client.create`, `onClick={X.m}`),
    which count as calls by whoever passes them. Imported targets are queued
    and resolved once every file is indexed."""

    def __init__(
        self,
        module: str,
        rel_file: str,
        specs: dict[str, tuple[str, str]],
        fn_nodes: dict[str, Node],
        fn_by_span: dict[tuple[int, int], str],
        calls: list[JsCall],
        pending_import_calls: list[tuple[str, tuple[str, str], str, int]],
        pending_ref_imports: list[tuple[str, tuple[str, str], str, int]],
        pending_member_calls: list[tuple[str, str, str, str, str, int]],
    ) -> None:
        self.module, self.rel_file, self.specs = module, rel_file, specs
        self.fn_nodes, self.fn_by_span, self.calls = fn_nodes, fn_by_span, calls
        self.pending_import_calls = pending_import_calls
        self.pending_ref_imports = pending_ref_imports
        self.pending_member_calls = pending_member_calls

    def member(self, at: Node, obj: Node, prop: Node, caller: str) -> None:
        """`X.m` called or passed at ``at``."""
        if _locally_shadowed(at, text(obj)):
            return
        spec = self.specs.get(text(obj))
        line = line_of(at)
        if spec and spec[1] in {"*", "require"}:
            self.pending_import_calls.append((caller, (spec[0], text(prop)), self.rel_file, line))
        elif spec:
            # `import { widgetsHttpClient as client } ...; client.list()`
            self.pending_member_calls.append((caller, spec[0], spec[1], text(prop), self.rel_file, line))
        else:
            # `const X = { m: .. }` / `const X = createY(..)` in this file
            self.pending_member_calls.append((caller, self.module, text(obj), text(prop), self.rel_file, line))

    def name(self, at: Node, name: str, caller: str, exact: bool) -> None:
        """A function named ``name`` called (``exact``) or passed at ``at``."""
        module, fn_nodes = self.module, self.fn_nodes
        callee = _scoped_callee(caller, name, module, fn_nodes)
        if callee is None:
            scope = _binding_scope(at, name)
            if scope is not None:
                # `const report = (m) => ...` in an anonymous callback or
                # an object-literal method: that local function is the
                # callee; a parameter / plain variable is not a function
                bound = _bound_function(scope, name)
                callee = self.fn_by_span.get((bound.start_byte, bound.end_byte)) if bound is not None else None
                if callee is None or callee == caller:
                    return
        if callee is None and f"{module}.{name}" in fn_nodes and f"{module}.{name}" != caller:
            callee = f"{module}.{name}"  # module-level function
        if callee:
            self.calls.append(JsCall(caller=caller, callee=callee, file=self.rel_file, line=line_of(at), exact=exact))
        elif name in self.specs:
            entry = (caller, self.specs[name], self.rel_file, line_of(at))
            if exact:
                self.pending_import_calls.append(entry)
            elif self.specs[name][1] not in {"*", "require"}:
                self.pending_ref_imports.append(entry)

    def ref(self, ref: Node | None, at: Node) -> None:
        """A function passed by reference at ``at`` (an argument, a value in
        an object argument, a JSX `on...` attribute)."""
        if ref is None or ref.type not in {"identifier", "member_expression"}:
            return
        caller = _enclosing_symbol(at, self.module)
        if caller.endswith(".module"):
            return
        if ref.type == "identifier":
            self.name(at, text(ref), caller, exact=False)
            return
        obj, prop = ref.child_by_field_name("object"), ref.child_by_field_name("property")
        if obj is not None and prop is not None and obj.type == "identifier":
            self.member(at, obj, prop, caller)


def _collect_instances(tree: Node) -> dict[str, str]:
    """`X -> createY` for a module-level `[export] const X = createY(...)`: an
    object a factory made (its methods are `createY.m`)."""
    out: dict[str, str] = {}
    for stmt in tree.named_children:
        decl = stmt
        if decl.type == "export_statement":
            decl = decl.child_by_field_name("declaration") or next(
                (c for c in decl.named_children if c.type in {"lexical_declaration", "variable_declaration"}), None
            )
        if decl is None or decl.type not in {"lexical_declaration", "variable_declaration"}:
            continue
        for d in decl.named_children:
            if d.type != "variable_declarator":
                continue
            value = d.child_by_field_name("value")
            while value is not None and value.type in {"await_expression", "parenthesized_expression"}:
                value = value.named_children[0] if value.named_children else None
            if value is None or value.type not in {"call_expression", "new_expression"}:
                continue
            fn = value.child_by_field_name("function") or value.child_by_field_name("constructor")
            name = _declarator_name(d)
            if fn is not None and fn.type == "identifier" and name:
                out[name] = text(fn)
    return out


def _member_function(
    src_mod: str,
    name: str,
    member: str,
    exports_by_mod: dict[str, dict[str, str]],
    known: set[str],
    fn_nodes: dict[str, Node],
    instances_by_mod: dict[str, dict[str, str]],
    import_specs_by_mod: dict[str, dict[str, tuple[str, str]]],
) -> str | None:
    """Scanned function `name.member` is, for `name` living in ``src_mod``:
    a method of an object of methods (`X.m`), or of the object a factory
    returns when `name = createY(...)` (`createY.m`), through barrels."""
    sources = _export_sources(src_mod, name, exports_by_mod, known)
    if not sources:
        sources = {(mod, name) for mod in (src_mod, f"{src_mod}.index") if mod in known}
    found: set[str] = set()
    for mod, local in sources:
        direct = f"{mod}.{local}.{member}"
        if direct in fn_nodes:
            found.add(direct)
            continue
        factory = (instances_by_mod.get(mod) or {}).get(local)
        if not factory:
            continue
        fq: str | None = f"{mod}.{factory}" if f"{mod}.{factory}" in fn_nodes else None
        spec = (import_specs_by_mod.get(mod) or {}).get(factory)
        if fq is None and spec and spec[1] not in {"*", "require"}:
            fq = _imported_function(spec[0], spec[1], exports_by_mod, known, fn_nodes)
        if fq and f"{fq}.{member}" in fn_nodes:
            found.add(f"{fq}.{member}")
    return next(iter(found)) if len(found) == 1 else None


def _resolve_specs_through_barrels(
    import_specs_by_mod: dict[str, dict[str, tuple[str, str]]],
    exports_by_mod: dict[str, dict[str, str]],
    known: set[str],
) -> None:
    for specs in import_specs_by_mod.values():
        for local, (src_mod, orig) in list(specs.items()):
            if orig == "*" or (src_mod in known and orig in (exports_by_mod.get(src_mod) or {})):
                continue
            hit = _final_source(src_mod, orig, exports_by_mod, known)
            if hit and hit != (src_mod, orig):
                specs[local] = hit


# (module, client name) -> every base it was given: one factory instantiated
# with two clients (`createDelivery(clientApi)`, `createDelivery(portalApi)`)
# calls through both. Reset per `analyze`.
_CLIENT_BASES: dict[tuple[str, str], list[str]] = {}
_PARAM_HOLE = re.compile(r"^\{[a-z_$][A-Za-z0-9_$]*\}")


def _is_bound_base(base: str) -> bool:
    """A base that says where it goes: not empty and not still a factory
    parameter hole (`{baseUrl}/api`); env placeholders are fine."""
    return bool(base) and not _PARAM_HOLE.match(base)


def _note_client_base(module: str, clients: dict[str, str], key: str, base: str) -> bool:
    """Record ``base`` for ``key`` in ``module``; ``clients[key]`` keeps the
    first bound base (stable across rounds). True when anything changed."""
    seen = _CLIENT_BASES.setdefault((module, key), [])
    changed = False
    if base not in seen:
        # a later round may only complete a base (`{X}/api` -> `{X}/api/v1`):
        # that is the same base, not a second one
        grown = [b for b in seen if _is_bound_base(b) and base.startswith(b)]
        for b in grown:
            seen[seen.index(b)] = base
        if not grown:
            seen.append(base)
        while seen.count(base) > 1:
            seen.remove(base)
        changed = True
    bound = [b for b in seen if _is_bound_base(b)]
    pick = bound[0] if bound else seen[0]
    if clients.get(key) != pick:
        clients[key] = pick
        changed = True
    return changed


def _other_client_bases(module: str, recv: str, base: str) -> list[str]:
    """The other bound bases ``recv`` was given besides ``base``."""
    return [b for b in _CLIENT_BASES.get((module, recv), []) if _is_bound_base(b) and b != base]


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
        for node in typed_walk(tree, _FN_OR_DECL, JS_TYPES):
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
            if i >= len(params):
                continue
            inline = None
            if arg.type == "call_expression":
                inline = (factories_by_mod.get(module) or {}).get(call_callee_name(arg))
            if arg.type == "call_expression" and call_callee_name(arg) == "create":
                # createShopClient(axios.create({ baseURL: ... }))
                base = _create_call_base(arg, consts_by_mod.get(module) or {})
                if base is None:
                    continue
            elif inline is not None:
                # createDeliveryHttpClient(createClientApi(import.meta.env.X))
                base = _instantiate_factory(inline, arg, consts_by_mod.get(module) or {})
                if not base:
                    continue
            elif arg.type != "identifier":
                continue
            else:
                src = text(arg)
                base = clients.get(src)
                if base is None:
                    base = dest_clients.get(src)
                if base is None:
                    continue
            pname = params[i]
            for key in (pname, f"this.{pname}", f"self.{pname}"):
                if _note_client_base(dest_mod, dest_clients, key, base):
                    changed = True
                if _note_client_base(module, clients, key, base):
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
            for node in typed_walk(tree, _FN_NODES, JS_TYPES):
                if node.type in _FN_NODES:
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
            for node in typed_walk(tree, frozenset({"variable_declarator"}), JS_TYPES):
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
            for node in typed_walk(tree, frozenset({"call_expression", "new_expression"}), JS_TYPES):
                if node.type == "call_expression":
                    if _bind_client_args(module, clients, node, call_callee_name(node)):
                        changed = True
                elif node.type == "new_expression":
                    if _bind_client_args(module, clients, node, _ctor_leaf(node)):
                        changed = True
        if not changed:
            break



# module -> its file's directory parts (set per `analyze`)
_MODULE_DIRS: dict[str, tuple[str, ...]] = {}


def _spec_to_module(
    spec: str,
    current: str,
    path_aliases: list | None = None,
) -> str:
    spec = spec.strip()
    if spec.startswith("."):
        # the importing file's real directory: a dotted file name
        # (`widgets.queries.ts` -> module `...widgets.widgets.queries`) is not
        # a folder per dot
        known_dir = _MODULE_DIRS.get(current)
        parts = [*known_dir, "_"] if known_dir is not None else current.split(".")
        rest = spec
        while rest.startswith("../"):
            parts = parts[:-1]
            rest = rest[3:]
        if rest.startswith("./"):
            rest = rest[2:]
        rest = _strip_js_ext(rest)
        bits = [p for p in rest.split("/") if p and p != "."]
        return ".".join([*parts[:-1], *bits]) if bits else ".".join(parts[:-1]) or current
    mapped = _map_ts_alias(spec, path_aliases or [], current)
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


def _map_ts_alias(spec: str, aliases: list, current: str | None = None) -> str | None:
    """Map ``spec`` with tsconfig ``paths`` (or a Vite ``resolve.alias``).

    Entries are ``(pattern, targets)`` or ``(pattern, targets, scope)``; a
    scoped entry (the module prefix of the config's directory) only applies
    to files under it, and the deepest matching scope wins, so two apps in one
    repo can each define their own ``@/*``."""
    if not aliases:
        return None

    def scope_of(item: tuple) -> str:
        return item[2] if len(item) > 2 else ""

    usable = [
        a
        for a in aliases
        if not scope_of(a) or (current is not None and (current == scope_of(a) or current.startswith(scope_of(a) + ".")))
    ]
    first: str | None = None
    for item in sorted(usable, key=lambda a: (-len(scope_of(a)), -len(a[0]))):
        pattern, targets = item[0], item[1]
        for target in targets:
            mapped = _apply_ts_pattern(spec, pattern, target)
            if not mapped:
                continue
            module = _path_to_module(mapped)
            # `"~/*": ["src/*", "generated/*"]`: the first target that exists,
            # like TypeScript (falls back to the first mapping)
            if not _KNOWN_MODULES or module in _KNOWN_MODULES or f"{module}.index" in _KNOWN_MODULES:
                return module
            first = first or module
        if first:
            break
    return first


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
    opts = data.get("compilerOptions")
    if isinstance(opts, dict):
        # baseUrl is relative to the config that sets it; paths without a
        # baseUrl are relative to the config that sets them
        opts = dict(opts)
        if opts.get("baseUrl"):
            opts["_baseDir"] = str((path.parent / str(opts["baseUrl"])).resolve())
        if opts.get("paths"):
            opts["_pathsDir"] = str(path.parent)
        data = dict(data, compilerOptions=opts)
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


def _ts_path_aliases(root: Path) -> list[tuple[str, list[str], str]]:
    """compilerOptions.paths from tsconfig.json / jsconfig.json (with extends),
    plus simple Vite ``resolve.alias`` entries. Targets are made relative to
    the scan root; each alias is scoped to the directory of its config."""
    configs: list[Path] = []
    vite: list[Path] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for name in filenames:
            if name in {"tsconfig.json", "jsconfig.json"} or (
                name.startswith("tsconfig.") and name.endswith(".json")
            ):
                configs.append(Path(dirpath) / name)
            elif re.match(r"vite\.config\.(?:[cm]?[jt]s)$", name):
                vite.append(Path(dirpath) / name)

    def rel_to_root(abs_dir: Path, target: str) -> str | None:
        try:
            joined = (abs_dir / target.replace("\\", "/").lstrip("/")).resolve()
            out = joined.relative_to(root).as_posix()
        except (ValueError, OSError):
            return None
        if target.rstrip("/").endswith("*") and not out.endswith("*"):
            out = out + "/*"
        return out

    def scope_for(cfg_dir: Path) -> str:
        rel = cfg_dir.resolve().relative_to(root).as_posix() if cfg_dir.resolve() != root else ""
        return ".".join(p for p in rel.split("/") if p and p != ".")

    aliases: list[tuple[str, list[str], str]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    # a config another one `extends` (tsconfig.base.json) is not a project by
    # itself: its paths reach only the configs that extend it
    extended: set[Path] = set()
    for cfg_path in configs:
        try:
            data = _parse_jsonc(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        ext = data.get("extends") if isinstance(data, dict) else None
        for item in ext if isinstance(ext, list) else [ext]:
            if not item or str(item).startswith("@"):
                continue
            target = (cfg_path.parent / str(item)).resolve()
            if not target.exists() and not str(item).endswith(".json"):
                target = (cfg_path.parent / (str(item) + ".json")).resolve()
            extended.add(target)
    for cfg_path in sorted(configs):
        if cfg_path.resolve() in extended:
            continue
        data = _load_tsconfig(cfg_path)
        opts = data.get("compilerOptions") if isinstance(data, dict) else None
        if not isinstance(opts, dict):
            continue
        paths = opts.get("paths") or {}
        if not isinstance(paths, dict):
            continue
        base_dir = Path(opts.get("_baseDir") or opts.get("_pathsDir") or cfg_path.parent)
        scope = scope_for(cfg_path.parent)
        for pattern, targets in paths.items():
            if isinstance(targets, str):
                target_list = [targets]
            elif isinstance(targets, list):
                target_list = [str(t) for t in targets if t]
            else:
                continue
            joined = [j for t in target_list for j in [rel_to_root(base_dir, t)] if j]
            key = (str(pattern), tuple(joined), scope)
            if not joined or key in seen:
                continue
            seen.add(key)
            aliases.append((str(pattern), joined, scope))
    for cfg_path in sorted(vite):
        try:
            raw = cfg_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scope = scope_for(cfg_path.parent)
        # '@': path.resolve(__dirname, './src')  /  '@': fileURLToPath(new URL('./src', import.meta.url))
        object_form = re.finditer(
            r"""['"]?([@~#][\w\-/]*)['"]?\s*:\s*(?:path\.)?(?:resolve|join|fileURLToPath)\s*\((?:[^'"()]*?,\s*)?(?:new\s+URL\(\s*)?['"]([^'"]+)['"]""",
            raw,
        )
        # alias: [{ find: '@', replacement: path.resolve(__dirname, 'src') }]
        array_form = re.finditer(
            r"""find\s*:\s*['"]([^'"]+)['"]\s*,\s*replacement\s*:\s*(?:(?:path\.)?(?:resolve|join|fileURLToPath)\s*\((?:[^'"()]*?,\s*)?(?:new\s+URL\(\s*)?)?['"]([^'"]+)['"]""",
            raw,
        )
        # '@': '/src' or './src' (a leading '/' is the project root in Vite)
        plain_form = re.finditer(r"""['"]([@~#][\w\-/]*)['"]\s*:\s*['"]([./][^'"]*)['"]""", raw)
        # { find: /^@\//, replacement: ... }: a simple prefix regex
        regex_form = re.finditer(
            r"""find\s*:\s*/\^?((?:\\.|[^/\\])+?)/[a-z]*\s*,\s*replacement\s*:\s*(?:(?:path\.)?(?:resolve|join|fileURLToPath)\s*\((?:[^'"()]*?,\s*)?(?:new\s+URL\(\s*)?)?['"]([^'"]+)['"]""",
            raw,
        )
        found: list[tuple[str, str]] = [(m.group(1), m.group(2)) for m in [*object_form, *array_form]]
        found += [(m.group(1), m.group(2)) for m in plain_form]
        for m in regex_form:
            prefix = re.sub(r"\\(.)", r"\1", m.group(1))
            if re.fullmatch(r"[@~#][\w\-]*/?", prefix):
                found.append((prefix.rstrip("/"), m.group(2)))
        for name, target in found:
            if target.startswith("/"):
                target = "." + target
            target_rel = rel_to_root(cfg_path.parent, target)
            if not target_rel:
                continue
            for pattern, tgt in ((name, target_rel), (name.rstrip("/") + "/*", target_rel.rstrip("/") + "/*")):
                key = (pattern, (tgt,), scope)
                if key not in seen:
                    seen.add(key)
                    aliases.append((pattern, [tgt], scope))
    return aliases


def _unwrap_fallback(node: Node) -> Node:
    """`X ?? ''` / `X || 'http://localhost'` / `(X as string)` -> `X`: the value
    a factory gets when the env var is set."""
    while True:
        if node.type in {"parenthesized_expression", "as_expression", "non_null_expression"} and node.named_children:
            node = node.named_children[0]
            continue
        if node.type == "binary_expression":
            op = node.child_by_field_name("operator")
            left = node.child_by_field_name("left")
            if op is not None and text(op) in {"??", "||"} and left is not None:
                node = left
                continue
        return node


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
        val, _u = _resolve_js_string(_unwrap_fallback(args[i]), consts)
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
    cur = parent_of(node) if node is not None else None
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
        cur = parent_of(cur)
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
_NAV_VAR_WORDS = ("redirect", "callback", "returnto", "returnurl", "nexturl", "successurl", "cancelurl")

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


# node key -> top of its `+` chain; filled while climbing so sibling literals
# of a long chain reuse it (in tree-sitter `parent_of(node)` costs a walk from the
# root, so climbing from every literal of an n-term chain was ~O(n^3)).
_LITERAL_TOP: dict[tuple[int, int, int], Node] = {}


def _node_key(node: Node) -> tuple[int, int, int]:
    return (node.id, node.start_byte, node.end_byte)


def _literal_top(node: Node) -> Node:
    path: list[Node] = []
    cur = node
    while True:
        hit = _LITERAL_TOP.get(_node_key(cur))
        if hit is not None:
            top = hit
            break
        parent = parent_of(cur)
        if parent is not None and (
            (parent.type == "binary_expression" and _is_plus(parent))
            or parent.type == "parenthesized_expression"
        ):
            path.append(cur)
            cur = parent
            continue
        top = cur
        break
    for visited in path:
        _LITERAL_TOP[_node_key(visited)] = top
    return top


def _is_route_registration(call: Node, routers: dict[str, str]) -> bool:
    recv = _receiver_ident(call)
    assigned = _assigned_name(call)
    return bool(
        recv in routers
        or _is_router_factory(call)
        or (assigned and assigned in routers)
    )


def _literal_is_excluded(top: Node, routers: dict[str, str]) -> bool:
    parent = parent_of(top)
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
    if ptype == "jsx_expression" and parent_of(parent) is not None and parent_of(parent).type == "jsx_attribute":
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
    if ptype in {"variable_declarator", "assignment_expression"}:
        # `const redirectUri = `${APP_URL}/oauth/callback``: where the browser
        # comes back to, not a request
        target = parent.child_by_field_name("name") or parent.child_by_field_name("left")
        tname = text(target).lower().replace("_", "") if target is not None else ""
        if any(word in tname for word in _NAV_VAR_WORDS):
            return True
    if ptype == "arguments" and parent_of(parent) is not None and parent_of(parent).type in {
        "call_expression",
        "new_expression",
    }:
        call = parent_of(parent)
        name = call_callee_name(call).lower()
        if name in _LITERAL_SKIP_CALLS:
            return True
        if name in ROUTE_HTTP_METHODS and _is_route_registration(call, routers):
            return True
        if call.type == "new_expression" and name in {"regexp", "urlpattern", "url"}:
            return name != "url"
    return False


def _literal_method_hint(top: Node, consts: dict[str, str]) -> str | None:
    parent = parent_of(top)
    if parent is None:
        return None
    if parent.type == "arguments" and parent_of(parent) is not None and parent_of(parent).type == "call_expression":
        call = parent_of(parent)
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
            obj = parent_of(parent)
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


def _concrete_base(value: str | None) -> str | None:
    """An env placeholder (`{VITE_ALT_URL}`) or absolute URL; else None."""
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    lead = _HOLE.match(value)
    if lead is not None and _ENV_LIKE.match(lead.group(1)) and "{" not in value[lead.end():]:
        return value
    return None


def _object_member_owner(top: Node) -> str | None:
    """`paths.summary` when ``top`` is returned by an arrow/method stored as a
    property of a top-level `const paths = { ... }` (nested objects allowed)."""
    keys: list[str] = []
    cur = parent_of(top)
    while cur is not None:
        if cur.type == "pair":
            key = cur.child_by_field_name("key")
            if key is not None:
                keys.append(_unquote(text(key)))
        elif cur.type == "method_definition":
            name = cur.child_by_field_name("name")
            if name is not None:
                keys.append(text(name))
        elif cur.type == "variable_declarator":
            name = _declarator_name(cur)
            return ".".join([name, *reversed(keys)]) if name and keys else None
        elif cur.type in {"function_declaration", "class_declaration", "program"}:
            return None
        cur = parent_of(cur)
    return None


def _module_level_literal_consts(tree: Node) -> set[str]:
    """Names of top-level `const X = '<string/template/concat>'` declarations."""
    out: set[str] = set()
    for stmt in tree.named_children:
        decl = stmt
        if decl.type == "export_statement":
            decl = next((c for c in decl.named_children if c.type == "lexical_declaration"), decl)
        if decl.type not in {"lexical_declaration", "variable_declaration"}:
            continue
        for d in decl.named_children:
            if d.type != "variable_declarator":
                continue
            value = d.child_by_field_name("value")
            if value is not None and value.type in {"string", "template_string", "binary_expression"}:
                name = _declarator_name(d)
                if name:
                    out.add(name)
    return out


def _module_const_of(top: Node) -> str | None:
    """`X` when ``top`` is the value of a top-level `const X = ...`."""
    parent = parent_of(top)
    if parent is None or parent.type != "variable_declarator":
        return None
    decl = parent_of(parent)
    if decl is None or decl.type not in {"lexical_declaration", "variable_declaration"}:
        return None
    holder = parent_of(decl)
    if holder is not None and holder.type == "export_statement":
        holder = parent_of(holder)
    if holder is None or holder.type != "program":
        return None
    return _declarator_name(parent) or None


def _is_helper_return(top: Node) -> bool:
    """True when ``top`` is the whole returned value of a function whose only
    job is to return it (`(id) => `/sites/${id}``, `function p() { return '/x' }`)."""
    cur = parent_of(top)
    while cur is not None and cur.type in {"parenthesized_expression", "return_statement", "statement_block"}:
        cur = parent_of(cur)
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


def _call_target_qual(call: Node, module: str, imports: dict[str, str], functions: dict) -> str | None:
    func = call.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "identifier":
        name = text(func)
        local = f"{module}.{name}"
        if local in functions:
            return local
        return imports.get(name)
    if func.type == "member_expression":
        obj = func.child_by_field_name("object")
        prop = func.child_by_field_name("property")
        if obj is not None and prop is not None and obj.type == "identifier":
            target = imports.get(text(obj))
            if target and target.endswith(".*"):
                return f"{target[:-2]}.{text(prop)}"
            if target:
                # `import { paths } from './paths'` → paths.summary(id)
                return f"{target}.{text(prop)}"
            # local object helper: `const paths = { site: (id) => ... }`
            return f"{module}.{text(obj)}.{text(prop)}"
    return None


def _collect_route_literals(
    analysis: JsAnalysis,
    tree: Node,
    rel_file: str,
    module: str,
    consts: dict[str, str],
    routers: dict[str, str],
    imports: dict[str, str] | None = None,
) -> None:
    seen_tops: set[tuple[int, int]] = set()
    seen: set[tuple[str, str, str | None]] = set()
    imports = imports or {}
    module_consts = _module_level_literal_consts(tree)
    for node in walk(tree):
        if node.type == "call_expression":
            target = _call_target_qual(node, module, imports, analysis.functions)
            if target:
                top = _literal_top(node)
                analysis.helper_calls.append(
                    JsHelperCall(
                        caller=_enclosing_symbol(node, module),
                        callee=target,
                        file=rel_file,
                        line=line_of(node),
                        method=_literal_method_hint(top, consts),
                        args=[_resolve_js_string(a, consts)[0] for a in call_args(node)],
                        direct=top is not None and parent_of(top) is not None and parent_of(top).type == "arguments",
                    )
                )
        elif node.type == "identifier" and parent_of(node) is not None and parent_of(node).type != "variable_declarator":
            name = text(node)
            target = f"{module}.{name}" if name in module_consts else imports.get(name)
            if not target:
                continue
            caller = _enclosing_symbol(node, module)
            if caller.endswith(".module"):
                continue
            top = _literal_top(node)
            analysis.helper_calls.append(
                JsHelperCall(
                    caller=caller,
                    callee=target,
                    file=rel_file,
                    line=line_of(node),
                    method=_literal_method_hint(top, consts),
                    direct=top is node and parent_of(node).type == "arguments",
                    kind="ref",
                )
            )
    for node in walk(tree):
        if node.type in {"string", "template_string"}:
            pass
        elif (
            node.type == "identifier"
            and parent_of(node) is not None
            and parent_of(node).type not in {"variable_declarator", "import_specifier", "export_specifier"}
            and _looks_like_http_path(consts.get(text(node)))
            and not _enclosing_symbol(node, module).endswith(".module")
        ):
            # `get(window.base + REPORT)`: a path const used inside a function
            # starts a literal there (the expression may hold no string node)
            pass
        elif node.type == "call_expression" and _apply_path_helper(node, consts) is not None:
            # `api.get(site(id))`: the helper call is the path at this call
            # site — only when it is the whole argument. Inside a larger URL
            # (`${base(id)}/photos`) the composed URL is the literal instead.
            top = _literal_top(node)
            if top is not node or parent_of(node) is None or parent_of(node).type != "arguments":
                continue
        else:
            continue
        top = _literal_top(node)
        key = (top.start_byte, top.end_byte)
        if key in seen_tops:
            continue
        seen_tops.add(key)
        if _literal_is_excluded(top, routers):
            continue
        # A function that only returns a path never sends a request, and a
        # module-level const is not a request either: scan moves these literals
        # to their direct users (never silently dropped).
        is_helper = _is_helper_return(top)
        const_name = _module_const_of(top)
        value, _unresolved = _resolve_js_string(top, consts)
        path = _clean_route_literal(value)
        if path is None:
            continue
        method = _literal_method_hint(top, consts)
        # Bare `fetch('/x')` is relative to the page origin: same repo only.
        same_repo = bool(
            value
            and value.startswith("/")
            and parent_of(top) is not None
            and parent_of(top).type == "arguments"
            and parent_of(parent_of(top)) is not None
            and call_callee_name(parent_of(parent_of(top))).lower() == "fetch"
        )
        symbol = _enclosing_symbol(top, module)
        # `request({ method, url: `/x/${id}`, baseURL: ALT })`: a base in the
        # same config object is this request's base, whatever wrapper sends it
        config_base: str | None = None
        if parent_of(top) is not None and parent_of(top).type == "pair":
            pkey = parent_of(top).child_by_field_name("key")
            if pkey is not None and _unquote(text(pkey)) in {"url", "path", "endpoint"}:
                bnode = _object_prop_node(parent_of(parent_of(top)), _PER_CALL_BASE_KEYS)
                if bnode is not None:
                    bval, _bu = _resolve_js_string(bnode, consts)
                    config_base = bval or "{baseURL}"
        member = False
        if const_name and symbol.endswith(".module"):
            symbol = f"{module}.{const_name}"
            is_helper = True
        elif is_helper:
            owner = _object_member_owner(top)
            # the member may already be a function of its own (`X.m`, a
            # method of a module-level object): it is still a member helper
            if owner and (symbol.endswith(".module") or symbol == f"{module}.{owner}"):
                # `export const paths = { summary: (id) => `/items/${id}/...` }`
                symbol = f"{module}.{owner}"
                member = True
        # The base context is part of the key: two members of one object can
        # share a path but not a base (`{ url, baseURL: ALT }` vs plain `{ url }`).
        dedupe = (symbol, path, method, config_base)
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
                helper=is_helper,
                explicit_base=bool(config_base)
                or (bool(value) and (value.startswith("{") or "://" in value)),
                base_hole=(_HOLE.match(value).group(1) if value and _HOLE.match(value) else None),
                member_helper=member,
                bound_base=_concrete_base(config_base),
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
    if body.type in {
        "template_string",
        "string",
        "binary_expression",
        "parenthesized_expression",
        "call_expression",
    }:
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
    for node in typed_walk(tree, frozenset({"variable_declarator", "function_declaration"}), JS_TYPES):
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
        if not value or any(ch.isspace() for ch in value):
            continue
        lead = _HOLE.match(value)
        # `seg = (s) => encodeURIComponent(s)`: a transparent segment helper
        transparent = bool(lead) and lead.end() == len(value) and lead.group(1) in params
        # a leading base placeholder (`{VITE_API_URL}`, `{base}`) is allowed
        base_hole = lead.group(1) if lead and "/" in value[lead.end():] else None
        if not (
            transparent
            or value.startswith("/")
            or base_hole
            or value.startswith(("http://", "https://"))
        ):
            continue
        # Pure: the only holes are the helper's own parameters (plus a leading
        # env/base placeholder such as `{VITE_API_URL}`).
        holes = set(_HOLE.findall(value)) - ({base_hole} if base_hole else set())
        if not holes <= set(params):
            continue
        out[f"{name}()"] = value
        out[f"{name}()#params"] = ",".join(params)
    return out


_REEXPORTS: dict[str, tuple[dict[str, tuple[str, str]], list[str]]] = {}
# modules of every parsed JS/TS file, for path aliases with several targets
_KNOWN_MODULES: set[str] = set()


def _collect_reexports(
    tree: Node, module: str, path_aliases: list | None
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """`export { a as b } from './x'` → {b: (x, a)}; `export * from './y'` → [y]."""
    named: dict[str, tuple[str, str]] = {}
    stars: list[str] = []
    for node in tree.named_children:
        if node.type != "export_statement":
            continue
        raw = text(node)
        m = re.search(r"""from\s+['"]([^'"]+)['"]""", raw)
        if not m:
            continue
        src = _spec_to_module(m.group(1), module, path_aliases)
        if re.match(r"export\s+\*\s+from", raw):
            stars.append(src)
            continue
        block = re.search(r"export\s*(?:type\s*)?\{([^}]+)\}", raw)
        for part in (block.group(1).split(",") if block else []):
            part = part.strip().replace("type ", "")
            if not part:
                continue
            if " as " in part:
                orig, alias = (x.strip() for x in re.split(r"\s+as\s+", part, maxsplit=1))
            else:
                orig = alias = part
            named[alias] = (src, orig)
    return named, stars


def _helper_source(
    consts_by_mod: dict[str, dict[str, str]],
    mod: str,
    name: str,
    depth: int = 0,
) -> tuple[dict[str, str], str] | None:
    """Module consts that define helper `name` (directly or as object prefix),
    following barrels (`index` files, `export … from`)."""
    if depth > 5:
        return None
    for candidate in (mod, f"{mod}.index"):
        src = consts_by_mod.get(candidate)
        if src is None:
            continue
        if f"{name}()" in src or any(k.startswith(f"{name}.") for k in src):
            return src, name
        named, stars = _REEXPORTS.get(candidate, ({}, []))
        if name in named:
            nmod, orig = named[name]
            hit = _helper_source(consts_by_mod, nmod, orig, depth + 1)
            if hit:
                return hit
        for star in stars:
            hit = _helper_source(consts_by_mod, star, name, depth + 1)
            if hit:
                return hit
    return None


def _import_plain_consts(
    consts_by_mod: dict[str, dict[str, str]],
    import_specs_by_mod: dict[str, dict[str, tuple[str, str]]],
) -> None:
    """`import { API_URL, config } from '@/config'` → the values (env
    placeholders, URLs, paths, object props) are known in the importer."""

    def usable(v: str) -> bool:
        return v.startswith(("{", "/", "http://", "https://"))

    for module, specs in import_specs_by_mod.items():
        consts = consts_by_mod.setdefault(module, {})
        for local, (src_mod, orig) in specs.items():
            if orig == "*":
                continue
            src = consts_by_mod.get(src_mod) or consts_by_mod.get(f"{src_mod}.index") or {}
            val = src.get(orig)
            if val is not None and usable(val) and local not in consts:
                consts[local] = val
            if f"{orig}.*env" in src:
                consts.setdefault(f"{local}.*env", "1")
            for key, kval in src.items():
                if key.startswith(f"{orig}.") and not key.endswith(("()", "()#params", ".*env")) and usable(kval):
                    consts.setdefault(f"{local}.{key[len(orig) + 1:]}", kval)


def _import_path_helpers(
    consts_by_mod: dict[str, dict[str, str]],
    import_specs_by_mod: dict[str, dict[str, tuple[str, str]]],
) -> None:
    _import_plain_consts(consts_by_mod, import_specs_by_mod)
    for module, specs in import_specs_by_mod.items():
        consts = consts_by_mod.setdefault(module, {})
        for local, (src_mod, orig) in specs.items():
            if orig != "*":
                found = _helper_source(consts_by_mod, src_mod, orig)
                if found:
                    src_consts, real = found
                    if f"{real}()" in src_consts:
                        # overwrite: a later round may have completed the value
                        consts[f"{local}()"] = src_consts[f"{real}()"]
                        consts[f"{local}()#params"] = src_consts.get(f"{real}()#params", "")
                    for skey, sval in src_consts.items():
                        if skey.startswith(f"{real}.") and (skey.endswith("()") or skey.endswith("()#params")):
                            consts[f"{local}.{skey[len(real) + 1:]}"] = sval
                    continue
            src = consts_by_mod.get(src_mod) or consts_by_mod.get(f"{src_mod}.index") or {}
            if orig == "*":
                # `import * as P from './paths'` → P.site(), P.routes.team()
                for skey, sval in src.items():
                    if skey.endswith("()") or skey.endswith("()#params"):
                        consts[f"{local}.{skey}"] = sval
                continue
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
        if obj is None or prop is None or obj.type not in {"identifier", "member_expression"}:
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



def _bind_param_bases(analysis: JsAnalysis, fn_nodes: dict[str, Node]) -> None:
    """`getX(baseUrl, id) → fetch(`${baseUrl}/v1/x/${id}`)`: when every call site
    passes the same concrete base (env var / absolute URL), compose it into the
    traced URL so the normal matcher (and `services:`) can resolve it."""
    sites: dict[str, list[JsHelperCall]] = {}
    for hc in analysis.helper_calls:
        if hc.kind == "call":
            sites.setdefault(hc.callee, []).append(hc)
    for call in analysis.http_calls:
        m = _HOLE.match(call.url or "")
        if not m:
            continue
        params = _fn_param_names(fn_nodes.get(call.symbol))
        hole = m.group(1)
        if hole not in params:
            continue
        idx = params.index(hole)
        callers = sites.get(call.symbol) or []
        if not callers:
            continue
        values = {hc.args[idx] if idx < len(hc.args) else None for hc in callers}
        if len(values) != 1:
            continue
        value = next(iter(values))
        if not value:
            continue
        lead = _HOLE.match(value)
        concrete = value.startswith(("http://", "https://")) or (
            lead is not None and lead.end() == len(value) and _ENV_LIKE.match(lead.group(1))
        )
        if not concrete:
            continue
        call.url = value.rstrip("/") + call.url[m.end():]
    for lit in analysis.route_literals:
        if not lit.base_hole:
            continue
        bound = _common_concrete_arg(sites.get(lit.symbol) or [], fn_nodes.get(lit.symbol), lit.base_hole)
        if bound:
            lit.bound_base = bound


def _common_concrete_arg(callers: list, fn_node: Node | None, param: str) -> str | None:
    params = _fn_param_names(fn_node)
    if param not in params or not callers:
        return None
    idx = params.index(param)
    values = {hc.args[idx] if idx < len(hc.args) else None for hc in callers}
    if len(values) != 1:
        return None
    value = next(iter(values))
    if not value:
        return None
    lead = _HOLE.match(value)
    if value.startswith(("http://", "https://")) or (
        lead is not None and lead.end() == len(value) and _ENV_LIKE.match(lead.group(1))
    ):
        return value
    return None
