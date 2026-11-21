"""API documentation for the HTML book: what a route receives and returns.

Two sources, best first:

1. **An OpenAPI / Swagger file** (`swag init` writes ``docs/swagger.json``;
   FastAPI serves ``openapi.json``). Swagger 2.0 and OpenAPI 3.x, JSON or
   YAML. Gives summaries, parameters, the request body and each response
   with their JSON fields.
2. **swag comments** above a Go handler (``@Summary``, ``@Description``,
   ``@Tags``, ``@Param``, ``@Success``, ``@Failure``, ``@Router``), already in
   the graph as the handler's ``why``. Gives the same, with type *names*
   only (fields come from a spec file).

Operations are matched to graph routes by method and path, with path
parameters (``{id}``, ``:id``, ``<id>``) treated alike and the spec's base
path (``basePath`` / ``servers[0].url``) prepended.

Schemas are kept compact for the page: ``{"t": type, "f": [[name, schema,
required, description]], "i": items schema, "r": ref name, "e": enum,
"x": example}``; named schemas live once in a table, referenced by ``r``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
SPEC_NAMES = (
    "docs/swagger.json", "docs/swagger.yaml", "docs/openapi.json", "docs/openapi.yaml",
    "openapi.json", "openapi.yaml", "openapi.yml", "swagger.json", "swagger.yaml",
    "api/openapi.json", "api/openapi.yaml", "api/swagger.json", "api/swagger.yaml",
)
_PARAM = re.compile(r"\{[^}/]+\}|:[A-Za-z_][\w-]*|<[^>/]+>|\[[^\]/]+\]")


class SpecError(ValueError):
    pass


def norm_path(path: str) -> str:
    """`/api/v1/projects/:id/` and `/api/v1/projects/{projectId}` -> `/api/v1/projects/{}`."""
    p = _PARAM.sub("{}", (path or "").split("?", 1)[0].strip())
    p = re.sub(r"/+", "/", "/" + p.strip("/"))
    return p if p != "" else "/"


def load_spec(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - report any parse error the same way
        raise SpecError(f"{path}: not a readable OpenAPI/Swagger file ({' '.join(str(exc).split())[:160]})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        raise SpecError(f"{path}: not an OpenAPI/Swagger file (no `paths`)")
    return data


SPEC_GLOBS = ("openapi/*", "docs/*", "api/*", "spec/*", "specs/*")
SPEC_EXT = (".json", ".yaml", ".yml")
MAX_SPEC_BYTES = 30_000_000


def looks_like_spec(path: Path) -> bool:
    """Cheap check before parsing a candidate found by a glob."""
    try:
        if path.suffix.lower() not in SPEC_EXT or path.stat().st_size > MAX_SPEC_BYTES:
            return False
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    return bool(re.search(r"""(^|["'\s{])(openapi|swagger)["']?\s*:""", head))


def _candidates(folder: Path) -> list[Path]:
    seen: list[Path] = []
    for name in SPEC_NAMES:
        cand = folder / name
        if cand.is_file() and cand not in seen:
            seen.append(cand)
    if seen:
        return seen[:1]  # one well-known spec per repo
    for pattern in SPEC_GLOBS:  # else every spec in openapi/, docs/… (one per area)
        for cand in sorted(folder.glob(pattern)):
            if cand.is_file() and cand not in seen and looks_like_spec(cand):
                seen.append(cand)
    return seen


def find_specs(root: Path, services: list[str], repo_dirs: dict[str, str] | None = None) -> list[tuple[str, Path]]:
    """Spec files next to the code: `<root>/<repo dir>/docs/swagger.json`…,
    else any spec in `<repo dir>/openapi/`, `docs/`…; the repo dir is the
    graph's `repo_roots` entry when it has one, else the service name. A
    single-repo graph looks in `<root>` itself (service "")."""
    out: list[tuple[str, Path]] = []
    for svc in services:
        dirs = [root / repo_dirs[svc]] if repo_dirs and repo_dirs.get(svc) not in (None, "", ".") else []
        dirs.append(root / svc)
        for folder in dirs:
            found = _candidates(folder) if folder.is_dir() else []
            if found:
                out += [(svc, f) for f in found]
                break
    if not out:
        out = [("", f) for f in _candidates(root)]
    return out


class SchemaTable:
    """Named schemas, converted once to the page's compact form."""

    def __init__(self) -> None:
        self.named: dict[str, Any] = {}
        self._raw: dict[str, dict[str, Any]] = {}

    def add_raw(self, prefix: str, defs: dict[str, Any]) -> None:
        for name, schema in (defs or {}).items():
            if isinstance(schema, dict):
                self._raw[f"{prefix}{name}"] = schema

    def ref(self, ref: str) -> str:
        name = ref.rsplit("/", 1)[-1]
        if name not in self.named and name in self._raw:
            self.named[name] = {}  # placeholder stops recursion
            self.named[name] = self.convert(self._raw[name])
        return name

    def convert(self, schema: Any, depth: int = 0) -> dict[str, Any]:
        if not isinstance(schema, dict) or depth > 12:
            return {"t": "any"}
        if "$ref" in schema:
            return {"r": self.ref(str(schema["$ref"]))}
        for key in ("allOf", "oneOf", "anyOf"):
            if isinstance(schema.get(key), list):
                parts = [self.convert(s, depth + 1) for s in schema[key]]
                if key == "allOf":
                    merged: dict[str, Any] = {"t": "object", "f": []}
                    for part in parts:
                        part = self.named.get(part["r"], part) if "r" in part else part
                        merged["f"] += part.get("f", [])
                    return merged
                return {"t": key, "o": parts}
        out: dict[str, Any] = {}
        typ = schema.get("type")
        if isinstance(typ, list):
            typ = next((t for t in typ if t != "null"), typ[0] if typ else None)
        props = schema.get("properties")
        if typ == "array" or "items" in schema:
            out["t"] = "array"
            out["i"] = self.convert(schema.get("items"), depth + 1)
        elif typ == "object" or isinstance(props, dict):
            out["t"] = "object"
            req = set(schema.get("required") or [])
            out["f"] = [
                [str(k), self.convert(v, depth + 1), 1 if k in req else 0, str((v or {}).get("description") or "")]
                for k, v in (props or {}).items()
            ]
            if isinstance(schema.get("additionalProperties"), dict):
                out["m"] = self.convert(schema["additionalProperties"], depth + 1)
        else:
            out["t"] = str(typ or "any") + (f" ({schema['format']})" if schema.get("format") else "")
        if isinstance(schema.get("enum"), list):
            out["e"] = [str(v) for v in schema["enum"][:20]]
        if "example" in schema and not isinstance(schema["example"], dict | list):
            out["x"] = str(schema["example"])
        return out


def _type_str(p: dict[str, Any]) -> str:
    s = p.get("schema") if isinstance(p.get("schema"), dict) else p
    t = s.get("type") or ("object" if "$ref" in s else "")
    if t == "array" and isinstance(s.get("items"), dict):
        it = s["items"]
        return f"{it.get('type') or it.get('$ref', '').rsplit('/', 1)[-1]}[]"
    return str(t) + (f" ({s['format']})" if s.get("format") else "")


def _param_desc(p: dict[str, Any]) -> str:
    s = p.get("schema") if isinstance(p.get("schema"), dict) else p
    bits = [str(p.get("description") or "")]
    items = s.get("items") if isinstance(s.get("items"), dict) else {}
    enum = s.get("enum") or items.get("enum")
    if enum:
        bits.append("one of: " + ", ".join(str(v) for v in enum[:20]))
    if s.get("default") is not None:
        bits.append(f"default: {s['default']}")
    return " · ".join(b for b in bits if b)


def operations_from_spec(spec: dict[str, Any], table: SchemaTable) -> list[dict[str, Any]]:
    """Spec -> [operation] with `m` (METHOD) and `p` (full path, normalized)."""
    v3 = str(spec.get("openapi", "")).startswith("3")
    if v3:
        table.add_raw("", (spec.get("components") or {}).get("schemas") or {})
        servers = spec.get("servers") or []
        url = str(servers[0].get("url") or "") if servers and isinstance(servers[0], dict) else ""
        base = re.sub(r"^[a-z]+://[^/]+", "", url)
    else:
        table.add_raw("", spec.get("definitions") or {})
        base = str(spec.get("basePath") or "")
    base = "" if base in ("/", "") else "/" + base.strip("/")
    ops = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params, body = [], None
            for p in list(shared) + list(op.get("parameters") or []):
                if not isinstance(p, dict):
                    continue
                if "$ref" in p:
                    continue
                if p.get("in") == "body":
                    body = table.convert(p.get("schema"))
                    continue
                params.append([str(p.get("name")), str(p.get("in")), _type_str(p), 1 if p.get("required") else 0,
                               _param_desc(p)])
            if v3 and isinstance(op.get("requestBody"), dict):
                content = op["requestBody"].get("content") or {}
                media = content.get("application/json") or next(iter(content.values()), {})
                body = table.convert((media or {}).get("schema"))
            responses = []
            for code, resp in (op.get("responses") or {}).items():
                if not isinstance(resp, dict):
                    continue
                schema = resp.get("schema")
                if v3:
                    content = resp.get("content") or {}
                    media = content.get("application/json") or next(iter(content.values()), {})
                    schema = (media or {}).get("schema")
                responses.append([str(code), str(resp.get("description") or ""),
                                  table.convert(schema) if isinstance(schema, dict) else None])
            ops.append(_clean({
                "m": method.upper(), "p": norm_path(base + "/" + str(path).lstrip("/")),
                "sum": str(op.get("summary") or ""), "desc": str(op.get("description") or ""),
                "tags": [str(t) for t in op.get("tags") or []], "params": params, "body": body,
                "resp": responses, "src": "spec",
            }))
    return ops


# ── swag comments ───────────────────────────────────────────────────────────

_TAG = re.compile(r"(?:^|\s)@(Summary|Description|Tags|Param|Success|Failure|Router|Accept|Produce|ID|Security|Header|Deprecated)\b", re.I)
_QUOTED = re.compile(r'"([^"]*)"')


def _swag_parts(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Comment -> (text before the first annotation, [(annotation, value)]).
    Works whether the comment kept its line breaks or was joined into one line."""
    marks = list(_TAG.finditer(text or ""))
    if not marks:
        return (text or "").strip(), []
    lead = text[: marks[0].start()].strip()
    parts = []
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        parts.append((m.group(1).lower(), " ".join(text[m.end() : end].split())))
    return lead, parts


def parse_swag(text: str) -> dict[str, Any] | None:
    """swag annotations in a comment -> operation (type names, no fields)."""
    lead, parts = _swag_parts(text)
    if not parts:
        return None
    op: dict[str, Any] = {"sum": "", "desc": "", "tags": [], "params": [], "body": None, "resp": [], "src": "swag"}
    for key, rest in parts:
        if key == "summary":
            op["sum"] = rest
        elif key == "description":
            op["desc"] = (op["desc"] + "\n" + rest).strip()
        elif key == "tags":
            op["tags"] = [t.strip() for t in rest.split(",") if t.strip()]
        elif key == "param":
            bits = rest.split(None, 4)
            if len(bits) >= 4:
                desc = _QUOTED.search(rest)
                name, where, typ, req = bits[:4]
                if where == "body":
                    op["body"] = {"r": typ, "n": typ}
                else:
                    op["params"].append([name, where, typ, 1 if req.lower() == "true" else 0,
                                         desc.group(1) if desc else ""])
        elif key in ("success", "failure"):
            bits = rest.split(None, 2)
            if bits:
                schema = None
                kind = bits[1].strip("{}") if len(bits) >= 2 else ""
                if len(bits) >= 3 and kind in ("object", "array"):
                    typ = bits[2].split()[0]
                    if typ.startswith("map[") or typ in ("interface{}", "object"):
                        schema = {"t": "object"}
                    else:
                        ref = {"r": typ, "n": typ}
                        schema = {"t": "array", "i": ref} if kind == "array" else ref
                elif len(bits) >= 3 and kind:
                    schema = {"t": bits[2].split()[0]}
                desc = _QUOTED.search(rest)
                op["resp"].append([bits[0], desc.group(1) if desc else "", schema])
        elif key == "router":
            rm = re.match(r"(\S+)\s*\[(\w+)\]", rest)
            if rm:
                op["router"] = [rm.group(2).upper(), rm.group(1)]
    if not (op["sum"] or op["params"] or op["resp"] or op.get("router")):
        return None
    if not op["sum"]:
        op["sum"] = plain_summary(lead)
    return _clean(op)


def comment_text(text: str) -> str:
    """A comment without its swag annotations (and without a leading
    `Name godoc` / `Name handles GET /x` line marker when that is all it says)."""
    lead, _parts = _swag_parts(text)
    lead = " ".join(lead.split())
    return "" if re.fullmatch(r"\w+( godoc| handles \w+ \S+)?", lead) else lead


def plain_summary(text: str) -> str:
    """First sentence of an ordinary comment (annotations left out)."""
    lead, _parts = _swag_parts(text)
    first = " ".join(lead.split())
    if not first:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", first)
    return (m.group(1) if m else first)[:200]


def _clean(op: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in op.items() if v not in ("", [], None) or k == "src"}


def resolve_names(op: dict[str, Any], table: SchemaTable) -> None:
    """Point swag type names at spec schemas when a spec has them."""
    def fix(s: Any) -> None:
        if isinstance(s, dict):
            if "r" in s and s["r"] not in table.named:
                name = s["r"]
                full = s.get("n", "")
                for cand in (full, full.replace("/", "."), name, name.rsplit(".", 1)[-1]):
                    if cand in table._raw:  # noqa: SLF001
                        s["r"] = table.ref(cand)
                        break
            for v in s.values():
                fix(v) if isinstance(v, dict) else None
    fix(op.get("body"))
    for r in op.get("resp", []):
        fix(r[2])
