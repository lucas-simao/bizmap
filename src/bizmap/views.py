"""System views: the architecture (services ↔ stores ↔ topics) and the flow of
each event source (a topic, a Firestore trigger, a scheduled job), end to end.

Both are computed once from the graph, with a deterministic layered layout
(same graph → same drawing), and used by:

- the HTML export (``#/architecture``, ``#/queues``, ``#/topic/<name>``),
  drawn with the Cytoscape already inlined there, positions taken from here;
- ``bizmap export --format mermaid`` and ``bizmap report --mermaid``
  (``flowchart LR`` blocks for GitHub);
- the PR comment (``bizmap impact``) when a changed function publishes,
  consumes or handles one of these events.

Architecture
    Layers, left to right: front ends → APIs / services → topics → workers /
    functions → stores. Data is grouped by store (Postgres, Firestore…), one
    box per store with its table / collection count, never one node per table.
    Edges are aggregated per pair (service → store R / W / RW, service →
    topic, topic → consumer, front end → API over HTTP, store → function for a
    Firestore trigger) with the number of accesses behind them; an edge whose
    accesses are all heuristic is drawn dashed.

Event flow
    What the publisher reads → publishing function → topic → consumers → what
    they write → the next events (a Firestore trigger on a written collection,
    a topic a consumer publishes to), up to 3 hops. With the message fields
    when the scan saw them (published vs read by the consumer; read fields no
    publisher sends are flagged) and warnings: no consumer, no publisher,
    publisher and consumer in different services.

Tests and helpers (``is_hidden`` files) are left out, as on the HTML start
screen with "Show details" off.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bizmap.query import Index, _closure, _rank, _roots, _split

LAYERS = ("front", "api", "topic", "worker", "store")
ROLE_LAYER = {"web": 0, "api": 1, "lib": 1, "worker": 3}
MAX_HOPS = 3  # event hops in a flow (the topic itself is the first)
MAX_ITEMS = 30  # functions listed behind one aggregated edge
MAX_EXPAND = 40  # a store expands into one node per entity up to this many
MAX_FLOW_DATA = 8  # entities per column of a flow before "+N more"
COL_W = 240
ROW_H = 72
QUEUE_KINDS = ("pubsub", "topic", "celery_task", "celery", "rq", "cloud_task", "sqs", "sns", "kafka", "rabbitmq", "queue")


# ── context ──────────────────────────────────────────────────────────────────


@dataclass
class Ctx:
    ix: Index
    services: list[str]
    roles: list[str]  # web / api / worker / lib, per service
    svc: Callable[[str], int]
    hidden: Callable[[str], bool]
    ent_of: dict[str, str]  # data node id (and its columns) -> entity ref
    ents: dict[str, dict[str, Any]]  # ref -> {"name", "kind", "store"}
    link: Callable[[str], list[Any]] = field(default=lambda nid: [-1, ""])  # function / entrypoint -> [book index, kind]


def context_from_data(data: dict[str, Any], svc_of: dict[str, str | None] | None = None) -> Ctx:
    """A context for the CLI (report, export mermaid, impact): services from
    the graph's repos, roles guessed from the entrypoints."""
    from bizmap.export.home import is_hidden
    from bizmap.export.services import assign_services

    ix = Index.from_dict(data)
    svc_of = svc_of if svc_of is not None else assign_services(data)
    services = sorted({s for s in svc_of.values() if s})
    svc_ix = {s: i for i, s in enumerate(services)}

    def svc(nid: str) -> int:
        s = svc_of.get(nid)
        if s is None:
            s = ix.repo_of(nid)
        return svc_ix.get(s, -1) if s else -1

    hidden_cache: dict[str, bool] = {}

    def hidden(nid: str) -> bool:
        h = hidden_cache.get(nid)
        if h is None:
            ev = (ix.nodes.get(nid) or {}).get("evidence")
            h = hidden_cache[nid] = is_hidden(str(ev.get("file") or "") if isinstance(ev, dict) else "")
        return h

    ent_of, ents = entities_of(ix)
    return Ctx(ix, services, guess_roles(ix, services, svc), svc, hidden, ent_of, ents)


def entities_of(ix: Index, refs: dict[tuple[str, ...], str] | None = None) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Data nodes grouped as the query engine groups them (same kind, name and
    database across repos) -> (node id -> ref, ref -> meta). ``refs`` gives the
    page's own keys for a group (sorted node ids -> ref)."""
    from bizmap.query import _group

    ent_of: dict[str, str] = {}
    ents: dict[str, dict[str, Any]] = {}
    for name in sorted(ix.by_name):
        for g in _group(ix, ix.by_name[name]):
            kind = str(ix.nodes[g[0]].get("type"))
            if _split(g[0])[1].split(":", 1)[-1] == "unknown":
                continue
            ref = (refs or {}).get(tuple(sorted(g))) or (g[0] if len(g) == 1 else f"{kind}:{ix.label(g[0])}")
            stores = sorted({ix.store(n) for n in g})
            ents[ref] = {"name": ix.label(g[0]), "kind": kind, "store": " / ".join(stores)}
            for nid in g:
                ent_of[nid] = ref
    for cid, node in ix.nodes.items():
        if node.get("type") == "column":
            t = ix.table_of_column(cid)
            if t and t in ent_of:
                ent_of[cid] = ent_of[t]
    return ent_of, ents


def _ep_kind(node: dict[str, Any]) -> str:
    k = str(node.get("kind") or "other")
    if k == "http" or k.startswith("firebase_http") or k == "firebase_call":
        return "http"
    if k == "firestore" or "trigger" in k:
        return "trigger"
    if k in ("scheduled_job", "job", "cron") or "cron" in k or "schedule" in k:
        return "cron"
    return "queue"


def guess_roles(ix: Index, services: list[str], svc: Callable[[str], int]) -> list[str]:
    """web / api / worker / lib per service, the HTML side menu's rule: a JS/TS
    code base that calls APIs is a front end when it has UI files or no routes
    of its own; else routes make an API, consumers / triggers / jobs a worker.
    The HTML and the Mermaid output both use this; the functions of a front end
    or an API that run on a trigger, a queue or a schedule get their own
    "· functions" box (see :func:`worker_parts`)."""
    from bizmap.export.book import FRONT_EXT, SCREEN_DIRS, UI_EXT

    routes: dict[int, int] = defaultdict(int)
    jobs: dict[int, int] = defaultdict(int)
    calls_out: dict[int, int] = defaultdict(int)
    files: dict[int, list[str]] = defaultdict(list)
    for nid, node in ix.nodes.items():
        s = svc(nid)
        if node.get("type") == "entrypoint":
            if _ep_kind(node) == "http":
                routes[s] += 1
            else:
                jobs[s] += 1
        elif node.get("type") == "function":
            ev = node.get("evidence")
            f = str(ev.get("file") or "") if isinstance(ev, dict) else ""
            if f:
                files[s].append(f)
            if node.get("unresolved_http"):
                calls_out[s] += 1
    for edge in ix.out.edges:
        if edge.get("type") == "calls_http":
            calls_out[svc(edge["from"])] += 1
    for u in ix.unresolved_http or []:
        if isinstance(u, dict) and u.get("from"):
            calls_out[svc(str(u["from"]))] += 1
    roles = []
    for s in range(len(services)):
        fs = files[s]
        front = sum(1 for f in fs if f.endswith(FRONT_EXT))
        ui = any(f.endswith(UI_EXT) for f in fs) or any(f"/{d}/" in f"/{f}" for f in fs for d in SCREEN_DIRS[:3] + ("components",))
        if fs and front / len(fs) >= 0.6 and calls_out[s] and (ui or not routes[s]):
            roles.append("web")
        elif routes[s]:
            roles.append("api")
        elif jobs[s]:
            roles.append("worker")
        else:
            roles.append("lib")
    return roles


def worker_parts(ctx: Ctx) -> set[str]:
    """Entrypoints and functions of a front end / API / library that run on a
    trigger, a queue or a schedule (and nothing its routes or screens reach):
    they are drawn in the workers column as "<service> · functions"."""
    ix = ctx.ix
    out: set[str] = set()
    for s, role in enumerate(ctx.roles):
        if role == "worker":
            continue
        event_eps, main_starts = [], []
        for nid, node in ix.nodes.items():
            if ctx.svc(nid) != s or ctx.hidden(nid):
                continue
            if node.get("type") == "entrypoint":
                (main_starts if _ep_kind(node) == "http" else event_eps).append(nid)
        if not event_eps:
            continue
        from bizmap.export.book import is_ui_file

        for nid, node in ix.nodes.items():
            if node.get("type") == "function" and ctx.svc(nid) == s:
                ev = node.get("evidence")
                if is_ui_file(str(ev.get("file") or "") if isinstance(ev, dict) else ""):
                    main_starts.append(nid)
        main: set[str] = set()
        for nid in main_starts:
            main |= _closure(ix, nid)
        for ep in event_eps:
            out.add(ep)
            out |= {f for f in _closure(ix, ep) if f not in main and ctx.svc(f) == s}
    return out


def _fn_item(ctx: Ctx, nid: str) -> list[Any]:
    """[label, service, book index, kind, evidence]"""
    b = ctx.link(nid)
    return [ctx.ix.label(nid), ctx.svc(nid), b[0], ctx.ix.kind(nid), ctx.ix.evidence(nid) or ""]


def _width(n: int, top: int) -> float:
    return round(1.5 + 4.5 * math.log1p(n) / math.log1p(max(top, 1)), 2)


# ── layered layout ───────────────────────────────────────────────────────────


def layout(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *, col_w: int = COL_W, row_h: int = ROW_H,
    stagger: tuple[int, ...] = (), ref_layer: int | None = None,
) -> None:
    """Sets ``x`` / ``y`` on every node from its ``layer``: one column per
    non-empty layer, nodes ordered by the barycenter of their neighbours (a few
    sweeps), ties by label. Deterministic: no randomness, stable sorts.

    ``stagger``: layers moved half a row off ``ref_layer``'s rows, so a straight
    line from a node of ``ref_layer`` to a later column passes between their boxes."""
    by_layer: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        by_layer[n["layer"]].append(n)
    layers = sorted(by_layer)
    for lay in layers:
        by_layer[lay].sort(key=lambda n: (n.get("order", 0), n["label"].lower(), n["id"]))
    pos = {n["id"]: i for lay in layers for i, n in enumerate(by_layer[lay])}
    nbrs: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e["from"] != e["to"]:
            nbrs[e["from"]].append(e["to"])
            nbrs[e["to"]].append(e["from"])
    layer_of = {n["id"]: n["layer"] for n in nodes}
    for sweep in range(4):
        seq = layers if sweep % 2 == 0 else list(reversed(layers))
        for lay in seq:
            col = by_layer[lay]
            if len(col) < 2:
                continue
            bary: dict[str, float] = {}
            for n in col:
                ps = [pos[m] for m in nbrs[n["id"]] if layer_of.get(m, lay) != lay]
                bary[n["id"]] = sum(ps) / len(ps) if ps else pos[n["id"]]
            col.sort(key=lambda n: (round(bary[n["id"]], 6), n.get("order", 0), n["label"].lower(), n["id"]))
            for i, n in enumerate(col):
                pos[n["id"]] = i
    tallest = max((len(by_layer[lay]) for lay in layers), default=0)
    ref_top = (tallest - len(by_layer[ref_layer])) * row_h / 2 if ref_layer in by_layer else None
    for c, lay in enumerate(layers):
        col = by_layer[lay]
        top = (tallest - len(col)) * row_h / 2
        if ref_top is not None and lay in stagger and (top - ref_top) % row_h == 0:
            top += row_h / 2
        for i, n in enumerate(col):
            n["x"] = c * col_w
            n["y"] = round(top + i * row_h)


# ── architecture ─────────────────────────────────────────────────────────────


def architecture(ctx: Ctx) -> dict[str, Any]:
    """Nodes (services, topics, stores) and aggregated edges, laid out."""
    ix = ctx.ix
    nodes: dict[str, dict[str, Any]] = {}
    acc: dict[tuple[str, str, str], dict[str, Any]] = {}
    parts = worker_parts(ctx)

    def snode(s: int) -> str:
        nid = f"s{s}"
        if nid not in nodes:
            role = ctx.roles[s] if s < len(ctx.roles) else "lib"
            nodes[nid] = {"id": nid, "kind": "service", "role": role, "label": ctx.services[s], "svc": s,
                          "layer": ROLE_LAYER.get(role, 1), "order": 0}
        return nid

    def owner(x: str) -> str:
        """The box of a function / entrypoint: its service, or its service's
        "· functions" box when it runs on an event inside a front end / API."""
        s = ctx.svc(x)
        if x not in parts:
            return snode(s)
        nid = f"s{s}f"
        if nid not in nodes:
            nodes[nid] = {"id": nid, "kind": "service", "role": "worker", "label": f"{ctx.services[s]} · functions", "svc": s,
                          "layer": ROLE_LAYER["worker"], "order": 0}
        return nid

    def add(a: str, b: str, rel: str, rw: int, rank: int, item: list[Any] | None, key: str) -> None:
        e = acc.setdefault((a, b, rel), {"from": a, "to": b, "rel": rel, "rw": 0, "n": 0, "best": 0, "items": {}, "_keys": set()})
        e["rw"] |= rw
        e["best"] = max(e["best"], rank)
        if key not in e["_keys"]:
            e["_keys"].add(key)
            e["n"] += 1
        if item is not None:
            e["items"].setdefault(key, item)

    # stores: one box per store, with its entities
    store_ents: dict[str, list[str]] = defaultdict(list)
    for ref, meta in sorted(ctx.ents.items()):
        store_ents[meta["store"]].append(ref)
    ent_svc: dict[str, dict[int, list[int]]] = defaultdict(dict)  # ref -> svc -> [rw, best rank]
    for edge in ix.out.edges:
        t = edge.get("type")
        a, b = edge["from"], edge["to"]
        if t in ("reads", "writes") and not edge.get("derived") and b in ctx.ent_of and ix.kind(a) == "function":
            s = ctx.svc(a)
            if s < 0 or ctx.hidden(a):
                continue
            ref = ctx.ent_of[b]
            store = ctx.ents[ref]["store"]
            bit = 2 if t == "writes" else 1
            rank = _rank(edge.get("confidence"))
            cell = ent_svc[ref].setdefault(s, [0, 0])
            cell[0] |= bit
            cell[1] = max(cell[1], rank)
            sid = f"db:{store}"
            add(owner(a), sid, "rw", bit, rank, [*_fn_item(ctx, a), ctx.ents[ref]["name"], bit], f"{a}\x00{ref}")
    # topics: publishers and consumers
    for qid, qnode in sorted(ix.nodes.items()):
        if qnode.get("type") != "queue":
            continue
        tid = f"q:{qid}"
        pubs, subs = set(), set()
        for edge in ix.inc.get(qid, []):
            if edge.get("type") == "enqueues" and not edge.get("derived") and ctx.svc(edge["from"]) >= 0 and not ctx.hidden(edge["from"]):
                pubs.add(ctx.svc(edge["from"]))
                add(owner(edge["from"]), tid, "pub", 0, _rank(edge.get("confidence")), _fn_item(ctx, edge["from"]), edge["from"])
        for edge in ix.out.get(qid, []):
            if edge.get("type") == "triggers" and ctx.svc(edge["to"]) >= 0 and not ctx.hidden(edge["to"]):
                subs.add(ctx.svc(edge["to"]))
                add(tid, owner(edge["to"]), "sub", 0, _rank(edge.get("confidence")), _fn_item(ctx, edge["to"]), edge["to"])
        if not pubs and not subs:
            continue
        status = "ok" if pubs and subs else "no_consumer" if pubs else "no_publisher"
        nodes[tid] = {"id": tid, "kind": "topic", "label": str(qnode.get("name") or ix.label(qid)), "key": source_key(ix, qid),
                      "layer": 2, "status": status, "order": 0}
    # Firestore triggers: the collection's store → the function's service
    for eid, enode in sorted(ix.nodes.items()):
        if enode.get("type") != "entrypoint" or _ep_kind(enode) != "trigger" or not enode.get("collection"):
            continue
        s = ctx.svc(eid)
        if s < 0 or ctx.hidden(eid):
            continue
        ref = _collection_ref(ctx, str(enode["collection"]), eid)
        store = ctx.ents[ref]["store"] if ref else "Firestore"
        add(f"db:{store}", owner(eid), "trigger", 0, _rank(enode.get("confidence") or "exact"),
            [*_fn_item(ctx, eid), str(enode["collection"]), 0], eid)
        if f"db:{store}" not in nodes:
            store_ents.setdefault(store, [])
    # front end → API over HTTP
    for edge in ix.out.edges:
        if edge.get("type") != "calls_http" or edge.get("derived"):
            continue
        a, b = ctx.svc(edge["from"]), ctx.svc(edge["to"])
        if a < 0 or b < 0 or a == b or ctx.hidden(edge["from"]):
            continue
        add(owner(edge["from"]), owner(edge["to"]), "http", 0, _rank(edge.get("confidence")), [*_fn_item(ctx, edge["from"]), ix.label(edge["to"]), 0],
            f"{edge['from']}\x00{edge['to']}")
    for store, refs in sorted(store_ents.items()):
        sid = f"db:{store}"
        used = any(k[1] == sid or k[0] == sid for k in acc)
        if not used:
            continue
        kinds: dict[str, int] = defaultdict(int)
        for ref in refs:
            kinds[ctx.ents[ref]["kind"]] += 1
        expand = [[ref, ctx.ents[ref]["name"], ctx.ents[ref]["kind"],
                   sorted([s, v[0], 1 if v[1] <= 2 else 0] for s, v in ent_svc[ref].items())]
                  for ref in sorted(refs, key=lambda r: ctx.ents[r]["name"].lower())]
        nodes[sid] = {"id": sid, "kind": "store", "label": store, "layer": 4, "order": 0, "n": len(refs),
                      "count": dict(sorted(kinds.items())), "ents": expand if len(expand) <= MAX_EXPAND else [],
                      "all": [[e[0], e[1], e[2]] for e in expand]}
    # services with nothing drawn stay out (a library nobody links to)
    linked = {k[0] for k in acc} | {k[1] for k in acc}
    nodes = {k: v for k, v in nodes.items() if k in linked}
    top = max((e["n"] for e in acc.values()), default=1)
    edges = []
    for (a, b, rel), e in sorted(acc.items()):
        if a not in nodes or b not in nodes:
            continue
        items = [e["items"][k] for k in sorted(e["items"], key=lambda k: (str(e["items"][k][0]).lower(), k))]
        edges.append({
            "id": f"{a}>{b}>{rel}", "from": a, "to": b, "rel": rel, "rw": e["rw"], "n": e["n"],
            "guess": 1 if e["best"] <= 2 else 0, "w": _width(e["n"], top),
            "items": items[:MAX_ITEMS], "more": max(0, len(items) - MAX_ITEMS),
        })
    out_nodes = sorted(nodes.values(), key=lambda n: n["id"])
    layout(out_nodes, edges, stagger=(2, 3), ref_layer=1)
    for n in out_nodes:
        n.pop("order", None)
    return {"layers": list(LAYERS), "nodes": out_nodes, "edges": edges}


def _collection_ref(ctx: Ctx, name: str, near: str) -> str | None:
    """The entity of the collection a trigger listens on (same repo first)."""
    cands = [nid for nid in ctx.ix.by_name.get(name.lower(), []) if ctx.ix.kind(nid) == "collection" and nid in ctx.ent_of]
    repo = ctx.ix.repo_of(near)
    cands.sort(key=lambda nid: (ctx.ix.repo_of(nid) != repo, nid))
    return ctx.ent_of[cands[0]] if cands else None


# ── event sources and their flows ────────────────────────────────────────────


def source_key(ix: Index, qid: str) -> str:
    """URL key of a topic: its name, or the node id when two queues share it."""
    name = str(ix.nodes[qid].get("name") or ix.label(qid))
    same = [q for q, n in ix.nodes.items() if n.get("type") == "queue" and str(n.get("name") or ix.label(q)) == name]
    return name if len(same) <= 1 else qid


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _visible(ctx: Ctx, nid: str) -> bool:
    return not ctx.hidden(nid)


def _closure_of(ctx: Ctx, start: str) -> set[str]:
    return {f for f in _closure(ctx.ix, start) if _visible(ctx, f)} | {start}


def _data_of(ctx: Ctx, fns: set[str], etype: str) -> dict[str, int]:
    """entity ref -> best rank of ``etype`` (reads / writes) by ``fns``."""
    out: dict[str, int] = {}
    for f in fns:
        for e in ctx.ix.out.get(f, []):
            if e.get("type") == etype and not e.get("derived") and e["to"] in ctx.ent_of:
                ref = ctx.ent_of[e["to"]]
                out[ref] = max(out.get(ref, 0), _rank(e.get("confidence")))
    return out


def _publishes(ctx: Ctx, fns: set[str]) -> list[str]:
    return sorted({e["to"] for f in fns for e in ctx.ix.out.get(f, [])
                   if e.get("type") == "enqueues" and not e.get("derived") and ctx.ix.kind(e["to"]) == "queue"})


@dataclass
class _Src:
    key: str
    kind: str  # topic | firestore | cron
    label: str
    node: str  # queue id, collection ref or job entrypoint
    detail: str = ""


def event_sources(ctx: Ctx) -> list[_Src]:
    ix = ctx.ix
    out: list[_Src] = []
    for qid, node in sorted(ix.nodes.items()):
        if node.get("type") == "queue":
            out.append(_Src(source_key(ix, qid), "topic", str(node.get("name") or ix.label(qid)), qid, str(node.get("kind") or "")))
    fs: dict[str, list[str]] = defaultdict(list)
    for eid, node in sorted(ix.nodes.items()):
        if node.get("type") != "entrypoint" or ctx.hidden(eid):
            continue
        k = _ep_kind(node)
        if k == "trigger" and node.get("collection"):
            fs[str(node["collection"])].append(eid)
        elif k == "cron":
            label = str(node.get("symbol") or node.get("handler") or ix.label(eid))
            out.append(_Src(f"cron:{label}", "cron", label, eid, str(node.get("schedule") or "")))
    for coll, eids in sorted(fs.items()):
        events = sorted({str(ix.nodes[e].get("event") or ix.nodes[e].get("method") or "").lower() for e in eids} - {""})
        out.append(_Src(f"firestore:{coll}", "firestore", coll, coll, ", ".join(events)))
    return out


def _consumers_of(ctx: Ctx, src: _Src) -> list[str]:
    """Entrypoints (or functions) started by the source, an entrypoint winning
    over the handler it already covers."""
    ix = ctx.ix
    if src.kind == "topic":
        got = sorted({e["to"] for e in ix.out.get(src.node, []) if e.get("type") == "triggers" and _visible(ctx, e["to"])})
    elif src.kind == "firestore":
        got = sorted(eid for eid, n in ix.nodes.items() if n.get("type") == "entrypoint" and _ep_kind(n) == "trigger"
                     and str(n.get("collection") or "") == src.node and _visible(ctx, eid))
    else:
        got = [src.node]
    eps = [c for c in got if ix.kind(c) == "entrypoint"]
    covered: set[str] = set()
    for ep in eps:
        covered |= _closure(ix, ep) - {ep}
    return [c for c in got if c in eps or c not in covered]


def _publishers_of(ctx: Ctx, src: _Src) -> list[str]:
    ix = ctx.ix
    if src.kind == "topic":
        return sorted({e["from"] for e in ix.inc.get(src.node, []) if e.get("type") == "enqueues" and not e.get("derived")
                       and _visible(ctx, e["from"])})
    if src.kind == "firestore":
        writers = set()
        for nid in _collection_nodes(ctx, src.node):
            for e in ix.inc.get(nid, []):
                if e.get("type") == "writes" and not e.get("derived") and ix.kind(e["from"]) == "function" and _visible(ctx, e["from"]):
                    writers.add(e["from"])
        return sorted(writers)
    return []


def _collection_nodes(ctx: Ctx, name: str) -> list[str]:
    """Every node of the collection ``name`` (each repo's), columns included."""
    base = [nid for nid in ctx.ix.by_name.get(name.lower(), []) if ctx.ix.kind(nid) == "collection"]
    out = list(base)
    for nid in base:
        out.extend(c for c in ctx.ix.columns.get((ctx.ix.repo_of(nid) if _split(nid)[0] else None, name.lower()), [])
                   if ctx.ix.table_of_column(c) == nid)
    return out


def _fields(ctx: Ctx, src: _Src, pubs: list[str], cons_fns: set[str]) -> dict[str, list[str]] | None:
    ix = ctx.ix
    published: set[str] = set()
    read: set[str] = set()
    if src.kind == "topic":
        for p in pubs:
            for e in ix.out.get(p, []):
                if e.get("type") == "enqueues" and e["to"] == src.node:
                    published.update(str(f) for f in e.get("fields") or [])
            for d in ix.nodes[p].get("dataflow_out") or []:
                if str(d.get("topic")) == src.label:
                    published.update((d.get("fields") or {}).keys())
        for f in cons_fns:
            for d in (ix.nodes.get(f) or {}).get("dataflow_in") or []:
                if str(d.get("topic")) == src.label:
                    read.update((d.get("fields") or {}).keys())
    elif src.kind == "firestore":
        for p in pubs:
            for e in ix.out.get(p, []):
                if e.get("type") == "writes" and e["to"] in ctx.ent_of and ctx.ents[ctx.ent_of[e["to"]]]["name"] == src.node:
                    published.update(str(f) for f in e.get("fields") or [])
        for f in cons_fns:
            for e in ix.out.get(f, []):
                if e.get("type") == "reads" and e["to"] in ctx.ent_of and ctx.ents[ctx.ent_of[e["to"]]]["name"] == src.node:
                    read.update(str(x) for x in e.get("fields") or [])
    if not published and not read:
        return None
    pub_norm = {_norm(f) for f in published}
    missing = sorted(f for f in read if published and _norm(f) not in pub_norm)
    return {"pub": sorted(published), "read": sorted(read), "missing": missing}


def _root_ids(ctx: Ctx, fn: str) -> list[str]:
    """Entrypoints that reach ``fn`` (its route, job or consumer)."""
    rows, _partial = _roots(ctx.ix, fn)
    return sorted({r["root"] for r in rows if ctx.ix.kind(r["root"]) == "entrypoint"})


def _entry_labels(ctx: Ctx, fn: str) -> list[str]:
    return sorted({ctx.ix.label(r) for r in _root_ids(ctx, fn)})[:3]


def event_flow(ctx: Ctx, src: _Src, by_node: dict[str, _Src]) -> dict[str, Any]:
    pubs = _publishers_of(ctx, src)
    cons = _consumers_of(ctx, src)
    cons_fns: set[str] = set()
    for c in cons:
        cons_fns |= _closure_of(ctx, c)
    # what the publishers read: their route's (or their own) closure
    pub_reads: dict[str, int] = {}
    publishers = []
    for p in pubs:
        roots = _root_ids(ctx, p) or [p]
        fns: set[str] = set()
        for r in roots:
            fns |= _closure_of(ctx, r)
        reads = _data_of(ctx, fns, "reads")
        for ref, rank in reads.items():
            pub_reads[ref] = max(pub_reads.get(ref, 0), rank)
        publishers.append({"fn": _fn_item(ctx, p), "entry": _entry_labels(ctx, p), "reads": sorted(reads, key=lambda r: ctx.ents[r]["name"].lower())})
    consumers = []
    for c in cons:
        fns = _closure_of(ctx, c)
        writes = _data_of(ctx, fns, "writes")
        consumers.append({"fn": _fn_item(ctx, c), "writes": sorted(writes, key=lambda r: ctx.ents[r]["name"].lower())})
    pub_svcs = sorted({ctx.svc(p) for p in pubs if ctx.svc(p) >= 0})
    con_svcs = sorted({ctx.svc(c) for c in cons if ctx.svc(c) >= 0})
    warnings = []
    if src.kind == "topic" and pubs and not cons:
        warnings.append("no_consumer")
    if src.kind in ("topic", "firestore") and cons and not pubs:
        warnings.append("no_publisher")
    if pub_svcs and con_svcs and set(pub_svcs) != set(con_svcs):
        warnings.append("cross_service")
    fields = _fields(ctx, src, pubs, cons_fns)
    if fields and fields["missing"]:
        warnings.append("unpublished_fields")
    status = "no_consumer" if "no_consumer" in warnings else "no_publisher" if "no_publisher" in warnings else "ok"
    graph = _flow_graph(ctx, src, pubs, pub_reads, cons, by_node)
    return {
        "key": src.key, "node": src.node, "kind": src.kind, "label": src.label, "detail": src.detail, "status": status,
        "warnings": warnings, "pubSvcs": pub_svcs, "conSvcs": con_svcs,
        "publishers": publishers, "consumers": consumers, "fields": fields, **graph,
    }


def _flow_graph(ctx: Ctx, src: _Src, pubs: list[str], pub_reads: dict[str, int], cons: list[str],
                by_node: dict[str, _Src]) -> dict[str, Any]:
    """Columns: 0 publisher reads · 1 publishers · 2 event · 3 consumers ·
    4 their writes · then (event · consumers · writes) per next hop."""
    ix = ctx.ix
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def ent(col: int, ref: str, rank: int) -> str:
        nid = f"{col}:{ref}"
        if nid not in nodes:
            meta = ctx.ents[ref]
            nodes[nid] = {"id": nid, "kind": "data", "label": meta["name"], "sub": f"{meta['kind']} · {meta['store']}", "ref": ref, "layer": col}
        return nid

    def fn(col: int, f: str) -> str:
        nid = f"{col}:{f}"
        if nid not in nodes:
            s = ctx.svc(f)
            nodes[nid] = {"id": nid, "kind": "fn", "label": _handler_label(ctx, f), "sub": ctx.services[s] if s >= 0 else "",
                          "svc": s, "link": ctx.link(f)[0], "fk": ix.kind(f), "layer": col}
        return nid

    def event(col: int, s: _Src) -> str:
        nid = f"{col}:ev:{s.key}"
        if nid not in nodes:
            sub = {"topic": "topic", "firestore": "Firestore trigger", "cron": "schedule"}[s.kind]
            nodes[nid] = {"id": nid, "kind": "event", "ek": s.kind, "label": s.label, "sub": f"{sub}{' · ' + s.detail if s.detail else ''}",
                          "key": s.key, "layer": col}
        return nid

    def edge(a: str, b: str, rel: str, rank: int) -> None:
        k = f"{a}>{b}"
        cur = edges.get(k)
        if cur is None:
            edges[k] = {"id": k, "from": a, "to": b, "rel": rel, "guess": 1 if rank <= 2 else 0}
        elif rank > 2:
            cur["guess"] = 0

    def capped(refs: dict[str, int]) -> list[tuple[str, int]]:
        return sorted(refs.items(), key=lambda kv: ctx.ents[kv[0]]["name"].lower())[:MAX_FLOW_DATA]

    ev = event(2, src)
    for p in pubs:
        pn = fn(1, p)
        edge(pn, ev, "writes" if src.kind == "firestore" else "pub", 4)
        roots = _root_ids(ctx, p) or [p]
        fns: set[str] = set()
        for r in roots:
            fns |= _closure_of(ctx, r)
        for ref, rank in capped(_data_of(ctx, fns, "reads")):
            edge(ent(0, ref, rank), pn, "reads", rank)
    more: dict[int, int] = {}

    def hop(col: int, s: _Src, starts: list[str], depth: int, seen: set[str]) -> None:
        evn = event(col, s)
        for c in starts:
            cn = fn(col + 1, c)
            edge(evn, cn, "sub", 4)
            fns = _closure_of(ctx, c)
            writes = _data_of(ctx, fns, "writes")
            if len(writes) > MAX_FLOW_DATA:
                more[col + 2] = more.get(col + 2, 0) + len(writes) - MAX_FLOW_DATA
            for ref, rank in capped(writes):
                wn = ent(col + 2, ref, rank)
                edge(cn, wn, "writes", rank)
                if depth >= MAX_HOPS:
                    continue
                meta = ctx.ents[ref]
                nxt = by_node.get(meta["name"]) if meta["kind"] == "collection" else None
                if nxt is not None and nxt.key not in seen:
                    nn = event(col + 3, nxt)
                    edge(wn, nn, "trigger", 4)
                    hop(col + 3, nxt, _consumers_of(ctx, nxt), depth + 1, seen | {nxt.key})
            if depth >= MAX_HOPS:
                continue
            for qid in _publishes(ctx, fns):
                nxt = by_node.get(qid)
                if nxt is None or nxt.key in seen:
                    continue
                nn = event(col + 3, nxt)
                edge(cn, nn, "pub", 4)
                hop(col + 3, nxt, _consumers_of(ctx, nxt), depth + 1, seen | {nxt.key})

    hop(2, src, cons, 1, {src.key})
    if len(pub_reads) > MAX_FLOW_DATA:
        more[0] = len(pub_reads) - MAX_FLOW_DATA
    out_nodes = sorted(nodes.values(), key=lambda n: n["id"])
    out_edges = sorted(edges.values(), key=lambda e: e["id"])
    layout(out_nodes, out_edges, col_w=200, row_h=64)
    return {"nodes": out_nodes, "edges": out_edges, "more": {str(k): v for k, v in sorted(more.items())}}


def _handler_label(ctx: Ctx, nid: str) -> str:
    """A consumer entrypoint reads better as the function it runs (`on_order_placed`)."""
    if ctx.ix.kind(nid) != "entrypoint":
        return ctx.ix.label(nid)
    for e in ctx.ix.out.get(nid, []):
        if e.get("type") == "triggers" and ctx.ix.kind(e["to"]) == "function":
            return ctx.ix.label(e["to"])
    node = ctx.ix.nodes.get(nid) or {}
    return str(node.get("handler") or node.get("symbol") or ctx.ix.label(nid))


def event_flows(ctx: Ctx, only: set[str] | None = None) -> list[dict[str, Any]]:
    """Every event source's flow (or those whose publishers / consumers /
    handler closures meet the functions in ``only``)."""
    sources = event_sources(ctx)
    by_node: dict[str, _Src] = {}
    for s in sources:
        by_node[s.node] = s
    reach: set[str] | None = None
    if only is not None:  # a changed function reaches a publisher through its calls too
        reach = set(only)
        for f in only:
            if f in ctx.ix.nodes:
                reach |= _closure(ctx.ix, f)
    out = []
    for s in sources:
        if reach is not None and not _touches(ctx, s, reach):
            continue
        flow = event_flow(ctx, s, by_node)
        if not flow["publishers"] and not flow["consumers"]:
            continue
        out.append(flow)
    return out


def _touches(ctx: Ctx, s: _Src, fns: set[str]) -> bool:
    for p in _publishers_of(ctx, s):
        if p in fns:
            return True
    for c in _consumers_of(ctx, s):
        if c in fns or _closure_of(ctx, c) & fns:
            return True
    return False


def queue_rows(flows: list[dict[str, Any]]) -> list[list[Any]]:
    """[key, kind, label, detail, status, publisher count, consumer count, publisher services,
    consumer services, warnings]"""
    return [[f["key"], f["kind"], f["label"], f["detail"], f["status"], len(f["publishers"]), len(f["consumers"]),
             f["pubSvcs"], f["conSvcs"], f["warnings"]] for f in flows]


# ── Mermaid ──────────────────────────────────────────────────────────────────

_MID = re.compile(r"[^A-Za-z0-9_]")


def _mid(prefix: str, key: str, ids: dict[str, str]) -> str:
    got = ids.get(key)
    if got is None:
        base = prefix + "_" + (_MID.sub("_", key)[:40] or "n")
        got = base
        k = 2
        while got in ids.values():
            got = f"{base}_{k}"
            k += 1
        ids[key] = got
    return got


def _mtext(s: str) -> str:
    return s.replace('"', "'").replace("\n", " ")


RW_TXT = {1: "R", 2: "W", 3: "RW"}
LAYER_TITLE = {0: "Front ends", 1: "APIs / services", 2: "Topics", 3: "Workers / functions", 4: "Stores"}


def mermaid_architecture(arch: dict[str, Any]) -> str:
    """``flowchart LR`` in the HTML's column order. Mermaid ranks nodes by the
    direction of the links, so every link is written from the left column to
    the right one (a link that flows right to left, such as a Firestore
    trigger from a store to its function, keeps its arrowhead on the left
    with ``<---``), and an invisible chain (``~~~``) ties one node of each
    column to the next."""
    ids: dict[str, str] = {}
    lines = ["flowchart LR"]
    by_layer: dict[int, list[dict[str, Any]]] = defaultdict(list)
    layer_of: dict[str, int] = {}
    for n in arch["nodes"]:
        by_layer[n["layer"]].append(n)
        layer_of[n["id"]] = n["layer"]
    anchors = []
    for lay in sorted(by_layer):
        lines.append(f'  subgraph L{lay}["{LAYER_TITLE.get(lay, "")}"]')
        col = sorted(by_layer[lay], key=lambda n: (n["y"], n["id"]))
        for n in col:
            m = _mid({"service": "svc", "topic": "t", "store": "db"}.get(n["kind"], "n"), n["id"], ids)
            if n["kind"] == "store":
                count = ", ".join(f"{v} {k}{'s' if v != 1 else ''}" for k, v in n["count"].items())
                lines.append(f'    {m}[("{_mtext(n["label"])} · {count}")]')
            elif n["kind"] == "topic":
                warn = " ⚠ no consumer" if n["status"] == "no_consumer" else " ⚠ no publisher" if n["status"] == "no_publisher" else ""
                lines.append(f'    {m}>"{_mtext(n["label"])}{warn}"]')
            else:
                lines.append(f'    {m}["{_mtext(n["label"])}"]')
        anchors.append(ids[col[0]["id"]])
        lines.append("  end")
    if len(anchors) > 1:
        lines.append("  " + " ~~~ ".join(anchors))
    for e in arch["edges"]:
        lab = RW_TXT.get(e["rw"], "") if e["rel"] == "rw" else {"pub": "publishes", "sub": "consumes", "http": "HTTP",
                                                                  "trigger": "trigger"}.get(e["rel"], e["rel"])
        lab = f"{lab} {e['n']}".strip()
        a, b = ids[e["from"]], ids[e["to"]]
        if layer_of[e["from"]] > layer_of[e["to"]]:  # written left to right, arrowhead on the left
            arrow = "<-.-" if e["guess"] else "<---"
            lines.append(f'  {b} {arrow}|"{lab}"| {a}')
        else:
            arrow = "-.->" if e["guess"] else "-->"
            lines.append(f'  {a} {arrow}|"{lab}"| {b}')
    return "\n".join(lines)


def mermaid_flow(flow: dict[str, Any]) -> str:
    ids: dict[str, str] = {}
    lines = ["flowchart LR"]
    for n in sorted(flow["nodes"], key=lambda n: (n["layer"], n["y"])):
        m = _mid({"data": "d", "fn": "f", "event": "e"}[n["kind"]], n["id"], ids)
        text = _mtext(n["label"]) + (f"<br/>{_mtext(n['sub'])}" if n.get("sub") else "")
        if n["kind"] == "data":
            lines.append(f'  {m}[("{text}")]')
        elif n["kind"] == "event":
            warn = ""
            if n["key"] == flow["key"] and flow["status"] != "ok":
                warn = " ⚠ " + flow["status"].replace("_", " ")
            lines.append(f'  {m}>"{text}{warn}"]')
        else:
            lines.append(f'  {m}["{text}"]')
    for e in flow["edges"]:
        lab = {"reads": "R", "writes": "W", "pub": "publishes", "sub": "", "trigger": "trigger"}.get(e["rel"], "")
        arrow = "-.->" if e["guess"] else "-->"
        lines.append(f"  {ids[e['from']]} {arrow}{'|' + lab + '|' if lab else ''} {ids[e['to']]}")
    return "\n".join(lines)


def flow_notes(flow: dict[str, Any], services: list[str]) -> list[str]:
    """Plain-text warnings and the message shape of a flow (Markdown-safe)."""
    out = []
    if "no_consumer" in flow["warnings"]:
        out.append(f"⚠ `{flow['label']}` has no consumer in the scanned repos (one may live in a repo outside this scan).")
    if "no_publisher" in flow["warnings"]:
        out.append(f"⚠ nothing in the scanned repos publishes to `{flow['label']}`.")
    if "cross_service" in flow["warnings"]:
        a = ", ".join(services[s] for s in flow["pubSvcs"])
        b = ", ".join(services[s] for s in flow["conSvcs"])
        out.append(f"links services: {a} → {b}")
    f = flow.get("fields")
    if f:
        if f["pub"]:
            out.append("published fields: " + ", ".join(f"`{x}`" for x in f["pub"]))
        if f["read"]:
            out.append("read by the consumer: " + ", ".join(f"`{x}`" for x in f["read"]))
        if f["missing"]:
            out.append("⚠ read but never published: " + ", ".join(f"`{x}`" for x in f["missing"]))
    return out


def mermaid_markdown(ctx: Ctx, flows: list[dict[str, Any]] | None = None, arch: dict[str, Any] | None = None) -> str:
    """The architecture and every event flow as Markdown with ``mermaid`` blocks."""
    arch = arch if arch is not None else architecture(ctx)
    flows = flows if flows is not None else event_flows(ctx)
    parts = ["# Architecture", "", "```mermaid", mermaid_architecture(arch), "```", ""]
    if flows:
        parts += ["# Event sources", "", "| source | kind | status | publishers | consumers |", "|---|---|---|---:|---:|"]
        for f in flows:
            status = {"no_consumer": "no consumer in the scanned repos", "no_publisher": "no publisher in the scanned repos"}.get(f["status"], f["status"])
            parts.append(f"| `{f['label']}` | {f['kind']} | {status} | {len(f['publishers'])} | {len(f['consumers'])} |")
        parts.append("")
    for f in flows:
        parts += [f"## {f['kind']}: {f['label']}", ""]
        for note in flow_notes(f, ctx.services):
            parts.append(f"- {note}")
        parts += ["", "```mermaid", mermaid_flow(f), "```", ""]
    return "\n".join(parts)


__all__ = [
    "Ctx", "architecture", "context_from_data", "entities_of", "event_flows", "flow_notes", "layout",
    "mermaid_architecture", "mermaid_flow", "mermaid_markdown", "queue_rows",
]

