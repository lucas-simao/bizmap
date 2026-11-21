"""Questions about one table / collection / column, from the graph JSON:
``bizmap who-writes``, ``bizmap who-reads`` and ``bizmap trace``.

Only the JSON is read, into plain dicts plus in-memory indexes (no
``Graph`` objects): a 70k-node / 180k-edge graph answers in well under two
seconds. Nothing is guessed:

- an entity name that matches several *different* things (a Firestore
  collection and a Postgres table called ``orders``, two tables of the same
  name in two databases) is reported as ambiguous with every candidate
  (kind · store · repo · id) — the caller qualifies it;
- the same table / collection seen by several repos (same kind, same name,
  same or unknown database) is one entity and answered per repo, saying so;
- every answer ends with what the graph could *not* resolve and could change
  it (writes / reads with a table name built at runtime, publishes to a topic
  only known at runtime, HTTP calls that matched no route).

JSON output (``--format json``) is a stable contract, ``"schema":
"bizmap.query/v1"``; see ``docs/commands/query.md``.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "bizmap.query/v1"
DATA_TYPES = ("table", "view", "collection")
_RANK = {"exact": 4, "inferred": 3, "heuristic": 2, "unknown": 1}
_BY_RANK = {v: k for k, v in _RANK.items()}
# backwards from a function to what starts it: callers, handlers, queue consumers
_UP_EDGES = ("calls", "triggers")
# forwards from an entrypoint through its code
_DOWN_EDGES = ("calls", "triggers")
MAX_VISIT = 2_000  # nodes one walk may visit (a guard for huge, dense graphs: the row says so)
MAX_ROOTS = 25  # entrypoints listed per function
MAX_WRITER_HOPS = 50  # "written by" lines per table in a trace (who-writes lists them all)
_ENGINE = {"postgres": "Postgres", "mysql": "MySQL", "sqlite": "SQLite", "sqlserver": "SQL Server"}


class QueryError(Exception):
    """No match, or an ambiguous name: ``payload`` is the JSON answer."""

    def __init__(self, message: str, payload: dict[str, Any], code: int) -> None:
        super().__init__(message)
        self.payload = payload
        self.code = code


# ── the index ────────────────────────────────────────────────────────────────


def _split(node_id: str) -> tuple[str | None, str]:
    """``orders-api/table:orders`` → (``orders-api``, ``table:orders``)."""
    head = node_id.split(":", 1)[0]
    if "/" in head:
        repo, rest = node_id.split("/", 1)
        return repo, rest
    return None, node_id


@dataclass
class Index:
    nodes: dict[str, dict[str, Any]]
    out: dict[str, list[dict[str, Any]]]
    inc: dict[str, list[dict[str, Any]]]
    by_name: dict[str, list[str]]  # lower name -> data node ids (tables, views, collections)
    columns: dict[tuple[str | None, str], list[str]]  # (repo, lower table) -> column ids
    col_by_name: dict[str, list[str]]  # "table.column" lower -> column ids
    unresolved_http: list[dict[str, Any]] = field(default_factory=list)
    repo: str | None = None
    _up: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    _roots: dict[str, tuple[list[dict[str, Any]], bool]] = field(default_factory=dict)
    _closures: dict[str, set[str]] = field(default_factory=dict)

    def up(self, nid: str) -> list[tuple[str, int]]:
        """(caller, confidence rank) over calls / triggers, without derived copies."""
        got = self._up.get(nid)
        if got is None:
            got = self._up[nid] = [
                (e["from"], _rank(e.get("confidence")))
                for e in self.inc.get(nid, [])
                if e.get("type") in _UP_EDGES and not e.get("derived")
            ]
        return got

    @classmethod
    def load(cls, path: Path) -> Index:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid graph JSON ({exc})") from exc
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
            raise ValueError(f"{path} is not a BizMap graph (expected an object with `nodes` and `edges` lists)")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Index:
        nodes = {n["id"]: n for n in data.get("nodes") or [] if isinstance(n, dict) and "id" in n}
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        inc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in data.get("edges") or []:
            if not isinstance(e, dict) or "from" not in e or "to" not in e:
                continue
            out[e["from"]].append(e)
            inc[e["to"]].append(e)
        by_name: dict[str, list[str]] = defaultdict(list)
        columns: dict[tuple[str | None, str], list[str]] = defaultdict(list)
        col_by_name: dict[str, list[str]] = defaultdict(list)
        for nid, n in nodes.items():
            t = n.get("type")
            if t in DATA_TYPES:
                name = str(n.get("name") or _split(nid)[1].split(":", 1)[-1]).lower()
                if name not in ("unknown",):
                    by_name[name].append(nid)
            elif t == "column":
                repo = _split(nid)[0]
                table = str(n.get("table") or "").lower()
                columns[(repo, table)].append(nid)
                col_by_name[f"{table}.{str(n.get('name') or '').lower()}"].append(nid)
        for lst in (*by_name.values(), *columns.values(), *col_by_name.values()):
            lst.sort()
        return cls(
            nodes=nodes,
            out=out,
            inc=inc,
            by_name=by_name,
            columns=columns,
            col_by_name=col_by_name,
            unresolved_http=[u for u in data.get("unresolved_http") or [] if isinstance(u, dict)],
            repo=data.get("repo"),
        )

    # ── node helpers ──
    def kind(self, nid: str) -> str:
        return str(self.nodes.get(nid, {}).get("type") or _split(nid)[1].split(":", 1)[0])

    def repo_of(self, nid: str) -> str | None:
        n = self.nodes.get(nid) or {}
        return _split(nid)[0] or n.get("repo") or self.repo

    def store(self, nid: str) -> str:
        """Firestore / Postgres / MySQL / SQLite / SQL — for data nodes."""
        n = self.nodes.get(nid) or {}
        if n.get("type") == "column":
            t = self.table_of_column(nid)
            return self.store(t) if t else "SQL"
        db = str(n.get("db") or "").lower()
        if n.get("type") == "collection" or db.startswith("firestore"):
            return "Firestore"
        if str(n.get("engine") or "") in _ENGINE:
            return _ENGINE[str(n["engine"])]
        for key, label in (("postgres", "Postgres"), ("pg", "Postgres"), ("mysql", "MySQL"), ("sqlite", "SQLite")):
            if key in db.replace("-", "_").split("_") or db.startswith(key):
                return label
        return "SQL"

    def table_of_column(self, cid: str) -> str | None:
        n = self.nodes.get(cid) or {}
        repo = _split(cid)[0]
        name = str(n.get("table") or "")
        for t in ("table", "view", "collection"):
            tid = f"{repo}/{t}:{name}" if repo else f"{t}:{name}"
            if tid in self.nodes:
                return tid
        return None

    def label(self, nid: str) -> str:
        n = self.nodes.get(nid)
        leaf = _split(nid)[1]
        if n is None:
            return leaf
        t = n.get("type")
        if t == "entrypoint":
            kind = n.get("kind")
            if kind == "http":
                return f"{n.get('method') or 'ANY'} {n.get('path') or ''}".strip()
            sym = n.get("topic") or n.get("path") or n.get("symbol") or n.get("handler") or leaf.split(":", 2)[-1]
            return f"{kind} {sym}".strip()
        if t == "function":
            name = str(n.get("name") or leaf.removeprefix("function:"))
            return name
        if t == "column":
            return f"{n.get('table')}.{n.get('name')}"
        if t == "queue":
            return f"{n.get('kind') or 'queue'} {n.get('name') or leaf.split(':')[-1]}"
        return str(n.get("name") or leaf.split(":", 1)[-1])

    def evidence(self, nid: str) -> str | None:
        n = self.nodes.get(nid) or {}
        ev = n.get("evidence") or {}
        f = ev.get("file") if isinstance(ev, dict) else None
        if not f:
            return None
        line = n.get("start_line") or ev.get("line")
        return f"{f}:{line}" if line else str(f)

    def brief(self, nid: str) -> dict[str, Any]:
        """``{id, type, label, repo?, evidence?, kind?, method?, path?, store?}``."""
        n = self.nodes.get(nid) or {}
        out: dict[str, Any] = {"id": nid, "type": self.kind(nid), "label": self.label(nid)}
        repo = self.repo_of(nid)
        if repo:
            out["repo"] = repo
        ev = self.evidence(nid)
        if ev:
            out["evidence"] = ev
        if n.get("type") == "entrypoint":
            for key in ("kind", "method", "path"):
                if n.get(key):
                    out[key] = n[key]
        if n.get("type") in (*DATA_TYPES, "column"):
            out["store"] = self.store(nid)
            if n.get("db"):
                out["db"] = n["db"]
        if n.get("type") == "queue":
            out["name"] = n.get("name") or nid.split(":")[-1]
        return out


def _edge_ev(e: dict[str, Any]) -> str | None:
    ev = e.get("evidence")
    if not isinstance(ev, dict) or not ev.get("file"):
        return None
    return f"{ev['file']}:{ev['line']}" if ev.get("line") else str(ev["file"])


def _rank(conf: str | None) -> int:
    return _RANK.get(str(conf or "unknown"), 1)


# ── resolving the entity ─────────────────────────────────────────────────────


@dataclass
class Entity:
    """One logical table / collection / column, possibly seen by several repos."""

    kind: str  # table | view | collection | column
    name: str  # `orders` or `orders.status`
    nodes: list[str]  # node ids, one per repo

    def to_dict(self, ix: Index) -> dict[str, Any]:
        stores = sorted({ix.store(n) for n in self.nodes})
        return {
            "kind": self.kind,
            "name": self.name,
            "store": stores[0] if len(stores) == 1 else stores,
            "repos": sorted({r for r in (ix.repo_of(n) for n in self.nodes) if r}),
            "nodes": [ix.brief(n) for n in self.nodes],
        }


def _db_key(ix: Index, nid: str) -> str:
    n = ix.nodes.get(nid) or {}
    if n.get("type") == "column":
        t = ix.table_of_column(nid)
        return _db_key(ix, t) if t else ""
    return str(n.get("db") or "").lower()


def _group(ix: Index, ids: list[str]) -> list[list[str]]:
    """Candidates that are the same thing: same kind and name, same database
    (an unknown database joins the only known one of that kind and name)."""
    by_kind: dict[tuple[str, str], list[str]] = defaultdict(list)
    for nid in ids:
        n = ix.nodes[nid]
        name = ix.label(nid).lower()
        by_kind[(str(n.get("type")), name)].append(nid)
    groups: list[list[str]] = []
    for (_kind, _name), members in sorted(by_kind.items()):
        dbs: dict[str, list[str]] = defaultdict(list)
        for nid in members:
            dbs[_db_key(ix, nid)].append(nid)
        known = [k for k in dbs if k]
        if len(known) <= 1:
            groups.append(sorted(members))
        else:
            for k in sorted(known):
                groups.append(sorted(dbs[k]))
            if "" in dbs:  # unknown database next to several known ones: its own candidate
                groups.append(sorted(dbs[""]))
    return groups


def resolve(ix: Index, ref: str) -> Entity:
    """``orders`` · ``table:orders`` · ``collection:orders`` · ``orders-api/table:orders``
    · ``orders.status`` · ``column:orders.status`` · ``orders-api/orders``."""
    ref = ref.strip()
    if not ref:
        raise QueryError("empty entity name", {"schema": SCHEMA, "error": "not_found", "query": ref}, 1)
    repo, rest = _split(ref)
    kind = None
    if ":" in rest and rest.split(":", 1)[0] in (*DATA_TYPES, "column"):
        kind, rest = rest.split(":", 1)
    if ref in ix.nodes and ix.kind(ref) in (*DATA_TYPES, "column"):
        ids = [ref]
    else:
        low = rest.lower()
        if kind == "column" or (kind is None and "." in low and low not in ix.by_name):
            ids = list(ix.col_by_name.get(low, []))
            if kind is None and not ids:
                ids = list(ix.by_name.get(low, []))
        else:
            ids = [i for i in ix.by_name.get(low, []) if kind is None or ix.kind(i) == kind]
        if repo:
            ids = [i for i in ids if ix.repo_of(i) == repo]
    if not ids:
        hint = sorted(k for k in ix.by_name if rest.lower() in k)[:10]
        raise QueryError(
            f"no table, collection or column matches {ref!r}",
            {"schema": SCHEMA, "error": "not_found", "query": ref, "similar": hint},
            1,
        )
    groups = _group(ix, ids)
    if len(groups) > 1:
        candidates = []
        for g in groups:
            first = ix.nodes[g[0]]
            candidates.append({
                "kind": str(first.get("type")),
                "name": ix.label(g[0]),
                "store": ix.store(g[0]),
                "repos": sorted({r for r in (ix.repo_of(n) for n in g) if r}),
                "ids": g,
                "qualify": _qualified(ix, g),
            })
        raise QueryError(
            f"{ref!r} is ambiguous: {len(groups)} different things have that name",
            {"schema": SCHEMA, "error": "ambiguous", "query": ref, "candidates": candidates},
            2,
        )
    g = groups[0]
    return Entity(kind=str(ix.nodes[g[0]].get("type")), name=ix.label(g[0]), nodes=g)


def _qualified(ix: Index, ids: list[str]) -> str:
    """The shortest reference that names this candidate."""
    first = ix.nodes[ids[0]]
    kind = str(first.get("type"))
    name = ix.label(ids[0])
    if len(ids) == 1 and ix.repo_of(ids[0]):
        # by kind alone when that is enough, else repo-qualified
        same = [i for i in ix.by_name.get(name.lower(), []) if ix.kind(i) == kind] if kind != "column" else ix.col_by_name.get(name.lower(), [])
        if len(same) == 1:
            return f"{kind}:{name}"
        return ids[0]
    return f"{kind}:{name}"


# ── who writes / who reads ───────────────────────────────────────────────────


def _data_targets(ix: Index, entity: Entity) -> dict[str, str]:
    """Node ids an access to the entity lands on → the entity node (per repo)."""
    targets: dict[str, str] = {}
    for nid in entity.nodes:
        targets[nid] = nid
        if entity.kind != "column":
            name = str(ix.nodes[nid].get("name") or ix.label(nid)).lower()
            for cid in ix.columns.get((ix.repo_of(nid) if _split(nid)[0] else None, name), []):
                if ix.table_of_column(cid) == nid:  # not the fields of a same-named collection
                    targets[cid] = nid
    return targets


def _roots(ix: Index, fn: str) -> tuple[list[dict[str, Any]], bool]:
    """What starts ``fn``: entrypoints (routes, jobs, consumers, triggers) and
    queues reaching it through calls / triggers, each with its shortest path;
    when none, the outermost callers (a ``main``, an exported function).
    The bool is True when the walk hit ``MAX_VISIT`` (the list may be partial)."""
    cached = ix._roots.get(fn)
    if cached is not None:
        return cached
    parent: dict[str, tuple[str, int] | None] = {fn: None}
    best: dict[str, int] = {fn: 4}
    q = deque([fn])
    roots: list[str] = []
    tops: list[str] = []
    truncated = False
    while q:
        if len(parent) >= MAX_VISIT:
            truncated = True
            break
        cur = q.popleft()
        t = ix.kind(cur)
        if cur != fn and t in ("entrypoint", "queue"):
            roots.append(cur)
            if t == "queue":
                continue
        ups = ix.up(cur)
        if not ups and cur != fn and t == "function":
            tops.append(cur)
        for src, rank in ups:
            if src in parent:
                continue
            parent[src] = (cur, rank)
            best[src] = min(best[cur], rank)
            q.append(src)
    eps = [r for r in roots if ix.kind(r) == "entrypoint"]
    eps_set = set(eps)

    def through_entry(q_id: str) -> bool:
        cur = parent.get(q_id)
        while cur is not None:
            if cur[0] in eps_set:
                return True
            cur = parent.get(cur[0])
        return False

    def feeds_entry(q_id: str) -> bool:
        return any(e.get("type") == "triggers" and e["to"] in eps_set for e in ix.out.get(q_id, []))

    # a queue behind a consumer entrypoint is that entrypoint's topic, not another root
    chosen = eps + [r for r in roots if ix.kind(r) == "queue" and not through_entry(r) and not feeds_entry(r)]
    if not chosen:
        chosen = tops
    chosen.sort(key=lambda r: (ix.kind(r) != "entrypoint", ix.label(r), r))
    rows = []
    for r in chosen[:MAX_ROOTS]:
        path = [r]
        cur = r
        while parent.get(cur) is not None:
            cur = parent[cur][0]  # type: ignore[index]
            path.append(cur)
        rows.append({"root": r, "path": path, "confidence": _BY_RANK[best.get(r, 1)]})
    result = (rows, truncated or len(chosen) > MAX_ROOTS)
    ix._roots[fn] = result
    return result


def _access_rows(ix: Index, entity: Entity, etype: str) -> list[dict[str, Any]]:
    targets = _data_targets(ix, entity)
    per_fn: dict[str, dict[str, Any]] = {}
    for tid, owner in targets.items():
        for e in ix.inc.get(tid, []):
            if e.get("type") != etype or e.get("derived"):
                continue
            fn = e["from"]
            row = per_fn.setdefault(fn, {"fn": fn, "edges": [], "columns": set(), "rank": 0, "evidence": None, "table": owner})
            row["edges"].append(e)
            if ix.kind(tid) == "column":
                row["columns"].add(str(ix.nodes[tid].get("name")))
            row["rank"] = max(row["rank"], _rank(e.get("confidence")))
            ev = _edge_ev(e)
            if ev and (row["evidence"] is None or ev < row["evidence"]):
                row["evidence"] = ev
    rows = []
    for fn, row in per_fn.items():
        if ix.kind(fn) == "entrypoint":
            roots, partial = [{"root": fn, "path": [fn], "confidence": "exact"}], False
        else:
            roots, partial = _roots(ix, fn)
        access = _BY_RANK[row["rank"] or 1]
        entries = []
        for r in roots:
            path = r["path"]
            entries.append({
                "entrypoint": ix.brief(r["root"]),
                "handler": ix.brief(path[1]) if len(path) > 2 else None,
                "via": max(0, len(path) - 3),
                "path": [ix.brief(p) for p in path],
                "confidence": _BY_RANK[min(_rank(r["confidence"]), _rank(access))],
            })
        rows.append({
            "function": ix.brief(fn),
            "repo": ix.repo_of(fn),
            "entity_node": row["table"],
            "evidence": row["evidence"] or ix.evidence(fn),
            "columns": sorted(row["columns"]),
            "confidence": access,
            "entrypoints": entries,
            "entrypoints_partial": partial,
        })
    rows.sort(key=lambda r: (r["repo"] or "", -_rank(r["confidence"]), r["function"]["label"], r["function"]["id"]))
    return rows


def _unresolved_data(ix: Index, entity: Entity, etype: str) -> list[dict[str, Any]]:
    """Accesses to a table / collection whose name was built at runtime, in the
    entity's repos: any of them could be this entity."""
    repos = {ix.repo_of(n) for n in entity.nodes}
    family = "collection" if entity.kind == "collection" else "table"
    out = []
    for repo in sorted(repos, key=lambda r: r or ""):
        nid = f"{repo}/{family}:unknown" if repo else f"{family}:unknown"
        fns = sorted({e["from"] for e in ix.inc.get(nid, []) if e.get("type") == etype and not e.get("derived")})
        if fns:
            out.append({
                "kind": f"dynamic_{family}",
                "access": etype,
                "repo": repo,
                "count": len(fns),
                "functions": [ix.brief(f) for f in fns[:10]],
            })
    return out


def who(ix: Index, ref: str, access: str, entity: Entity | None = None) -> dict[str, Any]:
    """``access``: ``writes`` (who-writes) or ``reads`` (who-reads). ``entity``:
    already resolved (``bizmap export`` precomputes every entity's answer)."""
    entity = entity or resolve(ix, ref)
    rows = _access_rows(ix, entity, access)
    return {
        "schema": SCHEMA,
        "query": {"command": "who-writes" if access == "writes" else "who-reads", "entity": ref},
        "entity": entity.to_dict(ix),
        "access": access,
        "results": rows,
        "unresolved": _unresolved_data(ix, entity, access),
    }


# ── trace ────────────────────────────────────────────────────────────────────


def _closure(ix: Index, start: str, limit: int = MAX_VISIT) -> set[str]:
    """Functions an entrypoint / function runs (calls and triggers, forward)."""
    cached = ix._closures.get(start)
    if cached is not None:
        return cached
    seen = {start}
    q = deque([start])
    while q and len(seen) < limit:
        cur = q.popleft()
        for e in ix.out.get(cur, []):
            if e.get("type") in _DOWN_EDGES and not e.get("derived") and e["to"] not in seen and ix.kind(e["to"]) != "queue":
                seen.add(e["to"])
                q.append(e["to"])
    ix._closures[start] = seen
    return seen


def _writes_of(ix: Index, fns: set[str]) -> list[str]:
    """Data nodes (tables / collections; columns folded into their table) written by ``fns``."""
    out: set[str] = set()
    for f in fns:
        for e in ix.out.get(f, []):
            if e.get("type") != "writes" or e.get("derived"):
                continue
            to = e["to"]
            if ix.kind(to) == "column":
                to = ix.table_of_column(to) or to
            if _split(to)[1].split(":", 1)[-1] == "unknown":
                continue
            out.add(to)
    return sorted(out)


def trace(
    ix: Index, ref: str, depth: int = 2, direction: str = "both", entity: Entity | None = None
) -> dict[str, Any]:
    entity = entity or resolve(ix, ref)
    start = [ix.table_of_column(n) or n if ix.kind(n) == "column" else n for n in entity.nodes]
    hops_down: list[dict[str, Any]] = []
    hops_up: list[dict[str, Any]] = []
    unresolved: dict[str, Any] = {"queues": [], "http": [], "dynamic_writes": []}
    seen_q: set[tuple[str, str]] = set()
    seen_feed: set[str] = set()
    seen_http: set[tuple[str, str]] = set()
    noted_fns: set[str] = set()

    def note_unresolved(fns: set[str]) -> None:
        for f in sorted(fns - noted_fns):
            noted_fns.add(f)
            n = ix.nodes.get(f) or {}
            for uq in n.get("unresolved_queues") or []:
                unresolved["queues"].append({"function": ix.brief(f), **({k: uq[k] for k in ("env", "reason", "kind") if isinstance(uq, dict) and k in uq})})
        for u in ix.unresolved_http:
            if str(u.get("from") or "") in fns and str(u.get("from")) not in {x.get("_from") for x in unresolved["http"]}:
                unresolved["http"].append({
                    "_from": str(u.get("from")),
                    "function": ix.brief(str(u.get("from"))),
                    "method": u.get("method"),
                    "url": u.get("url"),
                    "reason": u.get("reason"),
                })

    def writers_ops(data: str) -> list[tuple[str, str, set[str]]]:
        """(entrypoint or outermost function, writer, the operation's functions)
        for ``data``. The operation = the path from the entrypoint to the writer
        plus what each function on it calls directly (the handler that saves,
        then publishes)."""
        ent = Entity(kind=ix.kind(data), name=ix.label(data), nodes=[data])
        out = []
        for row in _access_rows(ix, ent, "writes"):
            fn = row["function"]["id"]
            entries = row["entrypoints"] or [{"entrypoint": {"id": fn}, "path": [{"id": fn}]}]
            for e in entries:
                path = {p["id"] for p in e["path"]} | {fn}
                ops = set(path)
                for p in path:
                    for x in ix.out.get(p, []):
                        if x.get("type") == "calls" and not x.get("derived"):
                            ops.add(x["to"])
                out.append((e["entrypoint"]["id"], fn, ops))
        return out

    # ── downstream ──
    frontier = list(start)
    seen_data = set(start)
    for level in range(1, depth + 1):
        if direction not in ("down", "both"):
            break
        nxt: list[str] = []
        for data in frontier:
            for e in ix.out.get(data, []):
                if e.get("type") != "feeds" or e["id"] in seen_feed:
                    continue
                seen_feed.add(e["id"])
                hops_down.append(_feed_hop(ix, e, level))
                if e["to"] not in seen_data:
                    seen_data.add(e["to"])
                    nxt.append(e["to"])
            # what the code that writes this data does next: publishes, HTTP calls
            for root, _writer, fns in writers_ops(data):
                note_unresolved(fns)
                for f in sorted(fns):
                    for e in ix.out.get(f, []):
                        if e.get("type") == "enqueues" and not e.get("derived"):
                            key = (data, e["to"])
                            if key in seen_q:
                                continue
                            seen_q.add(key)
                            hop = _queue_hop(ix, e["to"], [f], level, data, root)
                            hops_down.append(hop)
                            for w in hop["writes"]:
                                if w["id"] not in seen_data:
                                    seen_data.add(w["id"])
                                    nxt.append(w["id"])
                        elif e.get("type") == "calls_http" and not e.get("derived"):
                            key = (data, e["to"])
                            if key in seen_http:
                                continue
                            seen_http.add(key)
                            target_fns = _closure(ix, e["to"])
                            writes = _writes_of(ix, target_fns)
                            hops_down.append({
                                "kind": "http", "depth": level, "after": ix.brief(data),
                                "trigger": ix.brief(root), "caller": ix.brief(f), "route": ix.brief(e["to"]),
                                "confidence": e.get("confidence"), "evidence": _edge_ev(e),
                                "writes": [ix.brief(w) for w in writes],
                            })
                            for w in writes:
                                if w not in seen_data:
                                    seen_data.add(w)
                                    nxt.append(w)
        frontier = nxt
        if not frontier:
            break

    # ── upstream ──
    frontier = list(start)
    seen_up = set(start)
    for level in range(1, depth + 1):
        if direction not in ("up", "both"):
            break
        nxt = []
        for data in frontier:
            for e in ix.inc.get(data, []):
                if e.get("type") != "feeds" or e["id"] in seen_feed:
                    continue
                seen_feed.add(e["id"])
                hops_up.append(_feed_hop(ix, e, level))
                if e["from"] not in seen_up:
                    seen_up.add(e["from"])
                    nxt.append(e["from"])
            # who writes it, and what starts that code from outside: a queue
            # (and its producers), an HTTP route (and its callers in other services)
            ops = writers_ops(data)
            for root, writer, _ops in ops[:MAX_WRITER_HOPS]:
                hop: dict[str, Any] = {
                    "kind": "writer", "depth": level, "into": ix.brief(data),
                    "trigger": ix.brief(root), "function": ix.brief(writer),
                }
                if ix.kind(root) == "queue" or any(
                    x.get("type") == "triggers" and ix.kind(x["from"]) == "queue" for x in ix.inc.get(root, [])
                ):
                    qs = [root] if ix.kind(root) == "queue" else sorted(
                        x["from"] for x in ix.inc.get(root, []) if x.get("type") == "triggers" and ix.kind(x["from"]) == "queue"
                    )
                    hop["queues"] = []
                    for qid in qs:
                        producers = sorted({x["from"] for x in ix.inc.get(qid, []) if x.get("type") == "enqueues" and not x.get("derived")})
                        hop["queues"].append({"queue": ix.brief(qid), "producers": [ix.brief(p) for p in producers]})
                callers = sorted({x["from"] for x in ix.inc.get(root, []) if x.get("type") == "calls_http"})
                if callers:
                    hop["http_callers"] = [ix.brief(c) for c in callers[:20]]
                    hop["http_callers_total"] = len(callers)
                hops_up.append(hop)
            if len(ops) > MAX_WRITER_HOPS:
                hops_up.append({"kind": "more_writers", "depth": level, "into": ix.brief(data),
                                "count": len(ops) - MAX_WRITER_HOPS})
        frontier = nxt
        if not frontier:
            break

    for repo_u in _unresolved_data(ix, entity, "writes"):
        unresolved["dynamic_writes"].append(repo_u)
    for item in unresolved["http"]:
        item.pop("_from", None)
    return {
        "schema": SCHEMA,
        "query": {"command": "trace", "entity": ref, "depth": depth, "direction": direction},
        "entity": entity.to_dict(ix),
        "upstream": hops_up,
        "downstream": hops_down,
        "unresolved": unresolved,
    }


def _feed_hop(ix: Index, e: dict[str, Any], level: int) -> dict[str, Any]:
    via = (e.get("via") or [{}])[0] or {}
    fns = [p for p in via.get("path") or [] if ix.kind(p) == "function"]
    return {
        "kind": "feeds",
        "depth": level,
        "from": ix.brief(e["from"]),
        "to": ix.brief(e["to"]),
        "confidence": e.get("confidence"),
        "columns": [
            {"from": str(c.get("from", "")).split(".", 1)[-1], "to": str(c.get("to", "")).split(".", 1)[-1],
             "confidence": c.get("confidence")}
            for c in e.get("column_map") or []
        ],
        "via": [ix.brief(f) for f in fns],
        "evidence": _edge_ev(e) or (ix.evidence(fns[0]) if fns else None),
    }


def _queue_hop(ix: Index, qid: str, producers: list[str], level: int, after: str, trigger: str) -> dict[str, Any]:
    consumers = []
    seen: set[str] = set()
    for e in ix.out.get(qid, []):
        if e.get("type") != "triggers":
            continue
        c = e["to"]
        if c in seen:
            continue
        seen.add(c)
        fns = _closure(ix, c)
        consumers.append({
            "consumer": ix.brief(c),
            "repo": ix.repo_of(c),
            "writes": [ix.brief(w) for w in _writes_of(ix, fns)],
            "confidence": e.get("confidence"),
        })
    # a consumer entrypoint and its handler function both hang off the queue:
    # keep the entrypoint (it already covers the handler's writes)
    eps = {c["consumer"]["id"] for c in consumers if c["consumer"]["type"] == "entrypoint"}
    handled = set()
    for ep in eps:
        handled |= _closure(ix, ep)
    consumers = [c for c in consumers if c["consumer"]["id"] in eps or c["consumer"]["id"] not in handled]
    writes: dict[str, dict[str, Any]] = {}
    for c in consumers:
        for w in c["writes"]:
            writes[w["id"]] = w
    return {
        "kind": "queue",
        "depth": level,
        "after": ix.brief(after),
        "trigger": ix.brief(trigger),
        "queue": ix.brief(qid),
        "producers": [ix.brief(p) for p in producers],
        "consumers": sorted(consumers, key=lambda c: (c["repo"] or "", c["consumer"]["label"])),
        "writes": [writes[k] for k in sorted(writes)],
    }


# ── rendering ────────────────────────────────────────────────────────────────

TXT = {
    "en": {
        "who-writes": "Who writes", "who-reads": "Who reads", "trace": "Trace",
        "writes": "writes", "reads": "reads", "via": "via {n} function", "via_n": "via {n} functions",
        "no_entry": "no entrypoint reaches it", "none_w": "No function writes it in this graph.",
        "none_r": "No function reads it in this graph.", "same_in": "Same {kind} in {n} repos: {repos}. To pick one, qualify it ({example}).",
        "unresolved": "Could change this answer", "dyn_writes": "{n} write(s) with a {family} name built at runtime in {repo} could not be resolved",
        "dyn_reads": "{n} read(s) with a {family} name built at runtime in {repo} could not be resolved",
        "nothing_unres": "Nothing unresolved in these repos could change this answer.",
        "ambiguous": "{ref!r} names {n} different things. Qualify it:", "not_found": "No table, collection or column matches {ref!r}.",
        "similar": "Similar names: {names}", "downstream": "Downstream (where the data goes)", "upstream": "Upstream (where it comes from)",
        "feeds": "feeds", "queue": "queue", "http": "HTTP", "written_by": "written by", "publishes": "publishes", "consumed_by": "consumed by",
        "nothing_down": "Nothing downstream found.", "nothing_up": "Nothing upstream found.",
        "unres_q": "{n} publish(es) to a topic only known at runtime in the functions involved",
        "unres_http": "{n} HTTP call(s) from the functions involved matched no route",
        "from_queue": "triggered by", "producers": "published by", "callers": "called over HTTP by", "table_level": "table level",
        "columns": "columns", "path": "path", "after_writing": "after writing", "then_writes": "then writes",
        "partial": "{fn} is reached from more places than listed (first {n} shown, or the walk stopped in a very dense call graph)",
        "hops": "{n} hops away:", "more_writers": "+{n} more writers of {name} (see `bizmap who-writes`)",
        "kinds": {"table": "table", "view": "view", "collection": "collection", "column": "column"},
    },
    "pt-BR": {
        "who-writes": "Quem escreve", "who-reads": "Quem lê", "trace": "Rastro",
        "writes": "escreve", "reads": "lê", "via": "via {n} função", "via_n": "via {n} funções",
        "no_entry": "nenhum ponto de entrada chega nela", "none_w": "Nenhuma função escreve nela neste grafo.",
        "none_r": "Nenhuma função lê dela neste grafo.", "same_in": "Mesma {kind} em {n} repos: {repos}. Para escolher um, qualifique ({example}).",
        "unresolved": "Pode mudar esta resposta", "dyn_writes": "{n} escrita(s) com nome de {family} montado em tempo de execução em {repo} não puderam ser resolvidas",
        "dyn_reads": "{n} leitura(s) com nome de {family} montado em tempo de execução em {repo} não puderam ser resolvidas",
        "nothing_unres": "Nada não resolvido nestes repos pode mudar esta resposta.",
        "ambiguous": "{ref!r} nomeia {n} coisas diferentes. Qualifique:", "not_found": "Nenhuma tabela, coleção ou coluna corresponde a {ref!r}.",
        "similar": "Nomes parecidos: {names}", "downstream": "Para onde vai", "upstream": "De onde vem",
        "feeds": "alimenta", "queue": "fila", "http": "HTTP", "written_by": "escrita por", "publishes": "publica", "consumed_by": "consumida por",
        "nothing_down": "Nada encontrado para onde o dado vai.", "nothing_up": "Nada encontrado de onde o dado vem.",
        "unres_q": "{n} publicação(ões) num tópico só conhecido em tempo de execução nas funções envolvidas",
        "unres_http": "{n} chamada(s) HTTP das funções envolvidas não ligaram em nenhuma rota",
        "from_queue": "disparada por", "producers": "publicado por", "callers": "chamada via HTTP por", "table_level": "nível de tabela",
        "columns": "colunas", "path": "caminho", "after_writing": "depois de escrever", "then_writes": "depois escreve",
        "partial": "{fn} é alcançada de mais lugares do que os listados (os primeiros {n}, ou o caminho parou num grafo de chamadas muito denso)",
        "hops": "a {n} passos:", "more_writers": "+{n} escritores de {name} (veja `bizmap who-writes`)",
        "kinds": {"table": "tabela", "view": "view", "collection": "coleção", "column": "coluna"},
    },
}


def _t(lang: str) -> dict[str, Any]:
    return TXT.get(lang, TXT["en"])


def _ent_line(d: dict[str, Any], T: dict[str, Any]) -> str:
    store = d["store"] if isinstance(d["store"], str) else "/".join(d["store"])
    repos = ", ".join(d["repos"])
    return f"{d['name']} ({T['kinds'].get(d['kind'], d['kind'])} · {store}{' · ' + repos if repos else ''})"


def _node_ref(b: dict[str, Any], T: dict[str, Any]) -> str:
    if b.get("type") in (*DATA_TYPES, "column"):
        return f"{b['label']} ({T['kinds'].get(b['type'], b['type'])} · {b.get('store', '')}{' · ' + b['repo'] if b.get('repo') else ''})"
    return f"{b['label']}{' [' + b['repo'] + ']' if b.get('repo') else ''}"


def render_error(err: QueryError, fmt: str, lang: str) -> str:
    T = _t(lang)
    p = err.payload
    if fmt == "json":
        return json.dumps(p, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    md = fmt == "md"
    lines = []
    if p["error"] == "ambiguous":
        lines.append(T["ambiguous"].format(ref=p["query"], n=len(p["candidates"])))
        lines.append("")
        for c in p["candidates"]:
            kind = T["kinds"].get(c["kind"], c["kind"])
            repos = ", ".join(c["repos"])
            ref = f"`{c['qualify']}`" if md else c["qualify"]
            lines.append(f"{'- ' if md else '  '}{kind} · {c['store']} · {repos or '-'}   →  {ref}")
    else:
        lines.append(T["not_found"].format(ref=p["query"]))
        if p.get("similar"):
            lines.append(T["similar"].format(names=", ".join(p["similar"])))
    return "\n".join(lines) + "\n"


def render(result: dict[str, Any], fmt: str = "text", lang: str = "en", show_path: bool = False) -> str:
    if fmt == "json":
        return json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if result["query"]["command"] == "trace":
        return _render_trace(result, fmt, lang)
    return _render_who(result, fmt, lang, show_path)


def _chain(entry: dict[str, Any], fn: dict[str, Any], T: dict[str, Any], show_path: bool) -> str:
    if show_path:
        return " → ".join(p["label"] for p in entry["path"])
    parts = [entry["entrypoint"]["label"]]
    if entry.get("handler") and entry["handler"]["id"] != fn["id"]:
        parts.append(entry["handler"]["label"])
    if entry["via"]:
        parts.append("(" + (T["via"] if entry["via"] == 1 else T["via_n"]).format(n=entry["via"]) + ")")
    if entry["entrypoint"]["id"] != fn["id"]:
        parts.append(fn["label"])
    return " → ".join(parts)


def _render_who(r: dict[str, Any], fmt: str, lang: str, show_path: bool) -> str:
    T = _t(lang)
    md = fmt == "md"
    verb = T["writes"] if r["access"] == "writes" else T["reads"]
    ent = r["entity"]
    title = f"{T[r['query']['command']]} {_ent_line(ent, T)}"
    out = [f"### {title}" if md else title, ""]
    if len(ent["repos"]) > 1:
        example = next((n["id"] for n in ent["nodes"]), "")
        out += [T["same_in"].format(kind=T["kinds"].get(ent["kind"], ent["kind"]), n=len(ent["repos"]), repos=", ".join(ent["repos"]),
                                    example=example), ""]
    if not r["results"]:
        out.append(T["none_w"] if r["access"] == "writes" else T["none_r"])
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in r["results"]:
        by_repo[row["repo"] or "-"].append(row)
    for repo in sorted(by_repo):
        out.append(f"**{repo}**" if md else repo)
        if md:
            out.append("")
            out.append(f"| {T['path'].capitalize()} | {verb} | conf. | file:line |")
            out.append("| --- | --- | --- | --- |")
        for row in by_repo[repo]:
            fn = row["function"]
            what = ent["name"] + (" (" + ", ".join(row["columns"]) + ")" if row["columns"] and ent["kind"] != "column" else "")
            entries = row["entrypoints"] or [None]
            for entry in entries:
                chain = _chain(entry, fn, T, show_path) if entry else f"{fn['label']}  ({T['no_entry']})"
                conf = entry["confidence"] if entry else row["confidence"]
                if md:
                    out.append(f"| {_md(chain)} | {_md(what)} | {conf} | `{row['evidence'] or ''}` |")
                else:
                    out.append(f"  {chain}  {verb} {what}  [{conf}]")
                    out.append(f"      {row['evidence'] or ''}")
            if row.get("entrypoints_partial"):
                note = T["partial"].format(fn=fn["label"], n=MAX_ROOTS)
                out.append(f"| {_md(note)} | | | |" if md else f"      ({note})")
        out.append("")
    out += _render_unresolved_who(r, T, md)
    return "\n".join(out).rstrip("\n") + "\n"


def _md(s: str) -> str:
    return s.replace("|", "\\|")


def _render_unresolved_who(r: dict[str, Any], T: dict[str, Any], md: bool) -> list[str]:
    out = [f"**{T['unresolved']}**" if md else f"{T['unresolved']}:"]
    if not r["unresolved"]:
        out.append(("- " if md else "  ") + T["nothing_unres"])
        return out
    for u in r["unresolved"]:
        family = T["kinds"].get(u["kind"].removeprefix("dynamic_"), u["kind"])
        key = "dyn_writes" if u["access"] == "writes" else "dyn_reads"
        out.append(("- " if md else "  ") + T[key].format(n=u["count"], family=family, repo=u["repo"] or "-")
                   + ": " + ", ".join(f["label"] for f in u["functions"]))
    return out


def _render_trace(r: dict[str, Any], fmt: str, lang: str) -> str:
    T = _t(lang)
    md = fmt == "md"
    ent = r["entity"]
    title = f"{T['trace']} {_ent_line(ent, T)}"
    out = [f"### {title}" if md else title, ""]
    b = "- " if md else "  "
    sub = "  - " if md else "    "
    for key, hops, none in (("downstream", r["downstream"], "nothing_down"), ("upstream", r["upstream"], "nothing_up")):
        if r["query"]["direction"] not in ("both", "down" if key == "downstream" else "up"):
            continue
        out.append(f"**{T[key]}**" if md else T[key] + ":")
        if not hops:
            out.append(b + T[none])
        last = 1
        for h in sorted(hops, key=lambda x: x["depth"]):  # stable: insertion order inside a level
            if h["depth"] != last:
                last = h["depth"]
                out.append(("- " if md else "  ") + T["hops"].format(n=last))
            ind = "  " * (h["depth"] - 1)
            if h["kind"] == "feeds":
                cols = (", ".join(f"{c['from']}→{c['to']}" for c in h["columns"][:6]) + (" …" if len(h["columns"]) > 6 else "")) if h["columns"] else T["table_level"]
                via = (" via " + ", ".join(v["label"] for v in h["via"])) if h["via"] else ""
                out.append(f"{ind}{b}{T['feeds']}: {_node_ref(h['from'], T)} → {_node_ref(h['to'], T)}  [{h['confidence']}]  {cols}{via}")
            elif h["kind"] == "queue":
                prods = ", ".join(p["label"] for p in h["producers"])
                out.append(f"{ind}{b}{T['queue']} {h['queue']['name']}: {h['trigger']['label']} → {prods} {T['publishes']}")
                for c in h["consumers"]:
                    w = ("; " + T["then_writes"] + " " + ", ".join(_node_ref(x, T) for x in c["writes"])) if c["writes"] else ""
                    out.append(f"{ind}{sub}{T['consumed_by']} {c['repo'] or '-'}: {c['consumer']['label']}  [{c['confidence']}]{w}")
            elif h["kind"] == "http":
                w = ("; " + T["then_writes"] + " " + ", ".join(_node_ref(x, T) for x in h["writes"])) if h["writes"] else ""
                out.append(f"{ind}{b}{T['http']}: {h['caller']['label']} → {_node_ref(h['route'], T)}  [{h['confidence']}]{w}")
            elif h["kind"] == "more_writers":
                out.append(f"{ind}{b}" + T["more_writers"].format(n=h["count"], name=h["into"]["label"]))
            elif h["kind"] == "writer":
                line = f"{ind}{b}{T['written_by']} {h['trigger']['label']}"
                if h["function"]["id"] != h["trigger"]["id"]:
                    line += f" → {h['function']['label']}"
                if h["trigger"].get("repo"):
                    line += f" [{h['trigger']['repo']}]"
                out.append(line + f"  ({_node_ref(h['into'], T)})")
                for q in h.get("queues") or []:
                    prods = ", ".join(_node_ref(p, T) for p in q["producers"]) or "?"
                    out.append(f"{ind}{sub}{T['from_queue']} {q['queue']['label']}; {T['producers']} {prods}")
                if h.get("http_callers"):
                    out.append(f"{ind}{sub}{T['callers']} " + ", ".join(_node_ref(c, T) for c in h["http_callers"]))
        out.append("")
    out.append(f"**{T['unresolved']}**" if md else T["unresolved"] + ":")
    out += [b + line for line in _trace_unresolved(r["unresolved"], T)]
    return "\n".join(out).rstrip("\n") + "\n"


def _trace_unresolved(u: dict[str, Any], T: dict[str, Any]) -> list[str]:
    lines = []
    for d in u["dynamic_writes"]:
        family = T["kinds"].get(d["kind"].removeprefix("dynamic_"), d["kind"])
        lines.append(T["dyn_writes"].format(n=d["count"], family=family, repo=d["repo"] or "-"))
    if u["queues"]:
        lines.append(T["unres_q"].format(n=len(u["queues"])))
    if u["http"]:
        lines.append(T["unres_http"].format(n=len(u["http"])))
    return lines or [T["nothing_unres"]]


def unresolved_footer(result: dict[str, Any], lang: str = "en") -> list[str]:
    """The answer's closing section as the text output writes it: the title
    ("Could change this answer:") and one line per item, without indentation.
    ``bizmap export`` shows these same lines under each answer."""
    T = _t(lang)
    if result["query"]["command"] == "trace":
        lines = _trace_unresolved(result["unresolved"], T)
    else:
        lines = [x.strip() for x in _render_unresolved_who(result, T, False)[1:]]
    return [T["unresolved"] + ":", *lines]
