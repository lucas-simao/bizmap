from __future__ import annotations

import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field

from tree_sitter import Node

from bizmap.detect.tsutil import call_args, call_callee_name, text, walk
from bizmap.overrides import lookup_yaml_constant

_ENV_SUB = re.compile(
    r"""(?:os\.)?environ\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""
)


@dataclass
class ResolvedString:
    text: str | None
    fully_static: bool
    dynamic: bool
    placeholder: str | None = None
    alternatives: list[str] | None = None
    note: str | None = None

    @property
    def usable(self) -> bool:
        if self.alternatives:
            return any(str(t).strip() for t in self.alternatives if t)
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

    def all_texts(self) -> list[str]:
        if self.alternatives:
            return [t for t in self.alternatives if t and str(t).strip()]
        text = self.path_text
        return [text] if text and str(text).strip() else []


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
    lefts = lv.all_texts() or [""]
    rights = rv.all_texts() or [""]
    if lv.fully_static and rv.fully_static and lv.text is not None and rv.text is not None and len(lefts) == 1 and len(rights) == 1:
        return ResolvedString(lv.text + rv.text, True, False)

    def piece(part: ResolvedString) -> str:
        if part.fully_static and part.text is not None:
            return part.text
        if part.placeholder:
            return part.placeholder
        return part.text or ""

    texts = [a + b for a in lefts for b in rights]
    texts = [t for t in dict.fromkeys(texts) if t]
    combined = piece(lv) + piece(rv)
    static = lv.fully_static and rv.fully_static
    return ResolvedString(
        texts[0] if texts else combined or None,
        static,
        not static,
        placeholder=None if static else (combined or None),
        alternatives=texts or None,
    )


def _placeholder_for(name: str) -> ResolvedString:
    token = name if name.startswith("{") else f"{{{name}}}"
    return ResolvedString(None, False, True, placeholder=token)


_BASE_FIELD_NAMES = {"base_url", "baseurl", "base", "host", "origin"}


def _local_base_field(attr_name: str, found: ResolvedString) -> bool:
    """True when ``self.base_url`` is a constructor/config field, not an env origin."""
    if attr_name.lower() not in _BASE_FIELD_NAMES or not found.unresolved:
        return False
    inner = (found.placeholder or found.path_text or "").strip("{}")
    leaf = inner.split(".")[-1].lower()
    return leaf in _BASE_FIELD_NAMES or leaf == attr_name.lower()


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
    dicts: dict[str, dict[str, ResolvedString]] | None = None,
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] | None = None,
    call_returns: dict[str, ResolvedString] | None = None,
) -> ResolvedString:
    imap = imap or ImportMap()
    consts_by_module = consts_by_module or {}
    dicts = dicts or {}
    dicts_by_module = dicts_by_module or {}
    call_returns = call_returns or {}
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
            yaml = consts_by_module.get("__yaml__") or {}
            for key in (f"{mod}.{orig}", orig, name):
                hit = yaml.get(key)
                if hit and hit.usable:
                    return hit
            return _placeholder_for(f"{mod}.{orig}")
        return _placeholder_for(name)
    if node.type == "attribute":
        raw = text(node)
        obj = node.named_children[0] if node.named_children else None
        attr = node.named_children[-1] if len(node.named_children) >= 2 else None
        attr_name = text(attr) if attr is not None else raw.rsplit(".", 1)[-1]
        if obj is not None and obj.type == "identifier":
            head = text(obj)
            if head == "self":
                key = f"self.{attr_name}"
                if key in env:
                    found = env[key]
                    if _local_base_field(attr_name, found):
                        return _placeholder_for("base")
                    return found
                if key in module_consts:
                    return module_consts[key]
                if attr_name in env:
                    found = env[attr_name]
                    if _local_base_field(attr_name, found):
                        return _placeholder_for("base")
                    return found
                if attr_name in module_consts:
                    return module_consts[attr_name]
                if attr_name.lower() in {"base_url", "baseurl", "base", "host", "origin"}:
                    return _placeholder_for("base")
            if head in imap.modules:
                mod = imap.modules[head]
                found = (consts_by_module.get(mod) or {}).get(attr_name)
                if found and found.usable:
                    return found
                yaml = consts_by_module.get("__yaml__") or {}
                for key in (f"{mod}.{attr_name}", f"{head}.{attr_name}", attr_name):
                    hit = yaml.get(key)
                    if hit and hit.usable:
                        return hit
                yaml_val = lookup_yaml_constant(
                    {k: (v.path_text if v.usable else "") for k, v in yaml.items()},
                    attr_name,
                    mod,
                )
                if yaml_val:
                    return ResolvedString(yaml_val, True, False)
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
            if head in {"settings", "config", "conf"}:
                return _placeholder_for(f"{head}.{attr_name}")
            if head in env:
                inner = env[head]
                if inner.placeholder or not inner.fully_static:
                    # origin-preserving: settings-like attrs keep the attr spelling
                    return _placeholder_for(f"{head}.{attr_name}")
        return _placeholder_for(raw)
    if node.type == "parenthesized_expression":
        if node.named_children:
            return resolve_expr(node.named_children[0], env, module_consts, imap, consts_by_module)
    if node.type == "conditional_expression":
        # ``A if cond else B`` — emit both candidates.
        children = list(node.named_children)
        if len(children) >= 3:
            left = resolve_expr(children[0], env, module_consts, imap, consts_by_module, dicts, dicts_by_module)
            right = resolve_expr(children[2], env, module_consts, imap, consts_by_module, dicts, dicts_by_module)
            texts: list[str] = []
            for part in (left, right):
                texts.extend(part.all_texts())
            texts = [t for t in dict.fromkeys(texts) if t]
            if not texts:
                return _placeholder_for(text(node)[:40])
            static = left.fully_static and right.fully_static
            return ResolvedString(
                texts[0],
                static,
                not static,
                placeholder=None if static else texts[0],
                alternatives=texts,
            )
        if node.named_children:
            return resolve_expr(node.named_children[0], env, module_consts, imap, consts_by_module, dicts, dicts_by_module)
    if node.type == "interpolation":
        inner = next((c for c in node.named_children if c.type != "type"), None)
        return resolve_expr(inner, env, module_consts, imap, consts_by_module)
    if node.type == "subscript":
        raw = text(node)
        env_m = _ENV_SUB.search(raw)
        if env_m:
            return _placeholder_for(env_m.group(1))
        found = _resolve_dict_subscript(
            node, env, module_consts, imap, consts_by_module, dicts, dicts_by_module
        )
        if found is not None:
            return found
        return _placeholder_for(raw[:48])
    if node.type == "call":
        qual = call_qualname(node)
        leaf = qual.rsplit(".", 1)[-1]
        if leaf in {"getenv", "get"} and (
            "environ" in qual or qual in {"os.getenv", "getenv"} or qual.endswith("os.getenv")
        ):
            pos = call_posargs(node)
            if pos:
                key = resolve_expr(
                    pos[0],
                    env,
                    module_consts,
                    imap,
                    consts_by_module,
                    dicts,
                    dicts_by_module,
                    call_returns,
                )
                if key.fully_static and key.text:
                    return _placeholder_for(key.text)
                if key.placeholder:
                    inner = key.placeholder.strip("{}")
                    if inner:
                        return _placeholder_for(inner)
        # One-hop helper: `_get_url()` whose body returns an f-string / concat.
        for key in (qual, leaf):
            found = call_returns.get(key)
            if found and found.usable:
                return found
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


def _subscript_index(node: Node) -> Node | None:
    idx = node.child_by_field_name("subscript")
    if idx is not None:
        return idx
    named = node.named_children
    if len(named) >= 2:
        return named[1]
    return None


def dict_literal_map(
    node: Node | None,
    resolve: Callable[[Node | None], ResolvedString],
) -> dict[str, ResolvedString]:
    out: dict[str, ResolvedString] = {}
    for key_n, val_n in dict_pairs(node):
        if key_n.type == "string":
            key = unquote(text(key_n))
        elif key_n.type in {"integer", "float"}:
            key = text(key_n)
        else:
            resolved = resolve(key_n)
            key = resolved.path_text if resolved.fully_static else ""
        if not key:
            continue
        out[key] = resolve(val_n)
    return out


def collect_module_dicts(
    assignments: list[tuple[str, Node]],
    imap: ImportMap,
    consts_by_module: dict[str, dict[str, ResolvedString]] | None = None,
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] | None = None,
) -> dict[str, dict[str, ResolvedString]]:
    env: dict[str, ResolvedString] = {}
    consts_by_module = consts_by_module or {}
    dicts_by_module = dicts_by_module or {}
    dicts: dict[str, dict[str, ResolvedString]] = {}

    def resolve(node: Node | None) -> ResolvedString:
        return resolve_expr(node, env, env, imap, consts_by_module, dicts, dicts_by_module)

    for name, value in assignments:
        env[name] = resolve(value)
        if value.type == "dictionary":
            dicts[name] = dict_literal_map(value, resolve)
        elif value.type == "identifier" and text(value) in dicts:
            dicts[name] = dicts[text(value)]
        elif value.type == "dictionary_comprehension":
            transformed = _dict_from_comprehension(
                value, dicts, env, imap, consts_by_module, dicts_by_module
            )
            if transformed:
                dicts[name] = transformed
    return dicts


def _module_fn_nodes(node: Node) -> dict[str, Node]:
    cur: Node | None = node
    while cur is not None and cur.parent is not None:
        cur = cur.parent
    if cur is None:
        return {}
    out: dict[str, Node] = {}
    for child in cur.named_children:
        fn = child
        if child.type == "decorated_definition" and child.named_children:
            fn = child.named_children[-1]
        if fn.type != "function_definition":
            continue
        name_n = fn.child_by_field_name("name")
        name = text(name_n) if name_n else ""
        if name:
            out[name] = fn
    return out


def _dict_from_comprehension(
    node: Node,
    dicts: dict[str, dict[str, ResolvedString]],
    env: dict[str, ResolvedString] | None = None,
    imap: ImportMap | None = None,
    consts_by_module: dict[str, dict[str, ResolvedString]] | None = None,
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] | None = None,
) -> dict[str, ResolvedString] | None:
    """``{k: transform(v) for k, v in queries.RAW.items()}`` with static transforms."""
    imap = imap or ImportMap()
    env = env or {}
    consts_by_module = consts_by_module or {}
    dicts_by_module = dicts_by_module or {}
    src: dict[str, ResolvedString] | None = None
    for cur in walk(node):
        if cur.type != "call":
            continue
        if call_callee_name(cur) != "items":
            continue
        func = cur.child_by_field_name("function")
        if func is None or func.type != "attribute" or not func.named_children:
            continue
        obj = func.named_children[0]
        src = lookup_dict_from_node(obj, imap, dicts, dicts_by_module)
        if src:
            break
        if obj.type == "identifier" and text(obj) in dicts:
            src = dicts[text(obj)]
            break
    if not src:
        return None
    pair = next((c for c in node.named_children if c.type == "pair"), None)
    val_n = pair.child_by_field_name("value") if pair is not None else None
    fns = _module_fn_nodes(node)
    transform, known, note = _eval_sql_transform(val_n, "v", env, fns)
    out: dict[str, ResolvedString] = {}
    for key, val in src.items():
        texts = val.all_texts() or ([val.path_text] if val.path_text else [])
        if not texts:
            out[key] = val
            continue
        mapped = [transform(t) for t in texts]
        mapped = [t for t in mapped if t]
        static = val.fully_static and known
        item = ResolvedString(
            mapped[0] if mapped else val.text,
            True if mapped else static,
            not known or not val.fully_static,
            placeholder=None if (known and val.fully_static) else (mapped[0] if mapped else val.placeholder),
            alternatives=mapped if len(mapped) > 1 else None,
            note=note,
        )
        if known and val.fully_static:
            item = ResolvedString(
                mapped[0] if mapped else val.text,
                True,
                False,
                alternatives=mapped if len(mapped) > 1 else None,
                note=note,
            )
        out[key] = item
    return out or None


def _eval_sql_transform(
    node: Node | None,
    value_param: str,
    env: dict[str, ResolvedString],
    fns: dict[str, Node],
) -> tuple[Callable[[str], str], bool, str | None]:
    """Return (transform, known, note) for a dict-comprehension value expr."""

    def identity(s: str) -> str:
        return s

    if node is None:
        return identity, True, None
    if node.type == "identifier" and text(node) == value_param:
        return identity, True, None
    built = _static_str_transform(node, value_param, env, fns, depth=0)
    if built is not None:
        return built, True, None
    raw = text(node)[:80]
    return identity, False, f"unknown SQL transform {raw}; kept raw tables"


def _const_text(node: Node | None, env: dict[str, ResolvedString]) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return unquote(text(node))
    if node.type == "identifier":
        name = text(node)
        found = env.get(name)
        if found and found.fully_static and found.text is not None:
            return found.text
        return None
    if node.type == "attribute":
        raw = text(node)
        found = env.get(raw)
        if found and found.fully_static and found.text is not None:
            return found.text
        return None
    return None


def _static_str_transform(
    node: Node | None,
    value_param: str,
    env: dict[str, ResolvedString],
    fns: dict[str, Node],
    depth: int,
) -> Callable[[str], str] | None:
    if node is None or depth > 6:
        return None
    if node.type == "identifier" and text(node) == value_param:
        return lambda s: s
    if node.type != "call":
        return None
    leaf = call_callee_name(node)
    func = node.child_by_field_name("function")
    pos = call_posargs(node)
    kwargs = call_kwargs(node)
    obj = None
    if func is not None and func.type == "attribute" and func.named_children:
        obj = func.named_children[0]
    inner = _static_str_transform(obj, value_param, env, fns, depth + 1) if obj is not None else None
    if leaf in {"strip", "lstrip", "rstrip", "dedent"}:
        base = inner or (
            (lambda s: s) if obj is not None and obj.type == "identifier" and text(obj) == value_param else None
        )
        if leaf == "dedent" and pos:
            base = inner or (
                (lambda s: s)
                if pos[0].type == "identifier" and text(pos[0]) == value_param
                else None
            )
        if base is None:
            return None
        if leaf == "strip":
            return lambda s, b=base: b(s).strip()
        if leaf == "lstrip":
            return lambda s, b=base: b(s).lstrip()
        if leaf == "rstrip":
            return lambda s, b=base: b(s).rstrip()
        return lambda s, b=base: textwrap.dedent(b(s))
    if leaf == "replace":
        if inner is None and obj is not None and obj.type == "identifier" and text(obj) == value_param:
            def inner(s: str) -> str:
                return s
        if inner is None or len(pos) < 2:
            return None
        old = _const_text(pos[0], env)
        new = _const_text(pos[1], env)
        if old is None or new is None:
            return None
        return lambda s, b=inner, o=old, n=new: b(s).replace(o, n)
    if leaf == "format":
        if inner is None and obj is not None and obj.type == "identifier" and text(obj) == value_param:
            def inner(s: str) -> str:
                return s
        if inner is None:
            return None
        fmt_kwargs: dict[str, str] = {}
        for k, vnode in kwargs.items():
            val = _const_text(vnode, env)
            if val is None:
                return None
            fmt_kwargs[k] = val
        fmt_args: list[str] = []
        for arg in pos:
            val = _const_text(arg, env)
            if val is None:
                return None
            fmt_args.append(val)
        captured_inner = inner
        captured_args = tuple(fmt_args)
        captured_kwargs = dict(fmt_kwargs)

        def apply_fmt(s: str) -> str:
            try:
                return captured_inner(s).format(*captured_args, **captured_kwargs)
            except (KeyError, IndexError, ValueError):
                return captured_inner(s)

        return apply_fmt
    if leaf in fns:
        if not pos or not (pos[0].type == "identifier" and text(pos[0]) == value_param):
            if inner is None:
                return None
        ret = _function_return_expr(fns[leaf])
        return _static_str_transform(ret, _first_param_name(fns[leaf]) or value_param, env, fns, depth + 1)
    return None


def _first_param_name(fn: Node) -> str | None:
    params = fn.child_by_field_name("parameters")
    if params is None:
        return None
    for child in params.named_children:
        if child.type == "identifier":
            return text(child)
        ident = child.child_by_field_name("name") or child.child_by_field_name("pattern")
        if ident is not None and ident.type == "identifier":
            return text(ident)
        for inner in child.named_children:
            if inner.type == "identifier":
                return text(inner)
    return None


def _function_return_expr(fn: Node) -> Node | None:
    body = fn.child_by_field_name("body")
    if body is None:
        return None
    expr: Node | None = None
    for child in body.named_children:
        if child.type == "return_statement" and child.named_children:
            expr = child.named_children[0]
        elif child.type not in {"expression_statement", "pass_statement"}:
            if child.type != "return_statement":
                # Non-trivial body (assignments, calls) — not a simple transform.
                if child.type != "function_definition":
                    return None
    return expr


def imported_module_name(head: str, imap: ImportMap) -> str | None:
    """Resolve ``queries`` / ``q`` to a scanned module path."""
    if head in imap.modules:
        return imap.modules[head]
    if head in imap.from_names:
        pkg, orig = imap.from_names[head]
        if pkg and orig:
            nested = f"{pkg}.{orig}"
            return nested
        return orig or pkg or head
    return None


def lookup_dict(
    name: str,
    imap: ImportMap,
    dicts: dict[str, dict[str, ResolvedString]],
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]],
) -> dict[str, ResolvedString] | None:
    if name in dicts:
        return dicts[name]
    if name in imap.from_names:
        mod, orig = imap.from_names[name]
        found = (dicts_by_module.get(mod) or {}).get(orig)
        if found:
            return found
        found = (dicts_by_module.get(mod) or {}).get(name)
        if found:
            return found
        nested = f"{mod}.{orig}" if mod else orig
        found = (dicts_by_module.get(nested) or {}).get(orig)
        if found:
            return found
    if name in imap.modules:
        return None
    return None


def lookup_dict_from_node(
    obj: Node | None,
    imap: ImportMap,
    dicts: dict[str, dict[str, ResolvedString]],
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]],
) -> dict[str, ResolvedString] | None:
    """``Q``, ``queries.Q``, or ``q.Q`` after ``import pkg.queries as q``."""
    if obj is None:
        return None
    if obj.type == "identifier":
        return lookup_dict(text(obj), imap, dicts, dicts_by_module)
    if obj.type != "attribute" or len(obj.named_children) < 2:
        return None
    head = obj.named_children[0]
    attr = obj.named_children[-1]
    if head.type != "identifier":
        return None
    dname = text(attr)
    local_hit = lookup_dict(dname, imap, dicts, dicts_by_module)
    if local_hit and text(head) not in imap.modules and text(head) not in imap.from_names:
        return local_hit
    mod = imported_module_name(text(head), imap)
    if not mod:
        return None
    found = (dicts_by_module.get(mod) or {}).get(dname)
    if found:
        return found
    # ``from . import queries`` stored as (pkg, queries) — try pkg.queries
    if "." not in mod:
        for key, dmap in dicts_by_module.items():
            if key == mod or key.endswith("." + mod):
                if dname in dmap:
                    return dmap[dname]
    return None


def _resolve_dict_subscript(
    node: Node,
    env: dict[str, ResolvedString],
    module_consts: dict[str, ResolvedString],
    imap: ImportMap,
    consts_by_module: dict[str, dict[str, ResolvedString]],
    dicts: dict[str, dict[str, ResolvedString]],
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]],
) -> ResolvedString | None:
    obj = node.child_by_field_name("value") or (
        node.named_children[0] if node.named_children else None
    )
    idx = _subscript_index(node)
    if obj is None:
        return None
    dmap = lookup_dict_from_node(obj, imap, dicts, dicts_by_module)
    if not dmap:
        return None
    key = resolve_expr(idx, env, module_consts, imap, consts_by_module, dicts, dicts_by_module)
    if key.fully_static and key.text is not None and key.text in dmap:
        return dmap[key.text]
    # Dynamic key: placeholder listing that the caller may expand to all values.
    return ResolvedString(
        None,
        False,
        True,
        placeholder="{" + text(obj) + "[*]}",
    )


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


def collect_local_env(
    fn_node: Node,
    module_consts: dict[str, ResolvedString],
    imap: ImportMap,
    consts_by_module: dict[str, dict[str, ResolvedString]] | None = None,
    dicts: dict[str, dict[str, ResolvedString]] | None = None,
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] | None = None,
    call_returns: dict[str, ResolvedString] | None = None,
) -> dict[str, ResolvedString]:
    """Function-local assignments, so ``base = os.environ['X']; base + '/p'`` keeps X."""
    env: dict[str, ResolvedString] = dict(module_consts)
    consts_by_module = consts_by_module or {}
    dicts = dicts or {}
    dicts_by_module = dicts_by_module or {}
    call_returns = call_returns or {}
    body = fn_node.child_by_field_name("body") or fn_node
    for node in walk(body):
        if node.type != "assignment":
            continue
        name = assignment_target_name(node)
        value = node.child_by_field_name("right")
        if not name or value is None:
            continue
        env[name] = resolve_expr(
            value,
            env,
            module_consts,
            imap,
            consts_by_module,
            dicts,
            dicts_by_module,
            call_returns,
        )
    return env


def simple_return_value(
    fn_node: Node,
    env: dict[str, ResolvedString],
    module_consts: dict[str, ResolvedString],
    imap: ImportMap,
    consts_by_module: dict[str, dict[str, ResolvedString]] | None = None,
    dicts: dict[str, dict[str, ResolvedString]] | None = None,
    dicts_by_module: dict[str, dict[str, dict[str, ResolvedString]]] | None = None,
) -> ResolvedString | None:
    """Single return expression of a helper (one hop, no nested call_returns)."""
    body = fn_node.child_by_field_name("body") or fn_node
    found: list[ResolvedString] = []
    for node in walk(body):
        if node.type != "return_statement":
            continue
        if not node.named_children:
            continue
        found.append(
            resolve_expr(
                node.named_children[0],
                env,
                module_consts,
                imap,
                consts_by_module,
                dicts,
                dicts_by_module,
            )
        )
    usable = [r for r in found if r.usable]
    if len(usable) == 1:
        return usable[0]
    if usable and all(r.path_text == usable[0].path_text for r in usable):
        return usable[0]
    return None
