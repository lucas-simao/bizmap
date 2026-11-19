from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tree_sitter import Node

from bizmap.detect.tsutil import call_args, call_callee_name, text, walk


@dataclass
class ResolvedString:
    text: str | None
    fully_static: bool
    dynamic: bool
    placeholder: str | None = None

    @property
    def usable(self) -> bool:
        return bool(
            (self.text and str(self.text).strip())
            or (self.placeholder and str(self.placeholder).strip())
        )

    @property
    def path_text(self) -> str:
        if self.fully_static and self.text is not None:
            return self.text
        if self.placeholder:
            return self.placeholder
        return self.text or ""

    @property
    def unresolved(self) -> bool:
        return not self.fully_static


@dataclass
class ImportMap:
    """Names visible in a Python module."""

    raw: list[str] = field(default_factory=list)
    # local name -> (module, original_name) for `from mod import orig as local`
    from_names: dict[str, tuple[str, str]] = field(default_factory=dict)
    # alias -> module for `import mod as alias`
    modules: dict[str, str] = field(default_factory=dict)


def unquote(raw: str) -> str:
    """Strip Python string prefixes and matching quotes."""
    s = raw.strip()
    i = 0
    while i < len(s) and s[i] in "frubFRUB":
        i += 1
    body = s[i:]
    if len(body) >= 6 and body[:3] in {'"""', "'''"} and body[-3:] == body[:3]:
        return body[3:-3]
    if len(body) >= 2 and body[0] in {'"', "'"} and body[-1] == body[0]:
        return body[1:-1]
    return raw


def join_route_path(*parts: str) -> str:
    """Join route prefixes without dropping unresolved placeholders."""
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
        if "://" in piece and not out:
            out = piece.rstrip("/")
            continue
        if out.endswith("://") or ("://" in out and out.rstrip("/").count("/") == 2 and not piece.startswith("/")):
            piece = "/" + piece.lstrip("/")
        if not piece.startswith("/") and not piece.startswith("{") and out and not out.endswith("/"):
            if "://" not in piece:
                piece = "/" + piece
        if out.endswith("/"):
            out = out[:-1]
        if piece == "/":
            if not out:
                out = "/"
            continue
        out += piece
    return out or "/"


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


def _placeholder_for(name: str) -> ResolvedString:
    token = name if name.startswith("{") else f"{{{name}}}"
    return ResolvedString(None, False, True, placeholder=token)


def parse_imports(root: Node, module: str) -> ImportMap:
    imap = ImportMap()
    for node in walk(root):
        if node.type == "import_statement":
            imap.raw.append(text(node))
            for child in node.named_children:
                _record_import_mod(imap, child)
        elif node.type == "import_from_statement":
            imap.raw.append(text(node))
            mod = _from_module(node, module)
            if not mod:
                continue
            skip_module = True
            for child in node.named_children:
                if child.type == "relative_import":
                    skip_module = False
                    continue
                if child.type == "dotted_name" and skip_module:
                    skip_module = False
                    continue
                if child.type == "aliased_import":
                    orig_n = child.child_by_field_name("name") or next(
                        (c for c in child.named_children if c.type in {"dotted_name", "identifier"}),
                        None,
                    )
                    alias_n = child.child_by_field_name("alias") or next(
                        (c for c in reversed(child.named_children) if c.type == "identifier"),
                        None,
                    )
                    orig = text(orig_n).split(".")[-1] if orig_n else ""
                    alias = text(alias_n) if alias_n else orig
                    if alias:
                        imap.from_names[alias] = (mod, orig or alias)
                elif child.type in {"dotted_name", "identifier"}:
                    name = text(child).split(".")[-1]
                    if name and name not in {"from", "import"}:
                        imap.from_names[name] = (mod, name)
    return imap


def _record_import_mod(imap: ImportMap, node: Node) -> None:
    if node.type == "aliased_import":
        orig_n = node.child_by_field_name("name") or next(
            (c for c in node.named_children if c.type in {"dotted_name", "identifier"}),
            None,
        )
        alias_n = node.child_by_field_name("alias") or next(
            (c for c in reversed(node.named_children) if c.type == "identifier"),
            None,
        )
        orig = text(orig_n) if orig_n else ""
        alias = text(alias_n) if alias_n else orig.split(".")[-1]
        if alias and orig:
            imap.modules[alias] = orig
        return
    if node.type in {"dotted_name", "identifier"}:
        name = text(node)
        if name:
            imap.modules[name.split(".")[-1]] = name
            imap.modules[name] = name


def _from_module(node: Node, current: str) -> str:
    rel = next((c for c in node.named_children if c.type == "relative_import"), None)
    dotted = next((c for c in node.named_children if c.type == "dotted_name"), None)
    if rel is not None:
        prefix = next((c for c in rel.children if c.type == "import_prefix"), None)
        rest_n = next((c for c in rel.named_children if c.type == "dotted_name"), None)
        dots = text(prefix) if prefix is not None else "."
        rest = text(rest_n) if rest_n is not None else ""
        n = len(dots)
        parts = current.split(".")
        parent = parts[:-n] if n <= len(parts) else []
        if rest:
            return ".".join([*parent, rest]) if parent else rest
        return ".".join(parent)
    if dotted is not None:
        return text(dotted)
    return ""


def collect_module_assignments(root: Node) -> list[tuple[str, Node]]:
    """Module-level `NAME = expr` (not inside a function or class)."""
    out: list[tuple[str, Node]] = []
    for child in root.named_children:
        if child.type == "expression_statement" and child.named_children:
            inner = child.named_children[0]
            if inner.type == "assignment":
                name = _assignment_name(inner)
                value = inner.child_by_field_name("right")
                if name and value is not None:
                    out.append((name, value))
        elif child.type == "decorated_definition":
            continue
    return out


def _assignment_name(node: Node) -> str | None:
    left = node.child_by_field_name("left")
    if left is None:
        return None
    if left.type == "identifier":
        return text(left)
    if left.type == "pattern_list" and left.named_children:
        first = left.named_children[0]
        if first.type == "identifier":
            return text(first)
    return None


def resolve_expr(
    node: Node | None,
    env: dict[str, ResolvedString],
    module_consts: dict[str, ResolvedString],
    imap: ImportMap | None = None,
    consts_by_module: dict[str, dict[str, ResolvedString]] | None = None,
) -> ResolvedString:
    imap = imap or ImportMap()
    consts_by_module = consts_by_module or {}
    if node is None:
        return ResolvedString(None, False, True)
    if node.type == "string":
        return _resolve_string_node(node, env, module_consts, imap, consts_by_module)
    if node.type == "concatenated_string":
        acc = ResolvedString("", True, False)
        for child in node.named_children:
            acc = _plus(acc, resolve_expr(child, env, module_consts, imap, consts_by_module))
        return acc
    if node.type == "binary_operator":
        ops = [text(c) for c in node.children if not c.is_named]
        if ops and ops[0] != "+":
            return _placeholder_for(text(node)[:40])
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        return _plus(
            resolve_expr(left, env, module_consts, imap, consts_by_module),
            resolve_expr(right, env, module_consts, imap, consts_by_module),
        )
    if node.type == "identifier":
        name = text(node)
        if name in env:
            return env[name]
        if name in module_consts:
            return module_consts[name]
        if name in imap.from_names:
            mod, orig = imap.from_names[name]
            found = (consts_by_module.get(mod) or {}).get(orig)
            if found and found.usable:
                return found
            return _placeholder_for(f"{mod}.{orig}")
        return _placeholder_for(name)
    if node.type == "attribute":
        raw = text(node)
        obj = node.named_children[0] if node.named_children else None
        attr = node.named_children[-1] if len(node.named_children) >= 2 else None
        attr_name = text(attr) if attr is not None else raw.rsplit(".", 1)[-1]
        if obj is not None and obj.type == "identifier":
            head = text(obj)
            if head in imap.modules:
                mod = imap.modules[head]
                found = (consts_by_module.get(mod) or {}).get(attr_name)
                if found and found.usable:
                    return found
                return _placeholder_for(f"{mod}.{attr_name}")
            if head in imap.from_names:
                mod, orig = imap.from_names[head]
                # `paths.BASE` where paths is a from-imported submodule
                found = (consts_by_module.get(mod) or {}).get(attr_name)
                if found and found.usable:
                    return found
                nested = consts_by_module.get(f"{mod}.{orig}") or {}
                if attr_name in nested and nested[attr_name].usable:
                    return nested[attr_name]
                return _placeholder_for(f"{head}.{attr_name}")
            if head in env:
                return _placeholder_for(f"{head}.{attr_name}")
        return _placeholder_for(raw)
    if node.type in {"parenthesized_expression", "conditional_expression"}:
        if node.named_children:
            return resolve_expr(node.named_children[0], env, module_consts, imap, consts_by_module)
    if node.type == "interpolation":
        inner = next((c for c in node.named_children if c.type != "type"), None)
        return resolve_expr(inner, env, module_consts, imap, consts_by_module)
    if node.type == "subscript":
        return _placeholder_for(text(node)[:48])
    if node.type == "call":
        # os.environ.get("X", "/fallback") — unresolved env, keep placeholder
        return _placeholder_for(text(node)[:48])
    if node.type in {"integer", "float", "true", "false", "none"}:
        return ResolvedString(text(node), True, False)
    return _placeholder_for(text(node)[:48] if text(node) else "expr")


def _resolve_string_node(
    node: Node,
    env: dict[str, ResolvedString],
    module_consts: dict[str, ResolvedString],
    imap: ImportMap,
    consts_by_module: dict[str, dict[str, ResolvedString]],
) -> ResolvedString:
    interpolations = [c for c in node.named_children if c.type == "interpolation"]
    if not interpolations:
        return ResolvedString(unquote(text(node)), True, False)
    # f-string: walk children in order (string_content + interpolation)
    acc = ResolvedString("", True, False)
    for child in node.children:
        if child.type in {"string_start", "string_end"}:
            continue
        if child.type == "string_content":
            acc = _plus(acc, ResolvedString(text(child), True, False))
        elif child.type == "interpolation":
            acc = _plus(
                acc, resolve_expr(child, env, module_consts, imap, consts_by_module)
            )
        elif child.type == "escape_interpolation":
            acc = _plus(acc, ResolvedString(text(child), True, False))
    return acc


def resolve_module_consts(
    assignments: list[tuple[str, Node]],
    imap: ImportMap,
    consts_by_module: dict[str, dict[str, ResolvedString]] | None = None,
) -> dict[str, ResolvedString]:
    env: dict[str, ResolvedString] = {}
    consts_by_module = consts_by_module or {}
    for _ in range(3):
        for name, value in assignments:
            env[name] = resolve_expr(value, env, env, imap, consts_by_module)
    return env


def call_kwargs(call: Node) -> dict[str, Node]:
    out: dict[str, Node] = {}
    for arg in call_args(call):
        if arg.type == "keyword_argument":
            name_n = arg.child_by_field_name("name")
            val_n = arg.child_by_field_name("value")
            if name_n is not None and val_n is not None:
                out[text(name_n)] = val_n
    return out


def call_posargs(call: Node) -> list[Node]:
    return [a for a in call_args(call) if a.type != "keyword_argument"]


def call_qualname(call: Node) -> str:
    func = call.child_by_field_name("function")
    if func is None and call.named_children:
        func = call.named_children[0]
    return text(func) if func is not None else call_callee_name(call)


def assignment_target_name(node: Node) -> str | None:
    if node.type != "assignment":
        return _assignment_name(node) if node.type else None
    return _assignment_name(node)


def dict_pairs(node: Node | None) -> list[tuple[Node, Node]]:
    if node is None:
        return []
    if node.type != "dictionary":
        # call(request={...}) already a dict; or nested
        if node.type == "keyword_argument":
            return dict_pairs(node.child_by_field_name("value"))
        return []
    pairs: list[tuple[Node, Node]] = []
    for child in node.named_children:
        if child.type == "pair":
            key = child.child_by_field_name("key")
            val = child.child_by_field_name("value")
            if key is None and child.named_children:
                key = child.named_children[0]
                val = child.named_children[1] if len(child.named_children) > 1 else None
            if key is not None and val is not None:
                pairs.append((key, val))
    return pairs


def find_dict_string(
    node: Node | None,
    keys: set[str],
    resolve: Callable[[Node | None], ResolvedString] | None = None,
) -> ResolvedString | None:
    """Find a string value for any of ``keys`` in a nested dict/call tree."""
    if node is None or resolve is None:
        return None
    wanted = {k.lower() for k in keys}
    for cur in walk(node):
        if cur.type == "keyword_argument":
            name = text(cur.child_by_field_name("name") or cur)
            val = cur.child_by_field_name("value")
            if name.lower() in wanted and val is not None:
                found = resolve(val)
                if found.usable:
                    return found
        elif cur.type == "pair":
            key_n = cur.child_by_field_name("key") or (
                cur.named_children[0] if cur.named_children else None
            )
            val_n = cur.child_by_field_name("value") or (
                cur.named_children[1] if len(cur.named_children) > 1 else None
            )
            key_txt = unquote(text(key_n)) if key_n is not None else ""
            if key_txt.lower() in wanted and val_n is not None:
                found = resolve(val_n)
                if found.usable:
                    return found
    return None
