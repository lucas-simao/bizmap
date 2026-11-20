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
from pathlib import PurePosixPath
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

    Added lines count as changed. A deletion marks the new-side line where the
    removed block was (and the one before it), so a function that only lost
    lines is still caught. Deleted files are ignored (nothing to map onto).
    """
    out: dict[str, set[int]] = {}
    path: str | None = None
    line = 0
    for raw in text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].rstrip("\n")
            target = _git_unquote(target) if target.startswith('"') else target.split("\t", 1)[0].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith("b/") else target
                out.setdefault(path, set())
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        m = _HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None or not raw:
            if path is not None and not raw:
                line += 1
            continue
        tag = raw[0]
        if tag == "+":
            out[path].add(line)
            line += 1
        elif tag == "-":
            out[path].add(max(line, 1))
            if line > 1:
                out[path].add(line - 1)
        elif tag == " ":
            line += 1
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
class Impact:
    functions: list[FunctionImpact]
    unmapped_files: list[str]
    hint: str | None = None

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


def _entrypoints_reaching(graph: Graph, node_id: str) -> set[str]:
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
        for edge in graph.incoming(cur):
            if edge.type in _CALL_EDGES:
                q.append(edge.from_id)
    return found


def _data_reached(graph: Graph, node_id: str) -> tuple[set[str], set[str]]:
    writes: set[str] = set()
    reads: set[str] = set()
    seen: set[str] = set()
    q = deque([node_id])
    while q:
        cur = q.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        for edge in graph.outgoing(cur):
            if edge.type == "writes":
                writes.add(edge.to_id)
            elif edge.type == "reads":
                reads.add(edge.to_id)
            elif edge.type in _CALL_EDGES:
                q.append(edge.to_id)
    return writes, reads


def compute_impact(graph: Graph, diff_text: str, *, prefix: str = ".") -> Impact:
    """Impact of ``diff_text`` (paths relative to the git root) on ``graph``.

    ``prefix`` is the scanned directory relative to the git root (the path
    given to ``bizmap scan``).
    """
    changes = parse_unified_diff(diff_text)
    hits, unmapped = _changed_functions(graph, changes, prefix)
    out: list[FunctionImpact] = []
    for nid, lines in sorted(hits.items()):
        node = graph.nodes[nid]
        writes, reads = _data_reached(graph, nid)
        reads -= writes
        out.append(
            FunctionImpact(
                function=nid,
                file=_function_file(node, graph, prefix) or "",
                lines=lines,
                entrypoints=sorted(_entrypoints_reaching(graph, nid)),
                writes=sorted(writes),
                reads=sorted(reads),
            )
        )
    hint = None
    if changes and len(unmapped) == len(changes):
        roots = sorted(set((graph.attrs.get("repo_roots") or {}).values())) or ["."]
        hint = (
            "none of the changed paths is under a scanned root "
            f"({', '.join(roots[:5])}{', …' if len(roots) > 5 else ''}"
            + (f"; prefix {prefix!r}" if graph.attrs.get("repo_roots_base") != "git" else "")
            + "). "
            + ("Pass --prefix <dir given to bizmap scan>." if graph.attrs.get("repo_roots_base") != "git" else "")
        ).strip()
    return Impact(functions=out, unmapped_files=unmapped, hint=hint)


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
    if not impact.functions:
        head.append("No mapped function changed in this diff.")
        if impact.unmapped_files:
            head.append("")
            head.append(f"{len(impact.unmapped_files)} changed file(s) have no mapped function.")
        if impact.hint:
            head.append("")
            head.append(f"Note: {impact.hint}")
        return "\n".join(head) + "\n"
    reaching = sorted(
        (f for f in impact.functions if _weight(f)), key=lambda f: (-_weight(f), f.function)
    )
    quiet = len(impact.functions) - len(reaching)
    head.append(
        f"**{len(impact.functions)}** changed function(s) · "
        f"**{len(impact.entrypoints)}** entrypoint(s) · "
        f"writes **{len(_tables_only(impact.writes))}** · reads **{len(_tables_only(impact.reads))}** table(s)"
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

    rows = [
        f"| `{_label(graph, fi.function)}` | {cell(fi.entrypoints)} | "
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
    if len(text) > max_chars:  # pathological labels: hard cut on a line boundary
        cut = text[: max_chars - 200].rsplit("\n", 1)[0]
        text = cut + "\n\n…truncated.\n"
    return text
