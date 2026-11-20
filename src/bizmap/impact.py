"""Change impact: map a unified diff onto the lineage graph.

A changed line inside a function marks that function as changed. For each
changed function the impact is:

- **entrypoints** that reach it (reverse ``calls`` / ``calls_http`` /
  ``triggers`` / ``enqueues``), across services when the graph is joint;
- **data** it or anything it calls reads or writes (tables, columns,
  collections, views).

Everything is read from the graph; nothing is re-scanned here.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from bizmap.graph import Graph, Node

MARKER = "<!-- bizmap-impact -->"

_CALL_EDGES = {"calls", "calls_http", "triggers", "enqueues"}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git_unquote(path: str) -> str:
    """Undo git's C-style quoting (`"caf\\303\\251.py"` → `café.py`)."""
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    body = path[1:-1]
    out = bytearray()
    i = 0
    simple = {"n": 10, "t": 9, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "r": 13, "v": 11}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            octal = body[i + 1:i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8))
                i += 4
                continue
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def parse_unified_diff(text: str) -> dict[str, set[int]]:
    """Return ``{new_path: changed new-side line numbers}``.

    Added lines count as changed. A change block (a run of ``-``/``+`` lines)
    with additions is fully described by them. A pure deletion marks the two
    new-side lines around the gap: ``{c, c+1}`` when the hunk's new side is
    empty (``@@ -14 +13,0 @@``: line 13 is *before* the gap), else the lines
    before and after it, kept inside the hunk. Deleted files are ignored.

    Hunk bodies are read by their header counts, so a removed ``-- comment``
    (``--- comment``) is not mistaken for a file header.
    """
    out: dict[str, set[int]] = {}
    path: str | None = None
    line = 0
    old_left = new_left = 0
    h_start = h_count = 0
    block_has_add = False
    block_del_at: int | None = None

    def flush() -> None:
        nonlocal block_has_add, block_del_at
        if path is not None and block_del_at is not None and not block_has_add:
            if h_count == 0:
                c = h_start
                marks = {c, c + 1} if c >= 1 else {1}
            else:
                lo, hi = h_start, h_start + h_count - 1
                marks = {min(max(p, lo), hi) for p in (block_del_at - 1, block_del_at)}
            out[path].update(m for m in marks if m >= 1)
        block_has_add = False
        block_del_at = None

    for raw in text.splitlines():
        if old_left > 0 or new_left > 0:
            tag = raw[:1] or " "
            if tag == "\\":  # "\ No newline at end of file"
                continue
            if tag == "+":
                if path is not None:
                    out[path].add(line)
                line += 1
                new_left -= 1
                block_has_add = True
                continue
            if tag == "-":
                old_left -= 1
                if block_del_at is None:
                    block_del_at = line
                continue
            flush()
            line += 1
            old_left -= 1
            new_left -= 1
            continue
        flush()
        if raw.startswith("+++ "):
            target = raw[4:].rstrip("\n")
            target = _git_unquote(target) if target.startswith('"') else target.split("\t", 1)[0].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith("b/") else target
                out.setdefault(path, set())
            continue
        m = _HUNK.match(raw)
        if m:
            om = re.match(r"^@@ -\d+(?:,(\d+))?", raw)
            old_left = int(om.group(1)) if om and om.group(1) is not None else 1
            h_start = int(m.group(1))
            h_count = int(m.group(2)) if m.group(2) is not None else 1
            new_left = h_count
            line = h_start
    flush()
    return {p: lines for p, lines in out.items() if lines}


@dataclass
class FunctionImpact:
    function: str
    file: str
    lines: list[int]
    entrypoints: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "file": self.file,
            "lines": self.lines,
            "entrypoints": self.entrypoints,
            "writes": self.writes,
            "reads": self.reads,
        }


@dataclass
class SchemaChange:
    """A changed line in a `.sql` file that names a known column."""

    column: str
    file: str
    line: int
    entrypoints: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    op: str = "changed"  # added | altered | dropped | renamed | changed | new table
    table: str = ""
    name: str = ""
    detail: str = ""  # new name for `renamed`, "N columns" for a new table

    @property
    def risky(self) -> bool:
        """Dropped / renamed while code still uses it."""
        return self.op in {"dropped", "renamed"} and bool(self.entrypoints or self.functions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "table": self.table,
            "name": self.name,
            "detail": self.detail,
            "column": self.column,
            "file": self.file,
            "line": self.line,
            "entrypoints": self.entrypoints,
            "functions": self.functions,
        }


@dataclass
class Impact:
    functions: list[FunctionImpact]
    unmapped_files: list[str]
    hint: str | None = None
    schema_changes: list[SchemaChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # scanned dir relative to the git root (for `file:line` links; not in JSON)
    prefix: str = "."

    @property
    def entrypoints(self) -> list[str]:
        return sorted({e for f in self.functions for e in f.entrypoints})

    @property
    def writes(self) -> list[str]:
        return sorted({t for f in self.functions for t in f.writes})

    @property
    def reads(self) -> list[str]:
        return sorted({t for f in self.functions for t in f.reads})

    def to_dict(self) -> dict[str, Any]:
        return {
            "functions": [f.to_dict() for f in self.functions],
            "entrypoints": self.entrypoints,
            "writes": self.writes,
            "reads": self.reads,
            "unmapped_files": self.unmapped_files,
            "hint": self.hint,
            "schema_changes": [c.to_dict() for c in self.schema_changes],
            "warnings": self.warnings,
        }


def _norm(path: str) -> str:
    p = PurePosixPath(path.replace("\\", "/")).as_posix()
    return "" if p == "." else p.removeprefix("./")


def _join(*parts: str) -> str:
    bits = [_norm(p) for p in parts if _norm(p)]
    return "/".join(bits)


def _repo_of(node: Node, graph: Graph) -> str:
    return str(node.attrs.get("repo") or graph.repo or graph.attrs.get("repo") or "")


def _function_file(node: Node, graph: Graph, prefix: str) -> str | None:
    ev = node.attrs.get("evidence") or {}
    file = ev.get("file")
    if not file:
        return None
    roots = graph.attrs.get("repo_roots") or {}
    root = roots.get(_repo_of(node, graph), ".") if roots else "."
    if graph.attrs.get("repo_roots_base") == "git":
        return _join(root, str(file))  # already relative to the git root
    return _join(prefix, root, str(file))


def _changed_functions(graph: Graph, changes: dict[str, set[int]], prefix: str) -> tuple[dict[str, list[int]], list[str]]:
    by_file: dict[str, list[tuple[int, int, str]]] = {}
    for nid, node in graph.nodes.items():
        if node.type != "function":
            continue
        start = node.attrs.get("start_line") or (node.attrs.get("evidence") or {}).get("line")
        end = node.attrs.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int) or end < start:
            continue
        path = _function_file(node, graph, prefix)
        if path:
            by_file.setdefault(path, []).append((start, end, nid))
    hits: dict[str, set[int]] = {}
    unmapped: list[str] = []
    for path, lines in sorted(changes.items()):
        spans = by_file.get(_norm(path))
        if not spans:
            unmapped.append(path)
            continue
        for ln in lines:
            inside = [s for s in spans if s[0] <= ln <= s[1]]
            if not inside:
                continue
            # innermost span: a nested function owns its own lines
            _start, _end, nid = min(inside, key=lambda s: (s[1] - s[0], -s[0]))
            hits.setdefault(nid, set()).add(ln)
    return {k: sorted(v) for k, v in hits.items()}, unmapped


def _edge_indexes(graph: Graph) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    out: dict[str, list[Any]] = {}
    inc: dict[str, list[Any]] = {}
    for edge in graph.edges.values():
        out.setdefault(edge.from_id, []).append(edge)
        inc.setdefault(edge.to_id, []).append(edge)
    return out, inc


def _entrypoints_reaching(graph: Graph, node_id: str, inc: dict[str, list[Any]] | None = None) -> set[str]:
    found: set[str] = set()
    seen: set[str] = set()
    q = deque([node_id])
    while q:
        cur = q.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        node = graph.get(cur)
        if node is not None and node.type == "entrypoint":
            found.add(cur)
        for edge in (inc.get(cur, []) if inc is not None else graph.incoming(cur)):
            if edge.type in _CALL_EDGES:
                q.append(edge.from_id)
    return found


def _data_reached(
    graph: Graph, node_id: str, out_idx: dict[str, list[Any]] | None = None
) -> tuple[set[str], set[str]]:
    writes: set[str] = set()
    reads: set[str] = set()
    seen: set[str] = set()
    q = deque([node_id])
    while q:
        cur = q.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        for edge in (out_idx.get(cur, []) if out_idx is not None else graph.outgoing(cur)):
            if edge.type == "writes":
                writes.add(edge.to_id)
            elif edge.type == "reads":
                reads.add(edge.to_id)
            elif edge.type in _CALL_EDGES:
                q.append(edge.to_id)
    return writes, reads


_SQL_TABLE_STMT = re.compile(
    r"(?is)\b(CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE(?:\s+IF\s+EXISTS)?(?:\s+ONLY)?)\s+([\w\".]+)"
)
_SQL_COLUMN_OP = re.compile(
    r'(?i)\b(ADD|ALTER|DROP|RENAME)\s+(?:COLUMN\s+)?(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"?([A-Za-z_]\w*)"?'
    r'(?:\s+TO\s+"?([A-Za-z_]\w*)"?)?'
)
_SQL_COLUMN_DEF = re.compile(r'^\s*,?\s*"?([A-Za-z_]\w*)"?\s+[A-Za-z]')
_SQL_NOT_COLUMN = {
    "constraint", "primary", "foreign", "unique", "check", "exclude", "like", "create",
    "alter", "table", "add", "drop", "references", "on", "insert", "update", "delete", "select",
    "to", "index", "default", "not", "null",
}
_SQL_OPS = {"add": "added", "alter": "altered", "drop": "dropped", "rename": "renamed"}


# removed `.sql` lines per file from the last `_sql_cosmetic_lines` call: a
# column definition is "added" when no removed line names it
_SQL_REMOVED: dict[str, list[str]] = {}


def _sql_cosmetic_lines(diff_text: str) -> dict[str, set[int]]:
    """New-side lines of `.sql` files whose change is only punctuation or
    spacing (`email TEXT NOT NULL` → `email TEXT NOT NULL,` when a column is
    appended after it)."""

    def norm(line: str) -> str:
        return re.sub(r"\s+", " ", line).strip().rstrip(",;").strip()

    out: dict[str, set[int]] = {}
    removed_by_file = _SQL_REMOVED
    removed_by_file.clear()
    path: str | None = None
    removed: list[str] = []
    added: list[tuple[int, str]] = []
    line = 0
    old_left = new_left = 0

    def flush() -> None:
        if path and path.lower().endswith(".sql") and removed and added:
            pool = [norm(r) for r in removed]
            for ln, txt in added:
                if norm(txt) in pool:
                    pool.remove(norm(txt))
                    out.setdefault(path, set()).add(ln)
        removed.clear()
        added.clear()

    for raw in diff_text.splitlines():
        if old_left > 0 or new_left > 0:
            tag = raw[:1] or " "
            if tag == "\\":
                continue
            if tag == "+":
                added.append((line, raw[1:]))
                line += 1
                new_left -= 1
            elif tag == "-":
                removed.append(raw[1:])
                if path:
                    removed_by_file.setdefault(path, []).append(raw[1:])
                old_left -= 1
            else:
                flush()
                line += 1
                old_left -= 1
                new_left -= 1
            continue
        flush()
        if raw.startswith("+++ "):
            target = raw[4:]
            target = _git_unquote(target) if target.startswith('"') else target.split("\t", 1)[0].strip()
            path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            continue
        m = _HUNK.match(raw)
        if m:
            om = re.match(r"^@@ -\d+(?:,(\d+))?", raw)
            old_left = int(om.group(1)) if om and om.group(1) is not None else 1
            new_left = int(m.group(2)) if m.group(2) is not None else 1
            line = int(m.group(1))
    flush()
    return out


def _column_users(graph: Graph, cids: list[str], inc: dict[str, list[Any]]) -> tuple[list[str], list[str]]:
    fns: set[str] = set()
    eps: set[str] = set()
    for cid in cids:
        for e in inc.get(cid, []):
            if e.type not in {"reads", "writes"}:
                continue
            src = graph.get(e.from_id)
            if src is None:
                continue
            if src.type == "function":
                fns.add(e.from_id)
            elif src.type == "entrypoint":
                eps.add(e.from_id)
    for fid in fns:
        eps |= _entrypoints_reaching(graph, fid, inc)
    return sorted(eps), sorted(fns)


def _schema_changes(
    graph: Graph,
    changes: dict[str, set[int]],
    unmapped: list[str],
    repo_root: Path,
    inc: dict[str, list[Any]],
    cosmetic: dict[str, set[int]] | None = None,
    missing: list[str] | None = None,
    outside: list[str] | None = None,
) -> list[SchemaChange]:
    """Changed lines of `.sql` files inside `CREATE TABLE` / `ALTER TABLE`:
    each column change with its operation (added / altered / dropped /
    renamed), or one row for a new table, with what reads or writes it."""
    by_name: dict[tuple[str, str], list[str]] = {}
    by_table: dict[str, list[str]] = {}
    for nid, node in graph.nodes.items():
        if node.type == "column":
            table = str(node.attrs.get("table") or "").lower().split(".")[-1]
            name = str(node.attrs.get("name") or "").lower()
            by_name.setdefault((table, name), []).append(nid)
            if not node.attrs.get("external"):
                by_table.setdefault(table, []).append(nid)
    out: list[SchemaChange] = []
    seen: set[tuple[str, str, str]] = set()
    new_tables: set[str] = set()
    root_resolved = repo_root.resolve()
    for path in unmapped:
        if not path.lower().endswith(".sql"):
            continue
        full = (repo_root / path).resolve()
        if not full.is_relative_to(root_resolved):
            # `../x.sql` or an absolute path in the diff: never read outside the root
            if outside is not None:
                outside.append(path)
            continue
        try:
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            if missing is not None:
                missing.append(path)
            continue
        skip = (cosmetic or {}).get(path, set())
        for ln in sorted(changes.get(path, ())):
            if not 1 <= ln <= len(lines) or ln in skip:
                continue
            start = ln - 1
            while start > 0 and ";" not in lines[start - 1]:
                start -= 1
            stmt = "\n".join(lines[start:ln])
            found = list(_SQL_TABLE_STMT.finditer(stmt))
            if not found:
                continue
            head = found[-1]
            table = head.group(2).replace('"', "").split(".")[-1].lower()
            is_create = head.group(1).upper().startswith("CREATE")
            head_line = start + stmt.count("\n", 0, head.start()) + 1
            if is_create and (head_line == ln or table in new_tables):
                # the `CREATE TABLE x (` line itself changed: a new table
                if table not in new_tables:
                    new_tables.add(table)
                    cols = by_table.get(table, [])
                    eps, fns = _column_users(graph, cols, inc)
                    out.append(
                        SchemaChange(
                            column=f"table:{table}", file=path, line=ln, entrypoints=eps, functions=fns,
                            op="new table", table=table, name="", detail=f"{len(cols)} columns",
                        )
                    )
                continue
            text = lines[ln - 1]
            ops: list[tuple[str, str, str | None]] = []
            for m in _SQL_COLUMN_OP.finditer(text):
                if m.group(2).lower() in _SQL_NOT_COLUMN:
                    continue
                ops.append((_SQL_OPS[m.group(1).lower()], m.group(2), m.group(3)))
            if not ops and is_create:
                cm = _SQL_COLUMN_DEF.match(text)
                if cm is not None and cm.group(1).lower() not in _SQL_NOT_COLUMN:
                    col_re = re.compile(rf'(?i)(?<![\w"]){re.escape(cm.group(1))}(?![\w"])')
                    was_there = any(col_re.search(r) for r in _SQL_REMOVED.get(path, []))
                    ops.append(("changed" if was_there else "added", cm.group(1), None))
            for op, col, new_name in ops:
                key = (table, col.lower(), op)
                if key in seen:
                    continue
                seen.add(key)
                cids = by_name.get((table, col.lower()), [])
                if not cids and op not in {"dropped", "renamed"}:
                    continue
                eps, fns = _column_users(graph, cids, inc)
                out.append(
                    SchemaChange(
                        column=cids[0] if cids else f"column:{table}.{col.lower()}",
                        file=path, line=ln, entrypoints=eps, functions=fns,
                        op=op, table=table, name=col.lower(), detail=(new_name or "").lower(),
                    )
                )
    return out


def compute_impact(
    graph: Graph, diff_text: str, *, prefix: str = ".", repo_root: Path | None = None
) -> Impact:
    """Impact of ``diff_text`` (paths relative to the git root) on ``graph``.

    ``prefix`` is the scanned directory relative to the git root (the path
    given to ``bizmap scan``).
    """
    changes = parse_unified_diff(diff_text)
    hits, unmapped = _changed_functions(graph, changes, prefix)
    out: list[FunctionImpact] = []
    out_idx, in_idx = _edge_indexes(graph)
    for nid, lines in sorted(hits.items()):
        node = graph.nodes[nid]
        writes, reads = _data_reached(graph, nid, out_idx)
        reads -= writes
        out.append(
            FunctionImpact(
                function=nid,
                file=_function_file(node, graph, prefix) or "",
                lines=lines,
                entrypoints=sorted(_entrypoints_reaching(graph, nid, in_idx)),
                writes=sorted(writes),
                reads=sorted(reads),
            )
        )
    hint = None
    code_exts = {".go", ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
    known = {
        p for n in graph.nodes.values() if n.type == "function" for p in [_function_file(n, graph, prefix)] if p
    }
    code_changes = [p for p in changes if PurePosixPath(p).suffix.lower() in code_exts]
    # only when code changed and none of it is a file the graph knows: the
    # roots/prefix are probably wrong (docs/CI-only diffs need no hint)
    if code_changes and not any(_norm(p) in known for p in changes):
        roots = sorted(set((graph.attrs.get("repo_roots") or {}).values())) or ["."]
        hint = (
            "none of the changed paths is under a scanned root "
            f"({', '.join(roots[:5])}{', …' if len(roots) > 5 else ''}"
            + (f"; prefix {prefix!r}" if graph.attrs.get("repo_roots_base") != "git" else "")
            + "). "
            + ("Pass --prefix <dir given to bizmap scan>." if graph.attrs.get("repo_roots_base") != "git" else "")
        ).strip()
    schema: list[SchemaChange] = []
    warnings: list[str] = []
    sql_changed = [p for p in unmapped if p.lower().endswith(".sql")]
    if repo_root is not None:
        missing: list[str] = []
        outside: list[str] = []
        schema = _schema_changes(
            graph, changes, unmapped, repo_root, in_idx, _sql_cosmetic_lines(diff_text), missing, outside
        )
        if missing:
            shown = ", ".join(missing[:3]) + (f", +{len(missing) - 3}" if len(missing) > 3 else "")
            warnings.append(
                f"{len(missing)} changed .sql file(s) not found under --repo-root {str(repo_root)!r}: {shown}"
            )
        if outside:
            shown = ", ".join(outside[:3]) + (f", +{len(outside) - 3}" if len(outside) > 3 else "")
            warnings.append(f"{len(outside)} changed .sql path(s) outside --repo-root ignored: {shown}")
    elif sql_changed:
        warnings.append(
            f"{len(sql_changed)} changed .sql file(s) not read for schema changes "
            "(start `bizmap mcp` with --repo-root, or run `bizmap impact --repo-root`)"
        )
    return Impact(
        functions=out, unmapped_files=unmapped, hint=hint, schema_changes=schema, warnings=warnings,
        prefix=prefix,
    )


# ── rendering ────────────────────────────────────────────────────────────────
#
# The PR comment: a one-line summary with a risk badge, then collapsible
# sections (diagram, entrypoints, data, schema, triggers, cross-service calls,
# changed functions, what is unresolved). Everything is read from the graph and
# the Impact; nothing here changes what the impact *is*.

RISK_LEVELS = ("none", "low", "medium", "high")
RISK_MARKER = "<!-- bizmap-risk: {} -->"
_RISK_TITLE = {
    "high": "🔴 High risk",
    "medium": "🟠 Medium risk",
    "low": "🟡 Low risk",
    "none": "🟢 No data impact",
}
_HTTP_KINDS = {"http", "firebase_http", "firebase_call", "cloud_task"}
_KIND_WORD = {
    "http": "route",
    "firebase_http": "route",
    "firebase_call": "callable",
    "cloud_task": "task",
    "scheduled_job": "job",
    "worker": "worker",
    "celery_task": "task",
    "pubsub": "subscriber",
    "firestore": "listener",
}
_LOW_CONF = {"heuristic", "unknown"}
DOCS_URL = "https://github.com/lucas-simao/bizmap/blob/main/docs/action.md"


def _is_prefixed(node_id: str) -> bool:
    return "/" in node_id.split(":", 1)[0]


def _repo_name(graph: Graph, node_id: str) -> str:
    node = graph.get(node_id)
    if node is not None and node.attrs.get("repo"):
        return str(node.attrs["repo"])
    if _is_prefixed(node_id):
        return node_id.split("/", 1)[0]
    return str(graph.repo or graph.attrs.get("repo") or "")


def _multi_repo(graph: Graph) -> bool:
    return len(graph.attrs.get("repo_roots") or {}) > 1


def _label(graph: Graph, node_id: str) -> str:
    node = graph.get(node_id)
    leaf = node_id.split("/", 1)[1] if _is_prefixed(node_id) else node_id
    if node is None:
        for kind in ("column:", "table:", "collection:", "view:", "function:", "db_trigger:"):
            if leaf.startswith(kind):
                return leaf.removeprefix(kind)
        return leaf
    a = node.attrs
    repo = a.get("repo")
    pre = f"{repo}: " if _multi_repo(graph) and repo else ""
    if node.type == "entrypoint":
        kind = a.get("kind")
        if kind in _HTTP_KINDS and (a.get("method") or a.get("path")):
            return f"{pre}{a.get('method') or ''} {a.get('path') or ''}".strip()
        word = {"scheduled_job": "job", "celery_task": "celery", "pubsub": "pubsub", "worker": "worker"}.get(
            str(kind), str(kind or "")
        )
        name = a.get("symbol") or a.get("handler") or a.get("topic") or a.get("collection") or a.get("path") or ""
        return f"{pre}{word} {name}".strip()
    if node.type == "function":
        return f"{pre}{a.get('name') or leaf.removeprefix('function:')}"
    if node.type == "db_trigger":
        return str(a.get("name") or leaf.removeprefix("db_trigger:"))
    for kind in ("column:", "table:", "collection:", "view:"):
        if leaf.startswith(kind):
            return f"{pre}{leaf.removeprefix(kind)}"
    return f"{pre}{leaf}"


def _short_fn(graph: Graph, node_id: str, *, repo_prefix: bool = True) -> str:
    """`src.orders.markPaid` → `orders.markPaid` (diagram and compact cells)."""
    node = graph.get(node_id)
    name = str((node.attrs.get("name") if node else None) or node_id.split("function:", 1)[-1])
    head, sep, tail = name.partition(" ")  # inline handler: `src.server.POST /x`
    bits = head.split(".")
    short = ".".join(bits[-2:]) if len(bits) > 2 else head
    if sep:
        short = f"{bits[-1] if len(bits) > 1 else head} {tail}"
    repo = node.attrs.get("repo") if node else None
    return f"{repo}: {short}" if repo_prefix and _multi_repo(graph) and repo else short


def _known(ids: list[str]) -> list[str]:
    """Drop `table:unknown` (a table name built at runtime): it is a note, not data."""
    return [i for i in ids if i.rpartition("/")[2] != "table:unknown"]


def _tables_only(ids: list[str]) -> list[str]:
    """Collapse columns into their table for the diagram."""
    out: set[str] = set()
    for nid in _known(ids):
        out.add(_table_of(nid))
    return sorted(out)


# receivers whose methods are never in the scanned code (`str.replace`)
_BUILTIN_RECEIVERS = {
    "str", "bytes", "int", "float", "bool", "list", "dict", "set", "tuple", "frozenset", "object",
    "Path", "PurePath", "PurePosixPath", "None", "string", "error", "Array", "Object", "String",
    "Map", "Set", "Promise", "JSON", "Math", "console", "Number", "Date", "RegExp",
}


def _interesting_call(name: str) -> bool:
    head = name.split(".", 1)[0].strip()
    if "|" in head or "[" in head:  # `str | None.rstrip`, `list[int].append`
        return False
    return head not in _BUILTIN_RECEIVERS


def _table_of(nid: str) -> str:
    head, sep, leaf = nid.rpartition("/")
    if leaf.startswith("column:"):
        return head + sep + "table:" + leaf.removeprefix("column:").split(".", 1)[0]
    return nid


def _column_of(nid: str) -> str | None:
    leaf = nid.rpartition("/")[2]
    if leaf.startswith("column:"):
        return leaf.removeprefix("column:").partition(".")[2] or None
    return None


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


def _code(text: str) -> str:
    """Inline code that survives backticks and pipes inside a table cell."""
    text = text.replace("|", "\\|")
    if "`" in text:
        return f"`` {text} ``"
    return f"`{text}`"


def _names(items: list[str], n: int = 4) -> str:
    shown = ", ".join(_code(i) for i in items[:n])
    return shown + (f" +{len(items) - n}" if len(items) > n else "")


class _Links:
    """`file:line` text, linked to the PR commit when a blob URL is given."""

    def __init__(self, blob_url: str | None) -> None:
        self.blob = blob_url.rstrip("/") if blob_url else None

    def where(self, path: str, line: int | None, end: int | None = None) -> str:
        if not path:
            return "—"
        text = f"{path}:{line}" if line else path
        if not self.blob:
            return _code(text)
        from urllib.parse import quote

        anchor = ""
        if line:
            anchor = f"#L{line}" + (f"-L{end}" if end and end > line else "")
        shown = text.replace("|", "\\|")
        return f"[{shown}]({self.blob}/{quote(path, safe='/')}{anchor})"

    @property
    def commit(self) -> str | None:
        if not self.blob or "/blob/" not in self.blob:
            return None
        base, _sep, sha = self.blob.rpartition("/blob/")
        return f"[`{sha[:7]}`]({base}/commit/{sha})" if sha else None


def _node_where(graph: Graph, node_id: str, prefix: str) -> tuple[str, int | None, int | None]:
    node = graph.get(node_id)
    if node is None:
        return "", None, None
    path = _function_file(node, graph, prefix) or ""
    ev = node.attrs.get("evidence") or {}
    start = node.attrs.get("start_line") or ev.get("line")
    end = node.attrs.get("end_line")
    return path, start if isinstance(start, int) else None, end if isinstance(end, int) else None


@dataclass
class _TableUse:
    written: set[str] = field(default_factory=set)  # column names
    read: set[str] = field(default_factory=set)
    write: bool = False
    read_any: bool = False
    functions: set[str] = field(default_factory=set)


@dataclass
class _Facts:
    """What the comment shows, gathered once from the graph."""

    tables: dict[str, _TableUse]
    services: dict[str, list[str]]  # table → services (repos) that read/write it
    ep_fns: dict[str, set[str]]  # entrypoint → changed functions it reaches
    direct: set[str]  # entrypoints whose own handler changed
    fired: dict[str, set[str]]  # db_trigger id → tables (fired by a write in the reach)
    table_triggers: dict[str, list[str]]  # written table → db_trigger ids on it
    http: list[Any]  # calls_http edges on the way to or from the change
    notes: list[str]
    low_conf: int
    columns: int
    risk: str
    reasons: list[tuple[str, str]]


def _gather(graph: Graph, impact: Impact, shared_services: int) -> _Facts:
    out_idx, in_idx = _edge_indexes(graph)
    changed = {f.function for f in impact.functions}
    tables: dict[str, _TableUse] = {}
    ep_fns: dict[str, set[str]] = {}
    for fi in impact.functions:
        for ep in fi.entrypoints:
            ep_fns.setdefault(ep, set()).add(fi.function)
        for nid, is_write in [(w, True) for w in _known(fi.writes)] + [(r, False) for r in _known(fi.reads)]:
            use = tables.setdefault(_table_of(nid), _TableUse())
            col = _column_of(nid)
            if is_write:
                use.write = True
                if col:
                    use.written.add(col)
            else:
                use.read_any = True
                if col:
                    use.read.add(col)
            use.functions.add(fi.function)
    direct = {
        e.from_id
        for fid in changed
        for e in in_idx.get(fid, [])
        if e.type == "triggers" and (graph.get(e.from_id) or Node("", "")).type == "entrypoint"
    }

    # downstream from every changed function (one multi-source walk)
    seen: set[str] = set()
    q = deque(sorted(changed))
    http: dict[str, Any] = {}
    fired: dict[str, set[str]] = {}
    low: set[str] = set()
    dyn: dict[str, set[str]] = {}
    reached_fns: list[str] = []
    while q:
        cur = q.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        node = graph.get(cur)
        if node is not None and node.type == "function":
            reached_fns.append(cur)
        for e in out_idx.get(cur, []):
            if e.type in {"reads", "writes"}:
                via = str(e.attrs.get("via") or "")
                if e.type == "writes" and via.startswith("db_trigger:"):
                    fired.setdefault(via, set()).add(_table_of(e.to_id))  # trigger → tables it writes
                if str(e.confidence) in _LOW_CONF and not e.attrs.get("derived"):
                    low.add(e.id)
                if e.to_id.rpartition("/")[2] in {"table:unknown"}:
                    dyn.setdefault(cur, set()).update(str(c) for c in (graph.get(e.to_id) or Node("", "")).attrs.get("candidates") or [])
            elif e.type in _CALL_EDGES:
                if e.type == "calls_http":
                    http[e.id] = e
                    if str(e.confidence) in _LOW_CONF:
                        low.add(e.id)
                q.append(e.to_id)
    # upstream: services that call into the change over HTTP
    up_seen: set[str] = set()
    q = deque(sorted(changed))
    while q:
        cur = q.popleft()
        if cur in up_seen:
            continue
        up_seen.add(cur)
        for e in in_idx.get(cur, []):
            if e.type in _CALL_EDGES:
                if e.type == "calls_http":
                    http[e.id] = e
                q.append(e.from_id)

    # triggers sitting on a written table (even when no via-edge recorded the firing)
    written_names = {t.rpartition("/")[2].removeprefix("table:").lower(): t for t, u in tables.items() if u.write}
    table_triggers: dict[str, list[str]] = {}
    for nid, node in graph.nodes.items():
        if node.type != "db_trigger":
            continue
        tname = str(node.attrs.get("table") or "").lower().split(".")[-1]
        if tname in written_names:
            table_triggers.setdefault(written_names[tname], []).append(nid)
    for t in table_triggers:
        table_triggers[t].sort()

    services: dict[str, list[str]] = {}
    if _multi_repo(graph) and tables:
        users: dict[str, set[str]] = {t: set() for t in tables}
        for e in graph.edges.values():
            if e.type not in {"reads", "writes"}:
                continue
            t = _table_of(e.to_id)
            if t in users:
                src = graph.get(e.from_id)
                if src is not None and src.type in {"function", "entrypoint"}:
                    r = _repo_name(graph, e.from_id)
                    if r:
                        users[t].add(r)
        services = {t: sorted(r) for t, r in users.items()}

    # what static analysis could not settle, near the change
    notes: list[str] = []
    call_notes: list[str] = []
    for fid in reached_fns:
        node = graph.nodes[fid]
        for u in node.attrs.get("unresolved_http") or []:
            if not isinstance(u, dict):
                continue
            call = f"{u.get('method') or ''} {u.get('url') or '?'}".strip()
            reason = str(u.get("reason") or "unmatched").replace("_", " ")
            notes.append(f"{_code(_short_fn(graph, fid))} calls {_code(call)} — {reason}")
        calls = [str(c) for c in node.attrs.get("unresolved_calls") or [] if _interesting_call(str(c))]
        if calls:
            call_notes.append(
                f"{_code(_short_fn(graph, fid))}: {_plural(len(calls), 'call')} to {_names(calls, 3)} "
                "not resolved (no scanned implementation)"
            )
    for fid in sorted(dyn):
        cands = sorted(dyn[fid])
        tail = f" (candidates: {', '.join(cands[:4])})" if cands else ""
        notes.append(f"{_code(_short_fn(graph, fid))} builds a table name at runtime{tail}")
    for ep in sorted(ep_fns):
        node = graph.get(ep)
        if node is None:
            continue
        if node.attrs.get("candidates") or str(node.attrs.get("confidence") or "") == "unknown":
            notes.append(f"{_code(_label(graph, ep))}: handler is ambiguous (several candidates)")
        elif node.attrs.get("unresolved"):
            notes.append(f"{_code(_label(graph, ep))}: route path not fully resolved")
    notes += call_notes

    columns = len({w for f in impact.functions for w in _known(f.writes + f.reads) if _column_of(w)})
    facts = _Facts(
        tables=tables, services=services, ep_fns=ep_fns, direct=direct, fired=fired,
        table_triggers=table_triggers, http=[http[k] for k in sorted(http)], notes=notes,
        low_conf=len(low), columns=columns, risk="none", reasons=[],
    )
    facts.risk, facts.reasons = _risk(graph, impact, facts, shared_services)
    return facts


def _risk(graph: Graph, impact: Impact, facts: _Facts, shared_services: int) -> tuple[str, list[tuple[str, str]]]:
    """The badge: the highest level among plain, checkable reasons."""
    reasons: list[tuple[str, str]] = []
    for ch in impact.schema_changes:
        if ch.risky:
            what = f"{ch.table}.{ch.name}"
            users = len(ch.entrypoints) or len(ch.functions)
            unit = "entrypoint" if ch.entrypoints else "function"
            verb = "uses" if users == 1 else "use"
            reasons.append(("high", f"{ch.op} column {_code(what)} while {users} {_plural(users, unit)} still {verb} it"))
    written = sorted(t for t, u in facts.tables.items() if u.write)
    for t in written:
        svc = facts.services.get(t, [])
        if len(svc) >= max(shared_services, 2):
            reasons.append(
                ("high", f"writes {_code(_label(graph, t))}, used by {len(svc)} services ({', '.join(svc)})")
            )
    for t in written:
        trig = facts.table_triggers.get(t, [])
        if trig:
            names = [_label(graph, x) for x in trig]
            reasons.append(
                ("medium", f"writes {_code(_label(graph, t))}, which has {_plural(len(names), 'trigger')} {_names(names, 3)}")
            )
    http_eps = sorted(
        (e for e in facts.direct if (graph.get(e) or Node("", "")).attrs.get("kind") in _HTTP_KINDS),
        key=lambda e: _label(graph, e),
    )
    if http_eps:
        names = [_label(graph, e) for e in http_eps]
        reasons.append(("medium", f"changes the handler of {_names(names, 3)}"))
    crossing = sorted(
        {
            f"{a} → {b}"
            for e in facts.http
            for a, b in [(_repo_name(graph, e.from_id), _repo_name(graph, e.to_id))]
            if a and b and a != b
        }
    )
    if crossing:
        reasons.append(("medium", f"crosses services ({', '.join(crossing[:4])})"))
    n_eps = len(facts.ep_fns)
    if written:
        if n_eps:
            reasons.append(
                ("medium", f"writes {len(written)} {_plural(len(written), 'table')} reached from {n_eps} {_plural(n_eps, 'entrypoint')}")
            )
        else:
            reasons.append(("low", f"writes {len(written)} {_plural(len(written), 'table')}, not reached from any entrypoint"))
    elif facts.tables:
        n = len(facts.tables)
        reasons.append(("low", f"only reads data ({n} {_plural(n, 'table')})"))
    elif n_eps:
        reasons.append(("low", f"reaches {n_eps} {_plural(n_eps, 'entrypoint')}, no table"))
    other_schema = [c for c in impact.schema_changes if not c.risky]
    if other_schema:
        ops = ", ".join(sorted({c.op for c in other_schema}))
        other = "other " if len(other_schema) < len(impact.schema_changes) else ""
        reasons.append(("low", f"{len(other_schema)} {other}schema {_plural(len(other_schema), 'change')} ({ops})"))
    rank = {lvl: i for i, lvl in enumerate(RISK_LEVELS)}
    reasons.sort(key=lambda r: -rank[r[0]])
    level = reasons[0][0] if reasons else "none"
    return level, reasons


def _mid(index: int) -> str:
    return f"n{index}"


def _mm(text: str) -> str:
    """A Mermaid label: quotes and angle brackets as entities."""
    return text.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def render_mermaid(
    graph: Graph,
    impact: Impact,
    *,
    limit: int = 40,
    risky: set[str] | frozenset[str] = frozenset(),
    triggers: dict[str, list[str]] | None = None,
    per_function: int = 4,
) -> str:
    """entrypoint → changed function → table, capped at ``limit`` nodes.

    At most ``per_function`` entrypoints per function (the rest folded into
    one "+N" node); columns are collapsed into their table; triggers hang off
    the table they fire on. Multi-repo graphs get one subgraph per service.
    """
    ids: dict[str, str] = {}
    nodes: list[tuple[str, str, str]] = []  # (mid, decl, repo or "")
    edges: list[str] = []
    multi = _multi_repo(graph)

    def node(key: str, decl: str, repo: str = "") -> str:
        if key not in ids:
            ids[key] = _mid(len(ids) + 1)
            nodes.append((ids[key], decl.replace("{id}", ids[key]), repo if multi else ""))
        return ids[key]

    dropped = 0
    for fi in impact.functions:
        eps = sorted(fi.entrypoints, key=lambda e: _label(graph, e))
        tables_w = _tables_only(fi.writes)
        tables_r = [t for t in _tables_only(fi.reads) if t not in tables_w]
        new = 1 + sum(1 for e in eps[:per_function] if e not in ids) + (1 if len(eps) > per_function else 0)
        new += sum(1 for t in tables_w + tables_r if t not in ids)
        if ids and len(ids) + new > limit:
            dropped += 1
            continue
        fn = node(fi.function, f'  {{id}}["{_mm(_short_fn(graph, fi.function, repo_prefix=False))}"]:::changed', _repo_name(graph, fi.function))
        for ep in eps[:per_function]:
            label = _label(graph, ep)
            if multi and ": " in label:
                label = label.split(": ", 1)[1]
            mid = node(ep, f'  {{id}}(["{_mm(label)}"])', _repo_name(graph, ep))
            edges.append(f"  {mid} --> {fn}")
        if len(eps) > per_function:
            more = node(f"more:{fi.function}", f'  {{id}}(["+{len(eps) - per_function} more"]):::more', _repo_name(graph, fi.function))
            edges.append(f"  {more} --> {fn}")
        for t, kind in [(t, "w") for t in tables_w] + [(t, "r") for t in tables_r]:
            cls = ":::risky" if t in risky else ""
            mid = node(t, f'  {{id}}[("{_mm(_label(graph, t))}")]{cls}')
            edges.append(f"  {fn} -- writes --> {mid}" if kind == "w" else f"  {fn} -. reads .-> {mid}")
    for t, trig in sorted((triggers or {}).items()):
        if t not in ids:
            continue
        for tr in trig[:3]:
            mid = node(tr, f'  {{id}}{{{{"⚡ {_mm(_label(graph, tr))}"}}}}:::trigger')
            edges.append(f"  {ids[t]} -. fires .-> {mid}")
    lines = ["flowchart LR"]
    if multi:
        repos = sorted({r for _m, _d, r in nodes if r})
        for i, repo in enumerate(repos, 1):
            lines.append(f'  subgraph s{i}["{_mm(repo)}"]')
            lines.extend("  " + d for _m, d, r in nodes if r == repo)
            lines.append("  end")
        lines.extend(d for _m, d, r in nodes if not r)
    else:
        lines.extend(d for _m, d, _r in nodes)
    lines.extend(dict.fromkeys(edges))
    if dropped:
        lines.append(f"  %% {dropped} more changed function(s) not drawn")
    lines.append("  classDef changed fill:#fff3bf,stroke:#f08c00,color:#1f2328")
    lines.append("  classDef risky fill:#ffe3e3,stroke:#e03131,color:#1f2328")
    lines.append("  classDef trigger fill:#f3f0ff,stroke:#7048e8,color:#1f2328")
    lines.append("  classDef more fill:#f1f3f5,stroke:#adb5bd,color:#495057,stroke-dasharray:3 3")
    return "\n".join(lines) + "\n"


def _fold_pass_through(graph: Graph, functions: list[FunctionImpact]) -> dict[str, str]:
    """caller → callee for a changed function that only hands off to another
    changed function with the very same impact (an inline route handler that
    calls `cancelOrder`): one row instead of two."""
    by_id = {f.function: f for f in functions}
    calls: dict[str, set[str]] = {}
    for edge in graph.edges.values():
        if edge.type == "calls" and edge.from_id in by_id and edge.to_id in by_id:
            calls.setdefault(edge.from_id, set()).add(edge.to_id)
    folded: dict[str, str] = {}

    def key(f: FunctionImpact) -> tuple:
        return (tuple(f.entrypoints), tuple(f.writes), tuple(f.reads))

    def runs_sql(fid: str) -> bool:
        """A reads/writes edge whose evidence is inside the function's own
        body (rolled-up copies of a callee's access don't count)."""
        node = graph.nodes.get(fid)
        if node is None:
            return False
        nev = node.attrs.get("evidence") or {}
        start = node.attrs.get("start_line") or nev.get("line")
        end = node.attrs.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int):
            return False
        for e in graph.edges.values():
            if e.from_id != fid or e.type not in {"reads", "writes"} or e.evidence is None:
                continue
            if e.evidence.file == nev.get("file") and start <= e.evidence.line <= end:
                return True
        return False

    own_data = {fid for fid in by_id if runs_sql(fid)}
    for caller in sorted(calls):
        if caller in own_data:
            continue  # it runs SQL itself: worth its own row even with the same reach
        same = sorted(c for c in calls[caller] if key(by_id[c]) == key(by_id[caller]) and c not in folded)
        if same and caller not in folded.values():
            folded[caller] = same[0]
    return folded


def _schema_section(graph: Graph, impact: Impact, links: _Links | None = None, cap: int = 30) -> list[str]:
    if not impact.schema_changes:
        return []
    links = links or _Links(None)
    out = ["| Change | Where | Used by entrypoints | Functions |", "|---|---|---|---|"]
    ordered = sorted(impact.schema_changes, key=lambda c: (not c.risky, c.file, c.line))
    for ch in ordered[:cap]:
        if ch.op == "new table":
            label = f"new table `{ch.table}` ({ch.detail})"
        else:
            op = f"renamed → `{ch.detail}`" if ch.op == "renamed" and ch.detail else ch.op
            if ch.op in {"dropped", "renamed"}:
                op = f"**{op}**"
            label = f"`{ch.table}.{ch.name}` {op}"
        if ch.risky:
            label = "⚠️ " + label + " — still used"
        empty = "— (not used yet)" if ch.op in {"added", "new table", "changed"} else "—"
        eps = "<br>".join(_code(_label(graph, e)) for e in ch.entrypoints[:6]) or empty
        if len(ch.entrypoints) > 6:
            eps += f"<br>+{len(ch.entrypoints) - 6}"
        fns = "<br>".join(_code(_short_fn(graph, f)) for f in ch.functions[:4]) or "—"
        if len(ch.functions) > 4:
            fns += f"<br>+{len(ch.functions) - 4}"
        out.append(f"| {label} | {links.where(ch.file, ch.line)} | {eps} | {fns} |")
    if len(ordered) > cap:
        out.append(f"\n+{len(ordered) - cap} more schema change(s).")
    return out


def _other_unmapped(impact: Impact) -> str:
    """Changed files no function or schema line maps to, named (not just counted)."""
    covered = {c.file for c in impact.schema_changes}
    rest = [p for p in impact.unmapped_files if p not in covered]
    if not rest:
        return ""
    shown = ", ".join(f"`{p}`" for p in rest[:5]) + (f", +{len(rest) - 5}" if len(rest) > 5 else "")
    return f"{len(rest)} changed {_plural(len(rest), 'file')} outside any function: {shown}."


def _weight(fi: FunctionImpact) -> int:
    return len(fi.entrypoints) + len(_tables_only(fi.writes)) + len(_tables_only(fi.reads))


def _details(title: str, body: list[str], *, open_: bool = False) -> list[str]:
    tag = "<details open>" if open_ else "<details>"
    return ["", tag, f"<summary>{title}</summary>", "", *body, "", "</details>"]


def render_markdown(
    graph: Graph,
    impact: Impact,
    *,
    limit: int = 40,
    max_rows: int = 50,
    max_chars: int = 60000,
    details_url: str | None = None,
    blob_url: str | None = None,
    mermaid: bool = True,
    shared_services: int = 3,
) -> str:
    """Markdown for a PR comment, always shorter than ``max_chars``.

    Starts with :data:`MARKER` and a hidden ``<!-- bizmap-risk: LEVEL -->``
    line. ``blob_url`` (``https://github.com/OWNER/REPO/blob/SHA``) turns
    ``file:line`` into links. Functions with no entrypoint and no data are
    summarized in one line; the rest are ranked by how much they reach and
    capped at ``max_rows`` (every other table is capped the same way).
    """
    from bizmap import __version__

    facts = _gather(graph, impact, shared_services)
    links = _Links(blob_url)
    prefix = getattr(impact, "prefix", ".") or "."
    head = [MARKER, RISK_MARKER.format(facts.risk), f"### BizMap impact · {_RISK_TITLE[facts.risk]}", ""]
    run = f" · [run]({details_url})" if details_url else ""
    commit = f" · commit {links.commit}" if links.commit else ""
    footer = [
        "",
        f"<sub>[BizMap]({DOCS_URL}) {__version__} · deterministic static analysis, no AI{commit}{run} · "
        "updated on every push</sub>",
    ]
    other = _other_unmapped(impact)
    notes = list(facts.notes)
    if facts.low_conf:
        notes.append(
            f"{facts.low_conf} {_plural(facts.low_conf, 'link')} in the reach "
            f"{'is' if facts.low_conf == 1 else 'are'} `heuristic`/`unknown` (route literals, dynamic SQL): included, worth a look"
        )
    if other:
        notes.append(other.rstrip("."))
    if impact.hint:
        notes.append(f"Note: {impact.hint}".rstrip("."))
    notes += [f"Note: {w}" for w in impact.warnings]

    def notes_md(cap: int) -> list[str]:
        md = [f"- {n}" for n in notes[:cap]]
        if len(notes) > cap:
            md.append(f"- +{len(notes) - cap} more")
        return md

    # ── short form: nothing reaches data, a route or a job ──
    if facts.risk == "none" and not impact.schema_changes:
        body = list(head)
        n = len(impact.functions)
        if n:
            body.append(
                f"**{n}** changed {_plural(n, 'function')}, none reaching a route, job or table."
            )
        else:
            body.append("No mapped function changed in this diff.")
            if impact.unmapped_files and not other:
                body += ["", f"{len(impact.unmapped_files)} changed file(s) have no mapped function."]
        if notes:
            body += ["", *notes_md(10)]
        text = "\n".join(body + footer) + "\n"
        return text if len(text) <= max_chars else _hard_cut(text, head, max_chars, details_url)

    folded = _fold_pass_through(graph, impact.functions)
    reaching = sorted(
        (f for f in impact.functions if _weight(f) and f.function not in folded),
        key=lambda f: (-_weight(f), f.function),
    )
    quiet = len([f for f in impact.functions if not _weight(f)])
    via: dict[str, list[str]] = {}
    for caller, callee in folded.items():
        via.setdefault(callee, []).append(caller)

    # one-line summary
    eps = sorted(facts.ep_fns)
    kinds: dict[str, int] = {}
    for ep in eps:
        word = _KIND_WORD.get(str((graph.get(ep) or Node("", "")).attrs.get("kind")), "entrypoint")
        kinds[word] = kinds.get(word, 0) + 1
    kind_txt = ", ".join(f"{c} {_plural(c, w)}" for w, c in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    n_w = len([t for t, u in facts.tables.items() if u.write])
    n_r = len([t for t, u in facts.tables.items() if u.read_any])
    n_fn = len(impact.functions)
    summary = (
        f"**{len(eps)}** {_plural(len(eps), 'entrypoint')}"
        + (f" ({kind_txt})" if kind_txt else "")
        + f" · writes **{n_w}** {_plural(n_w, 'table')} · reads **{n_r}** {_plural(n_r, 'table')}"
        + f" · **{facts.columns}** {_plural(facts.columns, 'column')}"
        + (f" — from **{n_fn}** changed {_plural(n_fn, 'function')}" if n_fn else "")
    )
    if impact.functions:
        head.append(summary)
    else:
        n_s = len(impact.schema_changes)
        head.append(f"No mapped function changed in this diff · **{n_s}** schema {_plural(n_s, 'change')}.")
    risky_tables = {
        t for t in facts.tables if facts.tables[t].write and len(facts.services.get(t, [])) >= max(shared_services, 2)
    }

    def data_labels(ids: list[str]) -> list[str]:
        """`invoices (id, status)`: columns grouped under their table."""
        cols: dict[str, list[str]] = {}
        for nid in ids:
            col = _column_of(nid)
            if col:
                cols.setdefault(_table_of(nid), []).append(col)
        out: list[str] = []
        for tid in _tables_only(ids):
            label = _label(graph, tid)
            if tid in cols:
                names = sorted(cols[tid])
                shown = ", ".join(names[:6]) + (f", +{len(names) - 6}" if len(names) > 6 else "")
                label = f"{label} ({shown})"
            out.append(label)
        return out

    def cell(ids: list[str], *, data: bool = False) -> str:
        ids = _known(ids) if data else ids
        if not ids:
            return "—"
        names = data_labels(ids) if data else [_label(graph, i) for i in ids]
        labels = [_code(n) for n in names[:8]]
        if len(names) > 8:
            labels.append(f"+{len(names) - 8}")
        return "<br>".join(labels)

    def fn_cell(fi: FunctionImpact) -> str:
        label = _code(_label(graph, fi.function))
        callers = sorted(via.get(fi.function, []))
        if callers:
            names = ", ".join(_code(_short_fn(graph, c)) for c in callers[:3])
            more = f" +{len(callers) - 3}" if len(callers) > 3 else ""
            label += f"<br><sub>also changed: {names}{more} (same impact)</sub>"
        return label

    def fn_where(fi: FunctionImpact) -> str:
        _p, start, end = _node_where(graph, fi.function, prefix)
        line = fi.lines[0] if fi.lines else start
        return links.where(fi.file, line, end if start and line and start <= line <= (end or line) else None)

    def ep_section(cap: int) -> list[str]:
        if not eps:
            return []
        order = sorted(eps, key=lambda e: (e not in facts.direct, -len(facts.ep_fns[e]), _label(graph, e)))
        rows = ["| Kind | Entrypoint | Where | Reaches changed |", "|---|---|---|---|"]
        for ep in order[:cap]:
            node = graph.get(ep) or Node("", "")
            word = _KIND_WORD.get(str(node.attrs.get("kind")), "entrypoint")
            mark = " · **handler changed**" if ep in facts.direct else ""
            path, line, _end = _node_where(graph, ep, prefix)
            fns = sorted(_short_fn(graph, f) for f in facts.ep_fns[ep])
            rows.append(f"| {word} | {_code(_label(graph, ep))}{mark} | {links.where(path, line)} | {_names(fns, 3)} |")
        if len(order) > cap:
            rows.append(f"\n+{len(order) - cap} more {_plural(len(order) - cap, 'entrypoint')}.")
        return _details(f"<b>Entrypoints</b> · {len(eps)}", rows)

    def data_section(cap: int) -> list[str]:
        if not facts.tables:
            return []
        order = sorted(
            facts.tables,
            key=lambda t: (not facts.tables[t].write, t not in risky_tables, t not in facts.table_triggers, _label(graph, t)),
        )
        rows = ["| Table | Writes | Reads | Changed functions |", "|---|---|---|---|"]
        for t in order[:cap]:
            use = facts.tables[t]
            flags = []
            svc = facts.services.get(t, [])
            if t in risky_tables:
                flags.append(f"⚠️ {len(svc)} services")
            elif len(svc) > 1:
                flags.append(f"{len(svc)} services")
            if t in facts.table_triggers:
                flags.append("⚡ trigger")
            name = f"**{_code(_label(graph, t))}**" + (f" <sub>{' · '.join(flags)}</sub>" if flags else "")
            w = ", ".join(sorted(use.written)[:8]) if use.written else ("_table_" if use.write else "—")
            r_cols = sorted(use.read - use.written)
            r = ", ".join(r_cols[:8]) if r_cols else ("_table_" if use.read_any and not use.read else "—")
            if len(use.written) > 8:
                w += f", +{len(use.written) - 8}"
            if len(r_cols) > 8:
                r += f", +{len(r_cols) - 8}"
            fns = sorted(_short_fn(graph, f) for f in use.functions)
            rows.append(f"| {name} | {w} | {r} | {_names(fns, 3)} |")
        if len(order) > cap:
            rows.append(f"\n+{len(order) - cap} more {_plural(len(order) - cap, 'table')}.")
        return _details(
            f"<b>Data</b> · writes {n_w} · reads {n_r} · {facts.columns} {_plural(facts.columns, 'column')}", rows
        )

    def trigger_section(cap: int) -> list[str]:
        trig = sorted({x for ts in facts.table_triggers.values() for x in ts})
        if not trig:
            return []
        rows = ["| Trigger | On table | When | Where |", "|---|---|---|---|"]
        for tid in trig[:cap]:
            a = (graph.get(tid) or Node("", "")).attrs
            events = a.get("events") or []
            when = " ".join(
                x for x in [str(a.get("timing") or ""), " OR ".join(str(e) for e in events) if isinstance(events, list) else str(events)] if x
            )
            fires = " · fires on this change" if tid in facts.fired else ""
            path, line, _e = _node_where(graph, tid, prefix)
            rows.append(
                f"| ⚡ {_code(_label(graph, tid))}{fires} | {_code(str(a.get('table') or '?'))} | {when or '—'} | {links.where(path, line)} |"
            )
        if len(trig) > cap:
            rows.append(f"\n+{len(trig) - cap} more.")
        return _details(f"<b>Triggers</b> · {len(trig)}", rows)

    def http_section(cap: int) -> list[str]:
        if not facts.http:
            return []
        rows = ["| Services | Call | Match |", "|---|---|---|"]
        for e in facts.http[:cap]:
            a, b = _repo_name(graph, e.from_id) or "?", _repo_name(graph, e.to_id) or "?"
            src = _short_fn(graph, e.from_id) if (graph.get(e.from_id) or Node("", "")).type == "function" else _label(graph, e.from_id)
            dst = _label(graph, e.to_id)
            if _multi_repo(graph) and ": " in dst:
                dst = dst.split(": ", 1)[1]
            match = str(e.confidence)
            if e.attrs.get("via"):
                match += f" · {str(e.attrs['via']).replace('_', ' ')}"
            svc = f"**{a}** → **{b}**" if a != b else f"within **{a}**"
            rows.append(f"| {svc} | {_code(src)} → {_code(dst)} | {match} |")
        if len(facts.http) > cap:
            rows.append(f"\n+{len(facts.http) - cap} more.")
        return _details(f"<b>Cross-service calls</b> · {len(facts.http)}", rows)

    def fn_section(n_rows: int) -> list[str]:
        if not impact.functions:
            return []
        shown = reaching[:n_rows]
        folded_note = f" ({len(folded)} folded into another row)" if folded else ""
        rows = [
            f"**{n_fn}** changed {_plural(n_fn, 'function')}{folded_note} · "
            f"**{len(impact.entrypoints)}** {_plural(len(impact.entrypoints), 'entrypoint')} · "
            f"writes **{n_w}** {_plural(n_w, 'table')} · reads **{n_r}** {_plural(n_r, 'table')}"
        ]
        if shown:
            rows += ["", "| Changed function | Where | Entrypoints | Writes | Reads |", "|---|---|---|---|---|"]
            rows += [
                f"| {fn_cell(fi)} | {fn_where(fi)} | {cell(fi.entrypoints)} | "
                f"{cell(fi.writes, data=True)} | {cell(fi.reads, data=True)} |"
                for fi in shown
            ]
        tail: list[str] = []
        if len(reaching) > n_rows:
            tail.append(f"+{len(reaching) - n_rows} more function(s) with impact not shown")
        if quiet:
            tail.append(f"+{quiet} changed function(s) reach no route or table")
        if tail:
            rows += ["", " · ".join(tail) + "."]
        if len(reaching) > n_rows and details_url:
            rows.append(f"Full list: [workflow run summary]({details_url}).")
        return _details(f"<b>Changed functions</b> · {n_fn}", rows)

    def build(n_rows: int, with_diagram: bool) -> str:
        body = list(head)
        top = [f"- {text}" for _lvl, text in facts.reasons[:5]]
        if len(facts.reasons) > 5:
            top.append(f"- +{len(facts.reasons) - 5} more")
        if top and facts.risk != "none":
            body += ["", f"**Why {facts.risk}:**", *top]
        if with_diagram and mermaid and reaching:
            sub = Impact(functions=reaching[: max(n_rows, 1)], unmapped_files=[])
            diagram = render_mermaid(graph, sub, limit=limit, risky=risky_tables, triggers=facts.table_triggers)
            body += _details(
                "<b>Diagram</b> · entrypoint → changed function → table",
                ["```mermaid", diagram.rstrip(), "```"],
                open_=True,
            )
        cap = max(n_rows, 5) if n_rows else 0
        if cap:
            body += ep_section(cap)
            body += data_section(cap)
        schema_md = _schema_section(graph, impact, links, cap=min(30, max(cap, 5)))
        if schema_md:
            risky_schema = any(c.risky for c in impact.schema_changes)
            body += _details(f"<b>Schema changes</b> · {len(impact.schema_changes)}", schema_md, open_=risky_schema)
        if cap:
            body += trigger_section(cap)
            body += http_section(cap)
        body += fn_section(n_rows)
        if notes:
            body += _details(f"<b>Unresolved &amp; notes</b> · {len(notes)}", notes_md(max(cap, 5)))
        return "\n".join(body + footer) + "\n"

    n = min(max_rows, len(reaching))
    text = build(n, True)
    while len(text) > max_chars and n > 0:
        n = max(0, n - max(1, n // 4))
        text = build(n, True)
    if len(text) > max_chars:
        text = build(0, False)
    if len(text) > max_chars:
        text = _hard_cut(text, head, max_chars, details_url)
    return text


def _hard_cut(text: str, head: list[str], max_chars: int, details_url: str | None) -> str:
    """Pathological labels: cut, keeping the marker and the header."""
    tail = "\n\n…truncated" + (f" — full report in the [run summary]({details_url})" if details_url else "") + ".\n"
    keep = "\n".join(head[:3]) + "\n"
    room = max_chars - len(keep) - len(tail)
    rest = text[len(keep):]
    cut = rest[:room].rsplit("\n", 1)[0] if room > 0 else ""
    out = keep + cut + tail
    return out if len(out) <= max_chars else (keep + tail)[:max_chars]
