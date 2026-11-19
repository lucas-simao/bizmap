"""Minimal JavaScript/TypeScript detector (Express routes, fetch, supabase.rpc)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Node, Parser

from bizmap.detect.tsutil import call_args, call_callee_name, line_of, text, walk
from bizmap.files import rel

try:
    import tree_sitter_javascript as tsjs

    _JS_LANGUAGE = Language(tsjs.language())
except Exception:  # pragma: no cover - optional dep
    _JS_LANGUAGE = None

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "all", "use"}


def _parser() -> Parser | None:
    if _JS_LANGUAGE is None:
        return None
    return Parser(_JS_LANGUAGE)


def _unquote(raw: str) -> str:
    s = raw.strip()
    if s.startswith(("f", "r")):
        s = s[1:]
    if len(s) >= 2 and s[0] in {"'", '"', "`"} and s[-1] == s[0]:
        return s[1:-1]
    return raw


def _string(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in {"string", "template_string", "string_fragment"}:
        if node.type == "template_string":
            # only fully static templates (no substitutions)
            if any(c.type == "template_substitution" for c in node.named_children):
                parts: list[str] = []
                for child in node.children:
                    if child.type == "string_fragment":
                        parts.append(text(child))
                    elif child.type == "template_substitution":
                        inner = child.named_children[0] if child.named_children else None
                        parts.append("{" + (text(inner) if inner is not None else "expr") + "}")
                joined = "".join(parts)
                return joined if joined else None
            return _unquote(text(node))
        return _unquote(text(node))
    if node.type == "identifier":
        return "{" + text(node) + "}"
    return None


@dataclass
class JsRoute:
    method: str
    path: str
    handler: str
    file: str
    line: int
    unresolved: bool = False


@dataclass
class JsHttpCall:
    method: str
    url: str
    file: str
    line: int
    symbol: str
    unresolved: bool = False


@dataclass
class JsRpcCall:
    name: str
    file: str
    line: int
    symbol: str


@dataclass
class JsAnalysis:
    routes: list[JsRoute] = field(default_factory=list)
    http_calls: list[JsHttpCall] = field(default_factory=list)
    rpc_calls: list[JsRpcCall] = field(default_factory=list)
    functions: dict[str, tuple[str, int]] = field(default_factory=dict)


class JsAnalyzer:
    def __init__(self, rules=None) -> None:
        self.rules = rules or []
        self._parser = _parser()

    def analyze(self, files: list[Path], root: Path) -> JsAnalysis:
        analysis = JsAnalysis()
        if self._parser is None:
            return analysis
        for path in files:
            source = path.read_bytes()
            tree = self._parser.parse(source).root_node
            rel_file = rel(path, root)
            module = rel_file.replace("/", ".").rsplit(".", 1)[0]
            for node in walk(tree):
                if node.type in {"function_declaration", "method_definition", "arrow_function", "function"}:
                    name_n = node.child_by_field_name("name")
                    name = text(name_n) if name_n else ""
                    if name:
                        analysis.functions[f"{module}.{name}"] = (rel_file, line_of(node))
                if node.type != "call_expression":
                    continue
                method = call_callee_name(node).lower()
                args = call_args(node)
                if method in HTTP_METHODS and args:
                    path_val = _string(args[0])
                    if path_val is None:
                        continue
                    unresolved = path_val.startswith("{") or "{" in path_val and "${" in text(args[0])
                    handler = "anonymous"
                    if len(args) >= 2:
                        handler = text(args[-1])[:80]
                    analysis.routes.append(
                        JsRoute(
                            method="USE" if method == "use" else method.upper(),
                            path=path_val if path_val.startswith("/") or path_val.startswith("{") else "/" + path_val,
                            handler=f"{module}.{handler}",
                            file=rel_file,
                            line=line_of(node),
                            unresolved=unresolved,
                        )
                    )
                if method == "fetch" and args:
                    url = _string(args[0])
                    if not url:
                        continue
                    http_method = "GET"
                    unresolved = "{" in url and not url.startswith("/{")
                    if len(args) > 1:
                        raw = text(args[1])
                        for m in HTTP_METHODS:
                            if f"method: '{m.upper()}'" in raw or f'method: "{m.upper()}"' in raw or f"method: '{m}'" in raw:
                                http_method = m.upper()
                    analysis.http_calls.append(
                        JsHttpCall(
                            method=http_method,
                            url=url,
                            file=rel_file,
                            line=line_of(node),
                            symbol=f"{module}.fetch",
                            unresolved=unresolved,
                        )
                    )
                if method == "rpc" and args:
                    name = _string(args[0])
                    if name:
                        analysis.rpc_calls.append(
                            JsRpcCall(name=name, file=rel_file, line=line_of(node), symbol=f"{module}.rpc")
                        )
        return analysis
