"""Parameters and return type of a function, as written in its code.

One small function per language reads a tree-sitter function node and returns
the attributes a graph ``function`` node gets:

    {"params": [{"name": "id", "type": "string"}, ...], "returns": "Order | null"}

``params`` is ``[]`` for a function without parameters; ``type`` is left out
when the parameter has no annotation, ``returns`` when there is no return
type. Types are the code's own text, whitespace collapsed, at most
``MAX_TYPE`` characters. Detectors call these once per function; nothing else
in the scan depends on them.
"""

from __future__ import annotations

from typing import Any

from bizmap.detect.tsutil import Node, text

MAX_TYPE = 120


def _clean(raw: str | None) -> str:
    out = " ".join((raw or "").split())
    return out if len(out) <= MAX_TYPE else out[: MAX_TYPE - 1] + "…"


def _param(name: str, typ: str | None = None, *, optional: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"name": _clean(name) or "_"}
    t = _clean(typ)
    if t:
        out["type"] = t
    if optional:
        out["optional"] = True
    return out


def _attrs(params: list[dict[str, Any]], returns: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"params": params}
    r = _clean(returns)
    if r:
        out["returns"] = r
    return out


# --- Go ---------------------------------------------------------------------


def go_signature(node: Node | None) -> dict[str, Any]:
    """`func (h *H) Find(ctx context.Context, id string) (*Order, error)` ->
    params ctx/id (the receiver is in the name), returns `(*Order, error)`;
    a variadic `names ...string` keeps its `...`."""
    if node is None:
        return {}
    plist = node.child_by_field_name("parameters")
    if plist is None:
        return {}
    params: list[dict[str, Any]] = []
    for p in plist.named_children:
        typ_n = p.child_by_field_name("type")
        typ = text(typ_n) if typ_n is not None else ""
        if p.type == "variadic_parameter_declaration":
            typ = "..." + typ
        elif p.type != "parameter_declaration":
            continue
        names = [text(n) for n in p.children_by_field_name("name")] if hasattr(p, "children_by_field_name") else []
        if not names:
            name_n = p.child_by_field_name("name")
            names = [text(name_n)] if name_n is not None else ["_"]
        params.extend(_param(n, typ) for n in names)
    result = node.child_by_field_name("result")
    return _attrs(params, text(result) if result is not None else None)


# --- Python -----------------------------------------------------------------


def py_signature(node: Node | None) -> dict[str, Any]:
    """`async def create(payload: OrderIn, *, db: Session = Depends(x)) -> OrderOut`
    -> payload/db with their types, returns `OrderOut`. `self`/`cls` and the
    bare `*` / `/` separators are left out; `*args` / `**kw` keep the prefix;
    defaults are not kept."""
    if node is None:
        return {}
    if node.type == "decorated_definition":
        node = node.child_by_field_name("definition")
        if node is None:
            return {}
    plist = node.child_by_field_name("parameters")
    if plist is None:
        return {}
    params: list[dict[str, Any]] = []
    for p in plist.named_children:
        if p.type in {"identifier", "list_splat_pattern", "dictionary_splat_pattern"}:
            params.append(_param(text(p)))
        elif p.type == "typed_parameter":
            name_n = next((c for c in p.named_children if c != p.child_by_field_name("type")), None)
            params.append(_param(text(name_n) if name_n is not None else "_", text(p.child_by_field_name("type"))))
        elif p.type in {"default_parameter", "typed_default_parameter"}:
            typ_n = p.child_by_field_name("type")
            params.append(_param(text(p.child_by_field_name("name")), text(typ_n) if typ_n is not None else None))
    if params and params[0]["name"] in {"self", "cls"}:
        params = params[1:]
    ret = node.child_by_field_name("return_type")
    return _attrs(params, text(ret) if ret is not None else None)


# --- TypeScript / JavaScript ------------------------------------------------


def _type_annotation(node: Node | None) -> str | None:
    """`: Promise<Order>` -> `Promise<Order>`."""
    if node is None:
        return None
    raw = text(node).strip()
    return raw[1:].strip() if raw.startswith(":") else raw


def ts_signature(node: Node | None) -> dict[str, Any]:
    """`(id: string, opts?: { expand?: boolean }): Promise<Order>` -> id/opts
    with their types (opts optional), returns `Promise<Order>`. A destructured
    parameter is named by its pattern (`{ page, size }`); a default value is
    not kept; plain JS gives the names only."""
    if node is None:
        return {}
    params: list[dict[str, Any]] = []
    single = node.child_by_field_name("parameter")  # `x => ...`
    plist = node.child_by_field_name("parameters")
    if single is not None:
        params.append(_param(text(single)))
    elif plist is not None:
        for p in plist.named_children:
            if p.type in {"required_parameter", "optional_parameter"}:
                pattern = p.child_by_field_name("pattern")
                name = text(pattern) if pattern is not None else "_"
                if name == "this":
                    continue  # TS `this` parameter: not an argument
                params.append(
                    _param(name, _type_annotation(p.child_by_field_name("type")), optional=p.type == "optional_parameter")
                )
            elif p.type == "assignment_pattern":
                left = p.child_by_field_name("left")
                params.append(_param(text(left) if left is not None else text(p)))
            elif p.type in {"identifier", "rest_pattern", "object_pattern", "array_pattern"}:
                params.append(_param(text(p)))
    else:
        return {}
    return _attrs(params, _type_annotation(node.child_by_field_name("return_type")))
