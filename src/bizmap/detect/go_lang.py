from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_go as tsgo
from tree_sitter import Language, Node, Parser

from bizmap.detect.rules import Rule, import_matches, method_matches
from bizmap.detect.tsutil import (
    call_args,
    call_callee_name,
    line_of,
    text,
    walk,
)
from bizmap.files import rel
from bizmap.graph import Confidence, Evidence
from bizmap.sql_analyze import looks_like_sql

_GO_LANGUAGE = Language(tsgo.language())


def _parser() -> Parser:
    return Parser(_GO_LANGUAGE)


@dataclass
class ResolvedString:
    text: str | None
    fully_static: bool
    dynamic: bool

    @property
    def usable(self) -> bool:
        return bool(self.text and self.text.strip())


@dataclass
class GoType:
    package: str
    name: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.package, self.name)


@dataclass
class GoFunction:
    package: str
    name: str
    receiver: str | None
    receiver_var: str | None
    file: str
    line: int
    why: str | None
    node: Node
    source: bytes
    params: dict[str, GoType] = field(default_factory=dict)
    return_type: GoType | None = None
    calls: list[tuple[str, int, Node]] = field(default_factory=list)  # (qualname guess later)

    @property
    def qualname(self) -> str:
        if self.receiver:
            return f"{self.package}.{self.receiver}.{self.name}"
        return f"{self.package}.{self.name}"


@dataclass
class SqlSite:
    function: GoFunction
    sql: str
    dynamic: bool
    evidence: Evidence
    fully_static: bool


@dataclass
class RouteHit:
    method: str
    path: str
    handler_expr: str
    file: str
    line: int
    evidence: Evidence


@dataclass
class JobHit:
    handler: GoFunction
    file: str
    line: int
    evidence: Evidence


@dataclass
class GoAnalysis:
    functions: dict[str, GoFunction]
    sql_sites: list[SqlSite]
    routes: list[RouteHit]
    jobs: list[JobHit]
    calls: list[tuple[str, str, Evidence, Confidence]]  # caller_qn, callee_qn


@dataclass
class _FileInfo:
    path: Path
    rel: str
    source: bytes
    tree: Node
    package: str
    imports: list[str]
    import_aliases: dict[str, str]  # alias -> last path component / pkg
    consts: dict[str, ResolvedString]
    structs: dict[str, dict[str, GoType]]  # type -> field -> type
    functions: list[GoFunction]


def _unquote_go_string(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == "`" and raw[-1] == "`":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] in {'"', "'"} and raw[-1] == raw[0]:
        inner = raw[1:-1]
        try:
            return codecs.decode(inner, "unicode_escape")
        except Exception:
            return inner.replace(r"\"", '"').replace(r"\\", "\\")
    return raw


def _go_string_node(node: Node) -> str | None:
    if node.type in {"interpreted_string_literal", "raw_string_literal"}:
        return _unquote_go_string(text(node))
    content = [c for c in node.named_children if c.type.endswith("_content")]
    if content and node.type.endswith("string_literal"):
        return _unquote_go_string(text(node))
    return None


def _preceding_comment(fn_node: Node, source: bytes) -> str | None:
    comments: list[Node] = []
    prev = fn_node.prev_named_sibling
    while prev is not None and prev.type == "comment":
        comments.append(prev)
        prev = prev.prev_named_sibling
    if not comments:
        return None
    comments.reverse()
    # Drop comments separated by more than one blank line from the function.
    kept: list[str] = []
    fn_start = fn_node.start_byte
    last_end = comments[0].end_byte
    for i, comment in enumerate(comments):
        if i == 0:
            kept.append(text(comment))
            last_end = comment.end_byte
            continue
        between = source[last_end:comment.start_byte].decode("utf-8", errors="replace")
        if between.count("\n") > 2:
            kept = [text(comment)]
        else:
            kept.append(text(comment))
        last_end = comment.end_byte
    between_fn = source[comments[-1].end_byte:fn_start].decode("utf-8", errors="replace")
    if between_fn.count("\n") > 2:
        return None
    lines: list[str] = []
    for block in kept:
        for line in block.splitlines():
            line = re.sub(r"^//\s?", "", line)
            line = re.sub(r"^/\*+\s?", "", line)
            line = re.sub(r"\s*\*/$", "", line)
            lines.append(line)
    why = " ".join(part.strip() for part in lines if part.strip()).strip()
    return why or None


def _parse_type(node: Node | None, current_pkg: str) -> GoType | None:
    if node is None:
        return None
    if node.type == "pointer_type":
        inner = node.child_by_field_name("element") or (node.named_children[0] if node.named_children else None)
        return _parse_type(inner, current_pkg)
    if node.type == "qualified_type":
        pkg = text(node.child_by_field_name("package"))
        name = text(node.child_by_field_name("name"))
        return GoType(package=pkg, name=name)
    if node.type in {"type_identifier", "identifier"}:
        return GoType(package=current_pkg, name=text(node))
    if node.named_children:
        return _parse_type(node.named_children[-1], current_pkg)
    raw = text(node).lstrip("*")
    if "." in raw:
        pkg, name = raw.split(".", 1)
        return GoType(package=pkg, name=name)
    if raw:
        return GoType(package=current_pkg, name=raw)
    return None


def _param_types(param_list: Node | None, pkg: str) -> dict[str, GoType]:
    out: dict[str, GoType] = {}
    if param_list is None:
        return out
    for decl in param_list.named_children:
        if decl.type != "parameter_declaration":
            continue
        type_node = decl.child_by_field_name("type")
        gtype = _parse_type(type_node, pkg)
        names = [c for c in decl.named_children if c.type == "identifier"]
        for ident in names:
            if gtype:
                out[text(ident)] = gtype
    return out


def _result_type(fn_node: Node, pkg: str) -> GoType | None:
    result = fn_node.child_by_field_name("result")
    if result is None:
        return None
    if result.type == "parameter_list":
        # first named type
        for decl in result.named_children:
            t = decl.child_by_field_name("type") if decl.type == "parameter_declaration" else decl
            parsed = _parse_type(t, pkg)
            if parsed:
                return parsed
        return None
    return _parse_type(result, pkg)


def _receiver(fn_node: Node, pkg: str) -> tuple[str | None, str | None, GoType | None]:
    recv = fn_node.child_by_field_name("receiver")
    if recv is None:
        return None, None, None
    for decl in recv.named_children:
        if decl.type != "parameter_declaration":
            continue
        names = [c for c in decl.named_children if c.type == "identifier"]
        var = text(names[0]) if names else None
        gtype = _parse_type(decl.child_by_field_name("type"), pkg)
        return (gtype.name if gtype else None), var, gtype
    return None, None, None


def _struct_fields(type_decl: Node, pkg: str) -> dict[str, dict[str, GoType]]:
    out: dict[str, dict[str, GoType]] = {}
    for spec in type_decl.named_children:
        if spec.type != "type_spec":
            continue
        name = text(spec.child_by_field_name("name"))
        typ = spec.child_by_field_name("type")
        if typ is None or typ.type != "struct_type":
            continue
        fields: dict[str, GoType] = {}
        field_list = typ.child_by_field_name("body") or (
            next((c for c in typ.named_children if c.type == "field_declaration_list"), None)
        )
        if field_list is None:
            continue
        for fd in field_list.named_children:
            if fd.type != "field_declaration":
                continue
            ftype = _parse_type(fd.child_by_field_name("type"), pkg)
            names = [c for c in fd.named_children if c.type in {"field_identifier", "identifier"}]
            for ident in names:
                if ftype:
                    fields[text(ident)] = ftype
        if name:
            out[name] = fields
    return out


def _resolve_expr(
    node: Node | None,
    env: dict[str, ResolvedString],
    package_consts: dict[str, ResolvedString],
) -> ResolvedString:
    if node is None:
        return ResolvedString(None, False, True)
    if node.type in {"interpreted_string_literal", "raw_string_literal"}:
        value = _go_string_node(node) or ""
        return ResolvedString(value, True, False)
    if node.type == "identifier":
        name = text(node)
        if name in env:
            return env[name]
        if name in package_consts:
            return package_consts[name]
        return ResolvedString(None, False, True)
    if node.type == "binary_expression":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        op_nodes = [c for c in node.children if not c.is_named]
        op = text(op_nodes[0]) if op_nodes else "+"
        lv = _resolve_expr(left, env, package_consts)
        rv = _resolve_expr(right, env, package_consts)
        if op != "+":
            return ResolvedString(None, False, True)
        if lv.fully_static and rv.fully_static and lv.text is not None and rv.text is not None:
            return ResolvedString(lv.text + rv.text, True, False)
        parts = [p for p in (lv.text, rv.text) if p]
        joined = "".join(parts) if parts else None
        return ResolvedString(joined, False, True)
    if node.type == "parenthesized_expression" and node.named_children:
        return _resolve_expr(node.named_children[0], env, package_consts)
    if node.type == "expression_list" and node.named_children:
        return _resolve_expr(node.named_children[0], env, package_consts)
    if node.type == "call_expression":
        return ResolvedString(None, False, True)
    return ResolvedString(None, False, True)


def _collect_package_strings(root: Node) -> dict[str, ResolvedString]:
    out: dict[str, ResolvedString] = {}
    for node in root.named_children:
        if node.type not in {"const_declaration", "var_declaration"}:
            continue
        for spec in node.named_children:
            if spec.type not in {"const_spec", "var_spec"}:
                continue
            name_node = spec.child_by_field_name("name")
            value_node = spec.child_by_field_name("value")
            if name_node is None:
                idents = [c for c in spec.named_children if c.type == "identifier"]
                name_node = idents[0] if idents else None
            if value_node is None:
                for child in spec.named_children:
                    if child.type == "expression_list":
                        value_node = child.named_children[0] if child.named_children else None
            if name_node is None:
                continue
            resolved = _resolve_expr(value_node, out, out)
            out[text(name_node)] = resolved
    return out


def _join_path(*parts: str) -> str:
    out = ""
    for part in parts:
        if part is None:
            continue
        piece = str(part).strip()
        if not piece:
            continue
        if not piece.startswith("/"):
            piece = "/" + piece
        if out.endswith("/"):
            out = out[:-1]
        if piece == "/":
            if not out:
                out = "/"
            continue
        out += piece
    return out or "/"


def _import_paths(root: Node) -> tuple[list[str], dict[str, str]]:
    imports: list[str] = []
    aliases: dict[str, str] = {}
    for node in walk(root):
        if node.type != "import_spec":
            continue
        path_node = node.child_by_field_name("path")
        path = _unquote_go_string(text(path_node)) if path_node else ""
        if not path:
            continue
        imports.append(path)
        last = path.rstrip("/").split("/")[-1]
        alias_node = node.child_by_field_name("name")
        alias = text(alias_node) if alias_node else last
        if alias and alias != ".":
            aliases[alias] = last
    return imports, aliases


def _contains_call(fn: GoFunction, method: str, receiver: str | None = None) -> bool:
    for node in walk(fn.node):
        if node.type != "call_expression":
            continue
        if call_callee_name(node) != method:
            continue
        if not receiver:
            return True
        func = node.child_by_field_name("function")
        if func is not None and func.type == "selector_expression":
            operand = func.child_by_field_name("operand")
            if text(operand) == receiver:
                return True
    return False


class GoAnalyzer:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = [r for r in rules if r.language == "go" and not r.is_stub]
        self._parser = _parser()

    def analyze(self, files: list[Path], root: Path) -> GoAnalysis:
        infos: list[_FileInfo] = []
        for path in files:
            source = path.read_bytes()
            tree = self._parser.parse(source).root_node
            pkg_node = next((c for c in tree.named_children if c.type == "package_clause"), None)
            pkg = "main"
            if pkg_node:
                ident = next((c for c in pkg_node.named_children if c.type == "package_identifier"), None)
                pkg = text(ident) or "main"
            imports, aliases = _import_paths(tree)
            structs: dict[str, dict[str, GoType]] = {}
            functions: list[GoFunction] = []
            for node in tree.named_children:
                if node.type == "type_declaration":
                    structs.update(_struct_fields(node, pkg))
                if node.type in {"function_declaration", "method_declaration"}:
                    functions.append(self._function(node, pkg, rel(path, root), source))
            infos.append(
                _FileInfo(
                    path=path,
                    rel=rel(path, root),
                    source=source,
                    tree=tree,
                    package=pkg,
                    imports=imports,
                    import_aliases=aliases,
                    consts=_collect_package_strings(tree),
                    structs=structs,
                    functions=functions,
                )
            )

        all_structs: dict[tuple[str, str], dict[str, GoType]] = {}
        fn_index: dict[str, GoFunction] = {}
        by_name: dict[str, list[GoFunction]] = {}
        by_method: dict[tuple[str, str, str], GoFunction] = {}
        for info in infos:
            for sname, fields in info.structs.items():
                all_structs[(info.package, sname)] = fields
            for fn in info.functions:
                fn_index[fn.qualname] = fn
                by_name.setdefault(fn.name, []).append(fn)
                if fn.receiver:
                    by_method[(fn.package, fn.receiver, fn.name)] = fn

        sql_sites: list[SqlSite] = []
        routes: list[RouteHit] = []
        jobs: list[JobHit] = []
        calls: list[tuple[str, str, Evidence, Confidence]] = []

        echo_rule = next((r for r in self.rules if r.produce.get("entrypoint", {}).get("kind") == "http"), None)
        pgx_rule = next((r for r in self.rules if "sql" in r.produce), None)
        ticker_rule = next((r for r in self.rules if r.detect.get("go_call")), None)

        for info in infos:
            prefix_map = self._path_prefixes(info, echo_rule)
            package_consts = dict(info.consts)
            # merge same-package consts from other files
            for other in infos:
                if other.package == info.package:
                    package_consts.update(other.consts)

            for fn in info.functions:
                env = dict(package_consts)
                self._scan_function_env(fn, env, package_consts)
                for node in walk(fn.node):
                    if node.type != "call_expression":
                        continue
                    method = call_callee_name(node)
                    args = call_args(node)
                    evidence = Evidence(file=info.rel, line=line_of(node), symbol=fn.qualname)

                    callee = self._resolve_callee(
                        node, fn, info, all_structs, fn_index, by_name, by_method
                    )
                    if callee and callee.qualname != fn.qualname:
                        parent = node.parent
                        if parent is not None and parent.type == "go_statement":
                            # Goroutine start; ticker-job rule owns this, not the call graph.
                            pass
                        else:
                            conf = (
                                Confidence.EXACT
                                if callee.receiver and fn.receiver
                                else Confidence.INFERRED
                            )
                            calls.append((fn.qualname, callee.qualname, evidence, conf))

                    if pgx_rule and method_matches(method, (pgx_rule.detect.get("calls") or {}).get("method")):
                        if import_matches(info.imports, pgx_rule.when):
                            spec = (pgx_rule.detect.get("calls") or {}).get("arguments", {}).get("sql", {})
                            idx = int(spec.get("index", 1))
                            sql_node = args[idx] if len(args) > idx else None
                            resolved = _resolve_expr(sql_node, env, package_consts)
                            if resolved.usable and (looks_like_sql(resolved.text) or resolved.dynamic):
                                sql_sites.append(
                                    SqlSite(
                                        function=fn,
                                        sql=resolved.text or "",
                                        dynamic=resolved.dynamic or not resolved.fully_static,
                                        evidence=Evidence(
                                            file=info.rel, line=line_of(node), symbol=fn.qualname
                                        ),
                                        fully_static=resolved.fully_static,
                                    )
                                )
                            elif sql_node is not None and not resolved.fully_static:
                                # dynamic SQL we couldn't fully resolve
                                guess = resolved.text or ""
                                if looks_like_sql(guess) or (guess and re.search(r"(?i)\b(UPDATE|INSERT|DELETE|SELECT)\b", guess)):
                                    sql_sites.append(
                                        SqlSite(
                                            function=fn,
                                            sql=guess,
                                            dynamic=True,
                                            evidence=Evidence(
                                                file=info.rel, line=line_of(node), symbol=fn.qualname
                                            ),
                                            fully_static=False,
                                        )
                                    )

                    if echo_rule and method_matches(
                        method, (echo_rule.detect.get("calls") or {}).get("method")
                    ):
                        if import_matches(info.imports, echo_rule.when):
                            argspec = (echo_rule.detect.get("calls") or {}).get("arguments") or {}
                            path_idx = int(argspec.get("path", {}).get("index", 0))
                            handler_idx = int(argspec.get("handler", {}).get("index", 1))
                            path_node = args[path_idx] if len(args) > path_idx else None
                            handler_node = args[handler_idx] if len(args) > handler_idx else None
                            path_res = _resolve_expr(path_node, env, package_consts)
                            prefix = self._receiver_prefix(node, prefix_map, echo_rule)
                            if path_res.usable and handler_node is not None:
                                full_path = _join_path(prefix, path_res.text or "")
                                routes.append(
                                    RouteHit(
                                        method=method.upper(),
                                        path=full_path,
                                        handler_expr=text(handler_node),
                                        file=info.rel,
                                        line=line_of(node),
                                        evidence=Evidence(
                                            file=info.rel,
                                            line=line_of(node),
                                            symbol=text(handler_node),
                                        ),
                                    )
                                )

        if ticker_rule:
            go_method = (ticker_rule.detect.get("go_call") or {}).get("method", "Run")
            req = ticker_rule.detect.get("requires_inside_callee") or {}
            req_method = (req.get("calls") or {}).get("method", "NewTicker")
            req_recv = (req.get("calls") or {}).get("receiver", "time")
            for info in infos:
                for node in walk(info.tree):
                    if node.type != "go_statement":
                        continue
                    call = next((c for c in node.named_children if c.type == "call_expression"), None)
                    if call is None:
                        continue
                    if call_callee_name(call) != go_method:
                        continue
                    dummy_fn = GoFunction(
                        package=info.package,
                        name="main",
                        receiver=None,
                        receiver_var=None,
                        file=info.rel,
                        line=line_of(node),
                        why=None,
                        node=node,
                        source=info.source,
                    )
                    # find enclosing function for type env
                    enclosing = None
                    for fn in info.functions:
                        if fn.node.start_byte <= node.start_byte <= fn.node.end_byte:
                            enclosing = fn
                            break
                    ctx_fn = enclosing or dummy_fn
                    callee = self._resolve_callee(
                        call, ctx_fn, info, all_structs, fn_index, by_name, by_method
                    )
                    if callee is None:
                        # unique Run method
                        candidates = by_name.get(go_method) or []
                        callee = candidates[0] if len(candidates) == 1 else None
                    if callee is None:
                        continue
                    if _contains_call(callee, req_method, req_recv):
                        jobs.append(
                            JobHit(
                                handler=callee,
                                file=info.rel,
                                line=line_of(node),
                                evidence=Evidence(
                                    file=info.rel,
                                    line=line_of(node),
                                    symbol=callee.qualname,
                                ),
                            )
                        )

        return GoAnalysis(
            functions=fn_index,
            sql_sites=sql_sites,
            routes=routes,
            jobs=jobs,
            calls=calls,
        )

    def _function(self, node: Node, pkg: str, rel_file: str, source: bytes) -> GoFunction:
        name_node = node.child_by_field_name("name")
        name = text(name_node) or "unknown"
        recv_name, recv_var, recv_type = _receiver(node, pkg)
        params = _param_types(node.child_by_field_name("parameters"), pkg)
        if recv_var and recv_type:
            params[recv_var] = recv_type
        return GoFunction(
            package=pkg,
            name=name,
            receiver=recv_name,
            receiver_var=recv_var,
            file=rel_file,
            line=line_of(node),
            why=_preceding_comment(node, source),
            node=node,
            source=source,
            params=params,
            return_type=_result_type(node, pkg),
        )

    def _scan_function_env(
        self,
        fn: GoFunction,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
    ) -> None:
        for node in walk(fn.node):
            if node.type in {"short_var_declaration", "assignment_statement", "const_declaration"}:
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if node.type == "const_declaration":
                    continue
                names: list[str] = []
                if left:
                    names = [text(c) for c in left.named_children if c.type == "identifier"]
                else:
                    # short_var: first expression_list is names
                    lists = [c for c in node.named_children if c.type == "expression_list"]
                    if lists:
                        names = [text(c) for c in lists[0].named_children if c.type == "identifier"]
                        if len(lists) > 1:
                            right = lists[1]
                if not names or right is None:
                    continue
                value_node = right.named_children[0] if right.type == "expression_list" and right.named_children else right
                resolved = _resolve_expr(value_node, env, package_consts)
                env[names[0]] = resolved

    def _path_prefixes(self, info: _FileInfo, echo_rule: Rule | None) -> dict[str, str]:
        prefixes: dict[str, str] = {}
        group_method = "Group"
        path_arg = 0
        if echo_rule:
            opts = echo_rule.options.get("receiver_path_prefixes") or {}
            group_method = opts.get("group_method", "Group")
            path_arg = int(opts.get("path_arg", 0))

        def prefix_of_receiver(recv: Node | None) -> str:
            if recv is None:
                return ""
            if recv.type == "identifier":
                return prefixes.get(text(recv), "")
            if recv.type == "call_expression" and call_callee_name(recv) == group_method:
                args = call_args(recv)
                path = _go_string_node(args[path_arg]) if len(args) > path_arg else ""
                inner = recv.child_by_field_name("function")
                operand = inner.child_by_field_name("operand") if inner and inner.type == "selector_expression" else None
                return _join_path(prefix_of_receiver(operand), path or "")
            if recv.type == "selector_expression":
                return prefix_of_receiver(recv.child_by_field_name("operand"))
            return ""

        changed = True
        while changed:
            changed = False
            for node in walk(info.tree):
                if node.type not in {"short_var_declaration", "assignment_statement"}:
                    continue
                lists = [c for c in node.named_children if c.type == "expression_list"]
                if len(lists) < 2:
                    continue
                names = [c for c in lists[0].named_children if c.type == "identifier"]
                values = lists[1].named_children
                if not names or not values:
                    continue
                val = values[0]
                if val.type == "call_expression" and call_callee_name(val) == group_method:
                    args = call_args(val)
                    path = _go_string_node(args[path_arg]) if len(args) > path_arg else ""
                    func = val.child_by_field_name("function")
                    operand = func.child_by_field_name("operand") if func and func.type == "selector_expression" else None
                    new_prefix = _join_path(prefix_of_receiver(operand), path or "")
                    ident = text(names[0])
                    if prefixes.get(ident) != new_prefix:
                        prefixes[ident] = new_prefix
                        changed = True
                elif val.type == "call_expression" and call_callee_name(val) in {"New"}:
                    prefixes.setdefault(text(names[0]), "")
        return prefixes

    def _receiver_prefix(self, call: Node, prefixes: dict[str, str], echo_rule: Rule | None) -> str:
        func = call.child_by_field_name("function")
        if func is None or func.type != "selector_expression":
            return ""
        operand = func.child_by_field_name("operand")
        if operand is None:
            return ""
        if operand.type == "identifier":
            return prefixes.get(text(operand), "")
        if operand.type == "call_expression":
            # e.Group("/api").GET(...)
            group_method = "Group"
            path_arg = 0
            if echo_rule:
                opts = echo_rule.options.get("receiver_path_prefixes") or {}
                group_method = opts.get("group_method", "Group")
                path_arg = int(opts.get("path_arg", 0))
            if call_callee_name(operand) == group_method:
                args = call_args(operand)
                path = _go_string_node(args[path_arg]) if len(args) > path_arg else ""
                inner_func = operand.child_by_field_name("function")
                inner_op = inner_func.child_by_field_name("operand") if inner_func else None
                base = prefixes.get(text(inner_op), "") if inner_op and inner_op.type == "identifier" else ""
                return _join_path(base, path or "")
        return ""

    def _local_types(
        self,
        fn: GoFunction,
        info: _FileInfo,
        fn_index: dict[str, GoFunction],
        by_name: dict[str, list[GoFunction]],
    ) -> dict[str, GoType]:
        types = dict(fn.params)
        for node in walk(fn.node):
            if node.type not in {"short_var_declaration", "assignment_statement"}:
                continue
            lists = [c for c in node.named_children if c.type == "expression_list"]
            if len(lists) < 2:
                continue
            names = [c for c in lists[0].named_children if c.type == "identifier"]
            values = lists[1].named_children
            if not names or not values:
                continue
            val = values[0]
            if val.type == "unary_expression" and val.named_children:
                val = val.named_children[0]
            callee_fn: GoFunction | None = None
            if val.type == "call_expression":
                cname = call_callee_name(val)
                func = val.child_by_field_name("function")
                if func is not None and func.type == "selector_expression":
                    operand = func.child_by_field_name("operand")
                    pkg = text(operand) if operand is not None and operand.type == "identifier" else ""
                    real = info.import_aliases.get(pkg, pkg)
                    callee_fn = fn_index.get(f"{real}.{cname}") or fn_index.get(f"{pkg}.{cname}")
                if callee_fn is None:
                    candidates = [c for c in by_name.get(cname, []) if c.receiver is None]
                    if len(candidates) == 1:
                        callee_fn = candidates[0]
                if callee_fn and callee_fn.return_type:
                    types[text(names[0])] = callee_fn.return_type
            elif val.type == "composite_literal":
                typ = _parse_type(val.child_by_field_name("type"), fn.package)
                if typ:
                    types[text(names[0])] = typ
        return types

    def _type_of_expr(
        self,
        node: Node | None,
        fn: GoFunction,
        info: _FileInfo,
        all_structs: dict[tuple[str, str], dict[str, GoType]],
        local_types: dict[str, GoType] | None = None,
    ) -> GoType | None:
        env = local_types if local_types is not None else fn.params
        if node is None:
            return None
        if node.type == "identifier":
            return env.get(text(node)) or fn.params.get(text(node))
        if node.type == "selector_expression":
            operand = node.child_by_field_name("operand")
            field_n = node.child_by_field_name("field")
            base = self._type_of_expr(operand, fn, info, all_structs, local_types)
            if base and field_n:
                fields = all_structs.get(base.key) or {}
                return fields.get(text(field_n))
        if node.type == "call_expression":
            # constructor NewX -> look at return type later via callee; skip
            return None
        if node.type == "unary_expression" and node.named_children:
            return self._type_of_expr(node.named_children[0], fn, info, all_structs, local_types)
        return None

    def _resolve_callee(
        self,
        call: Node,
        fn: GoFunction,
        info: _FileInfo,
        all_structs: dict[tuple[str, str], dict[str, GoType]],
        fn_index: dict[str, GoFunction],
        by_name: dict[str, list[GoFunction]],
        by_method: dict[tuple[str, str, str], GoFunction],
    ) -> GoFunction | None:
        name = call_callee_name(call)
        func = call.child_by_field_name("function")
        local_types = self._local_types(fn, info, fn_index, by_name)
        if func is None:
            return None
        if func.type == "identifier":
            # same package function
            qn = f"{fn.package}.{name}"
            if qn in fn_index:
                return fn_index[qn]
            candidates = [c for c in by_name.get(name, []) if c.receiver is None]
            return candidates[0] if len(candidates) == 1 else None
        if func.type == "selector_expression":
            operand = func.child_by_field_name("operand")
            gtype = self._type_of_expr(operand, fn, info, all_structs, local_types)
            if gtype:
                found = by_method.get((gtype.package, gtype.name, name))
                if found:
                    return found
                # package may be import alias mapped to package name
                alias_pkg = info.import_aliases.get(gtype.package, gtype.package)
                found = by_method.get((alias_pkg, gtype.name, name))
                if found:
                    return found
                for (_pkg, typ, method), gf in by_method.items():
                    if typ == gtype.name and method == name:
                        return gf
            candidates = [c for c in by_name.get(name, []) if c.receiver]
            if len(candidates) == 1:
                return candidates[0]
            # ident.Method where ident is package name
            if operand is not None and operand.type == "identifier":
                pkg = text(operand)
                qn = f"{pkg}.{name}"
                if qn in fn_index:
                    return fn_index[qn]
                real = info.import_aliases.get(pkg, pkg)
                qn2 = f"{real}.{name}"
                if qn2 in fn_index:
                    return fn_index[qn2]
        return None
