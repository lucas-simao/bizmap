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

    def to_dict(self) -> dict[str, Any]:
        return {
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
    r"(?is)\b(?:CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE(?:\s+IF\s+EXISTS)?(?:\s+ONLY)?)\s+([\w\".]+)"
)
_SQL_ADD_COLUMN = re.compile(r'(?i)\b(?:ADD|ALTER|DROP|RENAME)\s+(?:COLUMN\s+)?(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"?([A-Za-z_]\w*)"?')
_SQL_COLUMN_DEF = re.compile(r'^\s*,?\s*"?([A-Za-z_]\w*)"?\s+[A-Za-z]')
_SQL_NOT_COLUMN = {
    "constraint", "primary", "foreign", "unique", "check", "exclude", "like", "create",
    "alter", "table", "add", "drop", "references", "on", "insert", "update", "delete", "select",
}


def _schema_changes(
    graph: Graph,
    changes: dict[str, set[int]],
    unmapped: list[str],
    repo_root: Path,
    inc: dict[str, list[Any]],
) -> list[SchemaChange]:
    """Changed lines of `.sql` files inside `CREATE TABLE` / `ALTER TABLE` that
    name a column the graph knows, with what reads or writes that column."""
    by_name: dict[tuple[str, str], list[str]] = {}
    for nid, node in graph.nodes.items():
        if node.type == "column":
            table = str(node.attrs.get("table") or "").lower().split(".")[-1]
            name = str(node.attrs.get("name") or "").lower()
            by_name.setdefault((table, name), []).append(nid)
    out: list[SchemaChange] = []
    seen: set[str] = set()
    for path in unmapped:
        if not path.lower().endswith(".sql"):
            continue
        try:
            lines = (repo_root / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for ln in sorted(changes.get(path, ())):
            if not 1 <= ln <= len(lines):
                continue
            # statement holding the line: from the previous ';' up to it
            start = ln - 1
            while start > 0 and ";" not in lines[start - 1]:
                start -= 1
            stmt = "\n".join(lines[start:ln])
            found = list(_SQL_TABLE_STMT.finditer(stmt))
            if not found:
                continue
            table = found[-1].group(1).replace('"', "").split(".")[-1].lower()
            text = lines[ln - 1]
            cm = _SQL_ADD_COLUMN.search(text) or _SQL_COLUMN_DEF.match(text)
            if cm is None or cm.group(1).lower() in _SQL_NOT_COLUMN:
                continue
            for cid in by_name.get((table, cm.group(1).lower()), []):
                if cid in seen:
                    continue
                seen.add(cid)
                fns = sorted({e.from_id for e in inc.get(cid, []) if e.type in {"reads", "writes"}
                              and (graph.get(e.from_id) is not None and graph.nodes[e.from_id].type == "function")})
                eps: set[str] = {e.from_id for e in inc.get(cid, []) if e.type in {"reads", "writes"}
                                 and graph.get(e.from_id) is not None and graph.nodes[e.from_id].type == "entrypoint"}
                for fid in fns:
                    eps |= _entrypoints_reaching(graph, fid, inc)
                out.append(SchemaChange(column=cid, file=path, line=ln, entrypoints=sorted(eps), functions=fns))
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
    code_exts = {".go", ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".sql"}
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
    schema = _schema_changes(graph, changes, unmapped, repo_root, in_idx) if repo_root is not None else []
    return Impact(functions=out, unmapped_files=unmapped, hint=hint, schema_changes=schema)


# ── rendering ────────────────────────────────────────────────────────────────


def _label(graph: Graph, node_id: str) -> str:
    node = graph.get(node_id)
    leaf = node_id.split("/", 1)[1] if "/" in node_id.split(":", 1)[0] else node_id
    if node is None:
        return leaf
    a = node.attrs
    repo = a.get("repo")
    multi = len(graph.attrs.get("repo_roots") or {}) > 1
    pre = f"{repo}: " if multi and repo else ""
    if node.type == "entrypoint":
        if a.get("kind") == "http":
            return f"{pre}{a.get('method')} {a.get('path')}"
        return f"{pre}{a.get('kind')} {a.get('symbol') or a.get('handler') or a.get('path') or ''}".strip()
    if node.type == "function":
        return f"{pre}{a.get('name') or leaf.removeprefix('function:')}"
    for kind in ("column:", "table:", "collection:", "view:"):
        if leaf.startswith(kind):
            return f"{pre}{leaf.removeprefix(kind)}"
    return f"{pre}{leaf}"


def _mid(node_id: str) -> str:
    return "n" + re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def _esc(text: str) -> str:
    return text.replace('"', "'")


def _tables_only(ids: list[str]) -> list[str]:
    """Collapse columns into their table for the diagram."""
    out: set[str] = set()
    for nid in ids:
        head, sep, leaf = nid.rpartition("/")
        prefix = head + sep
        if leaf.startswith("column:"):
            out.add(prefix + "table:" + leaf.removeprefix("column:").split(".", 1)[0])
        else:
            out.add(nid)
    return sorted(out)


def render_mermaid(graph: Graph, impact: Impact, *, limit: int = 40) -> str:
    lines = ["flowchart LR"]
    shown: set[str] = set()

    def node(nid: str, shape: str) -> str:
        mid = _mid(nid)
        if nid not in shown:
            shown.add(nid)
            label = _esc(_label(graph, nid))
            if shape == "ep":
                lines.append(f'  {mid}(["{label}"])')
            elif shape == "fn":
                lines.append(f'  {mid}["{label}"]:::changed')
            else:
                lines.append(f'  {mid}[("{label}")]')
        return mid

    edges: list[str] = []
    for fi in impact.functions:
        if len(shown) >= limit:
            break
        fn = node(fi.function, "fn")
        for ep in fi.entrypoints[:limit]:
            edges.append(f"  {node(ep, 'ep')} --> {fn}")
        for t in _tables_only(fi.writes):
            edges.append(f"  {fn} -- writes --> {node(t, 'data')}")
        for t in _tables_only(fi.reads):
            if t in _tables_only(fi.writes):
                continue
            edges.append(f"  {fn} -. reads .-> {node(t, 'data')}")
    lines.extend(dict.fromkeys(edges))
    lines.append("  classDef changed fill:#fff3bf,stroke:#f08c00")
    return "\n".join(lines) + "\n"


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


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

    for caller in sorted(calls):
        same = sorted(c for c in calls[caller] if key(by_id[c]) == key(by_id[caller]) and c not in folded)
        if same and caller not in folded.values():
            folded[caller] = same[0]
    return folded


def _schema_section(graph: Graph, impact: Impact) -> list[str]:
    if not impact.schema_changes:
        return []
    out = ["**Schema changes**", "", "| Column | Used by entrypoints | Functions |", "|---|---|---|"]
    for ch in impact.schema_changes[:30]:
        eps = "<br>".join(f"`{_label(graph, e)}`" for e in ch.entrypoints[:6]) or "— (not used yet)"
        if len(ch.entrypoints) > 6:
            eps += f"<br>+{len(ch.entrypoints) - 6}"
        fns = "<br>".join(f"`{_label(graph, f)}`" for f in ch.functions[:4]) or "—"
        if len(ch.functions) > 4:
            fns += f"<br>+{len(ch.functions) - 4}"
        node = graph.get(ch.column)
        name = f"{node.attrs.get('table')}.{node.attrs.get('name')}" if node is not None else ch.column
        out.append(f"| `{name}` ({ch.file}:{ch.line}) | {eps} | {fns} |")
    if len(impact.schema_changes) > 30:
        out.append(f"\n+{len(impact.schema_changes) - 30} more changed column(s).")
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


def render_markdown(
    graph: Graph,
    impact: Impact,
    *,
    limit: int = 40,
    max_rows: int = 50,
    max_chars: int = 60000,
    details_url: str | None = None,
) -> str:
    """Markdown for a PR comment, always shorter than ``max_chars``.

    Functions with no entrypoint and no data are summarized in one line; the
    rest are ranked by how much they reach and capped at ``max_rows``.
    """
    head = [MARKER, "### BizMap impact", ""]
    schema_md = _schema_section(graph, impact)
    if not impact.functions and schema_md:
        body = head + ["No mapped function changed in this diff.", "", *schema_md]
        other = _other_unmapped(impact)
        if other:
            body += ["", other]
        return "\n".join(body) + "\n"
    if not impact.functions:
        head.append("No mapped function changed in this diff.")
        if impact.unmapped_files:
            head.append("")
            head.append(f"{len(impact.unmapped_files)} changed file(s) have no mapped function.")
        if impact.hint:
            head.append("")
            head.append(f"Note: {impact.hint}")
        return "\n".join(head) + "\n"
    folded = _fold_pass_through(graph, impact.functions)
    reaching = sorted(
        (f for f in impact.functions if _weight(f) and f.function not in folded),
        key=lambda f: (-_weight(f), f.function),
    )
    quiet = len([f for f in impact.functions if not _weight(f)])
    via: dict[str, list[str]] = {}
    for caller, callee in folded.items():
        via.setdefault(callee, []).append(caller)
    n_w, n_r = len(_tables_only(impact.writes)), len(_tables_only(impact.reads))
    head.append(
        f"**{len(impact.functions)}** changed {_plural(len(impact.functions), 'function')} · "
        f"**{len(impact.entrypoints)}** {_plural(len(impact.entrypoints), 'entrypoint')} · "
        f"writes **{n_w}** {_plural(n_w, 'table')} · reads **{n_r}** {_plural(n_r, 'table')}"
    )

    def data_labels(ids: list[str]) -> list[str]:
        """`invoices (id, status)`: columns grouped under their table."""
        cols: dict[str, list[str]] = {}
        for nid in ids:
            pre, sep, leaf = nid.rpartition("/")
            if leaf.startswith("column:"):
                table, _dot, col = leaf.removeprefix("column:").partition(".")
                cols.setdefault(pre + sep + "table:" + table, []).append(col)
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
        if not ids:
            return "—"
        names = data_labels(ids) if data else [_label(graph, i) for i in ids]
        labels = [f"`{n}`" for n in names[:8]]
        if len(names) > 8:
            labels.append(f"+{len(names) - 8}")
        return "<br>".join(labels)

    def fn_cell(fi: FunctionImpact) -> str:
        label = f"`{_label(graph, fi.function)}`"
        callers = sorted(via.get(fi.function, []))
        if callers:
            names = ", ".join(f"`{_label(graph, c)}`" for c in callers[:3])
            more = f" +{len(callers) - 3}" if len(callers) > 3 else ""
            label += f"<br><sub>also changed: {names}{more} (same impact)</sub>"
        return label

    rows = [
        f"| {fn_cell(fi)} | {cell(fi.entrypoints)} | "
        f"{cell(fi.writes, data=True)} | {cell(fi.reads, data=True)} |"
        for fi in reaching
    ]
    footer = [
        "",
        "<sub>Deterministic static analysis: entrypoints that reach each changed function and the data it "
        "(or its callees) reads/writes. Edges marked `heuristic`/`unknown` in the graph are included.</sub>",
    ]

    def build(n_rows: int, with_diagram: bool) -> str:
        shown = reaching[:n_rows]
        body = list(head)
        if with_diagram and shown:
            sub = Impact(functions=shown, unmapped_files=[])
            body += ["", "```mermaid", render_mermaid(graph, sub, limit=limit).rstrip(), "```"]
        if shown:
            body += ["", "| Changed function | Entrypoints | Writes | Reads |", "|---|---|---|---|"]
            body += rows[:n_rows]
        notes: list[str] = []
        if len(reaching) > n_rows:
            notes.append(f"+{len(reaching) - n_rows} more function(s) with impact not shown")
        if quiet:
            notes.append(f"+{quiet} changed function(s) reach no route or table")
        if notes:
            body += ["", " · ".join(notes) + "."]
        if schema_md:
            body += ["", *schema_md]
        other = _other_unmapped(impact)
        if other:
            body += ["", other]
        if len(reaching) > n_rows and details_url:
            body.append(f"Full list: [workflow run summary]({details_url}).")
        return "\n".join(body + footer) + "\n"

    n = min(max_rows, len(reaching))
    text = build(n, True)
    while len(text) > max_chars and n > 0:
        n = max(0, n - max(1, n // 4))
        text = build(n, True)
    if len(text) > max_chars:
        text = build(0, False)
    if len(text) > max_chars:  # pathological labels: hard cut, marker and header kept
        tail = "\n\n…truncated" + (f" — full report in the [run summary]({details_url})" if details_url else "") + ".\n"
        keep = "\n".join(head) + "\n"
        room = max_chars - len(keep) - len(tail)
        rest = text[len(keep):]
        cut = rest[:room].rsplit("\n", 1)[0] if room > 0 else ""
        text = keep + cut + tail
    return text
