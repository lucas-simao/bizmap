from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node


def text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def named_children(node: Node) -> list[Node]:
    return list(node.named_children)


def walk(node: Node) -> Iterator[Node]:
    """Iterative preorder over named descendants (punctuation is skipped)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        named = cur.named_children
        if named:
            stack.extend(reversed(named))


def first_child(node: Node, type_name: str) -> Node | None:
    for child in node.named_children:
        if child.type == type_name:
            return child
    return None


def children_of_type(node: Node, type_name: str) -> list[Node]:
    return [c for c in node.named_children if c.type == type_name]


def line_of(node: Node) -> int:
    return node.start_point[0] + 1


def call_callee_name(call: Node) -> str:
    """Last identifier of a call_expression function (Exec, POST, Run, ...)."""
    func = call.child_by_field_name("function")
    if func is None and call.named_children:
        func = call.named_children[0]
    if func is None:
        return ""
    if func.type == "identifier":
        return text(func)
    if func.type == "selector_expression":
        field = func.child_by_field_name("field")
        return text(field) if field else text(func).rsplit(".", 1)[-1]
    if func.type in {"attribute", "member_expression"}:
        # Python attribute / JS member_expression (app.get, supabase.rpc)
        return (
            text(func.named_children[-1])
            if func.named_children
            else text(func).rsplit(".", 1)[-1]
        )
    return text(func)


def call_args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    if args is None:
        for child in call.named_children:
            if child.type in {"argument_list", "arguments"}:
                args = child
                break
    if args is None:
        return []
    return list(args.named_children)


def selector_root_and_field(node: Node) -> tuple[str, str]:
    raw = text(node)
    if "." in raw:
        head, tail = raw.rsplit(".", 1)
        return head, tail
    return raw, raw
