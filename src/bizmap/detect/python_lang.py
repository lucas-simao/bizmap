from __future__ import annotations

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
    join_route_path,
    lookup_dict,
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
from bizmap.detect.tsutil import call_args, call_callee_name, line_of, text, walk
from bizmap.files import rel
from bizmap.graph import Confidence, Evidence
from bizmap.schema_replay import Schema
from bizmap.sql_analyze import looks_like_sql

_PY_LANGUAGE = Language(tspy.language())

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
FIRESTORE_DOC_EVENTS = {
    "on_document_created": "created",
    "on_document_updated": "updated",
    "on_document_deleted": "deleted",
    "on_document_written": "written",
}
FIRESTORE_READ = {"get", "stream", "list_documents"}
FIRESTORE_WRITE = {"set", "update", "delete", "add"}
SA_QUERY = {"select", "insert", "update", "delete"}
HTTP_CLIENT_LIBS = ("httpx", "requests")
CELERY_DECORATORS = {"task", "shared_task"}


def _parser() -> Parser:
    return Parser(_PY_LANGUAGE)


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


def _enclosing_fn_node(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None:
        if cur.type == "function_definition":
            return cur
        cur = cur.parent
    return None


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

    @property
    def qualname(self) -> str:
        if self.class_name:
            return f"{self.module}.{self.class_name}.{self.name}"
        return f"{self.module}.{self.name}"


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


@dataclass
class PyAccess:
    function: PyFunction
    kind: str
    table: str
    column: str | None
    confidence: Confidence
    evidence: Evidence
    target_kind: str = "table"


@dataclass
class PyHttpCall:
    function: PyFunction
    method: str
    url: str
    evidence: Evidence
    unresolved: bool = False
    confidence: Confidence = Confidence.INFERRED


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


class PythonAnalyzer:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = [r for r in rules if r.language == "python" and not r.is_stub]
        self._parser = _parser()
        self._fastapi = next((r for r in self.rules if r.framework == "fastapi"), None)
        self._flask = next((r for r in self.rules if r.framework == "flask"), None)
        self._firebase = next((r for r in self.rules if r.framework == "firebase"), None)
        self._sql_rules = [r for r in self.rules if "sql" in r.produce]

    def analyze(self, files: list[Path], root: Path) -> PyAnalysis:
        analysis = PyAnalysis()
        parsed: list[tuple[Path, str, bytes, Node, str, ImportMap, list[PyFunction]]] = []
        consts_by_module: dict[str, dict[str, ResolvedString]] = {}
        dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] = {}
        imaps: dict[str, ImportMap] = {}

        for path in files:
            source = path.read_bytes()
            tree = self._parser.parse(source).root_node
            rel_file = rel(path, root)
            module = _module_name(rel_file)
            imap = parse_imports(tree, module)
            imaps[module] = imap
            fns = self._collect_functions(tree, source, rel_file, module)
            for fn in fns:
                analysis.functions[fn.qualname] = fn
            parsed.append((path, rel_file, source, tree, module, imap, fns))

        for _path, _rf, _src, tree, module, imap, _fns in parsed:
            assigns = collect_module_assignments(tree)
            consts_by_module[module] = resolve_module_consts(assigns, imap, consts_by_module)
            dicts_by_module[module] = collect_module_dicts(
                assigns, imap, consts_by_module, dicts_by_module
            )
        # second pass so imported constants resolve
        for _path, _rf, _src, tree, module, imap, _fns in parsed:
            assigns = collect_module_assignments(tree)
            consts_by_module[module] = resolve_module_consts(assigns, imap, consts_by_module)
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

            routers, includes, blueprints = self._collect_prefixes(tree, resolve)
            enclosing = {id(fn.node): fn for fn in fns}
            self._detect_routes(
                analysis, tree, rel_file, imap, fns, enclosing, resolve, routers, includes, blueprints
            )
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
            )
            self._detect_main_worker(analysis, tree, module, fns, enclosing, rel_file)

        unique: dict[tuple, PySqlSite] = {}
        for site in analysis.sql_sites:
            key = (site.function.qualname, site.sql, site.evidence.line)
            unique[key] = site
        analysis.sql_sites = list(unique.values())
        _resolve_callable_injection(analysis)
        _resolve_object_dispatch(analysis)
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
                )
            )
        return fns

    def _collect_prefixes(self, tree: Node, resolve) -> tuple[dict[str, str], list[tuple[str, str]], dict[str, str]]:
        routers: dict[str, str] = {}
        includes: list[tuple[str, str]] = []
        blueprints: dict[str, str] = {}
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
    ) -> None:
        http_deco = set()
        if self._fastapi:
            spec = (self._fastapi.detect.get("decorators") or {}).get("method") or {}
            http_deco.update(str(x).lower() for x in spec.get("in") or HTTP_METHODS)
        else:
            http_deco.update(HTTP_METHODS)

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
            pos = call_posargs(call)
            kw = call_kwargs(call)
            path_node = pos[0] if pos else kw.get("rule")
            path_res = resolve(path_node) if path_node is not None else ResolvedString("", True, False)
            methods = _list_string_items(kw.get("methods")) or ["GET"]
            prefix = blueprints.get(receiver, "")
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

        # FastAPI @app.get / @router.post
        if method_l in http_deco and call is not None:
            if self._fastapi and not import_matches(imap.raw, self._fastapi.when):
                # still allow if receiver looks like app/router
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
                analysis.routes.append(
                    PyRoute(
                        method=method.upper(),
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
    ) -> None:
        by_local = {fn.name: fn for fn in fns if not fn.class_name}
        by_class_method: dict[tuple[str, str], PyFunction] = {
            (fn.class_name, fn.name): fn for fn in fns if fn.class_name
        }
        imported_fns = self._imported_functions(imap, analysis)
        has_http_lib = any(any(lib in s for s in imap.raw) for lib in HTTP_CLIENT_LIBS)
        local_dicts = local_dicts or {}
        dicts_by_module = dicts_by_module or {}
        consts_by_module = consts_by_module or {}
        call_returns = call_returns or {}
        protocols = protocols or {}
        protocol_impls = protocol_impls or {}
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
                    analysis, fn, node, method, qual, pos, has_http_lib, resolve_fn, evidence
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
                            dynamic=res.unresolved,
                            evidence=evidence,
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
            for res in _sql_arg_sources(arg, resolve, imap, local_dicts, dicts_by_module):
                sql_val = res.path_text if res.usable else ""
                if res.fully_static and res.text and looks_like_sql(res.text):
                    analysis.sql_sites.append(
                        PySqlSite(function=fn, sql=res.text, dynamic=False, evidence=evidence)
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
                analysis.accesses.append(PyAccess(fn, "write", t, None, Confidence.EXACT, evidence))
                return
            # insert/update table-level; columns come from .values
            t, c = cols[0]
            analysis.accesses.append(
                PyAccess(fn, "write", t, c, Confidence.INFERRED if not c else Confidence.EXACT, evidence)
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
                if cols and method == "add":
                    for c in cols:
                        analysis.accesses.append(PyAccess(fn, kind, table, c, Confidence.EXACT, evidence))
                else:
                    analysis.accesses.append(PyAccess(fn, kind, table, None, Confidence.INFERRED, evidence))
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
        has_http_lib: bool,
        resolve,
        evidence: Evidence,
    ) -> None:
        method_l = method.lower()
        looks_client = any(lib in qual for lib in HTTP_CLIENT_LIBS) or (
            has_http_lib and method_l in HTTP_METHODS | {"request"}
        )
        if not looks_client:
            return
        url_node = None
        kw = call_kwargs(node)
        if method_l == "request":
            mres = resolve(pos[0]) if pos else resolve(kw.get("method"))
            http_method = (mres.path_text if mres and mres.usable else "GET").upper()
            url_node = pos[1] if len(pos) > 1 else kw.get("url")
        else:
            http_method = method.upper()
            url_node = pos[0] if pos else kw.get("url")
        if url_node is None:
            return
        res = resolve(url_node)
        if not res.usable:
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
                    url=url,
                    evidence=evidence,
                    unresolved=unresolved,
                    confidence=conf,
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
        if obj is not None and obj.type == "identifier":
            dmap = lookup_dict(text(obj), imap, local_dicts, dicts_by_module)
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


def _resolve_callable_injection(analysis: PyAnalysis) -> None:
    """Link calls through injected callables (args, dataclass fields, aliases)."""
    by_name = _fn_by_name(analysis)
    existing = {(c, d) for c, d, _e, _k in analysis.calls}

    param_of: dict[str, list[str]] = {}
    class_fields: dict[str, list[str]] = {}
    class_nodes: dict[str, Node] = {}
    seen_files: set[str] = set()
    for fn in analysis.functions.values():
        param_of[fn.qualname] = _param_names(fn.node)
        cur = fn.node.parent
        while cur is not None:
            if cur.type == "class_definition":
                cname = text(cur.child_by_field_name("name"))
                if cname and cname not in class_fields:
                    class_fields[cname] = _class_field_names(cur)
                    class_nodes[cname] = cur
                break
            cur = cur.parent
        if fn.file in seen_files:
            continue
        seen_files.add(fn.file)
        root = fn.node
        while root.parent is not None:
            root = root.parent
        for node in walk(root):
            if node.type != "class_definition":
                continue
            cname = text(node.child_by_field_name("name"))
            if cname and cname not in class_fields:
                class_fields[cname] = _class_field_names(node)
                class_nodes[cname] = node

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
                continue
            if node.type != "call":
                continue
            callee = call_callee_name(node)
            pos = call_posargs(node)
            kw = call_kwargs(node)
            # ClassName(field=fn) / ClassName(fn)
            if callee in class_fields:
                fields = class_fields[callee]
                for i, arg in enumerate(pos):
                    if arg.type != "identifier" or i >= len(fields):
                        continue
                    target = _unique_fn(by_name.get(text(arg)) or [], fn.qualname)
                    if target:
                        ctor_fields.append((callee, fields[i], target.qualname))
                        passed.append((fn.qualname, callee, fields[i], target.qualname))
                for k, arg in kw.items():
                    if arg.type != "identifier":
                        continue
                    target = _unique_fn(by_name.get(text(arg)) or [], fn.qualname)
                    if target:
                        ctor_fields.append((callee, k, target.qualname))
                        passed.append((fn.qualname, callee, k, target.qualname))
            # func(persist) / func(run=persist)
            targets_fn = by_name.get(callee) or []
            uniq = _unique_fn(targets_fn)
            if uniq is None and len(targets_fn) == 1:
                uniq = targets_fn[0]
            if uniq is None:
                continue
            params = param_of.get(uniq.qualname) or []
            for i, arg in enumerate(pos):
                if arg.type != "identifier" or i >= len(params):
                    continue
                target = _unique_fn(by_name.get(text(arg)) or [], fn.qualname)
                if target:
                    passed.append((fn.qualname, uniq.qualname, params[i], target.qualname))
            for k, arg in kw.items():
                if arg.type != "identifier":
                    continue
                target = _unique_fn(by_name.get(text(arg)) or [], fn.qualname)
                if target:
                    passed.append((fn.qualname, uniq.qualname, k, target.qualname))

    param_targets: dict[tuple[str, str], set[str]] = {}
    for _caller, callee, param, target in passed:
        param_targets.setdefault((callee, param), set()).add(target)
    field_targets: dict[tuple[str, str], set[str]] = {}
    for cls, field_name, target in ctor_fields:
        field_targets.setdefault((cls, field_name), set()).add(target)

    new_calls: list[tuple[str, str, Evidence, Confidence]] = []
    for fn in analysis.functions.values():
        params = set(param_of.get(fn.qualname) or [])
        for node in walk(fn.node):
            if node.type != "call":
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
            # obj.field() dataclass / injected field
            if func is not None and func.type == "attribute" and func.named_children:
                field = method
                obj = func.named_children[0]
                obj_name = text(obj) if obj.type == "identifier" else ""
                if obj_name == "self" and fn.class_name:
                    for target in field_targets.get((fn.class_name, field), ()):
                        if (fn.qualname, target) not in existing:
                            new_calls.append((fn.qualname, target, evidence, Confidence.INFERRED))
                            existing.add((fn.qualname, target))
                if obj_name in params:
                    # svc.save() — svc is a param filled with Class(save=fn)
                    for (_cls, fld), targets in field_targets.items():
                        if fld != field:
                            continue
                        for target in targets:
                            if (fn.qualname, target) not in existing:
                                new_calls.append(
                                    (fn.qualname, target, evidence, Confidence.INFERRED)
                                )
                                existing.add((fn.qualname, target))
                    for target in param_targets.get((fn.qualname, obj_name), ()):
                        # param itself is a class instance; field call already handled
                        _ = target
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


def _resolve_object_dispatch(analysis: PyAnalysis) -> None:
    """Follow ``self.outbox.settle()`` / ``store.save()`` to class methods."""
    methods: dict[str, dict[str, PyFunction]] = {}
    class_nodes: dict[str, Node] = {}
    protocol_names: set[str] = set()
    for fn in analysis.functions.values():
        if not fn.class_name:
            continue
        methods.setdefault(fn.class_name, {})[fn.name] = fn
        cur = fn.node.parent
        while cur is not None:
            if cur.type == "class_definition":
                cname = text(cur.child_by_field_name("name"))
                if cname:
                    class_nodes[cname] = cur
                    bases = _class_bases(cur)
                    if bases & {"Protocol", "ABC", "ABCMeta"}:
                        protocol_names.add(cname)
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
            hits = [c for c in methods if type_name in methods[c] or c == type_name]
            return hits[:1] if len(hits) == 1 else []
        impls: list[str] = []
        for cname, meths in methods.items():
            if cname == type_name or cname in protocol_names:
                continue
            if proto_meths <= set(meths):
                impls.append(cname)
        return impls

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
            elif obj.type == "attribute" and obj.named_children:
                head = obj.named_children[0]
                field = text(obj.named_children[-1]) if len(obj.named_children) >= 2 else ""
                if text(head) == "self" and fn.class_name and field:
                    types.update(field_types.get((fn.class_name, field), set()))
            for tname in types:
                cal = methods.get(tname, {}).get(method)
                if cal is not None:
                    add_call(fn.qualname, cal.qualname, evidence)
                    continue
                for impl in implementers_of(tname):
                    cal = methods.get(impl, {}).get(method)
                    if cal is not None:
                        add_call(fn.qualname, cal.qualname, evidence)
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
                        if res.usable:
                            field = text(left)
                            out[field] = res
                            out[field.split(".", 1)[-1]] = res
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
