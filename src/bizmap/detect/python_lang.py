from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspy
from tree_sitter import Language, Node, Parser

from bizmap.detect.py_resolve import (
    ImportMap,
    ResolvedString,
    call_kwargs,
    call_posargs,
    call_qualname,
    collect_local_env,
    collect_module_assignments,
    collect_module_dicts,
    find_dict_string,
    imported_module_name,
    join_route_path,
    lookup_dict,
    lookup_dict_from_node,
    parse_imports,
    resolve_expr,
    resolve_module_consts,
    simple_return_value,
    unquote,
)
from bizmap.detect.py_schema import (
    ModelInfo,
    apply_python_schema,
    is_schema_rel,
)
from bizmap.detect.rules import Rule, import_matches, method_matches
from bizmap.detect.tsutil import (
    call_args,
    call_callee_name,
    clear_parent_index,
    index_deep_tree,
    line_of,
    parent_of,
    text,
    walk,
)
from bizmap.files import rel
from bizmap.graph import Confidence, Evidence
from bizmap.schema_replay import Schema
from bizmap.sql_analyze import looks_like_sql

_PY_LANGUAGE = Language(tspy.language())

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
HTTP_ROUTE_DECO = HTTP_METHODS | {"route", "api_route"}
APP_CTORS = {"FastAPI", "Flask", "APIRouter", "Blueprint", "Quart", "Sanic", "Starlette"}
APP_CTOR_ROUTERS = {"FastAPI", "APIRouter", "Quart", "Sanic", "Starlette"}
APP_CTOR_FLASK = {"Flask", "Blueprint"}
FIRESTORE_DOC_EVENTS = {
    "on_document_created": "created",
    "on_document_updated": "updated",
    "on_document_deleted": "deleted",
    "on_document_written": "written",
}
FIRESTORE_READ = {"get", "stream", "list_documents"}
FIRESTORE_WRITE = {"set", "update", "delete", "add"}
SA_QUERY = {"select", "insert", "update", "delete"}
HTTP_CLIENT_LIBS = ("httpx", "requests", "aiohttp", "urllib")
HTTP_CLIENT_CTORS = {
    "Client",
    "AsyncClient",
    "Session",
    "AsyncSession",
    "ClientSession",
}
HTTP_CLIENT_FUNCS = HTTP_METHODS | {"request", "urlopen", "urlretrieve"}
HTTP_WRAPPER_METHODS = {"_get", "_post", "_put", "_patch", "_delete", "_request"}
HTTP_BASE_FIELDS = {"base_url", "baseurl", "base", "host", "origin"}
HTTP_PATH_PARAM_NAMES = {"path", "url", "endpoint", "route", "urlpath"}
HTTP_HELPER_RECV = ("_transport", "_session", "_client", "session", "client", "transport")
CELERY_DECORATORS = {"task", "shared_task"}


def _parser() -> Parser:
    return Parser(_PY_LANGUAGE)


def _declared_source_roots(files: list[Path], root: Path) -> list[Path]:
    """Source roots declared next to a pyproject.toml / setup.cfg / setup.py:
    `src/`, `[tool.setuptools.packages.find] where`, Poetry `packages[].from`."""
    import tomllib

    root = root.resolve()
    seen: set[Path] = set()
    out: set[Path] = set()
    for path in files:
        d = path.resolve().parent
        while True:
            if d in seen:
                break
            seen.add(d)
            if any((d / name).is_file() for name in ("pyproject.toml", "setup.cfg", "setup.py")):
                if (d / "src").is_dir():
                    out.add(d / "src")
                pyproject = d / "pyproject.toml"
                if pyproject.is_file():
                    try:
                        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        data = {}
                    find = (((data.get("tool") or {}).get("setuptools") or {}).get("packages") or {})
                    if isinstance(find, dict):
                        where = (find.get("find") or {}).get("where") or []
                        for w in [where] if isinstance(where, str) else where:
                            out.add((d / str(w)).resolve())
                    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("packages") or []
                    for pkg in poetry if isinstance(poetry, list) else []:
                        if isinstance(pkg, dict) and pkg.get("from"):
                            out.add((d / str(pkg["from"])).resolve())
            if d == root or root not in d.parents:
                break
            d = d.parent
    return sorted((p for p in out if p.is_dir()), key=lambda p: len(p.parts), reverse=True)


def _python_module_names(files: list[Path], root: Path) -> dict[Path, str]:
    """Importable module name per file.

    1. Under a source root — declared (see `_declared_source_roots`) or any
       ancestor directory named `src` — relative to that root, with or without
       `__init__.py` (PEP 420 namespace packages).
    2. Inside a package (`__init__.py` chain): dotted path from the parent of
       the highest package directory.
    3. Otherwise: relative to the scan root, as before.
    Names that would collide fall back to (3) for every file involved.
    """
    root = root.resolve()
    src_roots = _declared_source_roots(files, root)
    names: dict[Path, str] = {}
    for path in files:
        full = path.resolve()
        fallback = _module_name(rel(path, root))
        # 1. a source root wins over a partial `__init__.py` chain (PEP 420
        #    namespace packages: `src/pipeline/` without `__init__.py`,
        #    `src/pipeline/render/` with one → `pipeline.render`)
        hit = next((sr for sr in src_roots if sr in full.parents), None)
        if hit is None:
            hit = next(
                (
                    d
                    for d in full.parents
                    if d.name == "src" and (d == root or root in d.parents)
                ),
                None,
            )
        if hit is not None:
            names[path] = _module_name(full.relative_to(hit).as_posix())
            continue
        # 2. regular packages: from the parent of the highest package dir
        top: Path | None = None
        d = full.parent
        while (d / "__init__.py").is_file() and (d == root or root in d.parents):
            top = d
            if d == root:
                break
            d = d.parent
        if top is not None and top != root:
            names[path] = _module_name(full.relative_to(top.parent).as_posix())
            continue
        names[path] = fallback
    counts: dict[str, int] = {}
    for name in names.values():
        counts[name] = counts.get(name, 0) + 1
    for path, name in list(names.items()):
        if counts[name] > 1:
            names[path] = _module_name(rel(path, root))
    return names


def _module_name(rel_file: str) -> str:
    path = rel_file[:-3] if rel_file.endswith(".py") else rel_file
    if path.endswith("/__init__"):
        path = path[: -len("/__init__")]
    return path.replace("/", ".").replace("\\", ".")


def _docstring(fn_node: Node) -> str | None:
    body = fn_node.child_by_field_name("body")
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children:
        inner = first.named_children[0]
        if inner.type == "string":
            return unquote(text(inner)).strip() or None
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


def _enclosing_class(node: Node) -> str | None:
    cur = node.parent
    while cur is not None:
        if cur.type == "class_definition":
            return text(cur.child_by_field_name("name")) or None
        cur = cur.parent
    return None


def _enclosing_function_names(node: Node) -> str | None:
    """`outer.mid` for a function nested in functions (up to the class/module)."""
    names: list[str] = []
    cur = node.parent
    while cur is not None and cur.type != "class_definition":
        if cur.type == "function_definition":
            ident = cur.child_by_field_name("name")
            if ident is not None:
                names.append(text(ident))
        cur = cur.parent
    return ".".join(reversed(names)) or None


# node key -> enclosing function_definition (or None); filled for every node
# on the climb, so the many strings of one function share it (tree-sitter's
# `node.parent` walks from the root each time).
_ENCLOSING_FN: dict[tuple[int, int, int], Node | None] = {}
_MISSING = object()


def _enclosing_fn_node(node: Node) -> Node | None:
    """Nearest function_definition above ``node``. The cache value of a node
    is the answer for anything whose climb passes through it (a definition
    maps to itself)."""
    path: list[tuple[int, int, int]] = []
    cur = parent_of(node)
    found: Node | None = None
    while cur is not None:
        key = (cur.id, cur.start_byte, cur.end_byte)
        hit = _ENCLOSING_FN.get(key, _MISSING)
        if hit is not _MISSING:
            found = hit  # type: ignore[assignment]
            break
        path.append(key)
        if cur.type == "function_definition":
            found = cur
            break
        cur = parent_of(cur)
    for key in path:
        _ENCLOSING_FN[key] = found
    return found


def _decorator_target(dec: Node) -> Node | None:
    if not dec.named_children:
        return None
    return dec.named_children[0]


def _list_string_items(node: Node | None) -> list[str]:
    if node is None:
        return []
    if node.type == "string":
        return [unquote(text(node))]
    out: list[str] = []
    for child in node.named_children:
        if child.type == "string":
            out.append(unquote(text(child)))
        elif child.type == "list":
            out.extend(_list_string_items(child))
    return out


def _http_receiver_ok(
    receiver: str,
    attr: Node | None,
    routers: dict[str, str],
    blueprints: dict[str, str],
    module_names: set[str],
) -> bool:
    """Accept a known app/router, or `@name.get("/p")` on a module-level variable."""
    if receiver in routers or receiver in blueprints:
        return True
    if not receiver or not receiver.isidentifier():
        return False
    if attr is None or attr.type != "attribute" or not attr.named_children:
        return False
    obj = attr.named_children[0]
    # Never a call chain such as patch("x.y") / create_app().get(...)
    if obj.type != "identifier":
        return False
    return receiver in module_names


def _module_level_names(tree: Node, imap: ImportMap) -> set[str]:
    names = set(imap.from_names) | set(imap.modules)
    for child in tree.named_children:
        stmt = child
        if child.type == "expression_statement" and child.named_children:
            stmt = child.named_children[0]
        if stmt.type == "assignment":
            left = stmt.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                names.add(text(left))
        if child.type == "function_definition":
            name_n = child.child_by_field_name("name")
            if name_n is not None:
                names.add(text(name_n))
        if child.type == "decorated_definition":
            fn_node = next((c for c in child.named_children if c.type == "function_definition"), None)
            if fn_node is not None:
                name_n = fn_node.child_by_field_name("name")
                if name_n is not None:
                    names.add(text(name_n))
    return names


def _lookup_app_factory(
    callee: str, imap: ImportMap, factories: dict[str, tuple[str, str]]
) -> tuple[str, str] | None:
    if callee in factories:
        return factories[callee]
    if callee in imap.from_names:
        mod, orig = imap.from_names[callee]
        found = factories.get(f"{mod}.{orig}") or factories.get(orig)
        if found:
            return found
        for key, spec in factories.items():
            if key.endswith("." + orig) or key == orig:
                return spec
    return None


def _merge_imported_apps(
    imap: ImportMap,
    routers: dict[str, str],
    blueprints: dict[str, str],
    routers_by_mod: dict[str, dict[str, str]],
    blueprints_by_mod: dict[str, dict[str, str]],
) -> None:
    for local, (mod, orig) in imap.from_names.items():
        for m, rmap in routers_by_mod.items():
            if m == mod or m.endswith("." + mod) or mod.endswith("." + m):
                if orig in rmap:
                    routers.setdefault(local, rmap[orig])
        for m, bmap in blueprints_by_mod.items():
            if m == mod or m.endswith("." + mod) or mod.endswith("." + m):
                if orig in bmap:
                    blueprints.setdefault(local, bmap[orig])


def _ctor_prefix(call: Node) -> tuple[str, str] | None:
    callee = call_callee_name(call)
    if callee not in APP_CTORS:
        return None
    kw = call_kwargs(call)
    if callee == "APIRouter":
        prefix = ""
        if "prefix" in kw:
            node = kw["prefix"]
            if node.type == "string":
                prefix = unquote(text(node))
        return ("router", prefix)
    if callee == "Blueprint":
        prefix = ""
        if "url_prefix" in kw and kw["url_prefix"].type == "string":
            prefix = unquote(text(kw["url_prefix"]))
        return ("blueprint", prefix)
    if callee in APP_CTOR_FLASK:
        return ("blueprint" if callee == "Blueprint" else "router", "")
    return ("router", "")


def _collect_app_factories(parsed: list) -> dict[str, tuple[str, str]]:
    """Functions that return FastAPI()/Flask()/APIRouter()/Blueprint().

    Nested helpers (``create_service() -> new_app(**kw) -> FastAPI(**kw)``)
    are followed across modules up to depth 5.
    """
    factories: dict[str, tuple[str, str]] = {}
    for _ in range(5):
        changed = False
        for _path, _rf, _src, _tree, module, imap, fns in parsed:
            for fn in fns:
                spec = _function_app_factory(fn.node, factories, imap)
                if spec is None:
                    continue
                for key in (fn.name, fn.qualname, f"{module}.{fn.name}"):
                    if factories.get(key) != spec:
                        factories[key] = spec
                        changed = True
        if not changed:
            break
    return factories


def _function_app_factory(
    fn_node: Node,
    factories: dict[str, tuple[str, str]] | None = None,
    imap: ImportMap | None = None,
) -> tuple[str, str] | None:
    factories = factories or {}
    imap = imap or ImportMap()
    local: dict[str, tuple[str, str]] = {}
    found: tuple[str, str] | None = None

    def spec_of(call: Node) -> tuple[str, str] | None:
        spec = _ctor_prefix(call)
        if spec is not None:
            return spec
        return _lookup_app_factory(call_callee_name(call), imap, factories)

    for node in walk(fn_node):
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is None or right is None or left.type != "identifier":
                continue
            if right.type == "call":
                spec = spec_of(right)
                if spec is not None:
                    local[text(left)] = spec
        if node.type != "return_statement":
            continue
        expr = node.named_children[0] if node.named_children else None
        if expr is None:
            continue
        if expr.type == "call":
            spec = spec_of(expr)
            if spec is not None:
                found = spec
        elif expr.type == "identifier" and text(expr) in local:
            found = local[text(expr)]
    return found


def _inside_firestore_write(node: Node) -> bool:
    cur = parent_of(node)
    depth = 0
    while cur is not None and depth < 24:
        if cur.type == "call" and call_callee_name(cur) in FIRESTORE_WRITE:
            return True
        cur = parent_of(cur)
        depth += 1
    return False


def _normalize_http_base(url: str) -> str:
    """Rewrite constructor/config origin fields to ``{base}`` + path."""
    if not url.startswith("{") or "}" not in url:
        return url
    inner, rest = url[1:].split("}", 1)
    leaf = inner.split(".")[-1]
    if leaf.lower() in HTTP_BASE_FIELDS:
        return "{base}" + rest
    return url


def _placeholder_is_local_base(res: ResolvedString) -> bool:
    if not res.unresolved:
        return False
    inner = (res.placeholder or res.path_text or "").strip("{}")
    leaf = inner.split(".")[-1].lower()
    return leaf in HTTP_BASE_FIELDS


_NON_HTTP_RECV = {
    "payload",
    "headers",
    "params",
    "data",
    "body",
    "query",
    "form",
    "cookies",
    "mapping",
    "dict",
    "items",
    "values",
    "keys",
}


def _call_is_decorator(node: Node | None) -> bool:
    """True when ``node`` is (or sits under) a ``@decorator`` expression."""
    cur = node
    for _ in range(8):
        if cur is None:
            return False
        if cur.type == "decorator":
            return True
        if cur.type in {"function_definition", "class_definition", "module"}:
            return False
        cur = parent_of(cur)
    return False


def _looks_like_unscanned_http(
    method_l: str,
    recv: str,
    qual: str,
    pos: list[Node],
    resolve,
) -> bool:
    """True for ``x.request('POST', path)`` / ``x.post(path)`` on an unknown transport."""
    if method_l not in HTTP_CLIENT_FUNCS:
        return False
    recv_leaf = (recv or "").rsplit(".", 1)[-1].lower()
    if recv_leaf in _NON_HTTP_RECV or recv_leaf in {"self", "cls"}:
        return False
    if recv_leaf in {"router", "app", "bp", "blueprint"}:
        return False
    qlow = (qual or "").lower()
    if any(tok in qlow for tok in ("headers", "params", "cookies", "mapping", "searchparams")):
        return False
    if not recv and "." not in (qual or ""):
        return False
    if method_l == "request":
        if len(pos) >= 2:
            mres = resolve(pos[0])
            token = (mres.path_text if mres and mres.usable else "").upper()
            if token in {m.upper() for m in HTTP_METHODS}:
                return True
            path_res = resolve(pos[1])
            path = path_res.path_text if path_res and path_res.usable else ""
            return bool(path.startswith("/") or path.startswith("{") or "://" in path)
        return bool(pos)
    if not pos:
        return False
    path_res = resolve(pos[0])
    path = path_res.path_text if path_res and path_res.usable else ""
    if path.startswith("/") or "://" in path or (path.startswith("{") and "/" in path):
        return True
    if pos[0].type == "string":
        raw = unquote(text(pos[0]))
        return raw.startswith("/") or "://" in raw
    return False


def _collect_py_http_clients(tree: Node, imap: ImportMap) -> set[str]:
    """Module names, constructors, and instances of requests/httpx/aiohttp/urllib."""
    clients: set[str] = set()
    for raw in imap.raw:
        low = raw.lower()
        for lib in HTTP_CLIENT_LIBS:
            if lib in low:
                clients.add(lib)
                if lib == "urllib":
                    clients.add("urllib.request")
                    clients.add("request")
    for local, (mod, orig) in imap.from_names.items():
        if any(lib in mod for lib in HTTP_CLIENT_LIBS):
            clients.add(local)
            if orig in HTTP_CLIENT_CTORS or orig.lower() in HTTP_CLIENT_FUNCS:
                clients.add(local)
    for alias, mod in imap.modules.items():
        if any(lib in mod for lib in HTTP_CLIENT_LIBS):
            clients.add(alias)
    for node in walk(tree):
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            lname = text(left) if left is not None and left.type == "identifier" else ""
            if not lname or right is None:
                continue
            if right.type == "identifier" and text(right) in clients:
                clients.add(lname)
                continue
            if right.type != "call":
                continue
            callee = call_callee_name(right)
            qual = call_qualname(right)
            if callee in HTTP_CLIENT_CTORS or any(lib in qual for lib in HTTP_CLIENT_LIBS):
                if (
                    callee in HTTP_CLIENT_CTORS
                    or "Client" in callee
                    or "Session" in callee
                    or any(lib in qual for lib in HTTP_CLIENT_LIBS)
                ):
                    clients.add(lname)
        if node.type == "with_item":
            value = node.child_by_field_name("value")
            alias = node.child_by_field_name("alias")
            if alias is None and node.named_children:
                alias = node.named_children[-1] if len(node.named_children) > 1 else None
            alias_name = ""
            if alias is not None:
                if alias.type == "identifier":
                    alias_name = text(alias)
                elif alias.named_children:
                    ident = alias.named_children[-1]
                    if ident.type == "identifier":
                        alias_name = text(ident)
            if not alias_name or value is None:
                continue
            qual = call_qualname(value) if value.type == "call" else text(value)
            callee = call_callee_name(value) if value.type == "call" else ""
            if callee in HTTP_CLIENT_CTORS or any(lib in qual for lib in HTTP_CLIENT_LIBS):
                clients.add(alias_name)
    return clients


def _lookup_py_http_helper(
    fn: PyFunction,
    method: str,
    qual: str,
    helpers: dict[str, PyHttpHelper],
) -> PyHttpHelper | None:
    if not helpers:
        return None
    for key in (
        qual,
        f"{fn.module}.{fn.class_name}.{method}" if fn.class_name else "",
        f"{fn.module}.{method}",
        f"{fn.class_name}.{method}" if fn.class_name else "",
        method,
    ):
        if key and key in helpers:
            found = helpers[key]
            # Don't treat the helper's own body as a call of itself.
            if found.function.qualname == fn.qualname:
                continue
            return found
    return None


def _collect_py_http_helpers(analysis: PyAnalysis) -> dict[str, PyHttpHelper]:
    helpers: dict[str, PyHttpHelper] = {}
    for fn in analysis.functions.values():
        helper = _py_http_helper_of(fn)
        if helper is None:
            continue
        helpers[fn.qualname] = helper
        helpers[fn.name] = helper
        if fn.class_name:
            helpers[f"{fn.class_name}.{fn.name}"] = helper
            helpers[f"{fn.module}.{fn.class_name}.{fn.name}"] = helper
    return helpers


def _py_http_helper_of(fn: PyFunction) -> PyHttpHelper | None:
    params = _param_names(fn.node)
    path_index = next(
        (i for i, p in enumerate(params) if p.lower() in HTTP_PATH_PARAM_NAMES),
        None,
    )
    wrapper = fn.name in HTTP_WRAPPER_METHODS or fn.name.lower() in HTTP_WRAPPER_METHODS
    if path_index is None and wrapper and params:
        path_index = 0
    if path_index is None:
        return None
    path_param = params[path_index]
    uses_http = False
    default_method = "POST"
    leaf = fn.name.lower().lstrip("_")
    if leaf in HTTP_METHODS:
        default_method = leaf.upper()
    for node in walk(fn.node):
        if node.type != "call":
            continue
        method = call_callee_name(node)
        qual = call_qualname(node)
        raw = text(node)
        if path_param not in raw:
            continue
        method_l = method.lower()
        httpish = (
            method_l in HTTP_CLIENT_FUNCS
            or method_l in HTTP_WRAPPER_METHODS
            or method_l == "request"
            or any(lib in qual for lib in HTTP_CLIENT_LIBS)
            or any(token in qual for token in HTTP_HELPER_RECV)
        )
        if not httpish:
            continue
        uses_http = True
        if method_l in HTTP_METHODS:
            default_method = method_l.upper()
        elif method_l == "request":
            pos = call_posargs(node)
            if pos and pos[0].type == "string":
                token = unquote(text(pos[0])).upper()
                if token in {m.upper() for m in HTTP_METHODS}:
                    default_method = token
        break
    if not uses_http:
        return None
    return PyHttpHelper(function=fn, path_index=path_index, default_method=default_method)


def _collect_py_sql_dict_helpers(
    analysis: PyAnalysis,
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]],
    imaps: dict[str, ImportMap],
) -> dict[str, PySqlDictHelper]:
    helpers: dict[str, PySqlDictHelper] = {}
    for fn in analysis.functions.values():
        params = _param_names(fn.node)
        if not params:
            continue
        imap = imaps.get(fn.module) or ImportMap()
        local_dicts = dicts_by_module.get(fn.module) or {}
        name_index = next(
            (i for i, p in enumerate(params) if p.lower() in {"name", "key", "query", "q"}),
            0,
        )
        param = params[name_index]
        for node in walk(fn.node):
            if node.type != "subscript":
                continue
            obj = node.child_by_field_name("value") or (
                node.named_children[0] if node.named_children else None
            )
            idx = node.named_children[1] if len(node.named_children) > 1 else None
            if idx is None or text(idx) != param:
                continue
            dmap = lookup_dict_from_node(obj, imap, local_dicts, dicts_by_module)
            if not dmap:
                continue
            dict_name = ""
            dict_mod = fn.module
            if obj is not None and obj.type == "identifier":
                dict_name = text(obj)
                if dict_name in imap.from_names:
                    dict_mod, dict_name = imap.from_names[dict_name]
            elif obj is not None and obj.type == "attribute":
                dict_name = text(obj.named_children[-1]) if obj.named_children else ""
                head = obj.named_children[0] if obj.named_children else None
                if head is not None and head.type == "identifier":
                    dict_mod = imported_module_name(text(head), imap) or fn.module
            if not dict_name:
                continue
            helper = PySqlDictHelper(
                function=fn,
                name_index=name_index,
                dict_mod=dict_mod,
                dict_name=dict_name,
            )
            helpers[fn.qualname] = helper
            helpers[fn.name] = helper
            helpers[f"{fn.module}.{fn.name}"] = helper
            break
    return helpers


def _ensure_path(path: str) -> str:
    if not path:
        return "/"
    if path.startswith("{") or path.startswith("/") or "://" in path:
        return path
    return "/" + path


def _collection_from_document(path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return path
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


@dataclass
class PyFunction:
    module: str
    name: str
    file: str
    line: int
    why: str | None
    node: Node
    class_name: str | None = None
    role: str | None = None
    # enclosing function(s) for nested definitions: `outer` / `outer.mid`
    parent: str | None = None

    @property
    def qualname(self) -> str:
        parts = [self.module]
        if self.class_name:
            parts.append(self.class_name)
        if self.parent:
            parts.append(self.parent)
        parts.append(self.name)
        return ".".join(parts)


@dataclass
class PyRoute:
    method: str
    path: str
    handler: PyFunction
    evidence: Evidence
    confidence: Confidence = Confidence.EXACT
    path_unresolved: bool = False
    kind: str = "http"
    extra: dict = field(default_factory=dict)


@dataclass
class PySqlSite:
    function: PyFunction
    sql: str
    dynamic: bool
    evidence: Evidence
    note: str | None = None
    confidence: Confidence | None = None
    via: str | None = None
    sink_not_scanned: bool = False


@dataclass
class PyAccess:
    function: PyFunction
    kind: str
    table: str
    column: str | None
    confidence: Confidence
    evidence: Evidence
    target_kind: str = "table"
    op: str | None = None  # INSERT / UPDATE / DELETE when the ORM call says so


@dataclass
class PyHttpCall:
    function: PyFunction
    method: str
    url: str
    evidence: Evidence
    unresolved: bool = False
    confidence: Confidence = Confidence.INFERRED
    client_unknown: bool = False


@dataclass
class PyHttpHelper:
    function: PyFunction
    path_index: int
    default_method: str = "POST"


@dataclass
class PySqlDictHelper:
    function: PyFunction
    name_index: int
    dict_mod: str | None
    dict_name: str


@dataclass
class PyQueueEvent:
    function: PyFunction
    queue_kind: str
    name: str
    role: str
    evidence: Evidence
    http_url: str | None = None
    http_method: str | None = None
    target_symbol: str | None = None


@dataclass
class PyRpcCall:
    function: PyFunction
    name: str
    evidence: Evidence


@dataclass
class PyUnresolvedCall:
    caller: str
    callee: str
    evidence: Evidence


@dataclass
class PyAnalysis:
    functions: dict[str, PyFunction] = field(default_factory=dict)
    routes: list[PyRoute] = field(default_factory=list)
    sql_sites: list[PySqlSite] = field(default_factory=list)
    calls: list[tuple[str, str, Evidence, Confidence]] = field(default_factory=list)
    accesses: list[PyAccess] = field(default_factory=list)
    http_calls: list[PyHttpCall] = field(default_factory=list)
    queue_events: list[PyQueueEvent] = field(default_factory=list)
    rpc_calls: list[PyRpcCall] = field(default_factory=list)
    unresolved_calls: list[PyUnresolvedCall] = field(default_factory=list)
    models: dict[str, ModelInfo] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)
    schema: Schema = field(default_factory=Schema)
    workers: list[PyFunction] = field(default_factory=list)
    # (caller, callee) -> how a `calls` edge was derived (e.g. "passed_as_arg")
    call_vias: dict[tuple[str, str], str] = field(default_factory=dict)
    # (caller, callee) -> why the call could not be followed
    unresolved_reasons: dict[tuple[str, str], str] = field(default_factory=dict)


class PythonAnalyzer:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = [r for r in rules if r.language == "python" and not r.is_stub]
        self._parser = _parser()
        self._fastapi = next((r for r in self.rules if r.framework == "fastapi"), None)
        self._flask = next((r for r in self.rules if r.framework == "flask"), None)
        self._firebase = next((r for r in self.rules if r.framework == "firebase"), None)
        self._sql_rules = [r for r in self.rules if "sql" in r.produce]

    def analyze(
        self,
        files: list[Path],
        root: Path,
        constants: dict[str, str] | None = None,
    ) -> PyAnalysis:
        analysis = PyAnalysis()
        _ENCLOSING_FN.clear()
        _PY_REEXPORTS.clear()
        clear_parent_index()
        parsed: list[tuple[Path, str, bytes, Node, str, ImportMap, list[PyFunction]]] = []
        forest: list[object] = []
        consts_by_module: dict[str, dict[str, ResolvedString]] = {}
        dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] = {}
        imaps: dict[str, ImportMap] = {}

        module_names = _python_module_names(files, root)
        for path in files:
            source = path.read_bytes()
            ts_tree = self._parser.parse(source)
            forest.append(ts_tree)
            tree = ts_tree.root_node
            index_deep_tree(tree)
            rel_file = rel(path, root)
            module = module_names.get(path) or _module_name(rel_file)
            imap = parse_imports(tree, module, is_package=rel_file.endswith("__init__.py"))
            imaps[module] = imap
            fns = self._collect_functions(tree, source, rel_file, module)
            for fn in fns:
                analysis.functions[fn.qualname] = fn
            parsed.append((path, rel_file, source, tree, module, imap, fns))

        _remap_imports_by_suffix(imaps, set(imaps))
        _follow_python_reexports(imaps, analysis.functions)

        yaml_consts = {
            str(k): ResolvedString(str(v), True, False) for k, v in (constants or {}).items()
        }
        consts_by_module["__yaml__"] = yaml_consts
        for _path, _rf, _src, tree, module, imap, _fns in parsed:
            assigns = collect_module_assignments(tree)
            consts_by_module[module] = resolve_module_consts(assigns, imap, consts_by_module)
            for key, val in yaml_consts.items():
                consts_by_module[module].setdefault(key, val)
                if "." in key:
                    consts_by_module[module].setdefault(key.rsplit(".", 1)[-1], val)
            dicts_by_module[module] = collect_module_dicts(
                assigns, imap, consts_by_module, dicts_by_module
            )
        for module, cmap in list(consts_by_module.items()):
            if module == "__yaml__":
                continue
            for key, val in yaml_consts.items():
                cmap.setdefault(key, val)
                if "." in key:
                    cmap.setdefault(key.rsplit(".", 1)[-1], val)
        # second pass so imported constants resolve
        for _path, _rf, _src, tree, module, imap, _fns in parsed:
            assigns = collect_module_assignments(tree)
            consts_by_module[module] = resolve_module_consts(assigns, imap, consts_by_module)
            for key, val in yaml_consts.items():
                consts_by_module[module].setdefault(key, val)
                if "." in key:
                    consts_by_module[module].setdefault(key.rsplit(".", 1)[-1], val)
            dicts_by_module[module] = collect_module_dicts(
                assigns, imap, consts_by_module, dicts_by_module
            )

        schema_inputs = [
            (p, rf, src, tree, mod, imap) for p, rf, src, tree, mod, imap, _ in parsed
        ]
        models, bindings = apply_python_schema(
            analysis.schema, schema_inputs, consts_by_module, root
        )
        analysis.models = models
        analysis.bindings = dict(bindings)
        for model in models.values():
            analysis.bindings.setdefault(model.class_name, model.table)

        scripts = _console_scripts(root)
        call_returns: dict[str, ResolvedString] = {}
        for fn in analysis.functions.values():
            imap_fn = imaps.get(fn.module) or ImportMap()
            local = consts_by_module.get(fn.module) or {}
            local_dicts = dicts_by_module.get(fn.module) or {}
            env = collect_local_env(
                fn.node, local, imap_fn, consts_by_module, local_dicts, dicts_by_module
            )
            ret = simple_return_value(
                fn.node, env, local, imap_fn, consts_by_module, local_dicts, dicts_by_module
            )
            if ret and ret.usable:
                call_returns[fn.name] = ret
                call_returns[fn.qualname] = ret
        protocols = _collect_protocols(parsed)
        protocol_impls = _protocol_implementers(parsed, protocols)
        factories = _collect_app_factories(parsed)
        prepared: list[tuple] = []
        for _path, rel_file, _source, tree, module, imap, fns in parsed:
            local = dict(consts_by_module.get(module) or {})
            local.update(_class_field_env(tree, local, imap, consts_by_module))
            local_dicts = dicts_by_module.get(module) or {}

            def resolve(
                node: Node | None,
                _local=local,
                _imap=imap,
                _dicts=local_dicts,
            ) -> ResolvedString:
                return resolve_expr(
                    node,
                    _local,
                    _local,
                    _imap,
                    consts_by_module,
                    _dicts,
                    dicts_by_module,
                    call_returns,
                )

            routers, includes, blueprints = self._collect_prefixes(
                tree, resolve, imap, factories
            )
            module_names = _module_level_names(tree, imap)
            prepared.append(
                (
                    rel_file,
                    tree,
                    module,
                    imap,
                    fns,
                    resolve,
                    routers,
                    includes,
                    blueprints,
                    module_names,
                    local_dicts,
                )
            )

        routers_by_mod: dict[str, dict[str, str]] = {}
        blueprints_by_mod: dict[str, dict[str, str]] = {}
        for _rf, _tree, module, _imap, _fns, _res, routers, _inc, blueprints, _names, _ld in prepared:
            routers_by_mod[module] = routers
            blueprints_by_mod[module] = blueprints
        for _rf, _tree, _module, imap, _fns, _res, routers, _inc, blueprints, _names, _ld in prepared:
            _merge_imported_apps(imap, routers, blueprints, routers_by_mod, blueprints_by_mod)

        for (
            rel_file,
            tree,
            module,
            imap,
            fns,
            resolve,
            routers,
            includes,
            blueprints,
            module_names,
            local_dicts,
        ) in prepared:
            enclosing = {id(fn.node): fn for fn in fns}
            self._detect_routes(
                analysis,
                tree,
                rel_file,
                imap,
                fns,
                enclosing,
                resolve,
                routers,
                includes,
                blueprints,
                module_names,
            )
            http_helpers = _collect_py_http_helpers(analysis)
            sql_helpers = _collect_py_sql_dict_helpers(analysis, dicts_by_module, imaps)
            self._detect_calls_and_usage(
                analysis,
                tree,
                rel_file,
                imap,
                fns,
                enclosing,
                resolve,
                scripts,
                local_dicts,
                dicts_by_module,
                consts_by_module,
                call_returns,
                protocols,
                protocol_impls,
                http_helpers,
                sql_helpers,
                routers,
                blueprints,
            )
            self._detect_main_worker(analysis, tree, module, fns, enclosing, rel_file)

        _link_function_tables(analysis, parsed)
        _link_field_dispatch(analysis, parsed)
        _link_executor_calls(analysis, parsed)
        _link_module_calls_and_passed_fns(analysis, parsed)
        _link_unscanned_sinks(analysis, parsed, consts_by_module, _declared_dependencies(root))
        _link_sql_references(analysis, parsed, consts_by_module, dicts_by_module)
        unique: dict[tuple, PySqlSite] = {}
        for site in analysis.sql_sites:
            key = (site.function.qualname, site.sql, site.evidence.line)
            unique[key] = site
        analysis.sql_sites = list(unique.values())
        trees = [(mod, rf, tree) for _p, rf, _s, tree, mod, _i, _f in parsed]
        _resolve_callable_injection(analysis, trees)
        _resolve_object_dispatch(analysis, trees)
        _ = forest
        return analysis

    def _collect_functions(
        self, tree: Node, source: bytes, rel_file: str, module: str
    ) -> list[PyFunction]:
        fns: list[PyFunction] = []
        role = "schema" if is_schema_rel(rel_file) else None
        for node in walk(tree):
            if node.type != "function_definition":
                continue
            name = text(node.child_by_field_name("name")) or "unknown"
            why = _docstring(node) or _preceding_hash_comment(node, source)
            fns.append(
                PyFunction(
                    module=module,
                    name=name,
                    file=rel_file,
                    line=line_of(node),
                    why=why,
                    node=node,
                    class_name=_enclosing_class(node),
                    role=role,
                    parent=_enclosing_function_names(node),
                )
            )
        return fns

    def _collect_prefixes(
        self,
        tree: Node,
        resolve,
        imap: ImportMap | None = None,
        factories: dict[str, tuple[str, str]] | None = None,
    ) -> tuple[dict[str, str], list[tuple[str, str]], dict[str, str]]:
        routers: dict[str, str] = {}
        includes: list[tuple[str, str]] = []
        blueprints: dict[str, str] = {}
        imap = imap or ImportMap()
        factories = factories or {}
        for node in walk(tree):
            if node.type == "assignment":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if left is None or right is None or right.type != "call":
                    continue
                lname = text(left) if left.type == "identifier" else ""
                callee = call_callee_name(right)
                kw = call_kwargs(right)
                pos = call_posargs(right)
                if callee == "APIRouter":
                    prefix = ""
                    if "prefix" in kw:
                        prefix = resolve(kw["prefix"]).path_text
                    routers[lname] = prefix
                elif callee == "Blueprint":
                    prefix = ""
                    if "url_prefix" in kw:
                        prefix = resolve(kw["url_prefix"]).path_text
                    elif len(pos) >= 3:
                        prefix = resolve(pos[2]).path_text
                    if lname:
                        blueprints[lname] = prefix
                elif callee in APP_CTORS and lname:
                    routers[lname] = ""
                elif lname:
                    spec = _lookup_app_factory(callee, imap, factories)
                    if spec is not None:
                        kind, prefix = spec
                        if kind == "blueprint":
                            blueprints[lname] = prefix
                        else:
                            routers[lname] = prefix
            if node.type == "call" and call_callee_name(node) == "include_router":
                pos = call_posargs(node)
                kw = call_kwargs(node)
                if not pos:
                    continue
                rname = text(pos[0])
                extra = ""
                if "prefix" in kw:
                    extra = resolve(kw["prefix"]).path_text
                includes.append((rname, extra))
        return routers, includes, blueprints

    def _detect_routes(
        self,
        analysis: PyAnalysis,
        tree: Node,
        rel_file: str,
        imap: ImportMap,
        fns: list[PyFunction],
        enclosing: dict[int, PyFunction],
        resolve,
        routers: dict[str, str],
        includes: list[tuple[str, str]],
        blueprints: dict[str, str],
        module_names: set[str] | None = None,
    ) -> None:
        http_deco = set()
        if self._fastapi:
            spec = (self._fastapi.detect.get("decorators") or {}).get("method") or {}
            http_deco.update(str(x).lower() for x in spec.get("in") or HTTP_METHODS)
        else:
            http_deco.update(HTTP_METHODS)
        http_deco.add("api_route")

        for node in walk(tree):
            if node.type != "decorated_definition":
                continue
            fn_node = next((c for c in node.named_children if c.type == "function_definition"), None)
            if fn_node is None:
                continue
            fn = enclosing.get(id(fn_node))
            if fn is None:
                continue
            for child in node.named_children:
                if child.type != "decorator":
                    continue
                target = _decorator_target(child)
                if target is None:
                    continue
                self._apply_decorator(
                    analysis,
                    fn,
                    target,
                    rel_file,
                    imap,
                    resolve,
                    http_deco,
                    routers,
                    includes,
                    blueprints,
                    module_names or set(),
                )

    def _apply_decorator(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        target: Node,
        rel_file: str,
        imap: ImportMap,
        resolve,
        http_deco: set[str],
        routers: dict[str, str],
        includes: list[tuple[str, str]],
        blueprints: dict[str, str],
        module_names: set[str] | None = None,
    ) -> None:
        evidence = Evidence(file=rel_file, line=line_of(target), symbol=fn.qualname)
        call = target if target.type == "call" else None
        attr = target if target.type == "attribute" else (call.child_by_field_name("function") if call else None)
        method = call_callee_name(call) if call else (text(attr).rsplit(".", 1)[-1] if attr else "")
        method_l = method.lower()
        receiver = ""
        if attr is not None and attr.type == "attribute" and attr.named_children:
            receiver = text(attr.named_children[0])
        qual = text(attr) if attr is not None else method

        # Celery / RQ task decorator
        if method_l in CELERY_DECORATORS or qual.endswith(".task"):
            analysis.queue_events.append(
                PyQueueEvent(
                    function=fn,
                    queue_kind="celery",
                    name=fn.qualname,
                    role="consume",
                    evidence=evidence,
                    target_symbol=fn.qualname,
                )
            )
            analysis.routes.append(
                PyRoute(
                    method="",
                    path=fn.qualname,
                    handler=fn,
                    evidence=evidence,
                    kind="celery_task",
                    extra={"task": fn.qualname},
                )
            )
            return

        # functions_framework
        if "functions_framework" in qual or method_l in {"http", "cloud_event"}:
            if any("functions_framework" in s for s in imap.raw) or "functions_framework" in qual:
                kind = "http" if method_l == "http" else "cloud_event"
                analysis.routes.append(
                    PyRoute(
                        method="POST" if kind == "http" else "EVENT",
                        path=f"/{fn.name}",
                        handler=fn,
                        evidence=evidence,
                        kind="firebase_http" if kind == "http" else "cloud_event",
                    )
                )
                return

        # Firebase
        if method in FIRESTORE_DOC_EVENTS or method_l in FIRESTORE_DOC_EVENTS:
            doc = ""
            if call is not None:
                kw = call_kwargs(call)
                pos = call_posargs(call)
                raw = kw.get("document") or (pos[0] if pos else None)
                if raw is not None:
                    doc = resolve(raw).path_text
            coll = _collection_from_document(doc) if doc else ""
            analysis.routes.append(
                PyRoute(
                    method=FIRESTORE_DOC_EVENTS.get(method, method_l).upper(),
                    path=doc or f"/{fn.name}",
                    handler=fn,
                    evidence=evidence,
                    kind="firestore",
                    extra={"collection": coll, "document": doc, "event": FIRESTORE_DOC_EVENTS.get(method, method_l)},
                )
            )
            if coll:
                analysis.queue_events.append(
                    PyQueueEvent(
                        function=fn,
                        queue_kind="firestore",
                        name=coll,
                        role="consume",
                        evidence=evidence,
                    )
                )
            return
        if method_l == "on_request":
            analysis.routes.append(
                PyRoute(method="POST", path=f"/{fn.name}", handler=fn, evidence=evidence, kind="firebase_http")
            )
            return
        if method_l == "on_call":
            analysis.routes.append(
                PyRoute(method="POST", path=f"/{fn.name}", handler=fn, evidence=evidence, kind="firebase_call")
            )
            return
        if method_l == "on_schedule":
            analysis.routes.append(
                PyRoute(method="", path=fn.qualname, handler=fn, evidence=evidence, kind="scheduled_job")
            )
            return
        if method_l == "on_task_dispatched":
            analysis.routes.append(
                PyRoute(method="POST", path=f"/{fn.name}", handler=fn, evidence=evidence, kind="cloud_task")
            )
            analysis.queue_events.append(
                PyQueueEvent(
                    function=fn, queue_kind="cloud_tasks", name=fn.name, role="consume", evidence=evidence
                )
            )
            return
        if method_l == "on_message_published":
            topic = ""
            if call is not None:
                kw = call_kwargs(call)
                if "topic" in kw:
                    topic = resolve(kw["topic"]).path_text.split("/")[-1]
            analysis.routes.append(
                PyRoute(
                    method="",
                    path=topic or fn.qualname,
                    handler=fn,
                    evidence=evidence,
                    kind="pubsub",
                    extra={"topic": topic},
                )
            )
            analysis.queue_events.append(
                PyQueueEvent(
                    function=fn,
                    queue_kind="pubsub",
                    name=topic or fn.name,
                    role="consume",
                    evidence=evidence,
                )
            )
            return

        # Flask @bp.route / @app.route
        if method_l == "route" and call is not None:
            if not _http_receiver_ok(receiver, attr, routers, blueprints, module_names or set()):
                return
            pos = call_posargs(call)
            kw = call_kwargs(call)
            path_node = pos[0] if pos else kw.get("rule")
            path_res = resolve(path_node) if path_node is not None else ResolvedString("", True, False)
            methods = _list_string_items(kw.get("methods")) or ["GET"]
            prefix = blueprints.get(receiver) or routers.get(receiver, "")
            full = join_route_path(prefix, path_res.path_text)
            unresolved = path_res.unresolved or ("{" in prefix and not prefix.startswith("/{"))
            for m in methods:
                analysis.routes.append(
                    PyRoute(
                        method=m.upper(),
                        path=_ensure_path(full),
                        handler=fn,
                        evidence=evidence,
                        confidence=Confidence.UNKNOWN if unresolved else Confidence.EXACT,
                        path_unresolved=unresolved,
                    )
                )
            return

        # FastAPI @app.get / @router.post — known app/router, factory return, or
        # module-level `@name.get("/path")` (never a call chain like patch("x.y")).
        if method_l in http_deco and call is not None:
            if not _http_receiver_ok(receiver, attr, routers, blueprints, module_names or set()):
                return
            if self._fastapi and not import_matches(imap.raw, self._fastapi.when):
                # still allow if receiver is a collected app/router
                pass
            pos = call_posargs(call)
            path_idx = 0
            if self._fastapi:
                path_idx = int(
                    ((self._fastapi.detect.get("decorators") or {}).get("arguments") or {})
                    .get("path", {})
                    .get("index", 0)
                )
            path_node = pos[path_idx] if len(pos) > path_idx else None
            if path_node is None and not pos:
                path_res = ResolvedString("/", True, False)
            elif path_node is None:
                path_res = ResolvedString(None, False, True, placeholder="{path}")
            else:
                path_res = resolve(path_node)
            if not path_res.usable:
                path_res = ResolvedString(None, False, True, placeholder=f"{{{text(path_node)}}}")
            router_prefix = routers.get(receiver, "")
            extras = [p for name, p in includes if name == receiver]
            prefixes = extras or [""]
            kw = call_kwargs(call)
            if method_l == "api_route":
                methods = [m.upper() for m in (_list_string_items(kw.get("methods")) or ["GET"])]
            else:
                methods = [method.upper()]
            for extra in prefixes:
                full = join_route_path(extra, router_prefix, path_res.path_text)
                unresolved = bool(path_res.unresolved or (extra and "{" in extra) or (router_prefix and "{" in router_prefix and not path_res.fully_static))
                if path_res.unresolved:
                    unresolved = True
                # unresolved prefix piece: if router_prefix is placeholder
                if router_prefix.startswith("{") or extra.startswith("{"):
                    unresolved = True
                if not path_res.fully_static:
                    unresolved = True
                for http_method in methods:
                    analysis.routes.append(
                        PyRoute(
                            method=http_method,
                            path=_ensure_path(full),
                            handler=fn,
                            evidence=evidence,
                            confidence=Confidence.UNKNOWN if unresolved else Confidence.EXACT,
                            path_unresolved=unresolved,
                        )
                    )

    def _detect_calls_and_usage(
        self,
        analysis: PyAnalysis,
        tree: Node,
        rel_file: str,
        imap: ImportMap,
        fns: list[PyFunction],
        enclosing: dict[int, PyFunction],
        resolve,
        scripts: dict[str, str],
        local_dicts: dict[str, dict[str, ResolvedString]] | None = None,
        dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] | None = None,
        consts_by_module: dict[str, dict[str, ResolvedString]] | None = None,
        call_returns: dict[str, ResolvedString] | None = None,
        protocols: dict[str, set[str]] | None = None,
        protocol_impls: dict[str, list[str]] | None = None,
        http_helpers: dict[str, PyHttpHelper] | None = None,
        sql_helpers: dict[str, PySqlDictHelper] | None = None,
        routers: dict[str, str] | None = None,
        blueprints: dict[str, str] | None = None,
    ) -> None:
        by_local = {fn.name: fn for fn in fns if not fn.class_name}
        by_class_method: dict[tuple[str, str], PyFunction] = {
            (fn.class_name, fn.name): fn for fn in fns if fn.class_name
        }
        imported_fns = self._imported_functions(imap, analysis)
        http_clients = _collect_py_http_clients(tree, imap)
        local_dicts = local_dicts or {}
        dicts_by_module = dicts_by_module or {}
        consts_by_module = consts_by_module or {}
        call_returns = call_returns or {}
        protocols = protocols or {}
        protocol_impls = protocol_impls or {}
        http_helpers = http_helpers or {}
        sql_helpers = sql_helpers or {}
        routers = routers or {}
        blueprints = blueprints or {}
        module_consts = consts_by_module.get(fns[0].module if fns else "") or {}

        for fn in fns:
            local_env = collect_local_env(
                fn.node,
                module_consts,
                imap,
                consts_by_module,
                local_dicts,
                dicts_by_module,
                call_returns,
            )

            def resolve_fn(
                node: Node | None,
                _env=local_env,
            ) -> ResolvedString:
                return resolve_expr(
                    node,
                    _env,
                    module_consts,
                    imap,
                    consts_by_module,
                    local_dicts,
                    dicts_by_module,
                    call_returns,
                )

            refmap = _firestore_refmap(fn.node, resolve_fn)
            for node in walk(fn.node):
                if node.type != "call":
                    continue
                method = call_callee_name(node)
                qual = call_qualname(node)
                args = call_args(node)
                pos = call_posargs(node)
                evidence = Evidence(file=rel_file, line=line_of(node), symbol=fn.qualname)

                self._record_call_edge(
                    analysis,
                    fn,
                    node,
                    method,
                    by_local,
                    by_class_method,
                    imported_fns,
                    evidence,
                    protocols,
                    protocol_impls,
                )
                self._record_sql(
                    analysis,
                    fn,
                    node,
                    method,
                    args,
                    pos,
                    imap,
                    resolve_fn,
                    evidence,
                    local_dicts,
                    dicts_by_module,
                )
                self._record_sqlalchemy(analysis, fn, node, method, pos, resolve_fn, evidence)
                self._record_firestore(analysis, fn, node, method, resolve_fn, evidence, refmap)
                self._record_http_client(
                    analysis,
                    fn,
                    node,
                    method,
                    qual,
                    pos,
                    http_clients,
                    resolve_fn,
                    evidence,
                    http_helpers,
                    routers,
                    blueprints,
                )
                self._record_sql_dict_helper(
                    analysis,
                    fn,
                    method,
                    qual,
                    pos,
                    resolve_fn,
                    evidence,
                    sql_helpers,
                    imap,
                    local_dicts,
                    dicts_by_module,
                )
                self._record_queues(analysis, fn, node, method, qual, pos, resolve_fn, evidence)
                self._record_rpc(analysis, fn, node, method, pos, resolve_fn, evidence)

            if _is_worker_fn(fn.node) and (
                fn.name in {"main", "run", "worker"}
                or fn.qualname in scripts.values()
                or fn.name in scripts.values()
            ):
                analysis.workers.append(fn)

    def _record_call_edge(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        node: Node,
        method: str,
        by_local: dict[str, PyFunction],
        by_class_method: dict[tuple[str, str], PyFunction],
        imported_fns: dict[str, PyFunction],
        evidence: Evidence,
        protocols: dict[str, set[str]] | None = None,
        protocol_impls: dict[str, list[str]] | None = None,
    ) -> None:
        func = node.child_by_field_name("function")
        # self.method
        if func is not None and func.type == "attribute" and func.named_children:
            obj = func.named_children[0]
            if text(obj) == "self" and fn.class_name:
                cal = by_class_method.get((fn.class_name, method))
                if cal and cal.qualname != fn.qualname:
                    analysis.calls.append((fn.qualname, cal.qualname, evidence, Confidence.INFERRED))
                    return
            # Class.method()
            if obj.type == "identifier":
                cal = by_class_method.get((text(obj), method))
                if cal and cal.qualname != fn.qualname:
                    analysis.calls.append((fn.qualname, cal.qualname, evidence, Confidence.INFERRED))
                    return
                proto = _param_protocol(fn, text(obj), protocols or {})
                if proto and method in (protocols or {}).get(proto, set()):
                    impls = (protocol_impls or {}).get(proto) or []
                    if not impls:
                        analysis.unresolved_calls.append(
                            PyUnresolvedCall(
                                caller=fn.qualname,
                                callee=f"{proto}.{method}",
                                evidence=evidence,
                            )
                        )
                        return
        if method in by_local and by_local[method].qualname != fn.qualname:
            analysis.calls.append((fn.qualname, by_local[method].qualname, evidence, Confidence.INFERRED))
            return
        if method in imported_fns and imported_fns[method].qualname != fn.qualname:
            analysis.calls.append(
                (fn.qualname, imported_fns[method].qualname, evidence, Confidence.INFERRED)
            )

    def _imported_functions(self, imap: ImportMap, analysis: PyAnalysis) -> dict[str, PyFunction]:
        out: dict[str, PyFunction] = {}
        for local, (mod, orig) in imap.from_names.items():
            matches = [
                f
                for f in analysis.functions.values()
                if f.name == orig and (f.module == mod or f.module.endswith("." + mod))
            ]
            if len(matches) == 1:
                out[local] = matches[0]
        return out

    def _record_sql(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        node: Node,
        method: str,
        args: list[Node],
        pos: list[Node],
        imap: ImportMap,
        resolve,
        evidence: Evidence,
        local_dicts: dict[str, dict[str, ResolvedString]] | None = None,
        dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] | None = None,
    ) -> None:
        if method in FIRESTORE_WRITE | FIRESTORE_READ | {"collection", "document"}:
            return
        if _inside_firestore_write(node):
            return
        for rule in self._sql_rules:
            if not method_matches(method, (rule.detect.get("calls") or {}).get("method")):
                continue
            if not import_matches(imap.raw, rule.when):
                continue
            idx = int((rule.detect.get("calls") or {}).get("arguments", {}).get("sql", {}).get("index", 0))
            sql_node = pos[idx] if len(pos) > idx else (args[idx] if len(args) > idx else None)
            if sql_node is None:
                continue
            if sql_node.type == "call":
                # execute(select(...)) / execute(text("...")) — inner call is handled separately
                inner_name = call_callee_name(sql_node)
                if inner_name == "text":
                    inner_pos = call_posargs(sql_node)
                    sql_node = inner_pos[0] if inner_pos else sql_node
                else:
                    continue
            for res in _sql_arg_sources(sql_node, resolve, imap, local_dicts, dicts_by_module):
                sql_val = res.path_text if res.fully_static else (res.text or res.placeholder or "")
                if sql_val and looks_like_sql(sql_val):
                    analysis.sql_sites.append(
                        PySqlSite(
                            function=fn,
                            sql=sql_val,
                            dynamic=res.unresolved and not res.note,
                            evidence=evidence,
                            note=res.note,
                            confidence=Confidence.INFERRED if res.note else None,
                        )
                    )
                elif res.unresolved and sql_node.type in {
                    "identifier",
                    "binary_operator",
                    "attribute",
                    "concatenated_string",
                    "string",
                    "subscript",
                }:
                    analysis.sql_sites.append(
                        PySqlSite(function=fn, sql=sql_val, dynamic=True, evidence=evidence)
                    )
        for arg in args:
            if arg.type == "keyword_argument":
                arg = arg.child_by_field_name("value") or arg
            if arg.type == "call":
                inner_name = call_callee_name(arg)
                if inner_name == "text":
                    # execute(text("...")): the SQL is the text() argument,
                    # not an unresolved call expression.
                    inner_pos = call_posargs(arg)
                    if not inner_pos:
                        continue
                    arg = inner_pos[0]
                elif inner_name in _SQLALCHEMY_BUILDERS:
                    # select()/insert()/update()/delete() are handled by the
                    # SQLAlchemy detector; their source text is not SQL. When
                    # the model/table cannot be resolved (it lives outside the
                    # scan), keep an honest `table:unknown` trace instead of
                    # dropping the access.
                    if not _builder_resolves(arg, analysis.bindings, analysis.models):
                        analysis.accesses.append(
                            PyAccess(
                                fn,
                                _builder_kind(arg),
                                "unknown",
                                None,
                                Confidence.UNKNOWN,
                                evidence,
                            )
                        )
                    continue
            for res in _sql_arg_sources(arg, resolve, imap, local_dicts, dicts_by_module):
                sql_val = res.path_text if res.usable else ""
                if res.fully_static and res.text and looks_like_sql(res.text):
                    analysis.sql_sites.append(
                        PySqlSite(
                            function=fn,
                            sql=res.text,
                            dynamic=False,
                            evidence=evidence,
                            note=res.note,
                            confidence=Confidence.INFERRED if res.note else None,
                        )
                    )
                elif res.unresolved and sql_val and (
                    looks_like_sql(sql_val)
                    or any(
                        tok in sql_val.upper()
                        for tok in ("UPDATE", "INSERT", "DELETE", "SELECT")
                    )
                ):
                    analysis.sql_sites.append(
                        PySqlSite(function=fn, sql=sql_val, dynamic=True, evidence=evidence)
                    )

    def _record_sqlalchemy(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        node: Node,
        method: str,
        pos: list[Node],
        resolve,
        evidence: Evidence,
    ) -> None:
        bindings = analysis.bindings
        models = analysis.models
        if method in SA_QUERY:
            if not pos:
                return
            cols: list[tuple[str, str | None]] = []
            for arg in pos:
                table, col = _resolve_table_ref(arg, bindings, models)
                if table:
                    cols.append((table, col))
            if not cols:
                return
            if method == "select":
                named = [(t, c) for t, c in cols if c]
                if named:
                    for t, c in named:
                        analysis.accesses.append(
                            PyAccess(fn, "read", t, c, Confidence.EXACT, evidence)
                        )
                else:
                    seen: set[str] = set()
                    for t, _c in cols:
                        if t not in seen:
                            seen.add(t)
                            analysis.accesses.append(
                                PyAccess(fn, "read", t, None, Confidence.INFERRED, evidence)
                            )
                return
            if method == "delete":
                t = cols[0][0]
                analysis.accesses.append(
                    PyAccess(fn, "write", t, None, Confidence.EXACT, evidence, op="DELETE")
                )
                return
            # insert/update table-level; columns come from .values
            t, c = cols[0]
            analysis.accesses.append(
                PyAccess(
                    fn,
                    "write",
                    t,
                    c,
                    Confidence.INFERRED if not c else Confidence.EXACT,
                    evidence,
                    op=method.upper(),
                )
            )
            return
        if method == "values":
            table, _ = _table_from_chain(node, bindings, models)
            if not table:
                return
            kw = call_kwargs(node)
            if kw:
                for col in kw:
                    analysis.accesses.append(
                        PyAccess(fn, "write", table, col.lower(), Confidence.EXACT, evidence)
                    )
            else:
                analysis.accesses.append(
                    PyAccess(fn, "write", table, None, Confidence.INFERRED, evidence)
                )
            return
        if method in {"add", "delete"} and pos:
            arg = pos[0]
            table = None
            cols: list[str] = []
            if arg.type == "call":
                table, _ = _resolve_table_ref(arg.child_by_field_name("function") or arg, bindings, models)
                if table is None:
                    table, _ = _resolve_table_ref(arg, bindings, models)
                for k in call_kwargs(arg):
                    cols.append(k.lower())
            else:
                table, _ = _resolve_table_ref(arg, bindings, models)
            if table:
                kind = "write"
                op = "INSERT" if method == "add" else "DELETE"
                if cols and method == "add":
                    for c in cols:
                        analysis.accesses.append(
                            PyAccess(fn, kind, table, c, Confidence.EXACT, evidence, op=op)
                        )
                else:
                    analysis.accesses.append(
                        PyAccess(fn, kind, table, None, Confidence.INFERRED, evidence, op=op)
                    )
            return
        if method == "query" and pos:
            table, col = _resolve_table_ref(pos[0], bindings, models)
            if table:
                analysis.accesses.append(
                    PyAccess(fn, "read", table, col, Confidence.INFERRED if not col else Confidence.EXACT, evidence)
                )
        # Model.column comparisons in where/filter
        if method in {"where", "filter", "filter_by"}:
            table, _ = _table_from_chain(node, bindings, models)
            for arg in pos:
                for n in walk(arg):
                    if n.type == "attribute":
                        t, c = _resolve_table_ref(n, bindings, models)
                        if t and c:
                            analysis.accesses.append(
                                PyAccess(fn, "read", t, c, Confidence.EXACT, evidence)
                            )
            for k in call_kwargs(node):
                if table:
                    analysis.accesses.append(
                        PyAccess(fn, "read", table, k.lower(), Confidence.EXACT, evidence)
                    )

    def _record_firestore(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        node: Node,
        method: str,
        resolve,
        evidence: Evidence,
        refmap: dict[str, str] | None = None,
    ) -> None:
        if method not in FIRESTORE_READ | FIRESTORE_WRITE | {"where", "collection", "document"}:
            return
        refmap = refmap or {}
        collections = _collections_in_chain(node, resolve)
        if not collections:
            pos = call_posargs(node)
            func = node.child_by_field_name("function")
            # ref.set() / col.document() where ref/col were assigned
            if func is not None and func.type == "attribute" and func.named_children:
                obj = func.named_children[0]
                obj_name = text(obj) if obj.type == "identifier" else ""
                if obj_name in refmap:
                    collections = [refmap[obj_name]]
            # transaction.update(ref, ...) / batch.set(ref, ...)
            if not collections and pos:
                arg = pos[0]
                arg_name = text(arg) if arg.type == "identifier" else ""
                if arg_name in refmap:
                    collections = [refmap[arg_name]]
        if not collections:
            return
        coll = collections[-1]
        if method in FIRESTORE_WRITE:
            analysis.accesses.append(
                PyAccess(fn, "write", coll, None, Confidence.EXACT, evidence, target_kind="collection")
            )
        elif method in FIRESTORE_READ | {"where"}:
            analysis.accesses.append(
                PyAccess(fn, "read", coll, None, Confidence.EXACT, evidence, target_kind="collection")
            )
        # subcollections already represented by last collection() in the chain

    def _record_http_client(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        node: Node,
        method: str,
        qual: str,
        pos: list[Node],
        http_clients: set[str],
        resolve,
        evidence: Evidence,
        http_helpers: dict[str, PyHttpHelper] | None = None,
        routers: dict[str, str] | None = None,
        blueprints: dict[str, str] | None = None,
    ) -> None:
        if _call_is_decorator(node):
            return
        method_l = method.lower()
        func = node.child_by_field_name("function")
        recv = ""
        if func is not None and func.type == "attribute" and func.named_children:
            recv = text(func.named_children[0])
        if recv in (routers or {}) or recv in (blueprints or {}):
            return
        helper = _lookup_py_http_helper(fn, method, qual, http_helpers or {})
        if helper is not None:
            self._record_http_helper_call(
                analysis, fn, pos, resolve, evidence, helper
            )
            return
        wrapper = method_l in HTTP_WRAPPER_METHODS or method in HTTP_WRAPPER_METHODS
        lib_call = any(lib in qual for lib in HTTP_CLIENT_LIBS)
        instance_call = bool(recv) and recv in http_clients and method_l in HTTP_CLIENT_FUNCS
        module_call = recv in HTTP_CLIENT_LIBS or recv.split(".")[-1] in HTTP_CLIENT_LIBS
        module_call = module_call and method_l in HTTP_CLIENT_FUNCS
        ident_client = (
            func is not None
            and func.type == "identifier"
            and text(func) in http_clients
            and method_l in HTTP_CLIENT_FUNCS
        )
        client_unknown = False
        if not (lib_call or instance_call or module_call or ident_client or wrapper):
            if _looks_like_unscanned_http(method_l, recv, qual, pos, resolve):
                client_unknown = True
            else:
                return
        url_node = None
        kw = call_kwargs(node)
        if method_l in {"request", "_request"}:
            mres = resolve(pos[0]) if pos else resolve(kw.get("method"))
            http_method = (mres.path_text if mres and mres.usable else "POST").upper()
            url_node = pos[1] if len(pos) > 1 else kw.get("url") or (pos[0] if pos else None)
        elif method_l in {"urlopen", "urlretrieve"}:
            http_method = "GET"
            url_node = pos[0] if pos else kw.get("url")
        else:
            http_method = method_l.lstrip("_").upper()
            url_node = pos[0] if pos else kw.get("url")
        if url_node is None:
            return
        res = resolve(url_node)
        if not res.usable:
            if client_unknown:
                analysis.http_calls.append(
                    PyHttpCall(
                        function=fn,
                        method=http_method,
                        url="{path}",
                        evidence=evidence,
                        unresolved=True,
                        client_unknown=True,
                    )
                )
            return
        urls = res.all_texts() or ([res.path_text] if res.path_text else [])
        multi = len(urls) > 1
        for url in urls:
            if not (url.startswith("/") or url.startswith("{") or "://" in url or "/" in url):
                continue
            unresolved = res.unresolved and not multi
            conf = Confidence.INFERRED if multi else (
                Confidence.UNKNOWN if res.unresolved else Confidence.INFERRED
            )
            analysis.http_calls.append(
                PyHttpCall(
                    function=fn,
                    method=http_method,
                    url=_normalize_http_base(url),
                    evidence=evidence,
                    unresolved=unresolved or client_unknown,
                    confidence=conf,
                    client_unknown=client_unknown,
                )
            )

    def _record_http_helper_call(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        pos: list[Node],
        resolve,
        evidence: Evidence,
        helper: PyHttpHelper,
    ) -> None:
        if helper.path_index >= len(pos):
            analysis.http_calls.append(
                PyHttpCall(
                    function=fn,
                    method=helper.default_method,
                    url="{base}/{path}",
                    evidence=evidence,
                    unresolved=True,
                )
            )
            return
        res = resolve(pos[helper.path_index])
        texts = res.all_texts() or ([res.path_text] if res.path_text else [])
        if not texts:
            analysis.http_calls.append(
                PyHttpCall(
                    function=fn,
                    method=helper.default_method,
                    url="{base}/{path}",
                    evidence=evidence,
                    unresolved=True,
                )
            )
            return
        for path in texts:
            if "://" in path or (path.startswith("{") and "/" in path and not path.startswith("/")):
                url = _normalize_http_base(path)
            elif path.startswith("/"):
                url = "{base}" + path
            elif path.startswith("{"):
                url = "{base}/" + path
            else:
                url = "{base}/" + path
            url = _normalize_http_base(url)
            analysis.http_calls.append(
                PyHttpCall(
                    function=fn,
                    method=helper.default_method,
                    url=url,
                    evidence=evidence,
                    unresolved=res.unresolved or "{" in url,
                    confidence=Confidence.UNKNOWN if res.unresolved else Confidence.INFERRED,
                )
            )

    def _record_sql_dict_helper(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        method: str,
        qual: str,
        pos: list[Node],
        resolve,
        evidence: Evidence,
        sql_helpers: dict[str, PySqlDictHelper],
        imap: ImportMap,
        local_dicts: dict[str, dict[str, ResolvedString]],
        dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]],
    ) -> None:
        helper = (
            sql_helpers.get(qual)
            or sql_helpers.get(method)
            or (sql_helpers.get(f"{fn.module}.{method}"))
        )
        if helper is None and fn.class_name:
            helper = sql_helpers.get(f"{fn.module}.{fn.class_name}.{method}")
        if helper is None or helper.name_index >= len(pos):
            return
        key_res = resolve(pos[helper.name_index])
        if not key_res.fully_static or not key_res.text:
            return
        dmap = (dicts_by_module.get(helper.dict_mod) or {}).get(helper.dict_name) if helper.dict_mod else None
        if dmap is None:
            dmap = lookup_dict(helper.dict_name, imap, local_dicts, dicts_by_module)
        if not dmap or key_res.text not in dmap:
            return
        sql_res = dmap[key_res.text]
        sql_val = sql_res.path_text if sql_res.usable else ""
        if sql_val and looks_like_sql(sql_val):
            analysis.sql_sites.append(
                PySqlSite(
                    function=fn,
                    sql=sql_val,
                    dynamic=sql_res.unresolved and not sql_res.note,
                    evidence=evidence,
                    note=sql_res.note,
                    confidence=Confidence.INFERRED if sql_res.note else None,
                )
            )

    def _record_queues(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        node: Node,
        method: str,
        qual: str,
        pos: list[Node],
        resolve,
        evidence: Evidence,
    ) -> None:
        if method in {"delay", "apply_async"}:
            target = qual.rsplit(".", 1)[0] if "." in qual else text(node.child_by_field_name("function"))
            analysis.queue_events.append(
                PyQueueEvent(
                    function=fn,
                    queue_kind="celery",
                    name=target or method,
                    role="produce",
                    evidence=evidence,
                    target_symbol=target,
                )
            )
            return
        if method == "enqueue":
            target = text(pos[0]) if pos else "task"
            analysis.queue_events.append(
                PyQueueEvent(
                    function=fn,
                    queue_kind="rq",
                    name=target,
                    role="produce",
                    evidence=evidence,
                    target_symbol=target,
                )
            )
            return
        if method in {"publish", "publish_message"}:
            topic = ""
            if pos:
                topic = resolve(pos[0]).path_text.split("/")[-1]
            kw = call_kwargs(node)
            if "topic" in kw:
                topic = resolve(kw["topic"]).path_text.split("/")[-1]
            if topic:
                analysis.queue_events.append(
                    PyQueueEvent(
                        function=fn, queue_kind="pubsub", name=topic, role="produce", evidence=evidence
                    )
                )
            return
        if method == "create_task" or "create_task" in qual:
            url_res = find_dict_string(node, {"url"}, resolve)
            method_res = find_dict_string(node, {"http_method", "method"}, resolve)
            parent_res = find_dict_string(node, {"parent", "queue"}, resolve)
            http_url = url_res.path_text if url_res and url_res.usable else None
            http_method = (
                method_res.path_text.upper() if method_res and method_res.usable else "POST"
            )
            if http_method.endswith("POST"):
                http_method = "POST"
            elif http_method.endswith("GET"):
                http_method = "GET"
            qname = "cloud-tasks"
            if parent_res and parent_res.usable:
                parent = parent_res.path_text.rstrip("/")
                qname = parent.split("/")[-1] or qname
            elif http_url:
                qname = http_url
            analysis.queue_events.append(
                PyQueueEvent(
                    function=fn,
                    queue_kind="cloud_tasks",
                    name=qname,
                    role="produce",
                    evidence=evidence,
                    http_url=http_url,
                    http_method=http_method,
                )
            )
            if http_url:
                analysis.http_calls.append(
                    PyHttpCall(
                        function=fn,
                        method=http_method,
                        url=http_url,
                        evidence=evidence,
                        unresolved=bool(url_res and url_res.unresolved),
                    )
                )

    def _record_rpc(
        self,
        analysis: PyAnalysis,
        fn: PyFunction,
        node: Node,
        method: str,
        pos: list[Node],
        resolve,
        evidence: Evidence,
    ) -> None:
        if method != "rpc" or not pos:
            return
        name = resolve(pos[0]).path_text
        if name:
            analysis.rpc_calls.append(PyRpcCall(function=fn, name=name, evidence=evidence))

    def _detect_main_worker(
        self,
        analysis: PyAnalysis,
        tree: Node,
        module: str,
        fns: list[PyFunction],
        enclosing: dict[int, PyFunction],
        rel_file: str,
    ) -> None:
        for node in tree.named_children:
            if node.type != "if_statement":
                continue
            cond = node.child_by_field_name("condition")
            if cond is None or "__main__" not in text(cond):
                continue
            body = node.child_by_field_name("consequence") or node.named_children[-1]
            main_fn = PyFunction(
                module=module,
                name="__main__",
                file=rel_file,
                line=line_of(node),
                why=None,
                node=body,
            )
            if main_fn.qualname not in analysis.functions:
                analysis.functions[main_fn.qualname] = main_fn
            if _is_worker_fn(body):
                analysis.workers.append(main_fn)
            by_local = {fn.name: fn for fn in fns}
            for call in walk(body):
                if call.type != "call":
                    continue
                method = call_callee_name(call)
                if method in by_local:
                    ev = Evidence(file=rel_file, line=line_of(call), symbol=main_fn.qualname)
                    analysis.calls.append(
                        (main_fn.qualname, by_local[method].qualname, ev, Confidence.INFERRED)
                    )
                    if _is_worker_fn(by_local[method].node):
                        analysis.workers.append(by_local[method])


def _is_worker_fn(node: Node) -> bool:
    for n in walk(node):
        if n.type == "while_statement":
            cond = n.child_by_field_name("condition")
            if cond is None and n.named_children:
                cond = n.named_children[0]
            if cond is not None and text(cond) in {"True", "true", "1"}:
                return True
        if n.type == "call" and call_callee_name(n) in {"run_forever", "serve_forever"}:
            return True
    return False


def _console_scripts(root: Path) -> dict[str, str]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    try:
        import tomllib
    except ImportError:
        return {}
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scripts = (data.get("project") or {}).get("scripts") or {}
    out: dict[str, str] = {}
    for name, spec in scripts.items():
        # module:func
        if isinstance(spec, str) and ":" in spec:
            out[name] = spec.replace(":", ".")
        elif isinstance(spec, str):
            out[name] = spec
    return out


def _resolve_table_ref(
    node: Node | None,
    bindings: dict[str, str],
    models: dict[str, ModelInfo],
) -> tuple[str | None, str | None]:
    if node is None:
        return None, None
    if node.type == "call":
        fn = node.child_by_field_name("function")
        if fn is not None and text(fn).split(".")[-1] in {"select", "insert", "update", "delete"}:
            pos = call_posargs(node)
            return _resolve_table_ref(pos[0] if pos else None, bindings, models)
        return _resolve_table_ref(fn or node.named_children[0], bindings, models)
    if node.type == "identifier":
        name = text(node)
        if name in bindings:
            return bindings[name], None
        if name in models:
            return models[name].table, None
        return None, None
    if node.type == "attribute":
        parts = text(node).split(".")
        if len(parts) >= 3 and parts[1] == "c":
            table = bindings.get(parts[0]) or (models[parts[0]].table if parts[0] in models else None)
            return table, parts[-1].lower()
        obj = node.named_children[0] if node.named_children else None
        attr = text(node.named_children[-1]) if len(node.named_children) >= 2 else parts[-1]
        table, col = _resolve_table_ref(obj, bindings, models)
        if table and attr not in {"c", "columns", "query"}:
            return table, attr.lower()
        return table, col
    return None, None


def _table_from_chain(
    call: Node,
    bindings: dict[str, str],
    models: dict[str, ModelInfo],
) -> tuple[str | None, str | None]:
    cur: Node | None = call
    last_call = call
    while cur is not None and cur.type == "call":
        last_call = cur
        func = cur.child_by_field_name("function")
        if func is not None and func.type == "attribute":
            obj = func.named_children[0]
            cur = obj if obj.type == "call" else None
            if cur is None:
                return _resolve_table_ref(obj, bindings, models)
            continue
        break
    pos = call_posargs(last_call)
    if pos:
        return _resolve_table_ref(pos[0], bindings, models)
    return _resolve_table_ref(last_call, bindings, models)


def _collections_in_chain(call: Node, resolve) -> list[str]:
    names: list[str] = []
    cur: Node | None = call
    while cur is not None:
        if cur.type == "call":
            if call_callee_name(cur) == "collection":
                pos = call_posargs(cur)
                if pos:
                    res = resolve(pos[0])
                    if res.usable and res.fully_static and res.path_text:
                        names.append(res.path_text)
                    else:
                        names.append("unknown")
            func = cur.child_by_field_name("function")
            if func is not None and func.type == "attribute":
                cur = func.named_children[0]
                continue
            cur = func
            continue
        if cur.type == "attribute":
            cur = cur.named_children[0] if cur.named_children else None
            continue
        break
    names.reverse()
    return names


_SQLALCHEMY_BUILDERS = {
    "select",
    "insert",
    "update",
    "delete",
    "union",
    "union_all",
    "exists",
    "values",
    "where",
    "returning",
    "on_conflict_do_update",
    "on_conflict_do_nothing",
}


_BUILDER_ROOTS = {"select", "insert", "update", "delete"}
_BUILDER_WRITES = {"insert", "update", "delete", "values", "on_conflict_do_update"}


def _builder_chain(node: Node) -> list[tuple[str, Node]]:
    """`update(T).where(...).values(...)` → [("values", …), ("where", …), ("update", …)]."""
    chain: list[tuple[str, Node]] = []
    cur: Node | None = node
    while cur is not None and cur.type == "call":
        chain.append((call_callee_name(cur), cur))
        func = cur.child_by_field_name("function")
        if func is not None and func.type == "attribute":
            cur = func.child_by_field_name("object")
        else:
            break
    return chain


def _builder_resolves(node: Node, bindings: dict[str, str], models: dict) -> bool:
    for name, call in _builder_chain(node):
        if name in _BUILDER_ROOTS:
            return any(_resolve_table_ref(a, bindings, models)[0] for a in call_posargs(call))
    return False


def _builder_kind(node: Node) -> str:
    names = {name for name, _ in _builder_chain(node)}
    return "write" if names & _BUILDER_WRITES else "read"


def _sql_arg_sources(
    node: Node | None,
    resolve,
    imap: ImportMap,
    local_dicts: dict[str, dict[str, ResolvedString]] | None,
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] | None,
) -> list[ResolvedString]:
    """Resolve a SQL argument, expanding module-level SQL dicts."""
    if node is None:
        return []
    local_dicts = local_dicts or {}
    dicts_by_module = dicts_by_module or {}
    if node.type == "subscript":
        obj = node.child_by_field_name("value") or (
            node.named_children[0] if node.named_children else None
        )
        if obj is not None:
            dmap = lookup_dict_from_node(obj, imap, local_dicts, dicts_by_module)
            if dmap:
                res = resolve(node)
                if res.fully_static and res.text:
                    return [res]
                if res.placeholder and res.placeholder.endswith("[*]}"):
                    return [
                        v
                        for v in dmap.values()
                        if v.fully_static and v.text and looks_like_sql(v.text)
                    ]
                if res.fully_static:
                    return [res]
                return [
                    v
                    for v in dmap.values()
                    if v.fully_static and v.text and looks_like_sql(v.text)
                ]
    return [resolve(node)]


def _firestore_refmap(fn_node: Node, resolve) -> dict[str, str]:
    """Map local names to collection paths assigned in this function."""
    refs: dict[str, str] = {}
    for node in walk(fn_node):
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            continue
        name = text(left)
        colls = _collections_in_chain(right, resolve) if right.type == "call" else []
        if not colls and right.type == "identifier" and text(right) in refs:
            refs[name] = refs[text(right)]
            continue
        if not colls and right.type == "call":
            func = right.child_by_field_name("function")
            if func is not None and func.type == "attribute" and func.named_children:
                obj = func.named_children[0]
                if obj.type == "identifier" and text(obj) in refs:
                    colls = [refs[text(obj)]]
        if colls:
            refs[name] = colls[-1]
    return refs


def _param_names(fn_node: Node) -> list[str]:
    params = fn_node.child_by_field_name("parameters")
    if params is None:
        return []
    names: list[str] = []
    for p in params.named_children:
        ident = None
        if p.type == "identifier":
            ident = text(p)
        elif p.type in {"typed_parameter", "default_parameter", "typed_default_parameter"}:
            name_n = p.child_by_field_name("name")
            if name_n is None and p.named_children:
                name_n = p.named_children[0]
            ident = text(name_n) if name_n is not None else ""
        if ident and ident not in {"self", "cls", "*args", "**kwargs"} and not ident.startswith("*"):
            names.append(ident)
    return names


def _class_field_names(class_node: Node) -> list[str]:
    body = class_node.child_by_field_name("body")
    if body is None:
        return []
    names: list[str] = []
    for stmt in body.named_children:
        assign = None
        if stmt.type == "expression_statement" and stmt.named_children:
            inner = stmt.named_children[0]
            if inner.type == "assignment":
                assign = inner
        if assign is None:
            continue
        left = assign.child_by_field_name("left")
        lname = text(left) if left is not None and left.type == "identifier" else ""
        if lname and not lname.startswith("_"):
            names.append(lname)
    return names


def _ann_is_callable(ann: Node | None) -> bool:
    if ann is None:
        return False
    raw = text(ann)
    return "Callable" in raw or raw in {"callable", "FunctionType"}


def _class_callable_fields(class_node: Node) -> set[str]:
    """Dataclass / annotated fields whose type is Callable."""
    body = class_node.child_by_field_name("body")
    if body is None:
        return set()
    names: set[str] = set()
    for stmt in body.named_children:
        assign = None
        if stmt.type == "expression_statement" and stmt.named_children:
            inner = stmt.named_children[0]
            if inner.type == "assignment":
                assign = inner
        if assign is None:
            continue
        left = assign.child_by_field_name("left")
        lname = text(left) if left is not None and left.type == "identifier" else ""
        if not lname or lname.startswith("_"):
            continue
        typ = assign.child_by_field_name("type")
        if typ is None:
            for child in assign.named_children:
                if child is left:
                    continue
                if child.type in {"type", "identifier", "subscript", "attribute"}:
                    typ = child
                    break
        if _ann_is_callable(typ):
            names.add(lname)
    return names


def _fn_by_name(analysis: PyAnalysis) -> dict[str, list[PyFunction]]:
    by_name: dict[str, list[PyFunction]] = {}
    for fn in analysis.functions.values():
        by_name.setdefault(fn.name, []).append(fn)
        by_name.setdefault(fn.qualname, []).append(fn)
    return by_name


def _unique_fn(matches: list[PyFunction], exclude: str | None = None) -> PyFunction | None:
    uniq = [f for f in matches if f.qualname != exclude]
    if len(uniq) == 1:
        return uniq[0]
    return None


def _index_class_fields(
    trees: list[tuple[str, str, Node]] | None,
    analysis: PyAnalysis,
) -> tuple[dict[str, list[str]], dict[str, Node], dict[str, set[str]]]:
    class_fields: dict[str, list[str]] = {}
    class_nodes: dict[str, Node] = {}
    callable_fields: dict[str, set[str]] = {}

    def take(node: Node) -> None:
        cname = text(node.child_by_field_name("name"))
        if not cname or cname in class_fields:
            return
        class_fields[cname] = _class_field_names(node)
        class_nodes[cname] = node
        callable_fields[cname] = _class_callable_fields(node)

    if trees:
        for _module, _rel_file, root in trees:
            for node in walk(root):
                if node.type == "class_definition":
                    take(node)
        return class_fields, class_nodes, callable_fields
    for fn in analysis.functions.values():
        cur = fn.node.parent
        while cur is not None:
            if cur.type == "class_definition":
                take(cur)
                break
            cur = cur.parent
    return class_fields, class_nodes, callable_fields


def _resolve_callable_injection(
    analysis: PyAnalysis,
    trees: list[tuple[str, str, Node]] | None = None,
) -> None:
    """Link calls through injected callables (args, dataclass fields, aliases)."""
    by_name = _fn_by_name(analysis)
    existing = {(c, d) for c, d, _e, _k in analysis.calls}

    param_of: dict[str, list[str]] = {fn.qualname: _param_names(fn.node) for fn in analysis.functions.values()}
    class_fields, class_nodes, callable_fields = _index_class_fields(trees, analysis)

    # (caller, callee, param_index_or_name, target_qualname)
    passed: list[tuple[str, str, str, str]] = []
    aliases: dict[tuple[str, str], str] = {}  # (function, local) -> target qualname
    ctor_fields: list[tuple[str, str, str]] = []  # (class, field, fn_qualname)

    for fn in analysis.functions.values():
        for node in walk(fn.node):
            if node.type == "assignment":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if left is None or right is None:
                    continue
                if left.type == "identifier" and right.type == "identifier":
                    target = _unique_fn(by_name.get(text(right)) or [], fn.qualname)
                    if target:
                        aliases[(fn.qualname, text(left))] = target.qualname

    def _record_ctor(node: Node, caller: str) -> None:
        callee = call_callee_name(node)
        pos = call_posargs(node)
        kw = call_kwargs(node)
        if callee in class_fields:
            fields = class_fields[callee]
            for i, arg in enumerate(pos):
                if arg.type != "identifier" or i >= len(fields):
                    continue
                target = _unique_fn(by_name.get(text(arg)) or [], caller)
                if target:
                    ctor_fields.append((callee, fields[i], target.qualname))
                    passed.append((caller, callee, fields[i], target.qualname))
            for k, arg in kw.items():
                if arg.type != "identifier":
                    continue
                target = _unique_fn(by_name.get(text(arg)) or [], caller)
                if target:
                    ctor_fields.append((callee, k, target.qualname))
                    passed.append((caller, callee, k, target.qualname))
        targets_fn = by_name.get(callee) or []
        uniq = _unique_fn(targets_fn)
        if uniq is None and len(targets_fn) == 1:
            uniq = targets_fn[0]
        if uniq is None:
            return
        params = param_of.get(uniq.qualname) or []
        for i, arg in enumerate(pos):
            if arg.type != "identifier" or i >= len(params):
                continue
            target = _unique_fn(by_name.get(text(arg)) or [], caller)
            if target:
                passed.append((caller, uniq.qualname, params[i], target.qualname))
        for k, arg in kw.items():
            if arg.type != "identifier":
                continue
            target = _unique_fn(by_name.get(text(arg)) or [], caller)
            if target:
                passed.append((caller, uniq.qualname, k, target.qualname))

    if trees:
        for module, _rel_file, root in trees:
            for node in walk(root):
                if node.type == "call":
                    _record_ctor(node, module)
    else:
        for fn in analysis.functions.values():
            for node in walk(fn.node):
                if node.type == "call":
                    _record_ctor(node, fn.qualname)

    param_targets: dict[tuple[str, str], set[str]] = {}
    for _caller, callee, param, target in passed:
        param_targets.setdefault((callee, param), set()).add(target)
    field_targets: dict[tuple[str, str], set[str]] = {}
    for cls, field_name, target in ctor_fields:
        field_targets.setdefault((cls, field_name), set()).add(target)

    init_maps: dict[str, dict[str, str]] = {
        cname: _init_self_fields(node) for cname, node in class_nodes.items()
    }
    param_ann: dict[str, dict[str, str]] = {
        fn.qualname: _param_annotations(fn.node) for fn in analysis.functions.values()
    }

    def link_unique(field_name: str) -> str | None:
        hit = _unique_fn(by_name.get(field_name) or [])
        return hit.qualname if hit else None

    def _node_key(node: Node) -> tuple[int, int, str]:
        return (node.start_byte, node.end_byte, node.type)

    by_node = {_node_key(fn.node): fn for fn in analysis.functions.values()}

    def enclosing_fns(fn: PyFunction) -> list[PyFunction]:
        out: list[PyFunction] = []
        cur = fn.node.parent
        while cur is not None:
            if cur.type == "function_definition":
                hit = by_node.get(_node_key(cur))
                if hit is not None:
                    out.append(hit)
            cur = cur.parent
        return out

    new_calls: list[tuple[str, str, Evidence, Confidence]] = []
    for fn in analysis.functions.values():
        params = set(param_of.get(fn.qualname) or [])
        anns = dict(param_ann.get(fn.qualname) or {})
        for enc in enclosing_fns(fn):
            params.update(param_of.get(enc.qualname) or [])
            anns.update(param_ann.get(enc.qualname) or {})
        for node in walk(fn.node):
            if node.type != "call":
                continue
            owner = _enclosing_fn_node(node)
            if owner is not None and _node_key(owner) != _node_key(fn.node):
                continue
            method = call_callee_name(node)
            func = node.child_by_field_name("function")
            evidence = Evidence(file=fn.file, line=line_of(node), symbol=fn.qualname)
            # fn() where fn is alias or param
            if func is not None and func.type == "identifier":
                name = text(func)
                alias = aliases.get((fn.qualname, name))
                if alias and (fn.qualname, alias) not in existing:
                    new_calls.append((fn.qualname, alias, evidence, Confidence.INFERRED))
                    existing.add((fn.qualname, alias))
                if name in params:
                    for target in param_targets.get((fn.qualname, name), ()):
                        if (fn.qualname, target) not in existing:
                            new_calls.append((fn.qualname, target, evidence, Confidence.INFERRED))
                            existing.add((fn.qualname, target))
                    uniq = link_unique(name)
                    if uniq and (fn.qualname, uniq) not in existing:
                        new_calls.append((fn.qualname, uniq, evidence, Confidence.INFERRED))
                        existing.add((fn.qualname, uniq))
            # obj.field() dataclass / injected field
            if func is not None and func.type == "attribute" and func.named_children:
                field = method
                obj = func.named_children[0]
                obj_name = text(obj) if obj.type == "identifier" else ""
                if obj.type == "attribute" and obj.named_children:
                    tail = text(obj.named_children[-1]) if len(obj.named_children) >= 2 else ""
                    # app.state.deps.fetch() / request.app.state.deps.fetch()
                    if tail and (
                        tail in params
                        or tail in anns
                        or any(tail == fld for _cls, fld in field_targets)
                    ):
                        obj_name = tail
                targets: set[str] = set()
                if obj_name == "self" and fn.class_name:
                    targets.update(field_targets.get((fn.class_name, field), ()))
                    param = (init_maps.get(fn.class_name) or {}).get(field)
                    if param:
                        targets.update(param_targets.get((fn.qualname, param), ()))
                        uniq = link_unique(param)
                        if uniq:
                            targets.add(uniq)
                    if field in (callable_fields.get(fn.class_name) or set()):
                        uniq = link_unique(field)
                        if uniq:
                            targets.add(uniq)
                    uniq = link_unique(field)
                    if uniq:
                        targets.add(uniq)
                if obj_name in params or obj_name in anns:
                    # svc.save() / deps.run_query() — injected field on a typed param
                    for (_cls, fld), fld_targets in field_targets.items():
                        if fld != field:
                            continue
                        targets.update(fld_targets)
                    typ = anns.get(obj_name)
                    known_fields = set(callable_fields.get(typ) or ())
                    known_fields.update(class_fields.get(typ) or ())
                    if not typ:
                        for fields in callable_fields.values():
                            known_fields.update(fields)
                    if field in known_fields or typ in class_fields or typ in callable_fields:
                        uniq = link_unique(field)
                        if uniq:
                            targets.add(uniq)
                callable_on_self = (
                    obj_name == "self"
                    and fn.class_name
                    and field in (callable_fields.get(fn.class_name) or set())
                )
                for target in sorted(targets):
                    if (fn.qualname, target) not in existing:
                        new_calls.append((fn.qualname, target, evidence, Confidence.INFERRED))
                        existing.add((fn.qualname, target))
                if callable_on_self and not targets:
                    analysis.unresolved_calls.append(
                        PyUnresolvedCall(
                            caller=fn.qualname,
                            callee=f"{fn.class_name}.{field}",
                            evidence=evidence,
                        )
                    )
                typ = anns.get(obj_name)
                class_methods = {
                    f.name for f in analysis.functions.values() if f.class_name == typ
                }
                if (
                    not targets
                    and typ
                    and typ in callable_fields
                    and field not in class_methods
                    and field not in {"__init__", "__new__", "__post_init__"}
                ):
                    analysis.unresolved_calls.append(
                        PyUnresolvedCall(
                            caller=fn.qualname,
                            callee=f"{typ}.{field}",
                            evidence=evidence,
                        )
                    )
    analysis.calls.extend(new_calls)


def _annotation_leaf(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in {"identifier", "type"}:
        raw = text(node)
        return raw.split(".")[-1] if raw else None
    if node.type == "attribute" and node.named_children:
        return text(node.named_children[-1])
    if node.type == "subscript":
        inner = node.child_by_field_name("value") or (
            node.named_children[0] if node.named_children else None
        )
        return _annotation_leaf(inner)
    if node.type == "binary_operator" and node.named_children:
        for child in node.named_children:
            leaf = _annotation_leaf(child)
            if leaf and leaf not in {"None", "NoneType"}:
                return leaf
    if node.named_children:
        return _annotation_leaf(node.named_children[0])
    raw = text(node).split("|")[0].strip()
    return raw.split(".")[-1] if raw else None


def _param_annotations(fn_node: Node) -> dict[str, str]:
    params = fn_node.child_by_field_name("parameters")
    if params is None:
        return {}
    out: dict[str, str] = {}
    for p in params.named_children:
        name = ""
        ann = None
        if p.type == "typed_parameter" or p.type == "typed_default_parameter":
            name_n = p.child_by_field_name("name") or (
                p.named_children[0] if p.named_children else None
            )
            name = text(name_n) if name_n is not None else ""
            ann = p.child_by_field_name("type")
            if ann is None:
                for c in p.named_children:
                    if c.type not in {"identifier", "default_parameter"} and c is not name_n:
                        if c.type in {
                            "type",
                            "identifier",
                            "attribute",
                            "subscript",
                            "binary_operator",
                        }:
                            ann = c
                            break
        if name and name not in {"self", "cls"}:
            leaf = _annotation_leaf(ann)
            if leaf:
                out[name] = leaf
    return out


def _class_bases(class_node: Node) -> set[str]:
    names: set[str] = set()
    for child in class_node.named_children:
        if child.type == "argument_list":
            for arg in child.named_children:
                leaf = _annotation_leaf(arg)
                if leaf:
                    names.add(leaf)
    return names


def _init_self_fields(class_node: Node) -> dict[str, str]:
    """Map ``self.field`` -> ``__init__`` param assigned to it."""
    body = class_node.child_by_field_name("body")
    if body is None:
        return {}
    init = None
    for stmt in body.named_children:
        fn = stmt
        if stmt.type == "decorated_definition":
            fn = next((c for c in stmt.named_children if c.type == "function_definition"), stmt)
        if fn.type == "function_definition" and text(fn.child_by_field_name("name")) == "__init__":
            init = fn
            break
    if init is None:
        return {}
    params = set(_param_names(init))
    fields: dict[str, str] = {}
    for node in walk(init):
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            continue
        if left.type == "attribute" and left.named_children:
            obj = left.named_children[0]
            field = text(left.named_children[-1]) if len(left.named_children) >= 2 else ""
            if text(obj) != "self" or not field:
                continue
            if right.type == "identifier" and text(right) in params:
                fields[field] = text(right)
            elif right.type == "call":
                fields[field] = call_callee_name(right)
    return fields


def _call_type_name(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "call":
        return call_callee_name(node)
    if node.type == "identifier":
        return text(node)
    return None


def _resolve_object_dispatch(
    analysis: PyAnalysis,
    trees: list[tuple[str, str, Node]] | None = None,
) -> None:
    """Follow ``self.outbox.settle()`` / ``store.save()`` to class methods."""
    methods: dict[str, dict[str, PyFunction]] = {}
    class_nodes: dict[str, Node] = {}
    protocol_names: set[str] = set()
    for fn in analysis.functions.values():
        if not fn.class_name:
            continue
        methods.setdefault(fn.class_name, {})[fn.name] = fn

    def take_class(node: Node) -> None:
        cname = text(node.child_by_field_name("name"))
        if not cname:
            return
        class_nodes[cname] = node
        bases = _class_bases(node)
        if bases & {"Protocol", "ABC", "ABCMeta"}:
            protocol_names.add(cname)

    if trees:
        for _module, _rel_file, root in trees:
            for node in walk(root):
                if node.type == "class_definition":
                    take_class(node)
    else:
        for fn in analysis.functions.values():
            if not fn.class_name:
                continue
            cur = fn.node.parent
            while cur is not None:
                if cur.type == "class_definition":
                    take_class(cur)
                    break
                cur = cur.parent

    field_types: dict[tuple[str, str], set[str]] = {}
    for cname, node in class_nodes.items():
        for fname, src in _init_self_fields(node).items():
            if src in methods:
                field_types.setdefault((cname, fname), set()).add(src)

    param_types: dict[tuple[str, str], set[str]] = {}
    for fn in analysis.functions.values():
        for name, typ in _param_annotations(fn.node).items():
            param_types.setdefault((fn.qualname, name), set()).add(typ)

    # Constructor call-sites: Drainer(Outbox()) / Drainer(outbox=box)
    local_types: dict[tuple[str, str], str] = {}
    module_instances: dict[tuple[str, str], str] = {}
    if trees:
        for module, rel_file, root in trees:
            for node in walk(root):
                if node.type != "assignment":
                    continue
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if left is None or right is None or left.type != "identifier":
                    continue
                tname = _call_type_name(right)
                if tname and tname in methods:
                    module_instances[(module, text(left))] = tname
                    module_instances[(rel_file, text(left))] = tname
    for fn in analysis.functions.values():
        for node in walk(fn.node):
            if node.type == "assignment":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if left is not None and left.type == "identifier" and right is not None:
                    tname = _call_type_name(right)
                    if tname and tname in methods:
                        local_types[(fn.qualname, text(left))] = tname
            if node.type != "call":
                continue
            callee = call_callee_name(node)
            if callee not in class_nodes:
                continue
            fields = _init_self_fields(class_nodes[callee])
            pos = call_posargs(node)
            kw = call_kwargs(node)
            # Map positional args onto init fields (field <- param).
            param_to_field = {v: k for k, v in fields.items()}
            init_fn = None
            body = class_nodes[callee].child_by_field_name("body")
            if body is not None:
                for stmt in body.named_children:
                    cand = stmt
                    if stmt.type == "decorated_definition":
                        cand = next(
                            (c for c in stmt.named_children if c.type == "function_definition"),
                            stmt,
                        )
                    if cand.type == "function_definition" and text(cand.child_by_field_name("name")) == "__init__":
                        init_fn = cand
                        break
            init_params = _param_names(init_fn) if init_fn is not None else []
            for i, arg in enumerate(pos):
                tname = _call_type_name(arg)
                if not tname:
                    if arg.type == "identifier":
                        tname = local_types.get((fn.qualname, text(arg)))
                if not tname:
                    continue
                if tname not in methods and tname not in class_nodes:
                    continue
                pname = init_params[i] if i < len(init_params) else None
                field = param_to_field.get(pname or "") if pname else None
                if field:
                    field_types.setdefault((callee, field), set()).add(tname)
            for k, arg in kw.items():
                tname = _call_type_name(arg)
                if not tname and arg.type == "identifier":
                    tname = local_types.get((fn.qualname, text(arg)))
                if not tname:
                    continue
                field = k if k in fields else param_to_field.get(k)
                if field:
                    field_types.setdefault((callee, field), set()).add(tname)

    def implementers_of(type_name: str) -> list[str]:
        if type_name in methods and type_name not in protocol_names:
            return [type_name]
        if type_name not in protocol_names and type_name in methods:
            return [type_name]
        proto_meths = set(methods.get(type_name, {})) - {"__init__", "__new__"}
        if not proto_meths:
            hits = sorted(c for c in methods if type_name in methods[c] or c == type_name)
            return hits[:1] if len(hits) == 1 else []
        impls: list[str] = []
        for cname, meths in methods.items():
            if cname == type_name or cname in protocol_names:
                continue
            if proto_meths <= set(meths):
                impls.append(cname)
        return sorted(impls)

    existing = {(c, d) for c, d, _e, _k in analysis.calls}
    new_calls: list[tuple[str, str, Evidence, Confidence]] = []

    def add_call(caller: str, callee: str, evidence: Evidence) -> None:
        if caller == callee or (caller, callee) in existing:
            return
        new_calls.append((caller, callee, evidence, Confidence.INFERRED))
        existing.add((caller, callee))

    for fn in analysis.functions.values():
        params = set(_param_names(fn.node))
        anns = _param_annotations(fn.node)
        for node in walk(fn.node):
            if node.type != "call":
                continue
            func = node.child_by_field_name("function")
            if func is None or func.type != "attribute" or not func.named_children:
                continue
            obj = func.named_children[0]
            method = call_callee_name(node)
            evidence = Evidence(file=fn.file, line=line_of(node), symbol=fn.qualname)
            types: set[str] = set()
            if obj.type == "identifier":
                oname = text(obj)
                if oname == "self" and fn.class_name:
                    continue  # same-class methods already recorded
                if oname in anns:
                    types.update(implementers_of(anns[oname]) or [anns[oname]])
                elif oname in params:
                    types.update(param_types.get((fn.qualname, oname), set()))
                loc = local_types.get((fn.qualname, oname))
                if loc:
                    types.add(loc)
                inst = module_instances.get((fn.module, oname)) or module_instances.get(
                    (fn.file, oname)
                )
                if inst:
                    types.add(inst)
            elif obj.type == "attribute" and obj.named_children:
                head = obj.named_children[0]
                field = text(obj.named_children[-1]) if len(obj.named_children) >= 2 else ""
                if text(head) == "self" and fn.class_name and field:
                    types.update(field_types.get((fn.class_name, field), set()))
            linked = False
            for tname in sorted(types):
                cal = methods.get(tname, {}).get(method)
                if cal is not None:
                    add_call(fn.qualname, cal.qualname, evidence)
                    linked = True
                    continue
                for impl in implementers_of(tname):
                    cal = methods.get(impl, {}).get(method)
                    if cal is not None:
                        add_call(fn.qualname, cal.qualname, evidence)
                        linked = True
            if types and not linked:
                labels = [f"{tname}.{method}" for tname in sorted(types)]
                seen_labels = {u.callee for u in analysis.unresolved_calls if u.caller == fn.qualname}
                for label in labels:
                    if label in seen_labels:
                        continue
                    analysis.unresolved_calls.append(
                        PyUnresolvedCall(
                            caller=fn.qualname,
                            callee=label,
                            evidence=evidence,
                        )
                    )
                    seen_labels.add(label)
    analysis.calls.extend(new_calls)


def _class_field_env(
    tree: Node,
    local: dict[str, ResolvedString],
    imap: ImportMap,
    consts_by_module: dict[str, dict[str, ResolvedString]],
) -> dict[str, ResolvedString]:
    """Class-level and ``self.x =`` string fields (base_url from env/const)."""
    out: dict[str, ResolvedString] = {}

    def resolve(node: Node | None) -> ResolvedString:
        env = {**local, **out}
        return resolve_expr(node, env, env, imap, consts_by_module)

    for node in walk(tree):
        if node.type != "class_definition":
            continue
        body = node.child_by_field_name("body")
        if body is None:
            continue
        for stmt in body.named_children:
            assign = None
            fn_node = None
            if stmt.type == "expression_statement" and stmt.named_children:
                inner = stmt.named_children[0]
                if inner.type == "assignment":
                    assign = inner
            elif stmt.type == "function_definition":
                fn_node = stmt
            if assign is not None:
                left = assign.child_by_field_name("left")
                right = assign.child_by_field_name("right")
                if left is not None and left.type == "identifier" and right is not None:
                    res = resolve(right)
                    if res.usable:
                        out[text(left)] = res
                        out[f"self.{text(left)}"] = res
            if fn_node is not None:
                for inner in walk(fn_node):
                    if inner.type != "assignment":
                        continue
                    left = inner.child_by_field_name("left")
                    right = inner.child_by_field_name("right")
                    if left is None or right is None or left.type != "attribute":
                        continue
                    if text(left).startswith("self."):
                        res = resolve(right)
                        field = text(left)
                        leaf = field.split(".", 1)[-1]
                        if leaf.lower() in HTTP_BASE_FIELDS and (
                            not res.usable or _placeholder_is_local_base(res)
                        ):
                            res = ResolvedString(None, False, True, placeholder="{base}")
                        if res.usable:
                            out[field] = res
                            out[leaf] = res
    return out


def _collect_protocols(parsed) -> dict[str, set[str]]:
    """Class name → method names for ``class X(Protocol)``."""
    out: dict[str, set[str]] = {}
    for _path, _rf, _src, tree, _module, _imap, _fns in parsed:
        for node in walk(tree):
            if node.type != "class_definition":
                continue
            supers = node.child_by_field_name("superclasses")
            if supers is None or "Protocol" not in text(supers):
                continue
            cname = text(node.child_by_field_name("name"))
            if not cname:
                continue
            meths: set[str] = set()
            body = node.child_by_field_name("body")
            if body is not None:
                for stmt in body.named_children:
                    fn_node = stmt
                    if stmt.type == "decorated_definition":
                        fn_node = next(
                            (c for c in stmt.named_children if c.type == "function_definition"),
                            None,
                        ) or stmt
                    if fn_node.type == "function_definition":
                        name = text(fn_node.child_by_field_name("name"))
                        if name and name not in {"__init__", "__new__"}:
                            meths.add(name)
            out[cname] = meths
    return out


def _protocol_implementers(parsed, protocols: dict[str, set[str]]) -> dict[str, list[str]]:
    """Protocol name → concrete class names that define its methods."""
    if not protocols:
        return {}
    class_methods: dict[str, set[str]] = {}
    protocol_names = set(protocols)
    for _path, _rf, _src, tree, _module, _imap, _fns in parsed:
        for node in walk(tree):
            if node.type != "class_definition":
                continue
            cname = text(node.child_by_field_name("name"))
            if not cname or cname in protocol_names:
                continue
            supers = node.child_by_field_name("superclasses")
            if supers is not None and "Protocol" in text(supers):
                continue
            meths: set[str] = set()
            body = node.child_by_field_name("body")
            if body is None:
                continue
            for stmt in body.named_children:
                fn_node = stmt
                if stmt.type == "decorated_definition":
                    fn_node = next(
                        (c for c in stmt.named_children if c.type == "function_definition"),
                        None,
                    ) or stmt
                if fn_node.type == "function_definition":
                    name = text(fn_node.child_by_field_name("name"))
                    if name:
                        meths.add(name)
            class_methods[cname] = meths
    impls: dict[str, list[str]] = {p: [] for p in protocols}
    for proto, wanted in protocols.items():
        if not wanted:
            continue
        for cname, meths in class_methods.items():
            if wanted <= meths:
                impls[proto].append(cname)
    for proto in impls:
        impls[proto] = sorted(impls[proto])
    return impls


def _param_protocol(fn: PyFunction, param: str, protocols: dict[str, set[str]]) -> str | None:
    """Return the Protocol name of a typed parameter, if any."""
    if not protocols:
        return None
    params = fn.node.child_by_field_name("parameters")
    if params is None:
        return None
    for p in params.named_children:
        name = ""
        type_txt = ""
        if p.type == "typed_parameter":
            name_n = p.child_by_field_name("name") or (
                p.named_children[0] if p.named_children else None
            )
            type_n = p.child_by_field_name("type")
            name = text(name_n) if name_n is not None else ""
            type_txt = text(type_n) if type_n is not None else ""
        elif p.type == "typed_default_parameter":
            name_n = p.child_by_field_name("name") or (
                p.named_children[0] if p.named_children else None
            )
            type_n = p.child_by_field_name("type")
            name = text(name_n) if name_n is not None else ""
            type_txt = text(type_n) if type_n is not None else ""
        if name == param:
            leaf = type_txt.split(".")[-1].strip()
            if leaf in protocols:
                return leaf
    return None



# ---------------------------------------------------------------------------
# SQL by reference: a function that names a SQL constant / dict entry reads or
# writes its tables, even when another (injected) function executes it.
# ---------------------------------------------------------------------------

_SQL_REF_NOTE = "sql referenced here; executed by another function"


def _sql_text(value: ResolvedString | None) -> str | None:
    if value is None or not value.fully_static or value.dynamic:
        return None
    raw = value.text
    if not raw or not looks_like_sql(raw):
        return None
    return raw


def _fn_range_index(fns: list[PyFunction]) -> dict[tuple[int, int], PyFunction]:
    out: dict[tuple[int, int], PyFunction] = {}
    for fn in fns:
        node = fn.node
        out[(node.start_byte, node.end_byte)] = fn
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None:
                out[(inner.start_byte, inner.end_byte)] = fn
    return out


def _sql_key_index(
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]],
) -> dict[str, str]:
    """String key → SQL, for keys that appear in exactly one SQL dict entry."""
    seen: dict[str, list[str]] = {}
    for dicts in dicts_by_module.values():
        for entries in dicts.values():
            for key, value in entries.items():
                sql = _sql_text(value)
                if sql is None:
                    continue
                seen.setdefault(key, [])
                if sql not in seen[key]:
                    seen[key].append(sql)
    return {
        k: v[0]
        for k, v in seen.items()
        if len(v) == 1 and len(k) >= 5 and any(ch in k for ch in "_.-:")
    }


def _norm_sql(sql: str) -> str:
    return " ".join(sql.split()).lower()


_SQL_KEY_SKIP_CALLS = {
    "print",
    "debug",
    "info",
    "warning",
    "warn",
    "error",
    "exception",
    "critical",
    "log",
    "getenv",
    "getattr",
    "hasattr",
    "setattr",
}


def _reachable_sql(
    analysis: PyAnalysis,
    start: str,
    sites_by_fn: dict[str, set[str]],
    callees: dict[str, set[str]],
    limit: int = 8,
) -> set[str]:
    out: set[str] = set(sites_by_fn.get(start, set()))
    frontier = [start]
    seen = {start}
    depth = 0
    while frontier and depth < limit:
        nxt: list[str] = []
        for qn in frontier:
            for callee in callees.get(qn, ()):
                if callee in seen:
                    continue
                seen.add(callee)
                out |= sites_by_fn.get(callee, set())
                nxt.append(callee)
        frontier = nxt
        depth += 1
    return out


def _link_sql_references(
    analysis: PyAnalysis,
    parsed: list,
    consts_by_module: dict[str, dict[str, ResolvedString]],
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]],
) -> None:
    """Attribute SQL to the function that *names* it (``inferred``, ``via: sql_reference``).

    Covers SQL kept in module constants or dicts (``Q["sales"]``,
    ``queries.Q["sales"]``, ``SALES_SQL``, ``sql.SALES``) and passed to an
    executor the detectors cannot resolve (dependency injection, a callable
    from another module, a generic ``run(...)``). A string argument equal to a
    key of exactly one SQL dict (``rpc("sales_by_day")``) counts as well
    (``via: sql_key``). Skipped when the function already executes that SQL
    itself or reaches a function that does through the call graph.
    """
    sites_by_fn: dict[str, set[str]] = {}
    for site in analysis.sql_sites:
        sites_by_fn.setdefault(site.function.qualname, set()).add(_norm_sql(site.sql))
    callees: dict[str, set[str]] = {}
    for caller, callee, _ev, _conf in analysis.calls:
        callees.setdefault(caller, set()).add(callee)
    key_index = _sql_key_index(dicts_by_module)
    found: list[tuple[PyFunction, str, Evidence, str]] = []

    for _path, rel_file, _src, tree, module, imap, fns in parsed:
        by_range = _fn_range_index(fns)
        local_consts = consts_by_module.get(module) or {}
        local_dicts = dicts_by_module.get(module) or {}

        def const_sql(name: str, *, imap=imap, local_consts=local_consts) -> str | None:
            if name in imap.from_names:
                mod, orig = imap.from_names[name]
                hit = _sql_text((consts_by_module.get(mod) or {}).get(orig))
                if hit:
                    return hit
            return _sql_text(local_consts.get(name))

        def dict_entries(expr: Node, *, imap=imap, local_dicts=local_dicts):
            if expr.type == "identifier":
                name = text(expr)
                if name in local_dicts:
                    return local_dicts[name]
                if name in imap.from_names:
                    mod, orig = imap.from_names[name]
                    return (dicts_by_module.get(mod) or {}).get(orig)
                return None
            if expr.type == "attribute":
                obj = expr.child_by_field_name("object")
                attr = expr.child_by_field_name("attribute")
                if obj is None or attr is None or obj.type != "identifier":
                    return None
                mod = imap.modules.get(text(obj))
                if mod is None and text(obj) in imap.from_names:
                    src_mod, orig = imap.from_names[text(obj)]
                    mod = f"{src_mod}.{orig}"
                if mod is None:
                    return None
                return (dicts_by_module.get(mod) or {}).get(text(attr))
            return None

        for node in walk(tree):
            sql: str | None = None
            via = "sql_reference"
            if node.type == "subscript":
                value = node.child_by_field_name("value")
                sub = node.child_by_field_name("subscript")
                entries = dict_entries(value) if value is not None else None
                if entries and sub is not None and sub.type == "string":
                    sql = _sql_text(entries.get(unquote(text(sub))))
            elif node.type == "call" and call_callee_name(node) == "get":
                fn_node = node.child_by_field_name("function")
                obj = fn_node.child_by_field_name("object") if fn_node is not None else None
                args = call_args(node)
                entries = dict_entries(obj) if obj is not None else None
                if entries and args and args[0].type == "string":
                    sql = _sql_text(entries.get(unquote(text(args[0]))))
            elif node.type == "attribute":
                obj = node.child_by_field_name("object")
                attr = node.child_by_field_name("attribute")
                if obj is not None and attr is not None and obj.type == "identifier":
                    mod = imap.modules.get(text(obj))
                    if mod is None and text(obj) in imap.from_names:
                        src_mod, orig = imap.from_names[text(obj)]
                        mod = f"{src_mod}.{orig}"
                    if mod:
                        sql = _sql_text((consts_by_module.get(mod) or {}).get(text(attr)))
            elif node.type == "identifier":
                parent = parent_of(node)
                if parent is not None and parent.type in {"attribute", "keyword_argument"}:
                    if parent.type == "attribute" or parent.child_by_field_name("name") == node:
                        continue
                if parent is not None and parent.type in {
                    "function_definition",
                    "class_definition",
                    "parameters",
                    "default_parameter",
                    "typed_parameter",
                    "import_from_statement",
                    "dotted_name",
                    "aliased_import",
                }:
                    continue
                if parent is not None and parent.type == "assignment" and parent.child_by_field_name("left") == node:
                    continue
                sql = const_sql(text(node))
            elif (
                node.type == "string"
                and (up := parent_of(node)) is not None
                and up.type == "argument_list"
                and (call_node := parent_of(up)) is not None
                and call_callee_name(call_node) not in _SQL_KEY_SKIP_CALLS
            ):
                key = unquote(text(node))
                if key in key_index:
                    sql = key_index[key]
                    via = "sql_key"
            if not sql:
                continue
            fdef = _enclosing_fn_node(node)
            if fdef is None:
                continue
            fn = by_range.get((fdef.start_byte, fdef.end_byte))
            if fn is None:
                continue
            ev = Evidence(file=rel_file, line=line_of(node), symbol=fn.qualname)
            found.append((fn, sql, ev, via))

    added: set[tuple[str, str]] = set()
    for fn, sql, ev, via in found:
        key = (fn.qualname, sql)
        if key in added:
            continue
        if _norm_sql(sql) in _reachable_sql(analysis, fn.qualname, sites_by_fn, callees):
            continue
        added.add(key)
        analysis.sql_sites.append(
            PySqlSite(
                function=fn,
                sql=sql,
                dynamic=False,
                evidence=ev,
                note=_SQL_REF_NOTE,
                confidence=Confidence.INFERRED,
                via=via,
            )
        )



# ---------------------------------------------------------------------------
# Function tables: `BUILDERS = {"x": _build_x, ...}; BUILDERS[kind](...)`.
# A call through a dict/list of functions calls every function in it
# (`inferred`), so lineage reaches the tables each one touches.
# ---------------------------------------------------------------------------


def _fn_ref(
    node: Node,
    module: str,
    imap: ImportMap,
    functions: dict,
    self_class: str | None = None,
) -> str | None:
    if node.type == "identifier":
        name = text(node)
        local = f"{module}.{name}"
        if local in functions:
            return local
        if name in imap.from_names:
            mod, orig = imap.from_names[name]
            target = f"{mod}.{orig}"
            if target in functions:
                return target
        return None
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and attr is not None and obj.type == "identifier":
            oname, aname = text(obj), text(attr)
            mod = imap.modules.get(oname)
            if mod and f"{mod}.{aname}" in functions:
                return f"{mod}.{aname}"
            if mod and _PY_REEXPORTS.get(f"{mod}.{aname}") in functions:
                # `import media_utils; media_utils.encode(...)` re-exported by __init__
                return _PY_REEXPORTS[f"{mod}.{aname}"]
            # `from pkg import module` + `module.fn`
            if oname in imap.from_names:
                src_mod, orig = imap.from_names[oname]
                target = f"{src_mod}.{orig}.{aname}"
                if target in functions:
                    return target
            if oname in {"self", "cls"} and self_class:
                target = f"{module}.{self_class}.{aname}"
                if target in functions:
                    return target
        return None
    if node.type == "call" and call_callee_name(node) == "partial":
        pos = call_posargs(node)
        return _fn_ref(pos[0], module, imap, functions, self_class) if pos else None
    return None


def _table_members(value: Node, module: str, imap: ImportMap, functions: dict) -> list[str]:
    elems: list[Node] = []
    if value.type == "dictionary":
        for pair in value.named_children:
            if pair.type == "pair":
                v = pair.child_by_field_name("value")
                if v is not None:
                    elems.append(v)
    elif value.type in {"list", "tuple", "set"}:
        elems = list(value.named_children)
    out: list[str] = []
    for el in elems:
        ref = _fn_ref(el, module, imap, functions)
        if ref and ref not in out:
            out.append(ref)
    return out


def _link_function_tables(analysis: PyAnalysis, parsed: list) -> None:
    functions = analysis.functions
    tables: dict[str, dict[str, list[str]]] = {}
    for _path, _rf, _src, tree, module, imap, _fns in parsed:
        local: dict[str, list[str]] = {}
        for name, value in collect_module_assignments(tree):
            members = _table_members(value, module, imap, functions)
            if members:
                local[name] = members
        tables[module] = local

    existing = {(c, d) for c, d, _e, _k in analysis.calls}

    for _path, rel_file, _src, _tree, module, imap, fns in parsed:
        visible = dict(tables.get(module) or {})
        for alias, (mod, orig) in imap.from_names.items():
            if orig in (tables.get(mod) or {}):
                visible[alias] = tables[mod][orig]

        def table_of(node: Node | None, *, visible=visible, imap=imap) -> list[str] | None:
            if node is None:
                return None
            if node.type == "identifier":
                return visible.get(text(node))
            if node.type == "attribute":
                obj = node.child_by_field_name("object")
                attr = node.child_by_field_name("attribute")
                if obj is not None and attr is not None and obj.type == "identifier":
                    mod = imap.modules.get(text(obj))
                    if mod:
                        return (tables.get(mod) or {}).get(text(attr))
                    if text(obj) in imap.from_names:
                        src_mod, orig = imap.from_names[text(obj)]
                        return (tables.get(f"{src_mod}.{orig}") or {}).get(text(attr))
            return None

        def lookup(expr: Node | None) -> list[str] | None:
            """`T[k]`, `T.get(k)`, `T.get(k, d)` → members of T."""
            if expr is None:
                return None
            if expr.type == "subscript":
                return table_of(expr.child_by_field_name("value"))
            if expr.type == "call" and call_callee_name(expr) == "get":
                func = expr.child_by_field_name("function")
                obj = func.child_by_field_name("object") if func is not None else None
                return table_of(obj)
            return None

        for fn in fns:
            fdef = fn.node
            if fdef.type == "decorated_definition":
                fdef = fdef.child_by_field_name("definition") or fdef
            local_alias: dict[str, list[str]] = {}
            for node in walk(fdef):
                owner = _enclosing_fn_node(node)
                if owner is None or (owner.start_byte, owner.end_byte) != (fdef.start_byte, fdef.end_byte):
                    continue
                if node.type == "assignment":
                    left = node.child_by_field_name("left")
                    members = lookup(node.child_by_field_name("right"))
                    if left is not None and left.type == "identifier" and members:
                        local_alias[text(left)] = members
                elif node.type == "for_statement":
                    left = node.child_by_field_name("left")
                    right = node.child_by_field_name("right")
                    members = table_of(right)
                    if members is None and right is not None and right.type == "call":
                        if call_callee_name(right) in {"values", "items"}:
                            func = right.child_by_field_name("function")
                            members = table_of(func.child_by_field_name("object") if func is not None else None)
                    if members and left is not None:
                        names = [n for n in walk(left) if n.type == "identifier"]
                        if names:
                            local_alias[text(names[-1])] = members
                elif node.type == "call":
                    func = node.child_by_field_name("function")
                    members = None
                    if func is not None:
                        members = lookup(func)
                        if members is None and func.type == "identifier":
                            members = local_alias.get(text(func))
                    if not members:
                        continue
                    ev = Evidence(file=rel_file, line=line_of(node), symbol=fn.qualname)
                    for callee in members:
                        if callee == fn.qualname or (fn.qualname, callee) in existing:
                            continue
                        existing.add((fn.qualname, callee))
                        analysis.calls.append((fn.qualname, callee, ev, Confidence.INFERRED))



# ---------------------------------------------------------------------------
# Field dispatch: functions stored in a field of a dataclass / NamedTuple /
# pydantic model / dict literal, then called through that field:
#   PANELS = [PanelDef("sales", build=_build_sales), ...]
#   for p in PANELS: p.build(conn)        # also getattr(p, "build")(), p["build"]()
# The call site calls every function ever assigned to that field (`inferred`).
# ---------------------------------------------------------------------------

_DISPATCH_SKIP_RECEIVERS = {"self", "cls", "super"}


def _callable_refs(value: Node, module: str, imap: ImportMap, functions: dict) -> list[str]:
    """A function reference, or the scanned functions a `lambda` value calls."""
    ref = _fn_ref(value, module, imap, functions)
    if ref:
        return [ref]
    if value.type != "lambda":
        return []
    out: list[str] = []
    body = value.child_by_field_name("body")
    for node in walk(body) if body is not None else []:
        if node.type == "call":
            func = node.child_by_field_name("function")
            hit = _fn_ref(func, module, imap, functions) if func is not None else None
            if hit and hit not in out:
                out.append(hit)
    return out


def _link_field_dispatch(analysis: PyAnalysis, parsed: list) -> None:
    functions = analysis.functions
    class_fields: dict[str, list[str]] = {}
    method_names: set[str] = set()
    for _path, _rf, _src, tree, _module, _imap, _fns in parsed:
        for node in walk(tree):
            if node.type != "class_definition":
                continue
            cname = text(node.child_by_field_name("name"))
            if cname and cname not in class_fields:
                class_fields[cname] = _class_field_names(node)
            body = node.child_by_field_name("body")
            for stmt in body.named_children if body is not None else []:
                fdef = stmt
                if fdef.type == "decorated_definition":
                    fdef = fdef.child_by_field_name("definition") or fdef
                if fdef.type == "function_definition":
                    mname = text(fdef.child_by_field_name("name"))
                    if mname:
                        method_names.add(mname)

    field_targets: dict[str, set[str]] = {}
    class_field_targets: dict[tuple[str, str], set[str]] = {}
    key_targets: dict[str, set[str]] = {}
    # module → roster name → dataclass names of its elements
    rosters: dict[str, dict[str, set[str]]] = {}
    for _path, _rf, _src, tree, module, _imap, _fns in parsed:
        local: dict[str, set[str]] = {}
        for name, value in collect_module_assignments(tree):
            elems: list[Node] = []
            if value.type in {"list", "tuple", "set"}:
                elems = list(value.named_children)
            elif value.type == "dictionary":
                elems = [p.child_by_field_name("value") for p in value.named_children if p.type == "pair"]
            classes = {
                call_callee_name(el)
                for el in elems
                if el is not None and el.type == "call" and call_callee_name(el) in class_fields
            }
            if classes:
                local[name] = classes
        rosters[module] = local
    for _path, _rf, _src, tree, module, imap, _fns in parsed:
        for node in walk(tree):
            if node.type == "call":
                cname = call_callee_name(node)
                fields = class_fields.get(cname)
                if not fields:
                    continue
                for i, arg in enumerate(call_posargs(node)):
                    if i < len(fields):
                        for ref in _callable_refs(arg, module, imap, functions):
                            field_targets.setdefault(fields[i], set()).add(ref)
                            class_field_targets.setdefault((cname, fields[i]), set()).add(ref)
                args = node.child_by_field_name("arguments")
                for kw in args.named_children if args is not None else []:
                    if kw.type != "keyword_argument":
                        continue
                    name = kw.child_by_field_name("name")
                    value = kw.child_by_field_name("value")
                    if name is None or value is None or text(name) not in fields:
                        continue
                    for ref in _callable_refs(value, module, imap, functions):
                        field_targets.setdefault(text(name), set()).add(ref)
                        class_field_targets.setdefault((cname, text(name)), set()).add(ref)
            elif node.type == "dictionary":
                for pair in node.named_children:
                    if pair.type != "pair":
                        continue
                    key = pair.child_by_field_name("key")
                    value = pair.child_by_field_name("value")
                    if key is None or value is None or key.type != "string":
                        continue
                    for ref in _callable_refs(value, module, imap, functions):
                        key_targets.setdefault(unquote(text(key)), set()).add(ref)

    if not field_targets and not key_targets:
        return
    existing = {(c, d) for c, d, _e, _k in analysis.calls}

    for _path, rel_file, _src, _tree, module, imap, fns in parsed:
        for fn in fns:
            fdef = fn.node
            if fdef.type == "decorated_definition":
                fdef = fdef.child_by_field_name("definition") or fdef
            def _field_targets_of(expr: Node | None) -> set[str]:
                """`x["k"]`, `x.get("k")`, `x.k`, `getattr(x, "k")` → functions stored under k."""
                if expr is None:
                    return set()
                if expr.type == "subscript":
                    sub = expr.child_by_field_name("subscript")
                    if sub is not None and sub.type == "string":
                        return key_targets.get(unquote(text(sub)), set())
                if expr.type == "call" and call_callee_name(expr) == "get":
                    pos = call_posargs(expr)
                    func_ = expr.child_by_field_name("function")
                    if (
                        pos
                        and pos[0].type == "string"
                        and func_ is not None
                        and func_.type == "attribute"
                    ):
                        return key_targets.get(unquote(text(pos[0])), set())
                if expr.type == "call" and call_callee_name(expr) == "getattr":
                    pos = call_posargs(expr)
                    if len(pos) >= 2 and pos[1].type == "string":
                        k = unquote(text(pos[1]))
                        return field_targets.get(k, set()) | key_targets.get(k, set())
                if expr.type == "attribute":
                    obj_ = expr.child_by_field_name("object")
                    attr_ = expr.child_by_field_name("attribute")
                    name_ = text(attr_) if attr_ is not None else ""
                    if (
                        name_ in field_targets
                        and name_ not in method_names
                        and not (obj_ is not None and text(obj_) in _DISPATCH_SKIP_RECEIVERS)
                    ):
                        return field_targets[name_]
                return set()

            def _roster_classes(
                expr: Node | None, module: str = module, imap: ImportMap = imap
            ) -> set[str]:
                """Classes of a roster expression: `SECTIONS`, `cfg.SECTIONS`,
                `SECTIONS[i]`, `SECTIONS.values()`."""
                if expr is None:
                    return set()
                if expr.type == "subscript":
                    return _roster_classes(expr.child_by_field_name("value"))
                if expr.type == "call" and call_callee_name(expr) in {"values", "items"}:
                    f_ = expr.child_by_field_name("function")
                    return _roster_classes(f_.child_by_field_name("object") if f_ is not None else None)
                if expr.type == "call" and call_callee_name(expr) in {"enumerate", "sorted", "list", "reversed"}:
                    pos_ = call_posargs(expr)
                    return _roster_classes(pos_[0]) if pos_ else set()
                if expr.type == "identifier":
                    nm = text(expr)
                    if nm in (rosters.get(module) or {}):
                        return rosters[module][nm]
                    if nm in imap.from_names:
                        src_mod, orig = imap.from_names[nm]
                        return (rosters.get(src_mod) or {}).get(orig, set())
                if expr.type == "attribute":
                    o_ = expr.child_by_field_name("object")
                    a_ = expr.child_by_field_name("attribute")
                    if o_ is not None and a_ is not None and o_.type == "identifier":
                        mod_ = imap.modules.get(text(o_))
                        if mod_ is None and text(o_) in imap.from_names:
                            sm, so = imap.from_names[text(o_)]
                            mod_ = f"{sm}.{so}"
                        if mod_:
                            return (rosters.get(mod_) or {}).get(text(a_), set())
                return set()

            roster_vars: dict[str, set[str]] = {}
            for node in walk(fdef):
                if node.type in {"for_statement", "for_in_clause"}:
                    left = node.child_by_field_name("left")
                    right = node.child_by_field_name("right")
                    if left is None or right is None:
                        # comprehension clause: the nodes around the `in` keyword
                        kids = list(node.children)
                        pos_in = next((i for i, k in enumerate(kids) if k.type == "in"), None)
                        if pos_in is not None:
                            before = [k for k in kids[:pos_in] if k.is_named]
                            after = [k for k in kids[pos_in + 1 :] if k.is_named]
                            left = left or (before[-1] if before else None)
                            right = right or (after[0] if after else None)
                    classes = _roster_classes(right)
                    if classes and left is not None:
                        idents = [n for n in walk(left) if n.type == "identifier"]
                        if idents:
                            roster_vars[text(idents[-1])] = classes
                elif node.type == "assignment":
                    left = node.child_by_field_name("left")
                    right = node.child_by_field_name("right")
                    classes = _roster_classes(right) if right is not None and right.type == "subscript" else set()
                    if classes and left is not None and left.type == "identifier":
                        roster_vars[text(left)] = classes

            def _roster_targets(
                recv: Node | None, field_name: str, roster_vars: dict[str, set[str]] = roster_vars
            ) -> set[str]:
                classes: set[str] = set()
                if recv is not None and recv.type == "identifier":
                    classes = roster_vars.get(text(recv), set())
                elif recv is not None and recv.type == "subscript":
                    classes = _roster_classes(recv)
                out_: set[str] = set()
                for c in classes:
                    out_ |= class_field_targets.get((c, field_name), set())
                return out_

            local_alias: dict[str, set[str]] = {}
            for node in walk(fdef):
                if node.type == "assignment":
                    left = node.child_by_field_name("left")
                    right = node.child_by_field_name("right")
                    if left is not None and left.type == "identifier":
                        found = _field_targets_of(right)
                        if found:
                            local_alias[text(left)] = found
            for node in walk(fdef):
                if node.type != "call":
                    continue
                owner = _enclosing_fn_node(node)
                if owner is None or (owner.start_byte, owner.end_byte) != (fdef.start_byte, fdef.end_byte):
                    continue
                func = node.child_by_field_name("function")
                targets: set[str] = set()
                if func is None:
                    continue
                if func.type == "identifier" and text(func) in local_alias:
                    targets = local_alias[text(func)]
                elif func.type == "call" and call_callee_name(func) == "get":
                    targets = _field_targets_of(func)
                elif func.type == "attribute" and _roster_targets(
                    func.child_by_field_name("object"),
                    text(func.child_by_field_name("attribute") or func),
                ):
                    # receiver comes from a roster of dataclass instances: it is
                    # field dispatch even if a scanned class has a same-named method
                    targets = _roster_targets(
                        func.child_by_field_name("object"),
                        text(func.child_by_field_name("attribute") or func),
                    )
                elif func.type == "attribute":
                    obj = func.child_by_field_name("object")
                    attr = func.child_by_field_name("attribute")
                    name = text(attr) if attr is not None else ""
                    if (
                        name in field_targets
                        and name not in method_names
                        and not (obj is not None and text(obj) in _DISPATCH_SKIP_RECEIVERS)
                    ):
                        targets = field_targets[name]
                elif func.type == "call" and call_callee_name(func) == "getattr":
                    pos = call_posargs(func)
                    if len(pos) >= 2 and pos[1].type == "string":
                        name = unquote(text(pos[1]))
                        if text(pos[0]) not in _DISPATCH_SKIP_RECEIVERS:
                            targets = field_targets.get(name, set()) | key_targets.get(name, set())
                elif func.type == "subscript":
                    sub = func.child_by_field_name("subscript")
                    if sub is not None and sub.type == "string":
                        targets = key_targets.get(unquote(text(sub)), set())
                if not targets:
                    continue
                ev = Evidence(file=rel_file, line=line_of(node), symbol=fn.qualname)
                for callee in sorted(targets):
                    if callee == fn.qualname or (fn.qualname, callee) in existing:
                        continue
                    existing.add((fn.qualname, callee))
                    analysis.calls.append((fn.qualname, callee, ev, Confidence.INFERRED))



# ---------------------------------------------------------------------------
# Executors: a function handed to a thread/process pool, the event loop, or a
# background-task queue is called by the code that hands it over.
# ---------------------------------------------------------------------------

# callee name → index of the callable among positional args
_EXECUTOR_POS = {
    "run_in_executor": 1,  # loop.run_in_executor(pool, fn, *args)
    "submit": 0,  # ThreadPoolExecutor / ProcessPoolExecutor
    "to_thread": 0,  # asyncio.to_thread(fn, *args)
    "run_sync": 0,  # anyio.to_thread.run_sync(fn, *args)
    "run_in_threadpool": 0,  # starlette.concurrency
    "add_task": 0,  # FastAPI / Starlette BackgroundTasks
    "apply_async": 0,  # multiprocessing.Pool
    "starmap": 0,
    "imap": 0,
    "imap_unordered": 0,
    "map": 0,  # executor.map(fn, it) / builtin map
    "call_soon": 0,
    "call_soon_threadsafe": 0,
    "call_later": 1,
    "call_at": 1,
    "Timer": 1,  # threading.Timer(interval, fn)
}
# constructors taking the callable as `target=`
_EXECUTOR_TARGET = {"Thread", "Process", "Timer"}


def _link_executor_calls(analysis: PyAnalysis, parsed: list) -> None:
    functions = analysis.functions
    existing = {(c, d) for c, d, _e, _k in analysis.calls}
    for _path, rel_file, _src, _tree, module, imap, fns in parsed:
        for fn in fns:
            fdef = fn.node
            if fdef.type == "decorated_definition":
                fdef = fdef.child_by_field_name("definition") or fdef
            for node in walk(fdef):
                if node.type != "call":
                    continue
                owner = _enclosing_fn_node(node)
                if owner is None or (owner.start_byte, owner.end_byte) != (fdef.start_byte, fdef.end_byte):
                    continue
                name = call_callee_name(node)
                refs: list[str] = []
                if name in _EXECUTOR_POS:
                    pos = call_posargs(node)
                    idx = _EXECUTOR_POS[name]
                    if len(pos) > idx:
                        ref = _fn_ref(pos[idx], module, imap, functions)
                        if ref:
                            refs.append(ref)
                if name in _EXECUTOR_TARGET:
                    args = node.child_by_field_name("arguments")
                    for kw in args.named_children if args is not None else []:
                        if kw.type == "keyword_argument" and text(kw.child_by_field_name("name")) in {
                            "target",
                            "function",
                        }:
                            value = kw.child_by_field_name("value")
                            ref = _fn_ref(value, module, imap, functions) if value is not None else None
                            if ref:
                                refs.append(ref)
                if not refs:
                    continue
                ev = Evidence(file=rel_file, line=line_of(node), symbol=fn.qualname)
                for callee in refs:
                    if callee == fn.qualname or (fn.qualname, callee) in existing:
                        continue
                    existing.add((fn.qualname, callee))
                    analysis.calls.append((fn.qualname, callee, ev, Confidence.INFERRED))



# ---------------------------------------------------------------------------
# `from reports import sales` + `sales.bind_store(...)`, and function
# references passed as arguments (`_respond(request, exports.export_items)`).
# ---------------------------------------------------------------------------

# Callees whose function arguments are registrations or metadata, not calls.
_PASSED_FN_SKIP = {
    "add_api_route",
    "add_url_rule",
    "add_route",
    "add_api_websocket_route",
    "add_event_handler",
    "add_exception_handler",
    "add_middleware",
    "include_router",
    "route",
    "register",
    "connect",
    "signal",
    "atexit",
    "wraps",
    "isinstance",
    "issubclass",
    "getattr",
    "hasattr",
    "setattr",
    "callable",
    "id",
    "repr",
    "print",
    "debug",
    "info",
    "warning",
    "error",
    "exception",
}


def _self_class_of(fn: PyFunction) -> str | None:
    return fn.class_name


def _param_used(fn_node: Node, pname: str) -> bool:
    """True when parameter ``pname`` is called or handed on in the body."""
    body = fn_node.child_by_field_name("body")
    if body is None:
        return False
    for node in walk(body):
        if node.type == "identifier" and text(node) == pname:
            parent = node.parent
            if parent is None:
                continue
            if parent.type == "call" and parent.child_by_field_name("function") == node:
                return True
            if parent.type in {"argument_list", "keyword_argument", "return_statement", "list", "tuple", "pair"}:
                return True
    return False


def _link_module_calls_and_passed_fns(analysis: PyAnalysis, parsed: list) -> None:
    functions = analysis.functions
    existing = {(c, d) for c, d, _e, _k in analysis.calls}

    def add(caller: str, callee: str, ev: Evidence, conf: Confidence, via: str | None) -> None:
        if caller == callee or (caller, callee) in existing:
            return
        existing.add((caller, callee))
        analysis.calls.append((caller, callee, ev, conf))
        if via:
            analysis.call_vias[(caller, callee)] = via

    for _path, rel_file, _src, _tree, module, imap, fns in parsed:
        for fn in fns:
            fdef = fn.node
            if fdef.type == "decorated_definition":
                fdef = fdef.child_by_field_name("definition") or fdef
            self_class = _self_class_of(fn)
            for node in walk(fdef):
                if node.type != "call":
                    continue
                owner = _enclosing_fn_node(node)
                if owner is None or (owner.start_byte, owner.end_byte) != (fdef.start_byte, fdef.end_byte):
                    continue
                ev = Evidence(file=rel_file, line=line_of(node), symbol=fn.qualname)
                func = node.child_by_field_name("function")
                callee = (
                    _fn_ref(func, module, imap, functions, self_class) if func is not None else None
                )
                # (a) module-qualified call resolved through the import
                if callee and func is not None and func.type == "attribute":
                    add(fn.qualname, callee, ev, Confidence.EXACT, None)
                # (b) function references passed as arguments
                name = call_callee_name(node)
                if name in _PASSED_FN_SKIP or name in _EXECUTOR_POS or name in _EXECUTOR_TARGET:
                    continue
                args = node.child_by_field_name("arguments")
                if args is None:
                    continue
                target_params: list[str] = []
                target_fn = functions.get(callee) if callee else None
                if target_fn is not None:
                    target_params = _param_names(
                        target_fn.node.child_by_field_name("definition")
                        if target_fn.node.type == "decorated_definition"
                        else target_fn.node
                    )
                    if target_params and target_params[0] in {"self", "cls"}:
                        target_params = target_params[1:]
                pos_index = 0
                for arg in args.named_children:
                    kw_name: str | None = None
                    value = arg
                    if arg.type == "keyword_argument":
                        kw = arg.child_by_field_name("name")
                        kw_name = text(kw) if kw is not None else None
                        value = arg.child_by_field_name("value") or arg
                    ref = _fn_ref(value, module, imap, functions, self_class)
                    this_pos = pos_index
                    if arg.type != "keyword_argument":
                        pos_index += 1
                    if not ref or ref == callee:
                        continue
                    if target_fn is not None:
                        pname = kw_name or (
                            target_params[this_pos] if this_pos < len(target_params) else None
                        )
                        if not pname or not _param_used(
                            target_fn.node.child_by_field_name("definition")
                            if target_fn.node.type == "decorated_definition"
                            else target_fn.node,
                            pname,
                        ):
                            continue
                    add(fn.qualname, ref, ev, Confidence.INFERRED, "passed_as_arg")



# ---------------------------------------------------------------------------
# Calls into first-party code that is not in the scan (direct, partial,
# executors): record them as unresolved (`target_not_scanned`) and keep SQL
# handed to them as an `inferred` access with `sink_not_scanned: true`.
# ---------------------------------------------------------------------------

_COMMON_THIRD_PARTY = {
    "aiohttp", "alembic", "anyio", "asyncpg", "boto3", "botocore", "celery", "click",
    "django", "dotenv", "fastapi", "firebase_admin", "flask", "google", "httpx", "jwt",
    "numpy", "pandas", "psycopg", "psycopg2", "pydantic", "pytest", "redis", "requests",
    "rq", "sqlalchemy", "sqlmodel", "starlette", "structlog", "typing_extensions",
    "uvicorn", "yaml", "attrs", "orjson", "ujson", "tenacity", "loguru", "sentry_sdk",
}


def _is_first_party(
    module_path: str, scanned_roots: set[str], declared: set[str] | None = None
) -> bool:
    import sys

    root = module_path.lstrip(".").split(".", 1)[0]
    if not root:
        return True  # relative import
    if root in scanned_roots:
        return True
    stdlib = getattr(sys, "stdlib_module_names", set())
    return root not in stdlib and root not in _COMMON_THIRD_PARTY and root not in (declared or set())


def _dotted(expr: Node) -> list[str] | None:
    parts: list[str] = []
    cur: Node | None = expr
    while cur is not None and cur.type == "attribute":
        attr = cur.child_by_field_name("attribute")
        if attr is None:
            return None
        parts.append(text(attr))
        cur = cur.child_by_field_name("object")
    if cur is None or cur.type != "identifier":
        return None
    parts.append(text(cur))
    return list(reversed(parts))


def _imported_qualname(expr: Node, imap: ImportMap) -> str | None:
    """`fetch` / `conn.fetch` / `db.conn.fetch` → qualified through imports."""
    parts = _dotted(expr)
    if not parts:
        return None
    head, rest = parts[0], parts[1:]
    if head in imap.from_names:
        src, orig = imap.from_names[head]
        return ".".join([src, orig, *rest])
    if rest and head in imap.modules:
        return ".".join([imap.modules[head], *rest])
    return None


_DIST_TO_IMPORT = {
    "pyyaml": "yaml",
    "python_dateutil": "dateutil",
    "beautifulsoup4": "bs4",
    "pillow": "PIL",
    "scikit_learn": "sklearn",
    "opencv_python": "cv2",
    "opencv_python_headless": "cv2",
    "psycopg2_binary": "psycopg2",
    "psycopg_binary": "psycopg",
    "python_jose": "jose",
    "pyjwt": "jwt",
    "python_multipart": "multipart",
    "python_dotenv": "dotenv",
    "google_cloud_storage": "google",
    "google_cloud_firestore": "google",
    "google_cloud_tasks": "google",
    "google_cloud_pubsub": "google",
    "google_auth": "google",
    "protobuf": "google",
    "msgpack_python": "msgpack",
}


def _declared_dependencies(root: Path) -> set[str]:
    """Import names of third-party packages declared anywhere in the scan
    (pyproject [project] / Poetry, requirements*.txt, setup.cfg)."""
    import tomllib

    names: set[str] = set()

    def add(spec: str) -> None:
        raw = re.split(r"[\s<>=!~;\[@(]", spec.strip(), maxsplit=1)[0]
        if not raw or raw.startswith(("-", "#", ".", "/")):
            return
        norm = raw.lower().replace("-", "_").replace(".", "_")
        names.add(_DIST_TO_IMPORT.get(norm, norm))
        if norm.startswith("google_"):
            names.add("google")

    import os

    skip = {"node_modules", ".venv", "venv", ".git", "__pycache__", "dist", "build", ".tox"}
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fname in filenames:
            if fname in {"pyproject.toml", "setup.cfg"} or (
                fname.startswith("requirements") and fname.endswith((".txt", ".in"))
            ):
                candidates.append(Path(dirpath) / fname)
    for path in sorted(candidates):
        name = path.name
        try:
            if name == "pyproject.toml":
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                for dep in (data.get("project") or {}).get("dependencies") or []:
                    add(str(dep))
                for group in ((data.get("project") or {}).get("optional-dependencies") or {}).values():
                    for dep in group or []:
                        add(str(dep))
                poetry = (data.get("tool") or {}).get("poetry") or {}
                for key in (poetry.get("dependencies") or {}):
                    add(str(key))
                for grp in (poetry.get("group") or {}).values():
                    for key in (grp or {}).get("dependencies") or {}:
                        add(str(key))
            elif name.startswith("requirements") and name.endswith((".txt", ".in")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    add(line)
            elif name == "setup.cfg":
                block = False
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("install_requires"):
                        block = True
                        tail = line.split("=", 1)[1] if "=" in line else ""
                        if tail.strip():
                            add(tail)
                        continue
                    if block:
                        if line and not line[0].isspace():
                            block = False
                        elif line.strip():
                            add(line)
        except (OSError, ValueError):
            continue
    names.discard("python")
    return names


def _link_unscanned_sinks(
    analysis: PyAnalysis,
    parsed: list,
    consts_by_module: dict[str, dict[str, ResolvedString]],
    declared: set[str] | None = None,
) -> None:
    scanned_modules = {module for _p, _rf, _s, _t, module, _i, _f in parsed}
    package_modules = {
        module for _p, rf, _s, _t, module, _i, _f in parsed if rf.endswith("__init__.py")
    }
    scanned_roots = {m.split(".", 1)[0] for m in scanned_modules}
    known_unresolved = {(u.caller, u.callee) for u in analysis.unresolved_calls}
    resolved_pairs = {(c, d) for c, d, _e, _k in analysis.calls}

    for _path, rel_file, _src, _tree, module, imap, fns in parsed:
        local_consts = consts_by_module.get(module) or {}

        def sql_of(arg: Node, *, imap=imap, local_consts=local_consts) -> str | None:
            if arg.type == "string":
                raw = unquote(text(arg))
                return raw if looks_like_sql(raw) else None
            if arg.type == "identifier":
                name = text(arg)
                if name in imap.from_names:
                    mod, orig = imap.from_names[name]
                    hit = _sql_text((consts_by_module.get(mod) or {}).get(orig))
                    if hit:
                        return hit
                return _sql_text(local_consts.get(name))
            if arg.type == "attribute":
                obj = arg.child_by_field_name("object")
                attr = arg.child_by_field_name("attribute")
                if obj is not None and attr is not None and obj.type == "identifier":
                    mod = imap.modules.get(text(obj))
                    if mod:
                        return _sql_text((consts_by_module.get(mod) or {}).get(text(attr)))
            return None

        for fn in fns:
            fdef = fn.node
            if fdef.type == "decorated_definition":
                fdef = fdef.child_by_field_name("definition") or fdef
            # local `sql = "SELECT ..."` (single static value per name)
            local_sql: dict[str, str | None] = {}
            for node in walk(fdef):
                if node.type != "assignment":
                    continue
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if left is None or right is None or left.type != "identifier":
                    continue
                val = sql_of(right)
                key = text(left)
                if key in local_sql and local_sql[key] != val:
                    local_sql[key] = None
                else:
                    local_sql[key] = val
            for node in walk(fdef):
                if node.type != "call":
                    continue
                owner = _enclosing_fn_node(node)
                if owner is None or (owner.start_byte, owner.end_byte) != (fdef.start_byte, fdef.end_byte):
                    continue
                name = call_callee_name(node)
                pos = call_posargs(node)
                target: Node | None = node.child_by_field_name("function")
                extra: list[Node] = list(pos)
                if name in _EXECUTOR_POS:
                    idx = _EXECUTOR_POS[name]
                    target = pos[idx] if len(pos) > idx else None
                    extra = pos[idx + 1 :] if len(pos) > idx else []
                if target is not None and target.type == "call" and call_callee_name(target) == "partial":
                    ppos = call_posargs(target)
                    extra = ppos[1:] + extra
                    target = ppos[0] if ppos else None
                if target is None:
                    continue
                qual = _imported_qualname(target, imap)
                if not qual or qual in analysis.functions or (fn.qualname, qual) in resolved_pairs:
                    continue
                if _PY_REEXPORTS.get(qual) in analysis.functions:
                    continue  # re-exported by a package __init__.py: resolved
                # Longest scanned prefix of the dotted path: a regular module
                # means our own code that is just not a plain function (class,
                # instance, constant) → leave it. A *package* (`__init__.py`)
                # means the next segment is a submodule that was not scanned →
                # report it (`pkg.store.views.counts.shop_counts`).
                if qual in scanned_modules:
                    continue
                parts = qual.split(".")
                longest = next(
                    (".".join(parts[:i]) for i in range(len(parts) - 1, 0, -1) if ".".join(parts[:i]) in scanned_modules),
                    None,
                )
                if longest is not None and longest not in package_modules:
                    continue
                src_module = qual.rsplit(".", 1)[0]
                if not _is_first_party(src_module, scanned_roots, declared):
                    continue
                ev = Evidence(file=rel_file, line=line_of(node), symbol=fn.qualname)
                if (fn.qualname, qual) not in known_unresolved:
                    known_unresolved.add((fn.qualname, qual))
                    analysis.unresolved_calls.append(PyUnresolvedCall(fn.qualname, qual, ev))
                analysis.unresolved_reasons[(fn.qualname, qual)] = "target_not_scanned"
                for arg in extra:
                    sql = sql_of(arg)
                    if not sql and arg.type == "identifier":
                        sql = local_sql.get(text(arg))
                    if not sql:
                        continue
                    norm = _norm_sql(sql)
                    matched = False
                    # the generic argument scan may have recorded this call as
                    # dynamic SQL (`{sql}`) → replace it with the resolved text
                    analysis.sql_sites = [
                        site
                        for site in analysis.sql_sites
                        if not (
                            site.function.qualname == fn.qualname
                            and site.dynamic
                            and site.evidence.line == ev.line
                        )
                    ]
                    for site in analysis.sql_sites:
                        if site.function.qualname == fn.qualname and _norm_sql(site.sql) == norm:
                            # confidence describes the SQL (literal → exact);
                            # the unknown executor is the sink_not_scanned flag
                            site.sink_not_scanned = True
                            matched = True
                    if not matched:
                        analysis.sql_sites.append(
                            PySqlSite(
                                function=fn,
                                sql=sql,
                                dynamic=False,
                                evidence=ev,
                                sink_not_scanned=True,
                            )
                        )



# ---------------------------------------------------------------------------
# Import → scanned module, by unique suffix (layout safety net).
# ---------------------------------------------------------------------------


def _suffix_module(name: str, scanned: set[str], cache: dict[str, str | None]) -> str | None:
    """`pipeline.render._utils` ↔ `render._utils`, `backend.app.x` ↔
    `app.x`: the unique scanned module that shares a 2+ segment suffix."""
    if name in scanned:
        return name
    if name in cache:
        return cache[name]
    parts = name.split(".")
    hits: set[str] = set()
    for mod in scanned:
        mparts = mod.split(".")
        k = min(len(parts), len(mparts))
        if k < 2:
            continue
        if parts[-k:] == mparts[-k:] and (k == len(parts) or k == len(mparts)):
            hits.add(mod)
    out = next(iter(hits)) if len(hits) == 1 else None
    cache[name] = out
    return out


def _remap_imports_by_suffix(imaps: dict[str, ImportMap], scanned: set[str]) -> None:
    cache: dict[str, str | None] = {}
    for imap in imaps.values():
        for alias, mod in list(imap.modules.items()):
            if mod not in scanned:
                hit = _suffix_module(mod, scanned, cache)
                if hit:
                    imap.modules[alias] = hit
        for local, (mod, orig) in list(imap.from_names.items()):
            full = f"{mod}.{orig}"
            if mod in scanned or full in scanned:
                continue
            sub = _suffix_module(full, scanned, cache)
            if sub:
                # `from pkg.sub import module`
                head, _, tail = sub.rpartition(".")
                imap.from_names[local] = (head, tail) if head else (sub, orig)
                if not head:
                    imap.modules.setdefault(local, sub)
                continue
            hit = _suffix_module(mod, scanned, cache)
            if hit:
                imap.from_names[local] = (hit, orig)



# ---------------------------------------------------------------------------
# Re-exports: `pkg/media_utils/__init__.py` does `from .encode import to_data_uri`
# (or `from .encode import *`); `from media_utils import to_data_uri` must
# reach `media_utils.encode.to_data_uri`.
# ---------------------------------------------------------------------------

_PY_REEXPORTS: dict[str, str] = {}


def _reexport_target(
    imaps: dict[str, ImportMap], functions: dict, mod: str, name: str
) -> tuple[str, str] | None:
    cur = (mod, name)
    seen: set[tuple[str, str]] = set()
    for _ in range(6):
        if f"{cur[0]}.{cur[1]}" in functions:
            return cur
        if cur in seen:
            return None
        seen.add(cur)
        src = imaps.get(cur[0])
        if src is None:
            return None
        if cur[1] in src.from_names:
            cur = src.from_names[cur[1]]
            continue
        star = next((m for m in src.star_modules if f"{m}.{cur[1]}" in functions), None)
        if star is not None:
            return star, cur[1]
        star = next((m for m in src.star_modules if m in imaps and cur[1] in imaps[m].from_names), None)
        if star is None:
            return None
        cur = (star, cur[1])
    return None


def _follow_python_reexports(imaps: dict[str, ImportMap], functions: dict) -> None:
    for module, imap in imaps.items():
        for local, (mod, orig) in list(imap.from_names.items()):
            if f"{mod}.{orig}" in functions or f"{mod}.{orig}" in imaps:
                continue
            hit = _reexport_target(imaps, functions, mod, orig)
            if hit is not None and hit != (mod, orig):
                imap.from_names[local] = hit
        # names this module re-exports (for `import pkg; pkg.fn()`)
        for local, (mod, orig) in imap.from_names.items():
            if f"{mod}.{orig}" in functions:
                _PY_REEXPORTS.setdefault(f"{module}.{local}", f"{mod}.{orig}")
        for star in imap.star_modules:
            prefix = f"{star}."
            for qual in functions:
                if qual.startswith(prefix) and "." not in qual[len(prefix):]:
                    _PY_REEXPORTS.setdefault(f"{module}.{qual[len(prefix):]}", qual)
