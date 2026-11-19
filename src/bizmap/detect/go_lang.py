from __future__ import annotations

import codecs
import re
from collections import defaultdict
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

CRON_IMPORT_FRAGMENTS = (
    "github.com/robfig/cron",
    "github.com/go-co-op/gocron",
)
CRON_METHODS = {"AddFunc", "AddJob", "Schedule", "Start", "AddCronJob"}
TIME_TICKER_METHODS = {"NewTicker", "Tick"}
TIME_AFTER = "After"
HTTP_ROUTE_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "Any", "Match"}
_PATH_SELECTOR = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\}$")


def _parser() -> Parser:
    return Parser(_GO_LANGUAGE)


@dataclass
class ResolvedString:
    text: str | None
    fully_static: bool
    dynamic: bool
    placeholder: str | None = None

    @property
    def usable(self) -> bool:
        return bool(
            (self.text and self.text.strip()) or (self.placeholder and self.placeholder.strip())
        )

    @property
    def path_text(self) -> str:
        if self.fully_static and self.text is not None:
            return self.text
        if self.placeholder:
            return self.placeholder
        return self.text or ""


@dataclass
class GoType:
    package: str
    name: str
    import_path: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.import_path or self.package, self.name)


@dataclass
class GoFunction:
    package: str
    import_path: str
    package_clause: str
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
    synthetic: bool = False

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
    handler: GoFunction | None
    confidence: Confidence
    candidates: list[str]
    path_confidence: Confidence
    file: str
    line: int
    evidence: Evidence
    path_unresolved: bool = False


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
    calls: list[tuple[str, str, Evidence, Confidence]]
    situations: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _FileInfo:
    path: Path
    rel: str
    source: bytes
    tree: Node
    package: str
    import_path: str
    short_package: str
    imports: list[str]
    import_aliases: dict[str, str]
    consts: dict[str, ResolvedString]
    const_names: set[str]
    structs: dict[str, dict[str, GoType]]
    functions: list[GoFunction]
    func_type_names: set[str] = field(default_factory=set)
    struct_func_fields: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class _Indexes:
    fn_index: dict[str, GoFunction]
    by_func: dict[tuple[str, str], GoFunction]
    by_method: dict[tuple[str, str, str], GoFunction]
    by_name: dict[str, list[GoFunction]]
    all_structs: dict[tuple[str, str], dict[str, GoType]]
    consts_by_import: dict[tuple[str, str], ResolvedString]
    const_names_by_import: dict[str, set[str]]
    func_fields: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    field_funcs: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    ctor_field_params: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)


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


def _parse_module_path(gomod: Path) -> str | None:
    try:
        for line in gomod.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("module "):
                return stripped.split(None, 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


def _module_for(file: Path) -> tuple[str | None, Path | None]:
    for parent in [file.parent, *file.parent.parents]:
        gomod = parent / "go.mod"
        if gomod.is_file():
            return _parse_module_path(gomod), parent
    return None, None


def _import_path_for(file: Path, root: Path, pkg_clause: str) -> str:
    mod, mod_root = _module_for(file)
    if mod and mod_root:
        try:
            rel_dir = file.parent.resolve().relative_to(mod_root.resolve())
        except ValueError:
            rel_dir = Path()
        if rel_dir == Path("."):
            return mod
        return f"{mod}/{rel_dir.as_posix()}"
    try:
        rel_dir = file.parent.resolve().relative_to(root.resolve())
    except ValueError:
        return pkg_clause
    if rel_dir == Path("."):
        return pkg_clause
    return rel_dir.as_posix()


def _package_display(file: Path, root: Path, pkg_clause: str) -> str:
    """One spelling for every function: directory relative to the scan root.

    Files at the scan root keep the ``package`` clause (``main``). Nested
    packages always use the directory path, so ``internal/services/appointments``
    never collides with a sibling ``appointments`` clause.
    """
    try:
        rel_dir = file.parent.resolve().relative_to(root.resolve())
    except ValueError:
        return pkg_clause
    if rel_dir == Path():
        return pkg_clause
    return rel_dir.as_posix()


def _preceding_comment(fn_node: Node, source: bytes) -> str | None:
    comments: list[Node] = []
    prev = fn_node.prev_named_sibling
    while prev is not None and prev.type == "comment":
        comments.append(prev)
        prev = prev.prev_named_sibling
    if not comments:
        return None
    comments.reverse()
    kept: list[str] = []
    fn_start = fn_node.start_byte
    last_end = comments[0].end_byte
    for i, comment in enumerate(comments):
        if i == 0:
            kept.append(text(comment))
            last_end = comment.end_byte
            continue
        between = source[last_end : comment.start_byte].decode("utf-8", errors="replace")
        if between.count("\n") > 2:
            kept = [text(comment)]
        else:
            kept.append(text(comment))
        last_end = comment.end_byte
    between_fn = source[comments[-1].end_byte : fn_start].decode("utf-8", errors="replace")
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


def _parse_type(
    node: Node | None,
    current_pkg: str,
    current_ip: str,
    aliases: dict[str, str],
) -> GoType | None:
    if node is None:
        return None
    if node.type == "pointer_type":
        inner = node.child_by_field_name("element") or (
            node.named_children[0] if node.named_children else None
        )
        return _parse_type(inner, current_pkg, current_ip, aliases)
    if node.type == "qualified_type":
        pkg = text(node.child_by_field_name("package"))
        name = text(node.child_by_field_name("name"))
        return GoType(package=pkg, name=name, import_path=aliases.get(pkg))
    if node.type in {"type_identifier", "identifier"}:
        return GoType(package=current_pkg, name=text(node), import_path=current_ip)
    if node.type == "generic_type" and node.named_children:
        return _parse_type(node.named_children[0], current_pkg, current_ip, aliases)
    if node.named_children:
        return _parse_type(node.named_children[-1], current_pkg, current_ip, aliases)
    raw = text(node).lstrip("*")
    if "." in raw:
        pkg, name = raw.split(".", 1)
        return GoType(package=pkg, name=name, import_path=aliases.get(pkg))
    if raw:
        return GoType(package=current_pkg, name=raw, import_path=current_ip)
    return None


def _param_types(
    param_list: Node | None, pkg: str, import_path: str, aliases: dict[str, str]
) -> dict[str, GoType]:
    out: dict[str, GoType] = {}
    if param_list is None:
        return out
    for decl in param_list.named_children:
        if decl.type != "parameter_declaration":
            continue
        type_node = decl.child_by_field_name("type")
        gtype = _parse_type(type_node, pkg, import_path, aliases)
        names = [c for c in decl.named_children if c.type == "identifier"]
        for ident in names:
            if gtype:
                out[text(ident)] = gtype
    return out


def _result_type(fn_node: Node, pkg: str, import_path: str, aliases: dict[str, str]) -> GoType | None:
    result = fn_node.child_by_field_name("result")
    if result is None:
        return None
    if result.type == "parameter_list":
        for decl in result.named_children:
            t = decl.child_by_field_name("type") if decl.type == "parameter_declaration" else decl
            parsed = _parse_type(t, pkg, import_path, aliases)
            if parsed:
                return parsed
        return None
    return _parse_type(result, pkg, import_path, aliases)


def _receiver(
    fn_node: Node, pkg: str, import_path: str, aliases: dict[str, str]
) -> tuple[str | None, str | None, GoType | None]:
    recv = fn_node.child_by_field_name("receiver")
    if recv is None:
        return None, None, None
    for decl in recv.named_children:
        if decl.type != "parameter_declaration":
            continue
        names = [c for c in decl.named_children if c.type == "identifier"]
        var = text(names[0]) if names else None
        gtype = _parse_type(decl.child_by_field_name("type"), pkg, import_path, aliases)
        return (gtype.name if gtype else None), var, gtype
    return None, None, None


def _struct_fields(
    type_decl: Node, pkg: str, import_path: str, aliases: dict[str, str]
) -> dict[str, dict[str, GoType]]:
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
            ftype = _parse_type(fd.child_by_field_name("type"), pkg, import_path, aliases)
            names = [c for c in fd.named_children if c.type in {"field_identifier", "identifier"}]
            for ident in names:
                if ftype:
                    fields[text(ident)] = ftype
        if name:
            out[name] = fields
    return out


def _func_type_names(type_decl: Node) -> set[str]:
    names: set[str] = set()
    for spec in type_decl.named_children:
        if spec.type != "type_spec":
            continue
        name = text(spec.child_by_field_name("name"))
        typ = spec.child_by_field_name("type")
        if name and typ is not None and typ.type == "function_type":
            names.add(name)
    return names


def _direct_func_fields(type_decl: Node) -> dict[str, set[str]]:
    """Struct fields whose declared type is ``func(...)``."""
    out: dict[str, set[str]] = {}
    for spec in type_decl.named_children:
        if spec.type != "type_spec":
            continue
        sname = text(spec.child_by_field_name("name"))
        typ = spec.child_by_field_name("type")
        if not sname or typ is None or typ.type != "struct_type":
            continue
        field_list = typ.child_by_field_name("body") or (
            next((c for c in typ.named_children if c.type == "field_declaration_list"), None)
        )
        if field_list is None:
            continue
        func_names: set[str] = set()
        for fd in field_list.named_children:
            if fd.type != "field_declaration":
                continue
            tnode = fd.child_by_field_name("type")
            is_func = tnode is not None and tnode.type == "function_type"
            names = [c for c in fd.named_children if c.type in {"field_identifier", "identifier"}]
            if is_func:
                for ident in names:
                    func_names.add(text(ident))
        if func_names:
            out[sname] = func_names
    return out


def _unwrap_literal(node: Node | None) -> Node | None:
    while node is not None:
        if node.type in {"literal_element", "parenthesized_expression", "expression_list"}:
            node = node.named_children[0] if node.named_children else None
            continue
        if node.type == "unary_expression":
            node = node.child_by_field_name("operand") or (
                node.named_children[0] if node.named_children else None
            )
            continue
        break
    return node


def _lookup_const(
    ip: str | None,
    name: str,
    consts_by_import: dict[tuple[str, str], ResolvedString],
) -> ResolvedString | None:
    """Resolve ``pkg.Const`` across scanned packages (exact import, then suffix)."""
    if not ip:
        named = [v for (_p, n), v in consts_by_import.items() if n == name and v.usable]
        return named[0] if len(named) == 1 else None
    found = consts_by_import.get((ip, name))
    if found and found.usable:
        return found
    suffix_hits = [
        v
        for (p, n), v in consts_by_import.items()
        if n == name
        and v.usable
        and (p.endswith("/" + ip) or ip.endswith("/" + p))
    ]
    if len(suffix_hits) == 1:
        return suffix_hits[0]
    return None


def _path_is_string_like(
    node: Node | None,
    resolved: ResolvedString,
    env: dict[str, ResolvedString],
    package_consts: dict[str, ResolvedString],
) -> bool:
    """True when a route path argument is a string/const, not ``h.options(c, ...)``."""
    if node is None or not resolved.usable:
        return False
    node = _unwrap_literal(node) or node
    if node.type in {"interpreted_string_literal", "raw_string_literal"}:
        return True
    if node.type == "binary_expression":
        return True
    if node.type == "selector_expression":
        return True
    if node.type == "identifier":
        name = text(node)
        if name in package_consts or name in env:
            return True
        return False
    if resolved.fully_static and resolved.text is not None:
        return True
    ph = resolved.placeholder or ""
    if "/" in ph or _PATH_SELECTOR.match(ph):
        return True
    return False


def _plus(lv: ResolvedString, rv: ResolvedString) -> ResolvedString:
    if lv.fully_static and rv.fully_static and lv.text is not None and rv.text is not None:
        return ResolvedString(lv.text + rv.text, True, False)

    def piece(part: ResolvedString) -> str:
        if part.fully_static and part.text is not None:
            return part.text
        if part.placeholder:
            return part.placeholder
        return part.text or ""

    combined = piece(lv) + piece(rv)
    return ResolvedString(combined or None, False, True, placeholder=combined or None)


def _resolve_expr(
    node: Node | None,
    env: dict[str, ResolvedString],
    package_consts: dict[str, ResolvedString],
    aliases: dict[str, str] | None = None,
    consts_by_import: dict[tuple[str, str], ResolvedString] | None = None,
) -> ResolvedString:
    aliases = aliases or {}
    consts_by_import = consts_by_import or {}
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
        return ResolvedString(None, False, True, placeholder=f"{{{name}}}")
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        field_n = node.child_by_field_name("field")
        if operand is not None and operand.type == "identifier" and field_n is not None:
            pkg = text(operand)
            name = text(field_n)
            ip = aliases.get(pkg)
            found = _lookup_const(ip, name, consts_by_import)
            if found and found.usable:
                return found
            return ResolvedString(None, False, True, placeholder=f"{{{pkg}.{name}}}")
        return ResolvedString(None, False, True, placeholder=f"{{{text(node)}}}")
    if node.type == "binary_expression":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        op_nodes = [c for c in node.children if not c.is_named]
        op = text(op_nodes[0]) if op_nodes else "+"
        lv = _resolve_expr(left, env, package_consts, aliases, consts_by_import)
        rv = _resolve_expr(right, env, package_consts, aliases, consts_by_import)
        if op != "+":
            return ResolvedString(None, False, True)
        return _plus(lv, rv)
    if node.type == "parenthesized_expression" and node.named_children:
        return _resolve_expr(node.named_children[0], env, package_consts, aliases, consts_by_import)
    if node.type == "expression_list" and node.named_children:
        return _resolve_expr(node.named_children[0], env, package_consts, aliases, consts_by_import)
    if node.type == "call_expression":
        return ResolvedString(None, False, True)
    return ResolvedString(None, False, True)


def _collect_package_strings(root: Node) -> tuple[dict[str, ResolvedString], set[str]]:
    out: dict[str, ResolvedString] = {}
    names: set[str] = set()
    for node in root.named_children:
        if node.type == "const_declaration":
            last: ResolvedString | None = None
            for spec in node.named_children:
                if spec.type != "const_spec":
                    continue
                name_node = spec.child_by_field_name("name")
                idents = [c for c in spec.named_children if c.type == "identifier"]
                spec_names = [text(name_node)] if name_node else [text(i) for i in idents]
                spec_names = [n for n in spec_names if n and n != "iota"]
                value_node = spec.child_by_field_name("value")
                if value_node is None:
                    for child in spec.named_children:
                        if child.type == "expression_list":
                            value_node = child.named_children[0] if child.named_children else child
                            break
                resolved: ResolvedString | None = None
                if value_node is not None:
                    resolved = _resolve_expr(value_node, out, out)
                    last = resolved
                elif last is not None:
                    resolved = last
                for n in spec_names:
                    names.add(n)
                    if resolved and resolved.fully_static and resolved.text is not None:
                        out[n] = resolved
                    else:
                        out.setdefault(n, ResolvedString(None, False, False, placeholder=f"{{{n}}}"))
        elif node.type == "var_declaration":
            for spec in node.named_children:
                if spec.type != "var_spec":
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
                names.add(text(name_node))
    return out, names


def _join_path(*parts: str) -> str:
    out = ""
    for part in parts:
        if part is None:
            continue
        piece = str(part).strip()
        if not piece:
            continue
        if piece.startswith("{") and not out:
            out = piece
            continue
        if out.endswith("}") and not piece.startswith("/") and not piece.startswith("{"):
            if not piece.startswith("+") and not piece.startswith("{"):
                piece = piece if piece.startswith("/") else "/" + piece
            out += piece
            continue
        if not piece.startswith("/") and not piece.startswith("{") and not out.endswith("/"):
            piece = "/" + piece
        if out.endswith("/"):
            out = out[:-1]
        if piece == "/":
            if not out:
                out = "/"
            continue
        if out and piece.startswith("{") and not out.endswith("/"):
            out += "/" + piece if not piece.startswith("/") else piece
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
        if alias and alias not in {".", "_"}:
            aliases[alias] = path
    return imports, aliases


def _in_loop(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in {"for_statement", "for_clause"}:
            return True
        parent = parent.parent
    return False


def _is_time_recv(operand: Node | None, info: _FileInfo) -> bool:
    if operand is None or operand.type != "identifier":
        return False
    name = text(operand)
    ip = info.import_aliases.get(name, "")
    return name == "time" or ip == "time" or ip.endswith("/time")


def _is_cron_recv(operand: Node | None, info: _FileInfo) -> bool:
    if operand is None:
        return False
    raw = text(operand)
    ip = info.import_aliases.get(raw, raw)
    if any(frag in (ip or "") for frag in CRON_IMPORT_FRAGMENTS):
        return True
    return "cron" in (ip or "").lower()


def _is_scheduler_call(call: Node, info: _FileInfo) -> bool:
    method = call_callee_name(call)
    func = call.child_by_field_name("function")
    operand = func.child_by_field_name("operand") if func is not None and func.type == "selector_expression" else None
    if _is_time_recv(operand, info):
        if method in TIME_TICKER_METHODS:
            return True
        if method == TIME_AFTER and _in_loop(call):
            return True
    if method in CRON_METHODS and (
        _is_cron_recv(operand, info)
        or any(any(frag in imp for frag in CRON_IMPORT_FRAGMENTS) for imp in info.imports)
    ):
        return True
    return False


class GoAnalyzer:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = [r for r in rules if r.language == "go" and not r.is_stub]
        self._parser = _parser()
        self._local_types_cache: dict[int, dict[str, GoType]] = {}
        self._file_types_cache: dict[str, dict[str, GoType]] = {}

    def analyze(self, files: list[Path], root: Path) -> GoAnalysis:
        self._local_types_cache.clear()
        self._file_types_cache.clear()
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
            import_path = _import_path_for(path, root, pkg)
            display = _package_display(path, root, pkg)
            structs: dict[str, dict[str, GoType]] = {}
            functions: list[GoFunction] = []
            func_type_names: set[str] = set()
            struct_func_fields: dict[str, set[str]] = {}
            for node in tree.named_children:
                if node.type == "type_declaration":
                    structs.update(_struct_fields(node, pkg, import_path, aliases))
                    func_type_names.update(_func_type_names(node))
                    for sname, fnames in _direct_func_fields(node).items():
                        struct_func_fields.setdefault(sname, set()).update(fnames)
                if node.type in {"function_declaration", "method_declaration"}:
                    functions.append(
                        self._function(node, display, pkg, import_path, aliases, rel(path, root), source)
                    )
            consts, const_names = _collect_package_strings(tree)
            infos.append(
                _FileInfo(
                    path=path,
                    rel=rel(path, root),
                    source=source,
                    tree=tree,
                    package=pkg,
                    import_path=import_path,
                    short_package=display,
                    imports=imports,
                    import_aliases=aliases,
                    consts=consts,
                    const_names=const_names,
                    structs=structs,
                    functions=functions,
                    func_type_names=func_type_names,
                    struct_func_fields=struct_func_fields,
                )
            )

        idx = self._build_indexes(infos)
        self._collect_func_field_bindings(infos, idx)

        sql_sites: list[SqlSite] = []
        routes: list[RouteHit] = []
        jobs: list[JobHit] = []
        calls: list[tuple[str, str, Evidence, Confidence]] = []
        situations: dict[str, list[str]] = defaultdict(list)

        echo_rule = next((r for r in self.rules if r.produce.get("entrypoint", {}).get("kind") == "http"), None)
        pgx_rule = next((r for r in self.rules if "sql" in r.produce), None)
        ticker_rule = next((r for r in self.rules if r.detect.get("go_call") or r.detect.get("go_statement")), None)

        for info in infos:
            package_consts: dict[str, ResolvedString] = {}
            for other in infos:
                if other.import_path == info.import_path:
                    package_consts.update(other.consts)
            prefix_map = self._path_prefixes(info, echo_rule, package_consts, idx)

            for fn in info.functions:
                env = dict(package_consts)
                self._scan_function_env(fn, env, package_consts, info, idx)
                for node in walk(fn.node):
                    if node.type != "call_expression":
                        continue
                    method = call_callee_name(node)
                    args = call_args(node)
                    evidence = Evidence(file=info.rel, line=line_of(node), symbol=fn.qualname)

                    callee = self._resolve_callee(node, fn, info, idx)
                    parent = node.parent
                    is_go = parent is not None and parent.type == "go_statement"
                    if callee and callee.qualname != fn.qualname and not is_go:
                        conf = (
                            Confidence.EXACT
                            if callee.receiver and fn.receiver
                            else Confidence.INFERRED
                        )
                        calls.append((fn.qualname, callee.qualname, evidence, conf))
                        for ref in self._const_refs_in_args(args, info, idx):
                            if ref not in situations[callee.qualname]:
                                situations[callee.qualname].append(ref)

                    if pgx_rule and method_matches(method, (pgx_rule.detect.get("calls") or {}).get("method")):
                        if import_matches(info.imports, pgx_rule.when):
                            spec = (pgx_rule.detect.get("calls") or {}).get("arguments", {}).get("sql", {})
                            arg_i = int(spec.get("index", 1))
                            sql_node = args[arg_i] if len(args) > arg_i else None
                            resolved = _resolve_expr(
                                sql_node, env, package_consts, info.import_aliases, idx.consts_by_import
                            )
                            sql_text = resolved.path_text if resolved.usable else ""
                            if sql_text and (looks_like_sql(sql_text) or resolved.dynamic):
                                sql_sites.append(
                                    SqlSite(
                                        function=fn,
                                        sql=sql_text,
                                        dynamic=resolved.dynamic or not resolved.fully_static,
                                        evidence=Evidence(
                                            file=info.rel, line=line_of(node), symbol=fn.qualname
                                        ),
                                        fully_static=resolved.fully_static,
                                    )
                                )
                            elif sql_node is not None and not resolved.fully_static:
                                guess = sql_text
                                if looks_like_sql(guess) or (
                                    guess and re.search(r"(?i)\b(UPDATE|INSERT|DELETE|SELECT)\b", guess)
                                ):
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

                    if (
                        echo_rule
                        and method in HTTP_ROUTE_METHODS
                        and method_matches(method, (echo_rule.detect.get("calls") or {}).get("method"))
                    ):
                        if import_matches(info.imports, echo_rule.when):
                            argspec = (echo_rule.detect.get("calls") or {}).get("arguments") or {}
                            path_idx = int(argspec.get("path", {}).get("index", 0))
                            handler_idx = int(argspec.get("handler", {}).get("index", 1))
                            if method == "Match":
                                path_idx, handler_idx = 1, 2
                            path_node = args[path_idx] if len(args) > path_idx else None
                            handler_node = args[handler_idx] if len(args) > handler_idx else None
                            path_res = _resolve_expr(
                                path_node, env, package_consts, info.import_aliases, idx.consts_by_import
                            )
                            prefix = self._receiver_prefix(
                                node, prefix_map, echo_rule, env, package_consts, info, idx
                            )
                            if (
                                handler_node is not None
                                and _path_is_string_like(path_node, path_res, env, package_consts)
                            ):
                                full_path = _join_path(prefix, path_res.path_text)
                                path_unresolved = (not path_res.fully_static) or ("{" in full_path)
                                path_conf = (
                                    Confidence.EXACT if not path_unresolved else Confidence.UNKNOWN
                                )
                                handler, candidates, hconf = self._resolve_handler_node(
                                    handler_node, fn, info, idx
                                )
                                route_method = "ANY" if method == "Any" else (
                                    "MATCH" if method == "Match" else method
                                )
                                routes.append(
                                    RouteHit(
                                        method=route_method,
                                        path=full_path,
                                        handler_expr=text(handler_node),
                                        handler=handler,
                                        confidence=hconf,
                                        candidates=candidates,
                                        path_confidence=path_conf,
                                        file=info.rel,
                                        line=line_of(node),
                                        evidence=Evidence(
                                            file=info.rel,
                                            line=line_of(node),
                                            symbol=text(handler_node),
                                        ),
                                        path_unresolved=path_unresolved,
                                    )
                                )

        if ticker_rule:
            jobs.extend(self._detect_jobs(infos, idx, calls, ticker_rule))

        return GoAnalysis(
            functions=idx.fn_index,
            sql_sites=sql_sites,
            routes=routes,
            jobs=jobs,
            calls=calls,
            situations=dict(situations),
        )

    def _build_indexes(self, infos: list[_FileInfo]) -> _Indexes:
        fn_index: dict[str, GoFunction] = {}
        by_func: dict[tuple[str, str], GoFunction] = {}
        by_method: dict[tuple[str, str, str], GoFunction] = {}
        by_name: dict[str, list[GoFunction]] = {}
        all_structs: dict[tuple[str, str], dict[str, GoType]] = {}
        consts_by_import: dict[tuple[str, str], ResolvedString] = {}
        const_names_by_import: dict[str, set[str]] = defaultdict(set)
        func_aliases: set[tuple[str, str]] = set()
        func_fields: dict[tuple[str, str], set[str]] = defaultdict(set)
        for info in infos:
            for n in info.func_type_names:
                func_aliases.add((info.import_path, n))
        for info in infos:
            for sname, fields in info.structs.items():
                all_structs[(info.import_path, sname)] = fields
                direct = set(info.struct_func_fields.get(sname) or ())
                for fname, gtype in fields.items():
                    if (gtype.import_path or info.import_path, gtype.name) in func_aliases:
                        direct.add(fname)
                if direct:
                    func_fields[(info.import_path, sname)] = direct
            for cname, resolved in info.consts.items():
                consts_by_import[(info.import_path, cname)] = resolved
            const_names_by_import[info.import_path].update(info.const_names)
            for fn in info.functions:
                fn_index[fn.qualname] = fn
                by_name.setdefault(fn.name, []).append(fn)
                if fn.receiver:
                    by_method[(fn.import_path, fn.receiver, fn.name)] = fn
                else:
                    by_func[(fn.import_path, fn.name)] = fn
        return _Indexes(
            fn_index=fn_index,
            by_func=by_func,
            by_method=by_method,
            by_name=by_name,
            all_structs=all_structs,
            consts_by_import=consts_by_import,
            const_names_by_import=dict(const_names_by_import),
            func_fields=dict(func_fields),
            field_funcs=defaultdict(list),
            ctor_field_params=defaultdict(dict),
        )

    def _function(
        self,
        node: Node,
        display_pkg: str,
        pkg_clause: str,
        import_path: str,
        aliases: dict[str, str],
        rel_file: str,
        source: bytes,
    ) -> GoFunction:
        name_node = node.child_by_field_name("name")
        name = text(name_node) or "unknown"
        recv_name, recv_var, recv_type = _receiver(node, pkg_clause, import_path, aliases)
        params = _param_types(
            node.child_by_field_name("parameters"), pkg_clause, import_path, aliases
        )
        if recv_var and recv_type:
            params[recv_var] = recv_type
        return GoFunction(
            package=display_pkg,
            import_path=import_path,
            package_clause=pkg_clause,
            name=name,
            receiver=recv_name,
            receiver_var=recv_var,
            file=rel_file,
            line=line_of(node),
            why=_preceding_comment(node, source),
            node=node,
            source=source,
            params=params,
            return_type=_result_type(node, pkg_clause, import_path, aliases),
        )

    def _synthetic_func(self, info: _FileInfo, node: Node, name: str) -> GoFunction:
        return GoFunction(
            package=info.short_package,
            import_path=info.import_path,
            package_clause=info.package,
            name=name,
            receiver=None,
            receiver_var=None,
            file=info.rel,
            line=line_of(node),
            why=None,
            node=node,
            source=info.source,
            synthetic=True,
        )

    def _scan_function_env(
        self,
        fn: GoFunction,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
        info: _FileInfo,
        idx: _Indexes,
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
                    lists = [c for c in node.named_children if c.type == "expression_list"]
                    if lists:
                        names = [text(c) for c in lists[0].named_children if c.type == "identifier"]
                        if len(lists) > 1:
                            right = lists[1]
                if not names or right is None:
                    continue
                value_node = (
                    right.named_children[0]
                    if right.type == "expression_list" and right.named_children
                    else right
                )
                resolved = _resolve_expr(
                    value_node, env, package_consts, info.import_aliases, idx.consts_by_import
                )
                env[names[0]] = resolved

    def _path_prefixes(
        self,
        info: _FileInfo,
        echo_rule: Rule | None,
        package_consts: dict[str, ResolvedString],
        idx: _Indexes,
    ) -> dict[str, str]:
        prefixes: dict[str, str] = {}
        group_method = "Group"
        path_arg = 0
        if echo_rule:
            opts = echo_rule.options.get("receiver_path_prefixes") or {}
            group_method = opts.get("group_method", "Group")
            path_arg = int(opts.get("path_arg", 0))

        def resolve_path(arg: Node | None) -> str:
            if arg is None:
                return ""
            literal = _go_string_node(arg)
            if literal is not None:
                return literal
            res = _resolve_expr(arg, package_consts, package_consts, info.import_aliases, idx.consts_by_import)
            return res.path_text if res.usable else ""

        def prefix_of_receiver(recv: Node | None) -> str:
            if recv is None:
                return ""
            if recv.type == "identifier":
                return prefixes.get(text(recv), "")
            if recv.type == "call_expression" and call_callee_name(recv) == group_method:
                args = call_args(recv)
                path = resolve_path(args[path_arg] if len(args) > path_arg else None)
                inner = recv.child_by_field_name("function")
                operand = (
                    inner.child_by_field_name("operand")
                    if inner and inner.type == "selector_expression"
                    else None
                )
                return _join_path(prefix_of_receiver(operand), path)
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
                    path = resolve_path(args[path_arg] if len(args) > path_arg else None)
                    func = val.child_by_field_name("function")
                    operand = (
                        func.child_by_field_name("operand")
                        if func and func.type == "selector_expression"
                        else None
                    )
                    new_prefix = _join_path(prefix_of_receiver(operand), path)
                    ident = text(names[0])
                    if prefixes.get(ident) != new_prefix:
                        prefixes[ident] = new_prefix
                        changed = True
                elif val.type == "call_expression" and call_callee_name(val) in {"New"}:
                    prefixes.setdefault(text(names[0]), "")
        return prefixes

    def _receiver_prefix(
        self,
        call: Node,
        prefixes: dict[str, str],
        echo_rule: Rule | None,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
        info: _FileInfo,
        idx: _Indexes,
    ) -> str:
        func = call.child_by_field_name("function")
        if func is None or func.type != "selector_expression":
            return ""
        operand = func.child_by_field_name("operand")
        if operand is None:
            return ""
        if operand.type == "identifier":
            return prefixes.get(text(operand), "")
        if operand.type == "call_expression":
            group_method = "Group"
            path_arg = 0
            if echo_rule:
                opts = echo_rule.options.get("receiver_path_prefixes") or {}
                group_method = opts.get("group_method", "Group")
                path_arg = int(opts.get("path_arg", 0))
            if call_callee_name(operand) == group_method:
                args = call_args(operand)
                arg = args[path_arg] if len(args) > path_arg else None
                path_res = _resolve_expr(arg, env, package_consts, info.import_aliases, idx.consts_by_import)
                path = path_res.path_text if path_res.usable else (_go_string_node(arg) if arg else "") or ""
                inner_func = operand.child_by_field_name("function")
                inner_op = inner_func.child_by_field_name("operand") if inner_func else None
                base = prefixes.get(text(inner_op), "") if inner_op and inner_op.type == "identifier" else ""
                return _join_path(base, path)
        return ""

    def _assignment_pairs(self, node: Node) -> list[tuple[str, Node]]:
        lists = [c for c in node.named_children if c.type == "expression_list"]
        if node.type == "var_spec":
            name_node = node.child_by_field_name("name")
            names = [text(name_node)] if name_node else [
                text(c) for c in node.named_children if c.type == "identifier"
            ]
            value = node.child_by_field_name("value")
            type_node = node.child_by_field_name("type")
            out: list[tuple[str, Node]] = []
            if names and value is not None:
                val = value.named_children[0] if value.type == "expression_list" and value.named_children else value
                out.append((names[0], val))
            elif names and type_node is not None:
                out.append((names[0], type_node))
            return out
        if len(lists) < 2:
            return []
        names = [c for c in lists[0].named_children if c.type == "identifier"]
        values = lists[1].named_children
        if not names or not values:
            return []
        return [(text(names[0]), values[0])]

    def _file_types(self, info: _FileInfo, idx: _Indexes) -> dict[str, GoType]:
        cached = self._file_types_cache.get(info.rel)
        if cached is not None:
            return cached
        types: dict[str, GoType] = {}
        dummy = self._synthetic_func(info, info.tree, "_file")
        dummy.params = {}
        for node in info.tree.named_children:
            if node.type == "var_declaration":
                for spec in node.named_children:
                    if spec.type != "var_spec":
                        continue
                    for name, val in self._assignment_pairs(spec):
                        inferred = self._infer_value_type(val, dummy, info, types, idx)
                        if inferred is None and val.type in {"type_identifier", "qualified_type", "pointer_type"}:
                            inferred = _parse_type(val, info.package, info.import_path, info.import_aliases)
                        if inferred:
                            types[name] = inferred
        self._file_types_cache[info.rel] = types
        return types

    def _local_types(self, fn: GoFunction, info: _FileInfo, idx: _Indexes) -> dict[str, GoType]:
        key = id(fn.node)
        cached = self._local_types_cache.get(key)
        if cached is not None:
            return cached
        types = dict(self._file_types(info, idx))
        types.update(fn.params)
        for node in walk(fn.node):
            if node.type not in {"short_var_declaration", "assignment_statement", "var_declaration"}:
                continue
            targets = []
            if node.type == "var_declaration":
                for spec in node.named_children:
                    if spec.type == "var_spec":
                        targets.extend(self._assignment_pairs(spec))
            else:
                targets = self._assignment_pairs(node)
            for name, val in targets:
                inferred = self._infer_value_type(val, fn, info, types, idx)
                if inferred:
                    types[name] = inferred
        self._local_types_cache[key] = types
        return types

    def _infer_value_type(
        self,
        val: Node,
        fn: GoFunction,
        info: _FileInfo,
        local_types: dict[str, GoType],
        idx: _Indexes,
    ) -> GoType | None:
        if val.type == "unary_expression":
            inner = val.child_by_field_name("operand") or (
                val.named_children[0] if val.named_children else None
            )
            if inner is not None:
                return self._infer_value_type(inner, fn, info, local_types, idx)
        if val.type == "parenthesized_expression" and val.named_children:
            return self._infer_value_type(val.named_children[0], fn, info, local_types, idx)
        if val.type == "composite_literal":
            return _parse_type(
                val.child_by_field_name("type"), fn.package_clause, fn.import_path, info.import_aliases
            )
        if val.type == "call_expression":
            return self._return_type_of_call(val, fn, info, local_types, idx)
        if val.type in {"type_identifier", "qualified_type", "pointer_type"}:
            return _parse_type(val, fn.package_clause, fn.import_path, info.import_aliases)
        return self._type_of_expr(val, fn, info, local_types, idx)

    def _return_type_of_call(
        self,
        call: Node,
        fn: GoFunction,
        info: _FileInfo,
        local_types: dict[str, GoType],
        idx: _Indexes,
    ) -> GoType | None:
        name = call_callee_name(call)
        func = call.child_by_field_name("function")
        if func is None:
            return None
        if func.type == "identifier":
            found = idx.by_func.get((fn.import_path, name))
            return found.return_type if found else None
        if func.type == "selector_expression":
            operand = func.child_by_field_name("operand")
            if operand is not None and operand.type == "identifier":
                ident = text(operand)
                if ident in info.import_aliases:
                    ip = info.import_aliases[ident]
                    found = idx.by_func.get((ip, name))
                    return found.return_type if found else None
            gtype = self._type_of_expr(operand, fn, info, local_types, idx)
            if gtype and gtype.import_path:
                found = idx.by_method.get((gtype.import_path, gtype.name, name))
                return found.return_type if found else None
        return None

    def _type_of_expr(
        self,
        node: Node | None,
        fn: GoFunction,
        info: _FileInfo,
        local_types: dict[str, GoType],
        idx: _Indexes,
    ) -> GoType | None:
        if node is None:
            return None
        if node.type == "parenthesized_expression" and node.named_children:
            return self._type_of_expr(node.named_children[0], fn, info, local_types, idx)
        if node.type == "unary_expression":
            inner = node.child_by_field_name("operand") or (
                node.named_children[0] if node.named_children else None
            )
            return self._type_of_expr(inner, fn, info, local_types, idx)
        if node.type == "identifier":
            name = text(node)
            return local_types.get(name) or fn.params.get(name)
        if node.type == "selector_expression":
            operand = node.child_by_field_name("operand")
            field_n = node.child_by_field_name("field")
            base = self._type_of_expr(operand, fn, info, local_types, idx)
            if base and field_n:
                fields = idx.all_structs.get(base.key) or {}
                return fields.get(text(field_n))
            return None
        if node.type == "composite_literal":
            return _parse_type(
                node.child_by_field_name("type"), fn.package_clause, fn.import_path, info.import_aliases
            )
        if node.type == "call_expression":
            return self._return_type_of_call(node, fn, info, local_types, idx)
        if node.type in {"type_conversion_expression", "conversion_expression", "type_assertion_expression"}:
            t = node.child_by_field_name("type")
            return _parse_type(t, fn.package_clause, fn.import_path, info.import_aliases)
        return None

    def _resolve_selector(
        self,
        func_node: Node,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
        local_types: dict[str, GoType],
    ) -> GoFunction | None:
        operand = func_node.child_by_field_name("operand")
        field_n = func_node.child_by_field_name("field")
        name = text(field_n) if field_n else ""
        gtype = self._type_of_expr(operand, fn, info, local_types, idx)
        if gtype:
            ip = gtype.import_path
            if ip:
                found = idx.by_method.get((ip, gtype.name, name))
                if found:
                    return found
                return self._resolve_func_field(ip, gtype.name, name, idx)
            # No resolved package: do not fall back to a globally unique method name.
            return None
        if operand is not None and operand.type == "identifier":
            ident = text(operand)
            if ident in info.import_aliases:
                ip = info.import_aliases[ident]
                return idx.by_func.get((ip, name))
            return None
        return None

    def _resolve_func_field(
        self, import_path: str, type_name: str, field: str, idx: _Indexes
    ) -> GoFunction | None:
        if field not in idx.func_fields.get((import_path, type_name), set()):
            return None
        assigned = idx.field_funcs.get((import_path, type_name, field), [])
        unique = list(dict.fromkeys(assigned))
        if len(unique) != 1:
            return None
        return idx.fn_index.get(unique[0])

    def _resolve_callee(
        self,
        call: Node,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
    ) -> GoFunction | None:
        name = call_callee_name(call)
        func = call.child_by_field_name("function")
        local_types = self._local_types(fn, info, idx)
        if func is None:
            return None
        if func.type == "identifier":
            # Unique within this package only — never a project-wide name fallback.
            return idx.by_func.get((fn.import_path, name))
        if func.type == "selector_expression":
            return self._resolve_selector(func, fn, info, idx, local_types)
        return None

    def _resolve_handler_node(
        self,
        node: Node,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
    ) -> tuple[GoFunction | None, list[str], Confidence]:
        node = _unwrap_literal(node) or node
        if node.type == "call_expression":
            # wrap(h.Version) / mw(handler): unwrap the inner func/method arg.
            inner = self._unwrap_wrapped_handler(node, fn, info, idx)
            if inner is not None:
                return inner, [inner.qualname], Confidence.EXACT
            callee = self._resolve_callee(node, fn, info, idx)
            name = call_callee_name(node)
            cands = [f.qualname for f in idx.by_name.get(name, [])]
            if callee:
                return callee, [callee.qualname], Confidence.EXACT
            return None, cands, Confidence.UNKNOWN
        if node.type == "selector_expression":
            local_types = self._local_types(fn, info, idx)
            found = self._resolve_selector(node, fn, info, idx, local_types)
            field = node.child_by_field_name("field")
            name = text(field) if field else ""
            cands = [f.qualname for f in idx.by_name.get(name, [])]
            if found:
                return found, [found.qualname], Confidence.EXACT
            return None, cands, Confidence.UNKNOWN
        if node.type == "identifier":
            name = text(node)
            found = idx.by_func.get((info.import_path, name))
            cands = [f.qualname for f in idx.by_name.get(name, [])]
            if found:
                return found, [found.qualname], Confidence.EXACT
            same = [f for f in idx.by_name.get(name, []) if f.import_path == info.import_path]
            if len(same) == 1:
                return same[0], [same[0].qualname], Confidence.EXACT
            return None, cands, Confidence.UNKNOWN
        if node.type == "unary_expression" and node.named_children:
            return self._resolve_handler_node(node.named_children[0], fn, info, idx)
        return None, [], Confidence.UNKNOWN

    def _unwrap_wrapped_handler(
        self,
        call: Node,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
    ) -> GoFunction | None:
        """If a wrapper takes a handler func, return that inner handler."""
        args = call_args(call)
        found: GoFunction | None = None
        for arg in args:
            inner = _unwrap_literal(arg) or arg
            if inner.type not in {"identifier", "selector_expression", "call_expression"}:
                continue
            resolved, _cands, conf = self._resolve_handler_node(inner, fn, info, idx)
            if resolved is not None and conf != Confidence.UNKNOWN:
                found = resolved
        return found

    def _resolve_func_expr(
        self,
        node: Node | None,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
    ) -> GoFunction | None:
        node = _unwrap_literal(node)
        if node is None:
            return None
        if node.type == "identifier":
            return idx.by_func.get((fn.import_path, text(node)))
        if node.type == "selector_expression":
            local_types = self._local_types(fn, info, idx)
            return self._resolve_selector(node, fn, info, idx, local_types)
        return None

    def _collect_func_field_bindings(self, infos: list[_FileInfo], idx: _Indexes) -> None:
        for info in infos:
            for fn in info.functions:
                for node in walk(fn.node):
                    lit = node
                    if node.type == "unary_expression":
                        inner = node.child_by_field_name("operand") or (
                            node.named_children[0] if node.named_children else None
                        )
                        if inner is None or inner.type != "composite_literal":
                            continue
                        lit = inner
                    if lit.type != "composite_literal":
                        continue
                    self._bind_composite(lit, fn, info, idx)
        for info in infos:
            for fn in info.functions:
                for node in walk(fn.node):
                    if node.type == "call_expression":
                        self._bind_constructor_call(node, fn, info, idx)

    def _bind_composite(
        self, lit: Node, fn: GoFunction, info: _FileInfo, idx: _Indexes
    ) -> None:
        gtype = _parse_type(
            lit.child_by_field_name("type"), fn.package_clause, fn.import_path, info.import_aliases
        )
        if gtype is None:
            return
        ip = gtype.import_path or fn.import_path
        func_fields = idx.func_fields.get((ip, gtype.name), set())
        if not func_fields:
            return
        body = None
        for child in lit.named_children:
            if child.type == "literal_value":
                body = child
                break
        if body is None:
            return
        for keyed in body.named_children:
            if keyed.type != "keyed_element":
                continue
            key_node = keyed.child_by_field_name("key")
            val_node = keyed.child_by_field_name("value")
            key_ident = _unwrap_literal(key_node)
            fname = text(key_ident) if key_ident is not None else ""
            if not fname or fname not in func_fields:
                continue
            val = _unwrap_literal(val_node)
            resolved = self._resolve_func_expr(val, fn, info, idx)
            if resolved is not None:
                idx.field_funcs[(ip, gtype.name, fname)].append(resolved.qualname)
            elif val is not None and val.type == "identifier":
                pname = text(val)
                if pname in fn.params:
                    idx.ctor_field_params[(fn.import_path, fn.name)][fname] = pname

    def _bind_constructor_call(
        self, call: Node, fn: GoFunction, info: _FileInfo, idx: _Indexes
    ) -> None:
        callee = self._resolve_callee(call, fn, info, idx)
        if callee is None or callee.return_type is None:
            return
        mapping = idx.ctor_field_params.get((callee.import_path, callee.name))
        if not mapping:
            return
        args = call_args(call)
        param_order = [p for p in callee.params if p != callee.receiver_var]
        ret = callee.return_type
        ret_ip = ret.import_path or callee.import_path
        for fname, pname in mapping.items():
            if pname not in param_order:
                continue
            i = param_order.index(pname)
            if i >= len(args):
                continue
            resolved = self._resolve_func_expr(args[i], fn, info, idx)
            if resolved is not None:
                idx.field_funcs[(ret_ip, ret.name, fname)].append(resolved.qualname)

    def _const_refs_in_args(self, args: list[Node], info: _FileInfo, idx: _Indexes) -> list[str]:
        refs: list[str] = []
        for arg in args:
            for node in walk(arg):
                if node.type != "selector_expression":
                    continue
                parent = node.parent
                if parent is not None and parent.type == "call_expression":
                    if parent.child_by_field_name("function") is node:
                        continue
                operand = node.child_by_field_name("operand")
                field_n = node.child_by_field_name("field")
                if operand is None or field_n is None or operand.type != "identifier":
                    continue
                pkg = text(operand)
                name = text(field_n)
                ip = info.import_aliases.get(pkg)
                if ip and name in idx.const_names_by_import.get(ip, set()):
                    ref = f"{pkg}.{name}"
                    if ref not in refs:
                        refs.append(ref)
                elif name in info.const_names and pkg not in info.import_aliases:
                    ref = name
                    if ref not in refs:
                        refs.append(ref)
        return refs

    def _enclosing(self, info: _FileInfo, node: Node) -> GoFunction:
        for fn in info.functions:
            if fn.node.start_byte <= node.start_byte <= fn.node.end_byte:
                return fn
        return self._synthetic_func(info, node, "main")

    def _detect_jobs(
        self,
        infos: list[_FileInfo],
        idx: _Indexes,
        calls: list[tuple[str, str, Evidence, Confidence]],
        ticker_rule: Rule,
    ) -> list[JobHit]:
        callees_of: dict[str, set[str]] = defaultdict(set)
        for caller, callee, _ev, _conf in calls:
            callees_of[caller].add(callee)
        jobs: list[JobHit] = []
        seen_handlers: set[str] = set()
        for info in infos:
            for node in walk(info.tree):
                if node.type != "go_statement":
                    continue
                call = next((c for c in node.named_children if c.type == "call_expression"), None)
                if call is None:
                    continue
                enclosing = self._enclosing(info, node)
                func = call.child_by_field_name("function")
                target: GoFunction | None
                if func is not None and func.type == "func_literal":
                    target = self._synthetic_func(info, func, f"go.func:L{line_of(func)}")
                    target.params = dict(self._local_types(enclosing, info, idx))
                    idx.fn_index[target.qualname] = target
                else:
                    target = self._resolve_callee(call, enclosing, info, idx)
                if target is None:
                    continue
                if not self._uses_scheduler(target, info, idx, callees_of, infos):
                    continue
                if target.qualname in seen_handlers:
                    continue
                seen_handlers.add(target.qualname)
                jobs.append(
                    JobHit(
                        handler=target,
                        file=info.rel,
                        line=line_of(node),
                        evidence=Evidence(
                            file=info.rel,
                            line=line_of(node),
                            symbol=target.qualname,
                        ),
                    )
                )
        return jobs

    def _uses_scheduler(
        self,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
        callees_of: dict[str, set[str]],
        infos: list[_FileInfo],
    ) -> bool:
        info_by_ip = {i.import_path: i for i in infos}
        seen: set[str] = set()
        stack = [fn]
        while stack:
            cur = stack.pop()
            if cur.qualname in seen:
                continue
            seen.add(cur.qualname)
            cur_info = info_by_ip.get(cur.import_path, info)
            for node in walk(cur.node):
                if node.type != "call_expression":
                    continue
                if _is_scheduler_call(node, cur_info):
                    return True
            for qn in callees_of.get(cur.qualname, ()):
                nxt = idx.fn_index.get(qn)
                if nxt is not None:
                    stack.append(nxt)
            # Closures / composite-literal bodies: resolve callees inside this node too.
            dummy_info = cur_info
            for node in walk(cur.node):
                if node.type != "call_expression":
                    continue
                parent = node.parent
                if parent is not None and parent.type == "go_statement":
                    continue
                callee = self._resolve_callee(node, cur, dummy_info, idx)
                if callee is not None and callee.qualname not in seen:
                    stack.append(callee)
        return False
