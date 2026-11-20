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


def unwrap_callee_expr(node: Node | None) -> Node | None:
    """Strip await / parens / TS instantiation so ``(await api.get<T>)`` is ``api.get``."""
    cur = node
    for _ in range(8):
        if cur is None:
            return None
        if cur.type in {
            "await_expression",
            "parenthesized_expression",
            "as_expression",
            "type_assertion",
            "non_null_expression",
            "instantiation_expression",
            "generic_type",
        }:
            nxt = (
                cur.child_by_field_name("function")
                or cur.child_by_field_name("expression")
                or cur.child_by_field_name("value")
                or (cur.named_children[0] if cur.named_children else None)
            )
            if nxt is None or nxt is cur:
                break
            cur = nxt
            continue
        break
    return cur


def call_callee_name(call: Node) -> str:
    """Last identifier of a call_expression function (Exec, POST, Run, ...)."""
    func = call.child_by_field_name("function")
    if func is None and call.named_children:
        func = call.named_children[0]
    func = unwrap_callee_expr(func) or func
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


def flatten_plus(node: Node, binary_types: set[str], paren_types: set[str] = frozenset({"parenthesized_expression"})) -> list[Node] | None:
    """Operands of a `+` chain (`"a" + b + ("c" + d)`), left to right,
    iteratively: a generated chain of thousands of terms must not hit the
    recursion limit. Returns None when ``node`` is not a `+` expression."""

    def is_plus(n: Node) -> bool:
        if n.type not in binary_types:
            return False
        ops = [c for c in n.children if not c.is_named]
        return bool(ops) and text(ops[0]) == "+"

    def unwrap(n: Node) -> Node:
        while n.type in paren_types and len(n.named_children) == 1 and is_plus(n.named_children[0]):
            n = n.named_children[0]
        return n

    if not is_plus(node):
        return None
    operands: list[Node] = []
    stack = [node]
    while stack:
        cur = unwrap(stack.pop())
        if is_plus(cur):
            left = cur.child_by_field_name("left") or (cur.named_children[0] if cur.named_children else None)
            right = cur.child_by_field_name("right") or (cur.named_children[-1] if cur.named_children else None)
            if right is not None:
                stack.append(right)
            if left is not None:
                stack.append(left)
        else:
            operands.append(cur)
    return operands
