from __future__ import annotations

import codecs
import gc
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import tree_sitter_go as tsgo
from tree_sitter import Language, Node, Parser

from bizmap import progress
from bizmap.detect.rules import Rule, import_matches, method_matches
from bizmap.detect.tsutil import (
    call_args,
    call_callee_name,
    clear_typed_walks,
    flatten_plus,
    line_of,
    text,
    typed_walk,
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
GROUP_TYPE_NAMES = {"Group", "Echo"}
PREFIX_MAX_ITERS = 32
PREFIX_MAX_LEN = 1024
_PATH_SELECTOR = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\}$")
SQL_PARAM_NAMES = {"query", "q", "sql"}
SQL_DB_METHODS = {
    "Exec",
    "Query",
    "QueryRow",
    "ExecContext",
    "QueryContext",
    "QueryRowContext",
    "Get",
    "Select",
    "NamedExec",
    "NamedQuery",
    "MustExec",
    "Queryx",
    "QueryRowx",
}
_SQL_VERB = re.compile(r"(?i)\b(WITH|SELECT|INSERT|UPDATE|DELETE|MERGE|EXECUTE)\b")
HTTP_PATH_PARAM_NAMES = {"path", "urlpath", "p"}
HTTP_METHOD_PARAM_NAMES = {"method", "httpmethod"}
HTTP_BASE_FIELD_NAMES = {"baseurl", "base_url", "base", "host", "origin"}


def _parser() -> Parser:
    return Parser(_GO_LANGUAGE)


@dataclass
class ResolvedString:
    text: str | None
    fully_static: bool
    dynamic: bool
    placeholder: str | None = None
    alternatives: list[str] | None = None

    @property
    def usable(self) -> bool:
        if self.alternatives:
            return any(t.strip() for t in self.alternatives if t)
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

    def all_texts(self) -> list[str]:
        if self.alternatives:
            return [t for t in self.alternatives if t and str(t).strip()]
        text = self.path_text
        return [text] if text and text.strip() else []


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
    # `streamEvents(hub, visibleTo(pool))`: functions called to build the
    # handler a factory returns (they shape what the route does)
    factory_parts: list[GoFunction] = field(default_factory=list)


@dataclass
class JobHit:
    handler: GoFunction
    file: str
    line: int
    evidence: Evidence


@dataclass
class CallHit:
    caller: str
    callee: str
    evidence: Evidence
    confidence: Confidence
    candidates: list[str] = field(default_factory=list)


@dataclass
class UnresolvedCall:
    caller: str
    callee: str
    evidence: Evidence
    reason: str = "no_scanned_implementer"  # or type_not_scanned


@dataclass
class GoHttpCall:
    function: GoFunction
    method: str
    url: str
    evidence: Evidence
    unresolved: bool = False


@dataclass
class GoHttpHelper:
    function: GoFunction
    path_index: int
    method_index: int | None
    default_method: str
    base_field: str
    base: ResolvedString | None = None


@dataclass
class GoAnalysis:
    functions: dict[str, GoFunction]
    sql_sites: list[SqlSite]
    routes: list[RouteHit]
    jobs: list[JobHit]
    calls: list[CallHit]
    situations: dict[str, list[str]] = field(default_factory=dict)
    unresolved_calls: list[UnresolvedCall] = field(default_factory=list)
    http_calls: list[GoHttpCall] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
    struct_func_fields: dict[str, set[str]] = field(default_factory=set)
    interfaces: dict[str, set[str]] = field(default_factory=dict)
    iface_embeds: dict[str, list[GoType]] = field(default_factory=dict)
    iface_sigs: dict[str, dict[str, GoSig | None]] = field(default_factory=dict)


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
    interfaces: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    implementers: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    iface_fields: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    field_concretes: dict[tuple[str, str, str], list[GoType]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    missing_gomod: bool = False
    known_import_paths: set[str] = field(default_factory=set)
    import_match_cache: dict[str, str | None] = field(default_factory=dict)
    known_by_last: dict[str, list[str]] = field(default_factory=dict)


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


@cache
def _module_for_dir(directory: str) -> tuple[str | None, str | None]:
    path = Path(directory)
    for parent in [path, *path.parents]:
        gomod = parent / "go.mod"
        if gomod.is_file():
            return _parse_module_path(gomod), str(parent)
    return None, None


def _module_for(file: Path) -> tuple[str | None, Path | None]:
    mod, root_s = _module_for_dir(str(file.parent.resolve()))
    return mod, Path(root_s) if root_s else None


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


GoSig = tuple[tuple[str, ...], int]  # (parameter type leaves, number of results)


def _type_leaf(node: Node | None) -> str:
    """`*pkg.Order` / `[]pkg.Order` / `pkg.Order` → `Order`; `string` → `string`."""
    raw = text(node).strip() if node is not None else ""
    raw = raw.lstrip("*&").removeprefix("[]").lstrip("*")
    raw = raw.split("[", 1)[0]  # generic instantiation
    return raw.rsplit(".", 1)[-1] if raw else ""


def _signature(node: Node) -> GoSig | None:
    """Parameter type leaves and result count of a method / method_elem."""
    params = node.child_by_field_name("parameters")
    if params is None:
        return None
    leaves: list[str] = []
    for decl in params.named_children:
        if decl.type not in {"parameter_declaration", "variadic_parameter_declaration"}:
            continue
        typ = decl.child_by_field_name("type")
        leaf = ("..." if decl.type == "variadic_parameter_declaration" else "") + _type_leaf(typ)
        names = [c for c in decl.named_children if c.type == "identifier"]
        leaves.extend([leaf] * max(1, len(names)))
    result = node.child_by_field_name("result")
    if result is None:
        n_results = 0
    elif result.type == "parameter_list":
        n_results = 0
        for decl in result.named_children:
            if decl.type == "parameter_declaration":
                names = [c for c in decl.named_children if c.type == "identifier"]
                n_results += max(1, len(names))
    else:
        n_results = 1
    return tuple(leaves), n_results


def _signatures_match(want: GoSig | None, have: GoSig | None) -> bool:
    """Same number of parameters and results, and the same parameter type
    names (a one-letter type parameter, `any` or `interface{}` matches anything)."""
    if want is None or have is None:
        return True
    (wp, wr), (hp, hr) = want, have
    if wr != hr or len(wp) != len(hp):
        return False
    loose = {"any", "interface{}", ""}
    for a, b in zip(wp, hp, strict=True):
        a0, b0 = a.removeprefix("..."), b.removeprefix("...")
        if a0 in loose or b0 in loose or len(a0) == 1 or len(b0) == 1:
            continue
        if a0 != b0:
            return False
    return True


def _interface_specs(
    type_decl: Node, pkg: str, import_path: str, aliases: dict[str, str]
) -> tuple[dict[str, set[str]], dict[str, list[GoType]], dict[str, dict[str, GoSig | None]]]:
    """Named interfaces → method names, embedded interface types, and the
    signature of each method."""
    methods: dict[str, set[str]] = {}
    embeds: dict[str, list[GoType]] = {}
    sigs: dict[str, dict[str, GoSig | None]] = {}
    for spec in type_decl.named_children:
        if spec.type != "type_spec":
            continue
        name = text(spec.child_by_field_name("name"))
        typ = spec.child_by_field_name("type")
        if not name or typ is None or typ.type != "interface_type":
            continue
        meths: set[str] = set()
        embs: list[GoType] = []
        msigs: dict[str, GoSig | None] = {}
        for elem in typ.named_children:
            if elem.type == "method_elem":
                ident = elem.child_by_field_name("name") or next(
                    (c for c in elem.named_children if c.type in {"field_identifier", "identifier"}),
                    None,
                )
                if ident is not None:
                    meths.add(text(ident))
                    msigs[text(ident)] = _signature(elem)
            elif elem.type == "type_elem":
                inner = elem.named_children[0] if elem.named_children else elem
                parsed = _parse_type(inner, pkg, import_path, aliases)
                if parsed:
                    embs.append(parsed)
            elif elem.type in {"type_identifier", "qualified_type"}:
                parsed = _parse_type(elem, pkg, import_path, aliases)
                if parsed:
                    embs.append(parsed)
        methods[name] = meths
        embeds[name] = embs
        sigs[name] = msigs
    return methods, embeds, sigs


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


# id(consts_by_import) → (that dict, its size when indexed, name → import
# paths). Holding the dict keeps its id from being reused. Keys only: values
# are read from the dict at lookup time, so updates of existing keys are
# seen; a new key changes the size and rebuilds the index.
_CONST_NAME_INDEX: dict[int, tuple[dict, int, dict[str, list[str]]]] = {}


def _const_paths_named(consts_by_import: dict[tuple[str, str], ResolvedString], name: str) -> list[str]:
    cached = _CONST_NAME_INDEX.get(id(consts_by_import))
    if cached is None or cached[0] is not consts_by_import or cached[1] != len(consts_by_import):
        by_name: dict[str, list[str]] = {}
        for p, n in consts_by_import:
            if p not in {"*", ""}:
                by_name.setdefault(n, []).append(p)
        if len(_CONST_NAME_INDEX) > 16:
            _CONST_NAME_INDEX.clear()
        cached = (consts_by_import, len(consts_by_import), by_name)
        _CONST_NAME_INDEX[id(consts_by_import)] = cached
    return cached[2].get(name, [])


def _lookup_const(
    ip: str | None,
    name: str,
    consts_by_import: dict[tuple[str, str], ResolvedString],
) -> ResolvedString | None:
    """Resolve ``pkg.Const`` across scanned packages (exact import, then suffix)."""
    if ip:
        found = consts_by_import.get((ip, name))
        if found and found.usable:
            return found
        suffix_hits = [
            v
            for p in _const_paths_named(consts_by_import, name)
            if (v := consts_by_import[(p, name)]).usable
            and (p.endswith("/" + ip) or ip.endswith("/" + p) or p == ip)
        ]
        if len(suffix_hits) == 1:
            return suffix_hits[0]
        last = ip.rstrip("/").split("/")[-1]
        for key in ((last, name), ("*", f"{last}.{name}"), ("*", name), ("", name)):
            hit = consts_by_import.get(key)
            if hit and hit.usable:
                return hit
        dotted = consts_by_import.get(("*", f"{ip}.{name}"))
        if dotted and dotted.usable:
            return dotted
        return None
    named = [
        v for p in _const_paths_named(consts_by_import, name) if (v := consts_by_import[(p, name)]).usable
    ]
    if len(named) == 1:
        return named[0]
    for key in (("*", name), ("", name)):
        hit = consts_by_import.get(key)
        if hit and hit.usable:
            return hit
    return None


def _path_is_string_like(
    node: Node | None,
    resolved: ResolvedString,
    env: dict[str, ResolvedString],
    package_consts: dict[str, ResolvedString],
) -> bool:
    """True when a route path argument is a string/const, not ``h.options(c, ...)``."""
    if node is None:
        return False
    node = _unwrap_literal(node) or node
    if node.type in {"interpreted_string_literal", "raw_string_literal"}:
        # any literal, including "" (the group root: `orders.POST("", h)`)
        return True
    if not resolved.usable:
        return False
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


def _is_placeholder_token(sql: str) -> bool:
    stripped = sql.strip()
    return bool(stripped) and stripped.startswith("{") and stripped.endswith("}") and " " not in stripped


_SPRINTF_VERB = re.compile(r"%[#+\-\d\.]*[svqTdWxXefgGct]")


def _sprintf(
    fmt: str,
    arg_nodes: list[Node],
    env: dict[str, ResolvedString],
    package_consts: dict[str, ResolvedString],
    aliases: dict[str, str],
    consts_by_import: dict[tuple[str, str], ResolvedString],
) -> ResolvedString:
    result = fmt
    unresolved = False
    for arg in arg_nodes:
        m = _SPRINTF_VERB.search(result)
        if not m:
            break
        resolved = _resolve_expr(arg, env, package_consts, aliases, consts_by_import)
        if resolved.fully_static and resolved.text is not None:
            repl = resolved.text
        else:
            repl = resolved.placeholder or (
                "{" + (resolved.path_text.strip("{}") or "arg") + "}"
            )
            unresolved = True
        result = result[: m.start()] + repl + result[m.end() :]
    still = bool(_SPRINTF_VERB.search(result))
    return ResolvedString(
        result if not unresolved else None,
        not unresolved and not still,
        unresolved or still,
        placeholder=result if (unresolved or still) else None,
    )


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


_GO_TRANSPARENT_STRINGS = {
    "TrimRight",
    "TrimLeft",
    "TrimSuffix",
    "TrimPrefix",
    "TrimSpace",
    "Trim",
    "ToLower",
}


def _unwrap_go_transparent(node: Node | None, aliases: dict[str, str]) -> Node | None:
    """`strings.TrimRight(x, "/")` → `x` (repeatedly)."""
    cur = node
    for _ in range(4):
        cur = _unwrap_literal(cur) or cur
        if cur is None or cur.type != "call_expression":
            return cur
        func = cur.child_by_field_name("function")
        if func is None or func.type != "selector_expression":
            return cur
        operand = func.child_by_field_name("operand")
        recv = text(operand) if operand is not None else ""
        if call_callee_name(cur) not in _GO_TRANSPARENT_STRINGS:
            return cur
        if not (aliases.get(recv) == "strings" or recv == "strings"):
            return cur
        args = call_args(cur)
        if not args:
            return cur
        cur = args[0]
    return cur


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
            ip = aliases.get(pkg) or ""
            if (ip == "net/http" or pkg == "http") and name.startswith("Method") and len(name) > 6:
                return ResolvedString(name[6:].upper(), True, False)
            found = _lookup_const(ip, name, consts_by_import)
            if found and found.usable:
                return found
            if name.lower() in HTTP_BASE_FIELD_NAMES:
                return ResolvedString(None, False, True, placeholder="{base}")
            return ResolvedString(None, False, True, placeholder=f"{{{pkg}.{name}}}")
        return ResolvedString(None, False, True, placeholder=f"{{{text(node)}}}")
    if node.type == "binary_expression":
        operands = flatten_plus(node, {"binary_expression"})
        if operands is None:
            return ResolvedString(None, False, True)  # not a string `+`
        acc = _resolve_expr(operands[0], env, package_consts, aliases, consts_by_import)
        for operand in operands[1:]:
            acc = _plus(acc, _resolve_expr(operand, env, package_consts, aliases, consts_by_import))
        return acc
    if node.type == "parenthesized_expression" and node.named_children:
        return _resolve_expr(node.named_children[0], env, package_consts, aliases, consts_by_import)
    if node.type == "expression_list" and node.named_children:
        return _resolve_expr(node.named_children[0], env, package_consts, aliases, consts_by_import)
    if node.type == "call_expression":
        method = call_callee_name(node)
        args = call_args(node)
        func = node.child_by_field_name("function")
        operand = None
        if func is not None and func.type == "selector_expression":
            operand = func.child_by_field_name("operand")
        recv = text(operand) if operand is not None else ""
        ip = aliases.get(recv) or ""
        if method == "Sprintf" and args and (ip == "fmt" or recv == "fmt"):
            fmt_res = _resolve_expr(args[0], env, package_consts, aliases, consts_by_import)
            fmt_text = fmt_res.path_text if fmt_res.usable else ""
            if fmt_text:
                return _sprintf(fmt_text, args[1:], env, package_consts, aliases, consts_by_import)
        if method in {"PathEscape", "QueryEscape", "PathUnescape", "QueryUnescape"} and args:
            inner = args[0]
            if inner.type == "identifier":
                return ResolvedString(
                    None, False, True, placeholder="{" + text(inner) + "}"
                )
            resolved = _resolve_expr(inner, env, package_consts, aliases, consts_by_import)
            if resolved.placeholder:
                return resolved
            return ResolvedString(None, False, True, placeholder="{id}")
        if method in _GO_TRANSPARENT_STRINGS and args and (ip == "strings" or recv == "strings"):
            # strings.TrimRight(base, "/") & co. do not change where a base points
            return _resolve_expr(args[0], env, package_consts, aliases, consts_by_import)
        if method in {"JoinPath", "Join"} and args and (
            (method == "JoinPath" and (ip == "net/url" or recv == "url"))
            or (method == "Join" and (ip in {"path", "path/filepath"} or recv in {"path", "filepath"}))
        ):
            parts = [_resolve_expr(a, env, package_consts, aliases, consts_by_import) for a in args]
            joined = ""
            for part in parts:
                piece = part.path_text if part.usable else "{" + "part" + "}"
                joined = (joined.rstrip("/") + "/" + piece.lstrip("/")) if joined else piece
            static = all(p.fully_static for p in parts)
            return ResolvedString(joined if static else None, static, not static, placeholder=None if static else joined)
        if method in {"Getenv", "LookupEnv"} and args and (ip == "os" or recv == "os"):
            key = _resolve_expr(args[0], env, package_consts, aliases, consts_by_import)
            if key.fully_static and key.text:
                return ResolvedString(None, False, True, placeholder="{" + key.text + "}")
            if key.placeholder:
                return ResolvedString(None, False, True, placeholder=key.placeholder)
        return ResolvedString(None, False, True, placeholder=f"{{{text(node)[:48]}}}")
    if node.type == "composite_literal":
        alts = _composite_string_texts(node, env, package_consts, aliases, consts_by_import)
        if alts:
            static = all(s.fully_static for s in alts)
            texts = [s.path_text for s in alts if s.usable]
            if len(texts) == 1:
                return ResolvedString(texts[0], static, not static)
            return ResolvedString(
                texts[0] if texts else None,
                static,
                not static,
                alternatives=texts,
            )
    return ResolvedString(None, False, True)


def _composite_string_texts(
    node: Node,
    env: dict[str, ResolvedString],
    package_consts: dict[str, ResolvedString],
    aliases: dict[str, str],
    consts_by_import: dict[tuple[str, str], ResolvedString],
) -> list[ResolvedString]:
    """String elements of ``[]string{"UPDATE ...", "DELETE ..."}`` (and arrays)."""
    typ = node.child_by_field_name("type")
    if typ is not None and typ.type not in {"slice_type", "array_type"}:
        return []
    body = None
    for child in node.named_children:
        if child.type == "literal_value":
            body = child
            break
    if body is None:
        return []
    out: list[ResolvedString] = []
    for elem in body.named_children:
        inner = _unwrap_literal(elem) or elem
        if inner.type == "literal_element" and inner.named_children:
            inner = inner.named_children[0]
        resolved = _resolve_expr(inner, env, package_consts, aliases, consts_by_import)
        if resolved.usable:
            out.extend(
                ResolvedString(t, resolved.fully_static, not resolved.fully_static)
                for t in resolved.all_texts()
            )
    return out


def _collect_package_strings(
    root: Node,
    aliases: dict[str, str] | None = None,
    consts_by_import: dict[tuple[str, str], ResolvedString] | None = None,
) -> tuple[dict[str, ResolvedString], set[str]]:
    out: dict[str, ResolvedString] = {}
    names: set[str] = set()
    aliases = aliases or {}
    consts_by_import = consts_by_import or {}
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
                    resolved = _resolve_expr(value_node, out, out, aliases, consts_by_import)
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
                resolved = _resolve_expr(value_node, out, out, aliases, consts_by_import)
                out[text(name_node)] = resolved
                names.add(text(name_node))
    return out, names


def _merge_yaml_go_consts(
    consts_by_import: dict[tuple[str, str], ResolvedString],
    constants: dict[str, str] | None,
) -> None:
    for key, val in (constants or {}).items():
        resolved = ResolvedString(str(val), True, False)
        key_s = str(key)
        consts_by_import[("*", key_s)] = resolved
        if "." in key_s:
            pkg, name = key_s.rsplit(".", 1)
            consts_by_import[(pkg, name)] = resolved
            consts_by_import[("*", name)] = resolved
            consts_by_import[("", name)] = resolved
        else:
            consts_by_import[("*", key_s)] = resolved
            consts_by_import[("", key_s)] = resolved


def _resolve_cross_package_consts(
    infos: list[_FileInfo],
    constants: dict[str, str] | None = None,
) -> None:
    """Re-resolve ``const X = shared.Y`` / ``var Base = shared.Base + "/v1"``."""
    consts_by_import: dict[tuple[str, str], ResolvedString] = {}
    _merge_yaml_go_consts(consts_by_import, constants)
    for info in infos:
        for key, val in (constants or {}).items():
            name = str(key).rsplit(".", 1)[-1]
            existing = info.consts.get(name)
            if existing is None or not existing.usable:
                info.consts[name] = ResolvedString(str(val), True, False)
        for cname, resolved in info.consts.items():
            consts_by_import[(info.import_path, cname)] = resolved
    for _ in range(8):
        changed = False
        for info in infos:
            consts, _names = _collect_package_strings(
                info.tree, info.import_aliases, consts_by_import
            )
            for cname, resolved in consts.items():
                prev = info.consts.get(cname)
                if not resolved.usable:
                    continue
                if prev and prev.fully_static and prev.text == resolved.text:
                    continue
                if prev and prev.fully_static and not resolved.fully_static:
                    continue
                if prev is None or prev.path_text != resolved.path_text or prev.fully_static != resolved.fully_static:
                    info.consts[cname] = resolved
                    consts_by_import[(info.import_path, cname)] = resolved
                    changed = True
        if not changed:
            break


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


_CALL = frozenset({"call_expression"})
_ASSIGN = frozenset({"short_var_declaration", "assignment_statement"})
_ASSIGN_VAR = frozenset({"short_var_declaration", "assignment_statement", "var_declaration"})
_RETURN = frozenset({"return_statement"})
_TYPE_SPEC = frozenset({"type_spec"})
_COMPOSITE = frozenset({"composite_literal"})
_LITERALS = frozenset({"unary_expression", "composite_literal"})
_GO_STMT = frozenset({"go_statement"})
_ENV_NODES = frozenset({"range_clause", "short_var_declaration", "assignment_statement"})


def _same_project(a: str, b: str) -> bool:
    """Two import paths of one project: same host and owner
    (`example.com/acme/...`), never the standard library."""
    pa, pb = a.split("/"), b.split("/")
    if "." not in pa[0] or len(pa) < 3 or len(pb) < 3:
        return False
    return pa[:2] == pb[:2]


def _import_specs(root: Node) -> Iterator[Node]:
    """`import_spec` nodes: imports are top-level declarations, so only the
    root's `import_declaration` children are visited, not the whole file."""
    for decl in root.named_children:
        if decl.type != "import_declaration":
            continue
        for node in walk(decl):
            if node.type == "import_spec":
                yield node


def _import_paths(root: Node) -> tuple[list[str], dict[str, str]]:
    imports: list[str] = []
    aliases: dict[str, str] = {}
    for node in _import_specs(root):
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


def _call_lhs_names(call: Node) -> list[str]:
    """Names assigned from ``req, _ := http.NewRequest(...)``."""
    parent = call.parent
    while parent is not None and parent.type == "expression_list":
        parent = parent.parent
    if parent is None or parent.type not in {"short_var_declaration", "assignment_statement"}:
        return []
    left = parent.child_by_field_name("left")
    if left is None:
        lists = [c for c in parent.named_children if c.type == "expression_list"]
        left = lists[0] if lists else None
    if left is None:
        return []
    names: list[str] = []
    for child in left.named_children:
        if child.type == "identifier":
            raw = text(child)
            if raw and raw != "_":
                names.append(raw)
    return names


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

    def analyze(
        self,
        files: list[Path],
        root: Path,
        profile: object | None = None,
        constants: dict[str, str] | None = None,
    ) -> GoAnalysis:
        """Analyse the files. The garbage collector's thresholds are raised
        meanwhile: the phase allocates millions of short-lived objects next to
        a large live set (the syntax trees), and the default thresholds made
        full collections a growing share of the time on large repos."""
        old = gc.get_threshold()
        gc.set_threshold(max(old[0], 50_000), max(old[1], 50), max(old[2], 100))
        try:
            return self._analyze(files, root, profile, constants)
        finally:
            clear_typed_walks()
            gc.unfreeze()
            gc.set_threshold(*old)

    def _analyze(
        self,
        files: list[Path],
        root: Path,
        profile: object | None,
        constants: dict[str, str] | None,
    ) -> GoAnalysis:
        self._local_types_cache.clear()
        self._file_types_cache.clear()
        clear_typed_walks()
        self._file_http_bases: dict[str, dict[tuple[str, str, str], ResolvedString]] = {}
        self._yaml_constants = constants or {}
        infos: list[_FileInfo] = []
        t_parse = time.perf_counter()
        for parsed_n, path in enumerate(files):
            progress.counter("go: parsing", parsed_n, len(files))
            source = path.read_bytes()
            tree = self._parser.parse(source).root_node
            pkg_node = next((c for c in tree.named_children if c.type == "package_clause"), None)
            pkg = "main"
            if pkg_node:
                ident = next((c for c in pkg_node.named_children if c.type == "package_identifier"), None)
                pkg = text(ident) or "main"
            imports, aliases = _import_paths(tree)
            rel_file = rel(path, root)
            import_path = _import_path_for(path, root, pkg)
            display = _package_display(path, root, pkg)
            structs: dict[str, dict[str, GoType]] = {}
            functions: list[GoFunction] = []
            func_type_names: set[str] = set()
            struct_func_fields: dict[str, set[str]] = {}
            interfaces: dict[str, set[str]] = {}
            iface_embeds: dict[str, list[GoType]] = {}
            iface_sigs: dict[str, dict[str, GoSig | None]] = {}
            for node in tree.named_children:
                if node.type == "type_declaration":
                    structs.update(_struct_fields(node, pkg, import_path, aliases))
                    func_type_names.update(_func_type_names(node))
                    for sname, fnames in _direct_func_fields(node).items():
                        struct_func_fields.setdefault(sname, set()).update(fnames)
                    meths, embs, isigs = _interface_specs(node, pkg, import_path, aliases)
                    interfaces.update(meths)
                    iface_embeds.update(embs)
                    iface_sigs.update(isigs)
                if node.type in {"function_declaration", "method_declaration"}:
                    functions.append(
                        self._function(node, display, pkg, import_path, aliases, rel_file, source)
                    )
            consts, const_names = _collect_package_strings(tree, aliases)
            infos.append(
                _FileInfo(
                    path=path,
                    rel=rel_file,
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
                    interfaces=interfaces,
                    iface_embeds=iface_embeds,
                    iface_sigs=iface_sigs,
                )
            )

        if profile is not None:
            profile.parse += time.perf_counter() - t_parse
        # the parsed files live until the end of the phase: keep the collector
        # from re-scanning them on every collection
        gc.freeze()

        t_resolve = time.perf_counter()
        _resolve_cross_package_consts(infos, getattr(self, "_yaml_constants", None))
        idx = self._build_indexes(infos)
        self._collect_func_field_bindings(infos, idx)
        http_helpers = self._collect_http_helpers(infos, idx)

        sql_sites: list[SqlSite] = []
        routes: list[RouteHit] = []
        jobs: list[JobHit] = []
        calls: list[CallHit] = []
        unresolved_calls: list[UnresolvedCall] = []
        http_calls: list[GoHttpCall] = []
        situations: dict[str, list[str]] = defaultdict(list)

        echo_rule = next((r for r in self.rules if r.produce.get("entrypoint", {}).get("kind") == "http"), None)
        pgx_rule = next((r for r in self.rules if "sql" in r.produce), None)
        ticker_rule = next((r for r in self.rules if r.detect.get("go_call") or r.detect.get("go_statement")), None)

        prefix_by_fn: dict[str, dict[str, str]] = {}
        consts_by_info: dict[str, dict[str, ResolvedString]] = {}
        consts_by_package: dict[str, dict[str, ResolvedString]] = {}
        for info in infos:  # file order, as before: later files win on duplicates
            consts_by_package.setdefault(info.import_path, {}).update(info.consts)
        for prefix_n, info in enumerate(infos):
            progress.counter("go: route prefixes", prefix_n, len(infos))
            package_consts = dict(consts_by_package[info.import_path])
            consts_by_info[info.rel] = package_consts
            for fn in info.functions:
                prefixes = self._path_prefixes(fn, info, echo_rule, package_consts, idx)
                self._seed_echo_params(fn, prefixes)
                prefix_by_fn[fn.qualname] = prefixes
        self._propagate_group_prefixes(infos, idx, echo_rule, prefix_by_fn, consts_by_info)

        for done_files, info in enumerate(infos):
            package_consts = consts_by_info[info.rel]
            progress.counter("go: analysing", done_files, len(infos))

            for fn in info.functions:
                env = dict(package_consts)
                self._scan_function_env(fn, env, package_consts, info, idx)
                prefix_map = prefix_by_fn.get(fn.qualname) or {}
                req_vars: dict[str, tuple[str, str, bool]] = {}
                for node in typed_walk(fn.node, _CALL):
                    method = call_callee_name(node)
                    args = call_args(node)
                    evidence = Evidence(file=info.rel, line=line_of(node), symbol=fn.qualname)

                    callees = self._resolve_callees(node, fn, info, idx)
                    parent = node.parent
                    is_go = parent is not None and parent.type == "go_statement"
                    if callees and not is_go:
                        for callee, conf, cands in callees:
                            if callee.qualname == fn.qualname:
                                continue
                            calls.append(
                                CallHit(
                                    caller=fn.qualname,
                                    callee=callee.qualname,
                                    evidence=evidence,
                                    confidence=conf,
                                    candidates=list(cands),
                                )
                            )
                            for ref in self._const_refs_in_args(args, info, idx):
                                if ref not in situations[callee.qualname]:
                                    situations[callee.qualname].append(ref)
                    if not is_go and not any(c.qualname != fn.qualname for c, _cf, _cs in callees):
                        note = self._unresolved_iface_note(node, fn, info, idx)
                        if note:
                            unresolved_calls.append(
                                UnresolvedCall(
                                    caller=fn.qualname,
                                    callee=note,
                                    evidence=evidence,
                                    reason=self._unresolved_reason(node, fn, info, idx),
                                )
                            )
                    if callees and not is_go:
                        self._method_value_calls(args, fn, info, idx, evidence, calls)
                    self._sql_from_helper_call(
                        node, args, fn, info, idx, env, package_consts, sql_sites, callees
                    )

                    yaml_sql = bool(
                        pgx_rule
                        and method_matches(method, (pgx_rule.detect.get("calls") or {}).get("method"))
                    )
                    if method in SQL_DB_METHODS or yaml_sql:
                        if not pgx_rule or import_matches(info.imports, pgx_rule.when):
                            resolved, texts = self._pick_sql_arg(
                                args, fn, env, package_consts, info, idx
                            )
                            for sql_text in texts:
                                if not sql_text or _is_placeholder_token(sql_text):
                                    continue
                                if looks_like_sql(sql_text) or (resolved and resolved.dynamic):
                                    sql_sites.append(
                                        SqlSite(
                                            function=fn,
                                            sql=sql_text,
                                            dynamic=(
                                                (resolved.dynamic or not resolved.fully_static or len(texts) > 1)
                                                if resolved
                                                else True
                                            ),
                                            evidence=Evidence(
                                                file=info.rel, line=line_of(node), symbol=fn.qualname
                                            ),
                                            fully_static=bool(
                                                resolved
                                                and resolved.fully_static
                                                and len(texts) == 1
                                            ),
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
                            prefix, prefix_unresolved = self._receiver_prefix(
                                node, prefix_map, echo_rule, env, package_consts, info, idx, fn
                            )
                            if (
                                handler_node is not None
                                and _path_is_string_like(path_node, path_res, env, package_consts)
                            ):
                                full_path = _join_path(prefix, path_res.path_text)
                                path_unresolved = (
                                    (not path_res.fully_static)
                                    or prefix_unresolved
                                    or ("{" in full_path)
                                )
                                path_conf = (
                                    Confidence.EXACT if not path_unresolved else Confidence.UNKNOWN
                                )
                                handler, candidates, hconf = self._resolve_handler_node(
                                    handler_node, fn, info, idx
                                )
                                route_method = "ANY" if method == "Any" else (
                                    "MATCH" if method == "Match" else method
                                )
                                parts = self._factory_parts(handler_node, handler, fn, info, idx)
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
                                        factory_parts=parts,
                                    )
                                )

                    self._record_go_http(
                        node,
                        method,
                        args,
                        fn,
                        info,
                        idx,
                        env,
                        package_consts,
                        req_vars,
                        http_calls,
                        skip=fn.qualname in http_helpers,
                    )
                    self._record_http_helper_call(
                        node,
                        args,
                        fn,
                        info,
                        idx,
                        env,
                        package_consts,
                        callees,
                        http_helpers,
                        http_calls,
                    )

        if ticker_rule:
            jobs.extend(self._detect_jobs(infos, idx, calls, ticker_rule))

        if profile is not None:
            profile.resolve += time.perf_counter() - t_resolve

        return GoAnalysis(
            functions=idx.fn_index,
            sql_sites=sql_sites,
            routes=routes,
            jobs=jobs,
            calls=calls,
            situations=dict(situations),
            unresolved_calls=unresolved_calls,
            http_calls=http_calls,
            warnings=list(idx.warnings),
        )

    def _build_indexes(self, infos: list[_FileInfo]) -> _Indexes:
        fn_index: dict[str, GoFunction] = {}
        by_func: dict[tuple[str, str], GoFunction] = {}
        by_method: dict[tuple[str, str, str], GoFunction] = {}
        by_name: dict[str, list[GoFunction]] = {}
        all_structs: dict[tuple[str, str], dict[str, GoType]] = {}
        consts_by_import: dict[tuple[str, str], ResolvedString] = {}
        _merge_yaml_go_consts(consts_by_import, getattr(self, "_yaml_constants", None))
        const_names_by_import: dict[str, set[str]] = defaultdict(set)
        func_aliases: set[tuple[str, str]] = set()
        func_fields: dict[tuple[str, str], set[str]] = defaultdict(set)
        interfaces: dict[tuple[str, str], set[str]] = {}
        iface_embeds: dict[tuple[str, str], list[GoType]] = {}
        iface_sigs: dict[tuple[str, str], dict[str, GoSig | None]] = {}
        for info in infos:
            for n in info.func_type_names:
                func_aliases.add((info.import_path, n))
            for iname, meths in info.interfaces.items():
                interfaces[(info.import_path, iname)] = set(meths)
                iface_sigs[(info.import_path, iname)] = dict(info.iface_sigs.get(iname) or {})
            for iname, embs in info.iface_embeds.items():
                iface_embeds[(info.import_path, iname)] = list(embs)
        # Flatten embedded interface method sets.
        changed = True
        while changed:
            changed = False
            for key, embs in iface_embeds.items():
                cur = interfaces.setdefault(key, set())
                for g in embs:
                    ekey = (g.import_path or key[0], g.name)
                    extra = interfaces.get(ekey, set())
                    if extra - cur:
                        cur |= extra
                        changed = True
                    own = iface_sigs.setdefault(key, {})
                    for meth, sig in (iface_sigs.get(ekey) or {}).items():
                        own.setdefault(meth, sig)
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
        methods_of: dict[tuple[str, str], set[str]] = defaultdict(set)
        for ip, typ, meth in by_method:
            methods_of[(ip, typ)].add(meth)
        sig_cache: dict[tuple[str, str, str], GoSig | None] = {}

        def method_sig(type_key: tuple[str, str], meth: str) -> GoSig | None:
            key = (type_key[0], type_key[1], meth)
            if key not in sig_cache:
                fn = by_method.get(key)
                sig_cache[key] = _signature(fn.node) if fn is not None and fn.node is not None else None
            return sig_cache[key]

        implementers: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for iface_key, iface_meths in interfaces.items():
            if not iface_meths:
                continue
            unexported = any(m[:1].islower() for m in iface_meths)
            for type_key, type_meths in methods_of.items():
                if type_key in interfaces:
                    continue
                if unexported and type_key[0] != iface_key[0]:
                    continue
                if iface_meths <= type_meths and all(
                    _signatures_match(
                        (iface_sigs.get(iface_key) or {}).get(m),
                        method_sig(type_key, m),
                    )
                    for m in iface_meths
                ):
                    implementers[iface_key].append(type_key)
        for key in implementers:
            implementers[key] = sorted(implementers[key])
        iface_fields: dict[tuple[str, str], set[str]] = defaultdict(set)
        for (ip, sname), fields in all_structs.items():
            for fname, gtype in fields.items():
                if (gtype.import_path or ip, gtype.name) in interfaces:
                    iface_fields[(ip, sname)].add(fname)
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
            interfaces=interfaces,
            implementers=dict(implementers),
            iface_fields=dict(iface_fields),
            field_concretes=defaultdict(list),
            warnings=[],
            missing_gomod=any(_module_for(info.path)[0] is None for info in infos),
            known_import_paths={info.import_path for info in infos if info.import_path},
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
        for node in typed_walk(fn.node, _ENV_NODES):
            if node.type == "range_clause":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                raw = []
                if left is not None:
                    raw = [text(c) for c in left.named_children if c.type == "identifier"]
                else:
                    raw = [text(c) for c in node.named_children if c.type == "identifier"]
                if right is None or len(raw) < 2:
                    continue
                value_name = raw[-1]
                if not value_name or value_name == "_":
                    continue
                resolved = _resolve_expr(
                    right, env, package_consts, info.import_aliases, idx.consts_by_import
                )
                env[value_name] = resolved
                continue
            if node.type in {"short_var_declaration", "assignment_statement", "const_declaration"}:
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if node.type == "const_declaration":
                    continue
                names = []
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

    def _record_go_http(
        self,
        node: Node,
        method: str,
        args: list[Node],
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
        req_vars: dict[str, tuple[str, str, bool]],
        http_calls: list[GoHttpCall],
        skip: bool = False,
    ) -> None:
        func = node.child_by_field_name("function")
        operand = None
        if func is not None and func.type == "selector_expression":
            operand = func.child_by_field_name("operand")
        recv = text(operand) if operand is not None else ""
        ip = info.import_aliases.get(recv) or ""
        is_net_http = ip == "net/http" or recv == "http"
        if (
            not is_net_http
            and method in {"Get", "Post", "Head", "PostForm"}
            and "net/http" in info.imports
            and (
                recv == "http.DefaultClient"
                or recv.rsplit(".", 1)[-1].lower() in {"http", "client", "httpclient", "hc"}
            )
        ):
            # method on an *http.Client value: `c.http.Get(url)`, `client.Post(url, …)`
            is_net_http = True
        evidence = Evidence(file=info.rel, line=line_of(node), symbol=fn.qualname)

        def emit(http_method: str, url: str, unresolved: bool) -> None:
            if skip or not url:
                return
            if url.startswith("{base}") and fn.receiver:
                bases = getattr(self, "_field_bases", None) or {}
                for (ip_, typ_, _field), base in bases.items():
                    if ip_ == fn.import_path and typ_ == fn.receiver and base.usable:
                        value = (base.path_text or "").rstrip("/")
                        # only a concrete base (env placeholder, absolute URL,
                        # static text) replaces `{base}`; an opaque expression
                        # would turn a matchable call into path_dynamic
                        concrete = (
                            value.startswith(("http://", "https://", "/"))
                            or re.fullmatch(r"\{[A-Z][A-Z0-9_]*\}", value) is not None
                            or (base.fully_static and value)
                        )
                        if concrete:
                            url = value + url[len("{base}"):]
                        break
            if not (
                url.startswith("/")
                or url.startswith("{")
                or "://" in url
                or "/" in url
            ):
                return
            http_calls.append(
                GoHttpCall(
                    function=fn,
                    method=http_method or "GET",
                    url=url,
                    evidence=evidence,
                    unresolved=unresolved or "{" in url,
                )
            )

        def resolve(n: Node | None) -> ResolvedString:
            return _resolve_expr(n, env, package_consts, info.import_aliases, idx.consts_by_import)

        if is_net_http and method in {"NewRequest", "NewRequestWithContext"} and args:
            if method == "NewRequest":
                mres = resolve(args[0]) if args else ResolvedString("GET", True, False)
                ures = resolve(args[1]) if len(args) > 1 else ResolvedString(None, False, True)
            else:
                mres = resolve(args[1]) if len(args) > 1 else ResolvedString("GET", True, False)
                ures = resolve(args[2]) if len(args) > 2 else ResolvedString(None, False, True)
            http_method = (mres.path_text or "GET").upper()
            if http_method.startswith("METHOD"):
                http_method = http_method[6:]
            url = ures.path_text
            unresolved = (not ures.fully_static) or "{" in url
            emit(http_method, url, unresolved)
            for name in _call_lhs_names(node):
                req_vars[name] = (http_method, url, unresolved)
            return
        if is_net_http and method in {"Get", "Post", "Head", "PostForm"} and args:
            http_method = "POST" if method.startswith("Post") else method.upper()
            ures = resolve(args[0])
            emit(http_method, ures.path_text, not ures.fully_static)
            return
        if method == "Do" and args:
            arg = args[0]
            arg_name = text(arg) if arg.type == "identifier" else ""
            if arg_name in req_vars:
                return
            # Request built elsewhere: no URL to attach.

    def _group_opts(self, echo_rule: Rule | None) -> tuple[str, int]:
        group_method = "Group"
        path_arg = 0
        if echo_rule:
            opts = echo_rule.options.get("receiver_path_prefixes") or {}
            group_method = str(opts.get("group_method", "Group"))
            path_arg = int(opts.get("path_arg", 0))
        return group_method, path_arg

    def _seed_echo_params(self, fn: GoFunction, prefixes: dict[str, str]) -> None:
        for name, gtype in fn.params.items():
            if gtype and gtype.name == "Echo":
                prefixes.setdefault(name, "")

    def _is_group_type(self, gtype: GoType | None) -> bool:
        return bool(gtype and gtype.name in GROUP_TYPE_NAMES)

    def _group_param_names(self, fn: GoFunction) -> list[str]:
        out: list[str] = []
        for name, gtype in fn.params.items():
            if name == fn.receiver_var:
                continue
            if self._is_group_type(gtype):
                out.append(name)
        return out

    def _path_prefixes(
        self,
        fn: GoFunction,
        info: _FileInfo,
        echo_rule: Rule | None,
        package_consts: dict[str, ResolvedString],
        idx: _Indexes,
    ) -> dict[str, str]:
        """Resolve Echo Group prefixes for one function.

        Scoped per function so ``g := e.Group("/a")`` in one func and
        ``g := e.Group("/b")`` in another cannot oscillate. Iteration is
        capped as a safety net against self-referential Group chains.
        """
        prefixes: dict[str, str] = {}
        group_method, path_arg = self._group_opts(echo_rule)
        self._seed_echo_params(fn, prefixes)

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

        assignments = self._group_assignments(fn)
        for _ in range(PREFIX_MAX_ITERS):
            changed = False
            for ident, val in assignments:
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
                    if len(new_prefix) > PREFIX_MAX_LEN:
                        continue
                    if prefixes.get(ident) != new_prefix:
                        prefixes[ident] = new_prefix
                        changed = True
                elif val.type == "call_expression" and call_callee_name(val) in {"New"}:
                    prefixes.setdefault(ident, "")
            if not changed:
                break
        return prefixes

    def _group_assignments(self, fn: GoFunction) -> list[tuple[str, Node]]:
        assignments: list[tuple[str, Node]] = []
        for node in typed_walk(fn.node, _ASSIGN):
            lists = [c for c in node.named_children if c.type == "expression_list"]
            if len(lists) < 2:
                continue
            names = [c for c in lists[0].named_children if c.type == "identifier"]
            values = lists[1].named_children
            if not names or not values:
                continue
            assignments.append((text(names[0]), values[0]))
        return assignments

    def _group_call_prefix(
        self,
        call: Node,
        prefixes: dict[str, str],
        echo_rule: Rule | None,
        package_consts: dict[str, ResolvedString],
        info: _FileInfo,
        idx: _Indexes,
    ) -> str:
        group_method, path_arg = self._group_opts(echo_rule)
        if call_callee_name(call) != group_method:
            return ""
        args = call_args(call)
        arg = args[path_arg] if len(args) > path_arg else None
        literal = _go_string_node(arg) if arg is not None else None
        if literal is None:
            path_res = _resolve_expr(arg, package_consts, package_consts, info.import_aliases, idx.consts_by_import)
            path = path_res.path_text if path_res.usable else ""
        else:
            path = literal
        inner = call.child_by_field_name("function")
        operand = (
            inner.child_by_field_name("operand")
            if inner and inner.type == "selector_expression"
            else None
        )
        base = ""
        if operand is not None and operand.type == "identifier":
            base = prefixes.get(text(operand), "")
        elif operand is not None and operand.type == "call_expression":
            base = self._group_call_prefix(operand, prefixes, echo_rule, package_consts, info, idx)
        return _join_path(base, path)

    def _returned_group_prefix(
        self,
        fn: GoFunction,
        prefixes: dict[str, str],
        echo_rule: Rule | None,
        package_consts: dict[str, ResolvedString],
        info: _FileInfo,
        idx: _Indexes,
    ) -> str | None:
        if not (fn.return_type and fn.return_type.name == "Group"):
            return None
        found: list[str] = []
        for node in typed_walk(fn.node, _RETURN):
            expr_list = next((c for c in node.named_children if c.type == "expression_list"), None)
            expr = None
            if expr_list is not None and expr_list.named_children:
                expr = expr_list.named_children[0]
            elif node.named_children:
                expr = node.named_children[0]
            if expr is None:
                continue
            expr = _unwrap_literal(expr) or expr
            if expr.type == "identifier":
                name = text(expr)
                if name in prefixes:
                    found.append(prefixes[name])
            elif expr.type == "call_expression":
                group_method, _path_arg = self._group_opts(echo_rule)
                if call_callee_name(expr) == group_method:
                    found.append(
                        self._group_call_prefix(expr, prefixes, echo_rule, package_consts, info, idx)
                    )
                else:
                    hits = self._resolve_callees(expr, fn, info, idx)
                    if len(hits) == 1:
                        # callee return filled in a later propagate pass
                        continue
        if not found:
            return None
        # Prefer a concrete prefix over empty.
        for prefix in found:
            if prefix:
                return prefix
        return found[0]

    def _propagate_group_prefixes(
        self,
        infos: list[_FileInfo],
        idx: _Indexes,
        echo_rule: Rule | None,
        prefix_by_fn: dict[str, dict[str, str]],
        consts_by_info: dict[str, dict[str, ResolvedString]],
    ) -> None:
        """Copy Group prefixes across returns and call arguments."""
        group_method, _path_arg = self._group_opts(echo_rule)
        for _ in range(PREFIX_MAX_ITERS):
            changed = False
            return_prefix: dict[str, str] = {}
            for info in infos:
                package_consts = consts_by_info[info.rel]
                for fn in info.functions:
                    prefixes = prefix_by_fn[fn.qualname]
                    ret = self._returned_group_prefix(
                        fn, prefixes, echo_rule, package_consts, info, idx
                    )
                    if ret is not None:
                        return_prefix[fn.qualname] = ret
            for info in infos:
                package_consts = consts_by_info[info.rel]
                for fn in info.functions:
                    prefixes = prefix_by_fn[fn.qualname]
                    for ident, val in self._group_assignments(fn):
                        if val.type != "call_expression":
                            continue
                        if call_callee_name(val) == group_method:
                            # `s := g.Group("/org")` where `g` is a parameter
                            # whose prefix arrived from a caller in this pass:
                            # recompute from the current prefixes (any depth).
                            new_prefix = self._group_call_prefix(
                                val, prefixes, echo_rule, package_consts, info, idx
                            )
                            if len(new_prefix) <= PREFIX_MAX_LEN and prefixes.get(ident) != new_prefix:
                                prefixes[ident] = new_prefix
                                changed = True
                            continue
                        hits = self._resolve_callees(val, fn, info, idx)
                        if len(hits) != 1:
                            continue
                        rp = return_prefix.get(hits[0][0].qualname)
                        if rp is None:
                            continue
                        if prefixes.get(ident) != rp:
                            prefixes[ident] = rp
                            changed = True
                    for node in typed_walk(fn.node, _CALL):
                        hits = self._resolve_callees(node, fn, info, idx)
                        if not hits:
                            continue
                        args = call_args(node)
                        for callee, _conf, _cands in hits:
                            params = [p for p in callee.params if p != callee.receiver_var]
                            dest = prefix_by_fn.get(callee.qualname)
                            if dest is None:
                                continue
                            for i, arg in enumerate(args):
                                if i >= len(params):
                                    break
                                pname = params[i]
                                ptype = callee.params.get(pname)
                                if not self._is_group_type(ptype):
                                    continue
                                arg_u = _unwrap_literal(arg) or arg
                                src: str | None = None
                                if arg_u.type == "identifier":
                                    src = prefixes.get(text(arg_u))
                                elif arg_u.type == "call_expression" and call_callee_name(arg_u) == group_method:
                                    src = self._group_call_prefix(
                                        arg_u, prefixes, echo_rule, package_consts, info, idx
                                    )
                                if src is None:
                                    continue
                                if dest.get(pname) != src:
                                    dest[pname] = src
                                    changed = True
            if not changed:
                break

    def _receiver_prefix(
        self,
        call: Node,
        prefixes: dict[str, str],
        echo_rule: Rule | None,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
        info: _FileInfo,
        idx: _Indexes,
        fn: GoFunction | None = None,
    ) -> tuple[str, bool]:
        func = call.child_by_field_name("function")
        if func is None or func.type != "selector_expression":
            return "", False
        operand = func.child_by_field_name("operand")
        if operand is None:
            return "", False
        if operand.type == "identifier":
            name = text(operand)
            if name in prefixes:
                return prefixes[name], False
            gtype = fn.params.get(name) if fn is not None else None
            if gtype and gtype.name == "Group":
                # Group defined elsewhere and not resolvable: keep a marker.
                return "{" + name + "}", True
            return "", False
        if operand.type == "call_expression":
            group_method, path_arg = self._group_opts(echo_rule)
            if call_callee_name(operand) == group_method:
                args = call_args(operand)
                arg = args[path_arg] if len(args) > path_arg else None
                path_res = _resolve_expr(arg, env, package_consts, info.import_aliases, idx.consts_by_import)
                path = path_res.path_text if path_res.usable else (_go_string_node(arg) if arg else "") or ""
                inner_func = operand.child_by_field_name("function")
                inner_op = inner_func.child_by_field_name("operand") if inner_func else None
                base = prefixes.get(text(inner_op), "") if inner_op and inner_op.type == "identifier" else ""
                unresolved = bool(path_res and not path_res.fully_static)
                return _join_path(base, path), unresolved
        return "", False

    def _unresolved_reason(self, call: Node, fn: GoFunction, info: _FileInfo, idx: _Indexes) -> str:
        func = call.child_by_field_name("function")
        operand = func.child_by_field_name("operand") if func is not None else None
        gtype = self._type_of_expr(operand, fn, info, self._local_types(fn, info, idx), idx) if operand else None
        if gtype is not None and (gtype.import_path or fn.import_path, gtype.name) not in idx.interfaces:
            return "type_not_scanned"
        return "no_scanned_implementer"

    def _method_value_calls(
        self,
        args: list[Node],
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
        evidence: Evidence,
        calls: list[CallHit],
    ) -> None:
        """`c.observe(ctx, c.svc.SubmitMarks)`: a function or method passed as a
        value to a scanned function is (inferred) called by the caller."""
        local_types: dict[str, GoType] | None = None
        for arg in args:
            arg = _unwrap_literal(arg) or arg
            target: GoFunction | None = None
            if arg.type == "selector_expression":
                if local_types is None:
                    local_types = self._local_types(fn, info, idx)
                target = self._resolve_selector(arg, fn, info, idx, local_types)
            elif arg.type == "identifier":
                name = text(arg)
                if name in fn.params:
                    continue
                target = idx.by_func.get((fn.import_path, name))
            if target is None or target.qualname == fn.qualname:
                continue
            calls.append(
                CallHit(
                    caller=fn.qualname,
                    callee=target.qualname,
                    evidence=evidence,
                    confidence=Confidence.INFERRED,
                    candidates=[],
                )
            )

    def _unresolved_iface_note(
        self,
        call: Node,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
    ) -> str | None:
        """Interface/protocol call with no scanned implementer."""
        func = call.child_by_field_name("function")
        if func is None or func.type != "selector_expression":
            return None
        method = call_callee_name(call)
        if not method:
            return None
        operand = func.child_by_field_name("operand")
        local_types = self._local_types(fn, info, idx)
        gtype = self._type_of_expr(operand, fn, info, local_types, idx)
        if gtype is None:
            return None
        iface_key = (gtype.import_path or fn.import_path, gtype.name)
        if iface_key not in idx.interfaces:
            ip = gtype.import_path or ""
            if ip and ip != fn.import_path and _same_project(ip, fn.import_path) and not self._match_scanned_import(idx, ip):
                return f"{gtype.name}.{method}"
            return None
        impls = [
            i
            for i in idx.implementers.get(iface_key) or []
            if not (fn.receiver and i == (fn.import_path, fn.receiver))
        ]
        if impls:
            return None
        return f"{gtype.name}.{method}"

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
        for node in typed_walk(fn.node, _ASSIGN_VAR):
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
            found = self._lookup_func(idx, fn.import_path, name)
            return found.return_type if found else None
        if func.type == "selector_expression":
            operand = func.child_by_field_name("operand")
            if operand is not None and operand.type == "identifier":
                ident = text(operand)
                if ident in info.import_aliases:
                    ip = info.import_aliases[ident]
                    found = self._lookup_func(idx, ip, name)
                    return found.return_type if found else None
            gtype = self._type_of_expr(operand, fn, info, local_types, idx)
            if gtype and gtype.import_path:
                found = self._lookup_method(idx, gtype.import_path, gtype.name, name)
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
                found = self._lookup_method(idx, ip, gtype.name, name)
                if found:
                    return found
                return self._resolve_func_field(ip, gtype.name, name, idx)
            # No resolved package: do not fall back to a globally unique method name.
            return None
        if operand is not None and operand.type == "identifier":
            ident = text(operand)
            if ident in info.import_aliases:
                ip = info.import_aliases[ident]
                return self._lookup_func(idx, ip, name)
            return None
        return None

    def _match_scanned_import(self, idx: _Indexes, ip: str | None) -> str | None:
        """Exact import path, then directory-suffix fallback when go.mod is missing."""
        if not ip:
            return None
        if ip in idx.known_import_paths:
            return ip
        if ip in idx.import_match_cache:
            return idx.import_match_cache[ip]
        if not idx.known_by_last and idx.known_import_paths:
            for known in idx.known_import_paths:
                if known:
                    idx.known_by_last.setdefault(known.rstrip("/").rsplit("/", 1)[-1], []).append(known)
        # a suffix match in either direction shares the last path segment
        last = ip.rstrip("/").rsplit("/", 1)[-1]
        hits = [
            p
            for p in idx.known_by_last.get(last, ())
            if ip.endswith("/" + p) or p.endswith("/" + ip)
        ]
        if len(hits) != 1:
            idx.import_match_cache[ip] = None
            return None
        idx.import_match_cache[ip] = hits[0]
        if idx.missing_gomod:
            msg = (
                f"warning: no go.mod; resolved import {ip!r} via scanned directory {hits[0]!r}"
            )
            if msg not in idx.warnings:
                idx.warnings.append(msg)
        return hits[0]

    def _lookup_func(self, idx: _Indexes, ip: str | None, name: str) -> GoFunction | None:
        if ip:
            found = idx.by_func.get((ip, name))
            if found:
                return found
            matched = self._match_scanned_import(idx, ip)
            if matched and matched != ip:
                return idx.by_func.get((matched, name))
        return None

    def _lookup_method(
        self, idx: _Indexes, ip: str | None, typ: str, name: str
    ) -> GoFunction | None:
        if ip:
            found = idx.by_method.get((ip, typ, name))
            if found:
                return found
            matched = self._match_scanned_import(idx, ip)
            if matched and matched != ip:
                return idx.by_method.get((matched, typ, name))
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
        hits = self._resolve_callees(call, fn, info, idx)
        return hits[0][0] if len(hits) == 1 else None

    def _resolve_callees(
        self,
        call: Node,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
    ) -> list[tuple[GoFunction, Confidence, list[str]]]:
        name = call_callee_name(call)
        func = call.child_by_field_name("function")
        local_types = self._local_types(fn, info, idx)
        if func is None:
            return []
        if func.type == "identifier":
            found = self._lookup_func(idx, fn.import_path, name)
            if found is None:
                return []
            # `helper()` in the same package: resolved by package + name
            return [(found, Confidence.EXACT, [])]
        if func.type == "selector_expression":
            return self._resolve_selector_callees(func, fn, info, idx, local_types, name)
        return []

    def _resolve_selector_callees(
        self,
        func_node: Node,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
        local_types: dict[str, GoType],
        method: str,
    ) -> list[tuple[GoFunction, Confidence, list[str]]]:
        found = self._resolve_selector(func_node, fn, info, idx, local_types)
        operand = func_node.child_by_field_name("operand")
        gtype = self._type_of_expr(operand, fn, info, local_types, idx)
        iface_key = (
            ((gtype.import_path or fn.import_path), gtype.name) if gtype is not None else None
        )
        is_iface = bool(iface_key and iface_key in idx.interfaces)
        if found is not None and not is_iface:
            # `pkg.Func()` is resolved by import path + name; a method call on
            # a value whose type was inferred stays inferred
            conf = Confidence.EXACT if (not found.receiver or fn.receiver) else Confidence.INFERRED
            return [(found, conf, [])]
        if not is_iface or gtype is None or iface_key is None:
            if found is not None:
                conf = Confidence.EXACT if (not found.receiver or fn.receiver) else Confidence.INFERRED
                return [(found, conf, [])]
            if operand is not None and operand.type == "selector_expression":
                obj_t = self._type_of_expr(
                    operand.child_by_field_name("operand"), fn, info, local_types, idx
                )
                field_n = operand.child_by_field_name("field")
                if obj_t is not None and field_n is not None:
                    concretes = idx.field_concretes.get(
                        (obj_t.import_path or "", obj_t.name, text(field_n)), []
                    )
                    unique = list(dict.fromkeys((c.import_path, c.name) for c in concretes))
                    callees: list[GoFunction] = []
                    for ip, typ in unique:
                        gf = self._lookup_method(idx, ip, typ, method)
                        if gf is not None:
                            callees.append(gf)
                    if len(callees) == 1:
                        return [(callees[0], Confidence.INFERRED, [])]
                    if callees:
                        callees = sorted(callees, key=lambda c: c.qualname)
                        cands = [c.qualname for c in callees]
                        return [(c, Confidence.INFERRED, list(cands)) for c in callees]
            return []
        impls = list(idx.implementers.get(iface_key) or [])
        if operand is not None and operand.type == "selector_expression":
            obj_t = self._type_of_expr(
                operand.child_by_field_name("operand"), fn, info, local_types, idx
            )
            field_n = operand.child_by_field_name("field")
            if obj_t is not None and field_n is not None:
                concretes = idx.field_concretes.get(
                    (obj_t.import_path or "", obj_t.name, text(field_n)), []
                )
                unique = list(dict.fromkeys((c.import_path, c.name) for c in concretes))
                if len(unique) == 1:
                    impls = [unique[0]]
        # a decorator implements the interface it wraps: `s.repo.X()` inside
        # Service.X is not a call to itself
        impls = [i for i in impls if not (fn.receiver and i == (fn.import_path, fn.receiver))]
        callees: list[GoFunction] = []
        for ip, typ in impls:
            gf = self._lookup_method(idx, ip, typ, method)
            if gf is not None:
                callees.append(gf)
        if not callees:
            return []
        callees = sorted(callees, key=lambda c: c.qualname)
        cands = [c.qualname for c in callees] if len(callees) > 1 else []
        return [(c, Confidence.INFERRED, list(cands)) for c in callees]

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
            if callee:
                return callee, [callee.qualname], Confidence.EXACT
            name = call_callee_name(node)
            return None, [f.qualname for f in idx.by_name.get(name, [])], Confidence.UNKNOWN
        if node.type == "selector_expression":
            local_types = self._local_types(fn, info, idx)
            found = self._resolve_selector(node, fn, info, idx, local_types)
            if found:
                return found, [found.qualname], Confidence.EXACT
            field = node.child_by_field_name("field")
            name = text(field) if field else ""
            return None, [f.qualname for f in idx.by_name.get(name, [])], Confidence.UNKNOWN
        if node.type == "identifier":
            name = text(node)
            found = idx.by_func.get((info.import_path, name))
            if found:
                return found, [found.qualname], Confidence.EXACT
            same = [f for f in idx.by_name.get(name, []) if f.import_path == info.import_path]
            if len(same) == 1:
                return same[0], [same[0].qualname], Confidence.EXACT
            return None, [f.qualname for f in idx.by_name.get(name, [])], Confidence.UNKNOWN
        if node.type == "unary_expression" and node.named_children:
            return self._resolve_handler_node(node.named_children[0], fn, info, idx)
        return None, [], Confidence.UNKNOWN

    def _factory_parts(
        self,
        handler_node: Node,
        handler: GoFunction | None,
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
    ) -> list[GoFunction]:
        """Functions called in the arguments of a handler factory
        (`listCarts(newCartRepo(db))` → newCartRepo)."""
        call = _unwrap_literal(handler_node) or handler_node
        if handler is None or call.type != "call_expression":
            return []
        if self._resolve_callee(call, fn, info, idx) is not handler:
            return []  # a wrapper unwrapped to an inner handler, not a factory
        out: list[GoFunction] = []
        for arg in call_args(call):
            inner = _unwrap_literal(arg) or arg
            if inner.type != "call_expression":
                continue
            part = self._resolve_callee(inner, fn, info, idx)
            if part is not None and part is not handler and part not in out:
                out.append(part)
        return out

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
            if inner.type == "call_expression":
                # wrap(mw(h.X)) unwraps to h.X; listCarts(newCartRepo(db)) does
                # not: newCartRepo(db) builds a value, it is not the handler
                nested = self._unwrap_wrapped_handler(inner, fn, info, idx)
                if nested is not None:
                    found = nested
                continue
            if inner.type not in {"identifier", "selector_expression"}:
                continue
            resolved, _cands, conf = self._resolve_handler_node(inner, fn, info, idx)
            if resolved is not None and conf != Confidence.UNKNOWN:
                found = resolved
        return found

    def _is_sql_param_ident(self, fn: GoFunction, node: Node | None) -> bool:
        node = _unwrap_literal(node)
        if node is None or node.type != "identifier":
            return False
        name = text(node)
        return name in SQL_PARAM_NAMES and name in fn.params

    def _arg_sql_texts(
        self,
        arg: Node | None,
        fn: GoFunction,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
        info: _FileInfo,
        idx: _Indexes,
    ) -> tuple[ResolvedString | None, list[str]]:
        if arg is None or self._is_sql_param_ident(fn, arg):
            return None, []
        resolved = _resolve_expr(arg, env, package_consts, info.import_aliases, idx.consts_by_import)
        texts = resolved.all_texts() if resolved.usable else []
        if not texts and not resolved.fully_static:
            guess = resolved.path_text
            if looks_like_sql(guess) or (guess and _SQL_VERB.search(guess)):
                texts = [guess]
        sql_texts = [
            t
            for t in texts
            if t and not _is_placeholder_token(t) and (looks_like_sql(t) or _SQL_VERB.search(t))
        ]
        return resolved, sql_texts

    def _pick_sql_arg(
        self,
        args: list[Node],
        fn: GoFunction,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
        info: _FileInfo,
        idx: _Indexes,
    ) -> tuple[ResolvedString | None, list[str]]:
        """Pick the first argument that is a SQL literal or const (pgx or database/sql)."""
        for arg in args:
            resolved, texts = self._arg_sql_texts(arg, fn, env, package_consts, info, idx)
            if texts:
                return resolved, texts
        return None, []

    def _sql_param_index(self, callee: GoFunction) -> int | None:
        order = [p for p in callee.params if p != callee.receiver_var]
        for i, name in enumerate(order):
            if name not in SQL_PARAM_NAMES:
                continue
            gtype = callee.params.get(name)
            if gtype is None or gtype.name == "string":
                return i
        return None

    def _http_method_from_name(self, name: str) -> str:
        low = name.lower()
        for meth in ("get", "post", "put", "patch", "delete", "head", "options"):
            if low.startswith(meth):
                return meth.upper()
        return "GET"

    def _collect_http_helpers(
        self, infos: list[_FileInfo], idx: _Indexes
    ) -> dict[str, GoHttpHelper]:
        field_bases = self._struct_http_bases(infos, idx)
        # direct calls in methods (`c.http.Get(c.baseURL + "/x")`) use them too
        self._field_bases = field_bases
        helpers: dict[str, GoHttpHelper] = {}
        for info in infos:
            for fn in info.functions:
                if not fn.receiver:
                    continue
                order = [p for p in fn.params if p != fn.receiver_var]
                path_index = next(
                    (i for i, p in enumerate(order) if p.lower() in HTTP_PATH_PARAM_NAMES),
                    None,
                )
                if path_index is None:
                    continue
                path_param = order[path_index]
                method_index = next(
                    (i for i, p in enumerate(order) if p.lower() in HTTP_METHOD_PARAM_NAMES),
                    None,
                )
                path_names = {path_param}
                for node in typed_walk(fn.node, _ASSIGN):
                    pairs = self._assignment_pairs(node)
                    for lhs, rhs in pairs:
                        raw = text(rhs)
                        if path_param in raw or any(n in raw for n in path_names):
                            path_names.add(lhs)
                base_field = ""
                for node in typed_walk(fn.node, _CALL):
                    method = call_callee_name(node)
                    args = call_args(node)
                    url_node = None
                    if method in {"NewRequest", "NewRequestWithContext"}:
                        url_node = args[1] if method == "NewRequest" and len(args) > 1 else (
                            args[2] if len(args) > 2 else None
                        )
                    elif method in {"Get", "Post", "Head", "PostForm"} and args:
                        url_node = args[0]
                    if url_node is None:
                        continue
                    raw = text(url_node)
                    if not any(n == raw or n in raw for n in path_names):
                        continue
                    field = self._base_field_in_expr(url_node, fn.receiver_var)
                    if not field:
                        for n2 in typed_walk(fn.node, _ASSIGN):
                            for lhs, rhs in self._assignment_pairs(n2):
                                if lhs == raw or lhs in path_names:
                                    field = self._base_field_in_expr(rhs, fn.receiver_var)
                                    if field:
                                        break
                            if field:
                                break
                    if field:
                        base_field = field
                        break
                if not base_field:
                    continue
                key = (fn.import_path, fn.receiver, base_field)
                helpers[fn.qualname] = GoHttpHelper(
                    function=fn,
                    path_index=path_index,
                    method_index=method_index,
                    default_method=self._http_method_from_name(fn.name),
                    base_field=base_field,
                    base=field_bases.get(key),
                )
        return helpers

    def _base_field_in_expr(self, node: Node | None, recv_var: str | None) -> str:
        if node is None or not recv_var:
            return ""
        if node.type == "selector_expression":
            operand = node.child_by_field_name("operand")
            field_n = node.child_by_field_name("field")
            if operand is not None and text(operand) == recv_var and field_n is not None:
                name = text(field_n)
                if name.lower() in HTTP_BASE_FIELD_NAMES:
                    return name
            return self._base_field_in_expr(operand, recv_var)
        for child in node.named_children:
            found = self._base_field_in_expr(child, recv_var)
            if found:
                return found
        return ""

    def _config_field_envs(self, infos: list[_FileInfo]) -> dict[tuple[str, str, str], str]:
        """(import path, struct, field) → env var: struct tags (`env:"X"`,
        `envconfig:"X"`) and composite literals assigning `os.Getenv("X")`."""
        out: dict[tuple[str, str, str], str] = {}
        tag_re = re.compile(r'(?:env|envconfig)\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)')
        for info in infos:
            for spec in typed_walk(info.tree, _TYPE_SPEC):
                sname = text(spec.child_by_field_name("name"))
                typ = spec.child_by_field_name("type")
                if not sname or typ is None or typ.type != "struct_type":
                    continue
                for fd in walk(typ):
                    if fd.type != "field_declaration":
                        continue
                    tag = fd.child_by_field_name("tag")
                    m = tag_re.search(text(tag)) if tag is not None else None
                    if not m:
                        continue
                    for ident in fd.named_children:
                        if ident.type in {"field_identifier", "identifier"}:
                            out[(info.import_path, sname, text(ident))] = m.group(1)
            for node in typed_walk(info.tree, _COMPOSITE):
                gtype = _parse_type(
                    node.child_by_field_name("type"), info.package, info.import_path, info.import_aliases
                )
                body = next((c for c in node.named_children if c.type == "literal_value"), None)
                if gtype is None or body is None:
                    continue
                for elem in body.named_children:
                    key_n = elem.child_by_field_name("key")
                    val_n = elem.child_by_field_name("value")
                    if key_n is None or val_n is None:
                        continue
                    res = _resolve_expr(
                        _unwrap_literal(val_n) or val_n,
                        info.consts,
                        info.consts,
                        info.import_aliases,
                    )
                    ph = res.placeholder or ""
                    if ph.startswith("{") and ph.endswith("}") and ph[1:-1].isupper():
                        out.setdefault(
                            (gtype.import_path or info.import_path, gtype.name, text(_unwrap_literal(key_n) or key_n)),
                            ph[1:-1],
                        )
        return out

    def _struct_http_bases(
        self, infos: list[_FileInfo], idx: _Indexes
    ) -> dict[tuple[str, str, str], ResolvedString]:
        """Resolve Client.baseURL from composite literals / env / const /
        config structs (`cfg.ReportAPIURL` with an env tag) / constructor
        parameters bound at every call site."""
        out: dict[tuple[str, str, str], ResolvedString] = {}
        config_envs = self._config_field_envs(infos)
        # (ctor import path, ctor name, param) → base keys fed by that param
        pending: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}

        def resolve_value(val: Node, fn: GoFunction | None, info: _FileInfo) -> ResolvedString:
            val = _unwrap_literal(val) or val
            if val.type == "selector_expression" and fn is not None:
                operand = val.child_by_field_name("operand")
                field_n = val.child_by_field_name("field")
                if operand is not None and field_n is not None and operand.type == "identifier":
                    gtype = fn.params.get(text(operand))
                    if gtype is None:
                        gtype = self._local_types(fn, info, idx).get(text(operand))
                    if gtype is not None:
                        env_name = config_envs.get(
                            (gtype.import_path or fn.import_path, gtype.name, text(field_n))
                        )
                        if env_name:
                            return ResolvedString(None, False, True, placeholder="{" + env_name + "}")
            return _resolve_expr(val, info.consts, info.consts, info.import_aliases, idx.consts_by_import)

        for info in infos:
            dummy = self._synthetic_func(info, info.tree, "_http_base")
            for node in typed_walk(info.tree, _COMPOSITE):
                gtype = _parse_type(
                    node.child_by_field_name("type"),
                    info.package,
                    info.import_path,
                    info.import_aliases,
                )
                if gtype is None:
                    continue
                body = next((c for c in node.named_children if c.type == "literal_value"), None)
                if body is None:
                    continue
                for elem in body.named_children:
                    key_n = elem.child_by_field_name("key")
                    val_n = elem.child_by_field_name("value")
                    if key_n is None or val_n is None:
                        keyed = [c for c in elem.named_children]
                        if len(keyed) >= 2 and keyed[0].type in {"field_identifier", "identifier"}:
                            key_n, val_n = keyed[0], keyed[1]
                    if key_n is None or val_n is None:
                        continue
                    field = text(key_n)
                    if field.lower() not in HTTP_BASE_FIELD_NAMES:
                        continue
                    fn = self._enclosing(info, node)
                    key = (gtype.import_path or info.import_path, gtype.name, field)
                    val_u = _unwrap_go_transparent(val_n, info.import_aliases) or val_n
                    if val_u.type == "identifier" and fn is not None and text(val_u) in fn.params:
                        # &ReportClient{baseURL: baseURL} inside NewReportClient(baseURL string)
                        pending.setdefault((fn.import_path, fn.name, text(val_u)), []).append(key)
                        continue
                    resolved = resolve_value(val_u, fn, info)
                    if resolved.usable:
                        out[key] = resolved
            _ = dummy
        if pending:
            seen: dict[tuple[str, str, str], set[str]] = {}
            values: dict[tuple[str, str, str], ResolvedString] = {}
            for info in infos:
                for fn in info.functions:
                    for node in typed_walk(fn.node, _CALL):
                        callee = self._resolve_callee(node, fn, info, idx)
                        if callee is None:
                            continue
                        order = [p for p in callee.params if p != callee.receiver_var]
                        args = call_args(node)
                        for i, pname in enumerate(order):
                            pkey = (callee.import_path, callee.name, pname)
                            if pkey not in pending or i >= len(args):
                                continue
                            res = resolve_value(
                                _unwrap_go_transparent(args[i], info.import_aliases) or args[i], fn, info
                            )
                            label = res.path_text if res.usable else ""
                            seen.setdefault(pkey, set()).add(label)
                            if res.usable:
                                values[pkey] = res
            for pkey, keys in pending.items():
                labels = seen.get(pkey) or set()
                if len(labels) == 1 and "" not in labels and pkey in values:
                    for key in keys:
                        out.setdefault(key, values[pkey])
        return out

    def _record_http_helper_call(
        self,
        node: Node,
        args: list[Node],
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
        callees: list[tuple[GoFunction, Confidence, list[str]]],
        helpers: dict[str, GoHttpHelper],
        http_calls: list[GoHttpCall],
    ) -> None:
        for callee, _conf, _cands in callees:
            helper = helpers.get(callee.qualname)
            if helper is None or helper.path_index >= len(args):
                continue
            path_res = _resolve_expr(
                args[helper.path_index],
                env,
                package_consts,
                info.import_aliases,
                idx.consts_by_import,
            )
            path = path_res.path_text if path_res.usable else ""
            if not path:
                continue
            http_method = helper.default_method
            if helper.method_index is not None and helper.method_index < len(args):
                mres = _resolve_expr(
                    args[helper.method_index],
                    env,
                    package_consts,
                    info.import_aliases,
                    idx.consts_by_import,
                )
                if mres.fully_static and mres.text:
                    http_method = mres.text.upper()
            base = helper.base.path_text if helper.base and helper.base.usable else ""
            if not base:
                local_types = self._local_types(fn, info, idx)
                func = node.child_by_field_name("function")
                operand = func.child_by_field_name("operand") if func is not None else None
                gtype = self._type_of_expr(operand, fn, info, local_types, idx) if operand else None
                if gtype:
                    key = (gtype.import_path or callee.import_path, gtype.name, helper.base_field)
                    file_bases = self._file_http_bases.get(info.rel)
                    if file_bases is None:
                        file_bases = self._file_http_bases[info.rel] = self._struct_http_bases([info], idx)
                    found = file_bases.get(key)
                    if found and found.usable:
                        base = found.path_text
            if not base:
                base = "{" + helper.base_field + "}"
            if "://" in path or path.startswith("{"):
                url = path if path.startswith("{") or "://" in path else base.rstrip("/") + path
            else:
                if "://" in base or base.startswith("{"):
                    url = base.rstrip("/") + (path if path.startswith("/") else "/" + path)
                else:
                    url = (base.rstrip("/") + path) if path.startswith("/") else (base + path)
            evidence = Evidence(file=info.rel, line=line_of(node), symbol=fn.qualname)
            http_calls.append(
                GoHttpCall(
                    function=fn,
                    method=http_method or "GET",
                    url=url,
                    evidence=evidence,
                    unresolved=("{" in url) or not path_res.fully_static,
                )
            )

    def _sql_from_helper_call(
        self,
        call: Node,
        args: list[Node],
        fn: GoFunction,
        info: _FileInfo,
        idx: _Indexes,
        env: dict[str, ResolvedString],
        package_consts: dict[str, ResolvedString],
        sql_sites: list[SqlSite],
        callees: list[tuple[GoFunction, Confidence, list[str]]],
    ) -> None:
        """Bind query-string arguments at helper call sites onto the caller."""
        seen: set[str] = set()
        for callee, _conf, _cands in callees:
            arg_i = self._sql_param_index(callee)
            if arg_i is None or arg_i >= len(args):
                continue
            resolved = _resolve_expr(
                args[arg_i], env, package_consts, info.import_aliases, idx.consts_by_import
            )
            for sql_text in resolved.all_texts():
                if not sql_text or sql_text in seen or _is_placeholder_token(sql_text):
                    continue
                if not (looks_like_sql(sql_text) or re.search(r"(?i)\b(UPDATE|INSERT|DELETE|SELECT)\b", sql_text)):
                    continue
                seen.add(sql_text)
                sql_sites.append(
                    SqlSite(
                        function=fn,
                        sql=sql_text,
                        dynamic=resolved.dynamic or not resolved.fully_static,
                        evidence=Evidence(file=info.rel, line=line_of(call), symbol=fn.qualname),
                        fully_static=resolved.fully_static and len(resolved.all_texts()) == 1,
                    )
                )

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
        for bind_n, info in enumerate(infos):
            progress.counter("go: field bindings", bind_n, len(infos))
            for fn in info.functions:
                for node in typed_walk(fn.node, _LITERALS):
                    lit = node
                    if node.type == "unary_expression":
                        inner = node.child_by_field_name("operand") or (
                            node.named_children[0] if node.named_children else None
                        )
                        if inner is not None and inner.type == "composite_literal":
                            lit = inner
                    if lit.type == "composite_literal":
                        self._bind_composite(lit, fn, info, idx)
        for info in infos:
            for fn in info.functions:
                for node in typed_walk(fn.node, _CALL):
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
        iface_fields = idx.iface_fields.get((ip, gtype.name), set())
        if not func_fields and not iface_fields:
            return
        body = None
        for child in lit.named_children:
            if child.type == "literal_value":
                body = child
                break
        if body is None:
            return
        local_types = self._local_types(fn, info, idx)
        for keyed in body.named_children:
            if keyed.type != "keyed_element":
                continue
            key_node = keyed.child_by_field_name("key")
            val_node = keyed.child_by_field_name("value")
            key_ident = _unwrap_literal(key_node)
            fname = text(key_ident) if key_ident is not None else ""
            if not fname:
                continue
            val = _unwrap_literal(val_node)
            if fname in func_fields:
                resolved = self._resolve_func_expr(val, fn, info, idx)
                if resolved is not None:
                    idx.field_funcs[(ip, gtype.name, fname)].append(resolved.qualname)
                elif val is not None and val.type == "identifier":
                    pname = text(val)
                    if pname in fn.params:
                        idx.ctor_field_params[(fn.import_path, fn.name)][fname] = pname
            if val is not None:
                concrete = self._infer_value_type(val, fn, info, local_types, idx)
                if concrete is not None and (concrete.import_path, concrete.name) not in idx.interfaces:
                    idx.field_concretes[(ip, gtype.name, fname)].append(concrete)
            if fname in iface_fields:
                if val is not None and val.type == "identifier":
                    pname = text(val)
                    if pname in fn.params:
                        idx.ctor_field_params[(fn.import_path, fn.name)][fname] = pname
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
        local_types = self._local_types(fn, info, idx)
        func_fields = idx.func_fields.get((ret_ip, ret.name), set())
        for fname, pname in mapping.items():
            if pname not in param_order:
                continue
            i = param_order.index(pname)
            if i >= len(args):
                continue
            if fname in func_fields:
                resolved = self._resolve_func_expr(args[i], fn, info, idx)
                if resolved is not None:
                    idx.field_funcs[(ret_ip, ret.name, fname)].append(resolved.qualname)
            concrete = self._infer_value_type(args[i], fn, info, local_types, idx)
            if concrete is not None and (concrete.import_path, concrete.name) not in idx.interfaces:
                idx.field_concretes[(ret_ip, ret.name, fname)].append(concrete)

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
        calls: list[CallHit],
        ticker_rule: Rule,
    ) -> list[JobHit]:
        callees_of: dict[str, set[str]] = defaultdict(set)
        for hit in calls:
            callees_of[hit.caller].add(hit.callee)
        jobs: list[JobHit] = []
        seen_handlers: set[str] = set()
        for info in infos:
            for node in typed_walk(info.tree, _GO_STMT):
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
            for node in typed_walk(cur.node, _CALL):
                if _is_scheduler_call(node, cur_info):
                    return True
            for qn in callees_of.get(cur.qualname, ()):
                nxt = idx.fn_index.get(qn)
                if nxt is not None:
                    stack.append(nxt)
            # Closures / composite-literal bodies: resolve callees inside this node too.
            dummy_info = cur_info
            for node in typed_walk(cur.node, _CALL):
                parent = node.parent
                if parent is not None and parent.type == "go_statement":
                    continue
                callee = self._resolve_callee(node, cur, dummy_info, idx)
                if callee is not None and callee.qualname not in seen:
                    stack.append(callee)
        return False
