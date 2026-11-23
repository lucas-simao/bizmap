"""Data for the HTML start screen: the question first ("who writes / reads X?"),
then what BizMap found.

Answers come from the query engine of the CLI (:mod:`bizmap.query`, the
``who-writes`` / ``who-reads`` / ``trace`` code), ported line by line to the
page (``query.js``) so the HTML answers any entity without precomputing every
answer (on a 70k-node graph that would weigh more than the page). The page
gets the graph the engine reads, packed in columns (``qg`` chunk, parsed on
the first question), and prints the same ``bizmap.query/v1`` JSON the CLI
prints — the tests compare them entity by entity.

The index (read when the page opens) carries the summary strip, the findings
cards and one compact record per table / view / collection (name, kind, store,
repos, counts and its services × data matrix cells); columns (``qc``), the
map's graph (``mg``) and the findings' full lists (``fx``) are chunks.

Heuristics (documented in ``docs/export-home.md``):

- **owner** of a table: the one service whose repo declares it in DDL / a
  migration (the table node is not an ``unresolved_relation`` and has typed
  columns or a ``.sql`` / migration evidence); else the service with
  strictly more writing functions than any other; else none. Any other
  service that writes it is an **unexpected writer**.
- **hidden by default** ("Show details" → tests and helpers): functions in
  tests, mocks, fixtures, generated code and ``scripts/`` / ``tools/``.
- **orphan**: a function with its own data access that nothing reaches through
  calls / triggers from an entrypoint, a queue, a ``main`` or a UI file.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from bizmap.export.book import is_noise, is_ui_file
from bizmap.query import (
    DATA_TYPES,
    Entity,
    Index,
    QueryError,
    _data_targets,
    _group,
    _qualified,
    _rank,
    _split,
    resolve,
)

CHUNK_ANSWERS = 40  # entities per answer chunk (a table and its columns stay together)
CARD_ITEMS = 50  # items per card kept in the index (the full list is a chunk)
_SCRIPTS = re.compile(r"((^|/)(scripts?|tools)/|_test\.go$|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$)")
_MAIN = re.compile(r"(^|[./])main$")


def is_hidden(file: str) -> bool:
    """Tests, mocks, fixtures, generated code, scripts and tools: hidden until
    "Show details → tests and helpers" is on."""
    return bool(file) and (is_noise(file) or bool(_SCRIPTS.search(file)))


def build_home(
    data: dict[str, Any],
    svc_of: dict[str, str | None],
    services: list[str],
    book_index: dict[str, int],
    lang: str = "en",
    roles: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """-> (the ``home`` part of the page index, {chunk id: chunk})."""
    ix = Index.from_dict(data)
    svc_ix = {s: i for i, s in enumerate(services)}

    def svc(nid: str) -> int:
        s = svc_of.get(nid)
        if s is None:  # a data node of a repo: its repo's service when there is one
            s = ix.repo_of(nid)
        return svc_ix.get(s, -1) if s else -1

    def file_of(nid: str) -> str:
        ev = (ix.nodes.get(nid) or {}).get("evidence")
        return str(ev.get("file") or "") if isinstance(ev, dict) else ""

    hidden_cache: dict[str, bool] = {}

    def hidden(nid: str) -> bool:
        h = hidden_cache.get(nid)
        if h is None:
            h = hidden_cache[nid] = is_hidden(file_of(nid))
        return h

    # ── entities: what the CLI accepts, grouped as it groups them ──
    def ref_for(group: list[str], kind: str, name: str) -> str:
        cands = [name, f"{kind}:{name}", _qualified(ix, group)]
        if len(group) == 1:
            cands.append(group[0])
        for r in cands:
            try:
                e = resolve(ix, r)
            except QueryError:
                continue
            if sorted(e.nodes) == sorted(group):
                return r
        return group[0] if len(group) == 1 else cands[2]

    entities: list[dict[str, Any]] = []
    for name in sorted(ix.by_name):
        for g in _group(ix, ix.by_name[name]):
            entities.append(_entity(ix, g))
    for name in sorted(ix.col_by_name):
        if name.startswith("unknown.") or name.endswith(".") or not name.split(".")[-1]:
            continue
        for g in _group(ix, ix.col_by_name[name]):
            entities.append(_entity(ix, g))
    seen_refs: set[str] = set()
    for e in entities:
        e["ref"] = ref_for(e["ids"], e["kind"], e["name"])
        if e["ref"] in seen_refs:  # never two entities behind one key
            e["ref"] = e["ids"][0]
        seen_refs.add(e["ref"])

    qg = engine_data(ix, data)
    # for the page: each engine node's index in the book (its pages, service) and the hidden ones
    qg["bi"] = [book_index.get(nid, -1) for nid in qg["ids"]]
    qg["hid"] = [k for k, nid in enumerate(qg["ids"]) if nid in ix.nodes and hidden(nid)]
    chunks: dict[str, Any] = {}
    # node and edge columns in slices (qn0…, qe0…); the page joins them on the first question
    n_nodes, n_edges = len(qg["ids"]), len(qg["e"]["a"])
    for k, start in enumerate(range(0, n_nodes, NODE_SLICE)):
        chunks[f"qn{k}"] = {f: qg[f][start : start + NODE_SLICE] for f in ("ids", "ty", "nm", "ef", "el", "bi")}
    for k, start in enumerate(range(0, n_edges, EDGE_SLICE)):
        chunks[f"qe{k}"] = {f: v[start : start + EDGE_SLICE] for f, v in qg["e"].items()}
    for f in ("ids", "ty", "nm", "ef", "el", "bi", "e"):
        del qg[f]
    qg["nn"] = -(-n_nodes // NODE_SLICE)
    qg["ne"] = -(-n_edges // EDGE_SLICE)
    chunks["qg"] = qg

    # ── owners, matrix cells, findings ──
    tables = [e for e in entities if e["kind"] != "column"]
    columns = [e for e in entities if e["kind"] == "column"]
    col_of_table: dict[str, dict[str, str]] = defaultdict(dict)  # table ref -> column name -> column ref
    table_by_ids: dict[str, dict[str, Any]] = {}
    for e in tables:
        for nid in e["ids"]:
            table_by_ids[nid] = e
    for c in columns:
        t = None
        for nid in c["ids"]:
            tid = ix.table_of_column(nid)
            if tid and tid in table_by_ids:
                t = table_by_ids[tid]
                break
        if t is not None:
            c["table"] = t["ref"]
            col_of_table[t["ref"]][c["name"].rsplit(".", 1)[-1]] = c["ref"]

    dynamic: dict[int, dict[str, int]] = defaultdict(dict)  # svc -> family -> rw bits
    dyn_items: list[list[Any]] = []
    for nid, node in ix.nodes.items():
        if node.get("type") not in DATA_TYPES or _split(nid)[1].split(":", 1)[-1] != "unknown":
            continue
        family = "collection" if node.get("type") == "collection" else "table"
        fns: dict[str, int] = {}
        for edge in ix.inc.get(nid, []):
            if edge.get("type") in ("reads", "writes") and not edge.get("derived"):
                fns[edge["from"]] = fns.get(edge["from"], 0) | (2 if edge["type"] == "writes" else 1)
        for fn, rw in sorted(fns.items()):
            s = svc(fn)
            if not hidden(fn):
                dynamic[s][family] = dynamic[s].get(family, 0) | rw
            dyn_items.append(_item(ix, fn, s, hidden(fn), book_index, f"{family}:{'w' if rw & 2 else ''}{'r' if rw & 1 else ''}"))

    unexpected: list[list[Any]] = []
    multi: list[list[Any]] = []
    never_read: list[str] = []
    never_written: list[str] = []
    for e in tables:
        acc = _accesses(ix, e)
        cells: dict[int, list[int]] = {}
        writers: dict[int, int] = defaultdict(int)
        cols_by_svc: dict[int, set[str]] = defaultdict(set)
        e["nw"] = e["nr"] = e["nwh"] = e["nrh"] = 0
        for fn, (rw, rank_r, rank_w, cols) in acc.items():
            s = svc(fn)
            cell = cells.setdefault(s, [0, 0, 0, 0])
            hid = hidden(fn)
            for bit, rank in ((1, rank_r), (2, rank_w)):
                if not rw & bit:
                    continue
                if hid:
                    cell[2] |= bit
                    cell[3] = max(cell[3], rank)
                else:
                    cell[0] |= bit
                    cell[1] = max(cell[1], rank)
                key = ("nw" if bit == 2 else "nr") + ("h" if hid else "")
                e[key] += 1
            if rw & 2 and not hid:
                writers[s] += 1
                cols_by_svc[s].update(cols)
        e["m"] = sorted([s, *v] for s, v in cells.items())
        owner, why = _owner(ix, e, svc, writers)
        e["own"], e["ownw"] = owner, why
        if len(writers) > 1:
            multi.append([e["ref"], len(writers), sorted(writers)])
        if owner >= 0:
            for s in sorted(writers):
                if s == owner:
                    continue
                cols = sorted(cols_by_svc[s])
                target = col_of_table[e["ref"]].get(cols[0]) if len(cols) == 1 else None
                unexpected.append([s, e["ref"], cols, owner, why, target or e["ref"]])
        if e["nw"] and not e["nr"]:
            never_read.append(e["ref"])
        if e["nr"] and not e["nw"]:
            never_written.append(e["ref"])
    multi.sort(key=lambda x: (-x[1], x[0]))

    # orphans: own data access, nothing reaches them
    data_fns: set[str] = set()
    for nid, node in ix.nodes.items():
        if node.get("type") not in (*DATA_TYPES, "column"):
            continue
        for edge in ix.inc.get(nid, []):
            if edge.get("type") in ("reads", "writes") and not edge.get("derived") and ix.kind(edge["from"]) == "function":
                data_fns.add(edge["from"])
    # one forward walk (calls / triggers, as the engine walks them) from every
    # entrypoint, queue, `main` and UI file
    starts = [nid for nid in ix.nodes if ix.kind(nid) in ("entrypoint", "queue")
              or (ix.kind(nid) == "function" and (_MAIN.search(ix.label(nid)) or is_ui_file(file_of(nid))))]
    reached = set(starts)
    stack = list(starts)
    while stack:
        cur = stack.pop()
        for edge in ix.out.get(cur, []):
            if edge.get("type") in ("calls", "triggers") and not edge.get("derived") and edge["to"] not in reached:
                reached.add(edge["to"])
                stack.append(edge["to"])
    orphans = [_item(ix, fn, svc(fn), hidden(fn), book_index, "") for fn in sorted(data_fns) if fn not in reached]

    # cross-store copies (feeds whose ends are in different stores)
    mirrors = []
    for edge in ix.out.edges:  # every edge once (the index keeps one list, see query._Adj)
        if edge.get("type") == "feeds" and ix.store(edge["from"]) != ix.store(edge["to"]):
            mirrors.append([ix.store(edge["from"]), ix.store(edge["to"])])

    # ── summary ──
    repos = sorted({r for r in (_split(nid)[0] for nid in ix.nodes) if r}) or ([str(data.get("repo"))] if data.get("repo") else [])
    if not repos and ix.nodes:
        repos = ["app"]
    eps: dict[str, int] = defaultdict(int)
    for node in ix.nodes.values():
        if node.get("type") == "entrypoint":
            eps[_ep_kind(node)] += 1
    for nid, node in ix.nodes.items():
        if node.get("type") == "function" and _MAIN.search(ix.label(nid)) and not ix.up(nid):
            eps["main"] += 1
    unresolved = _unresolved_list(ix, data, svc, hidden, book_index)
    unresolved.extend(x for x in dyn_items)
    stores: dict[str, int] = defaultdict(int)
    for e in tables:
        stores[e["st"]] += 1
    summary = {
        "repos": len(repos),
        "services": len(services),
        "data": len(tables),
        "stores": dict(sorted(stores.items())),
        "eps": sum(eps.values()),
        "epk": dict(sorted(eps.items())),
        "flows": len(mirrors),
        "unres": len(unresolved),
        "unresk": _count_kinds(unresolved),
    }

    # ── map: services, data and topics as one small graph ──
    chunks["mg"] = _map_graph(ix, tables, entities, services, svc, hidden)
    topic_list = []
    for qid, node in sorted(ix.nodes.items()):
        if node.get("type") == "queue":
            producers = {e["from"] for e in ix.inc.get(qid, []) if e.get("type") == "enqueues" and not e.get("derived")}
            consumers = {e["to"] for e in ix.out.get(qid, []) if e.get("type") == "triggers"}
            topic_list.append([qid, ix.label(qid), len(producers), len(consumers)])
    qc = []
    for c in columns:
        acc = _accesses(ix, c)
        nw = sum(1 for fn, v in acc.items() if v[0] & 2 and not hidden(fn))
        nr = sum(1 for fn, v in acc.items() if v[0] & 1 and not hidden(fn))
        qc.append([c["ref"], c["name"], c["st"], c["rp"], c.get("table", ""), nw, nr])
    chunks["qc"] = {"cols": qc}
    chunks["fx"] = {
        "unexpected": unexpected, "multi": multi, "dynamic": dyn_items, "orphans": orphans,
        "neverRead": never_read, "neverWritten": never_written, "unresolved": unresolved,
    }
    cards = {
        "unexpected": unexpected[:CARD_ITEMS], "nunexpected": len(unexpected),
        "multi": multi[:CARD_ITEMS], "nmulti": len(multi),
        "dynamic": _by_svc(dyn_items), "ndynamic": len(dyn_items),
        "orphans": _by_svc(orphans), "norphans": len(orphans),
        "mirrors": _pairs(mirrors), "nmirrors": len(mirrors),
        "neverRead": len(never_read), "neverWritten": len(never_written),
    }
    # ── architecture and event flows (#/architecture, #/queues, #/topic/<name>) ──
    from bizmap.views import Ctx, architecture, entities_of, event_flows, queue_rows

    ent_of, ents = entities_of(ix, {tuple(sorted(e["ids"])): e["ref"] for e in tables})
    # the side menu's kinds (book.classify_service), as the CLI's Mermaid (book.service_kinds)
    vctx = Ctx(ix, services, roles or ["lib"] * len(services), svc, hidden, ent_of, ents,
               link=lambda nid: [book_index.get(nid, -1), ix.kind(nid)])
    arch = architecture(vctx)
    flows = event_flows(vctx)
    chunks["ar"] = arch
    chunks["ev"] = {"flows": flows}
    events = queue_rows(flows)
    views = {
        "services": sum(1 for n in arch["nodes"] if n["kind"] == "service"),
        "stores": sum(1 for n in arch["nodes"] if n["kind"] == "store"),
        "topics": sum(1 for n in arch["nodes"] if n["kind"] == "topic"),
        "events": events,
        "problems": sum(1 for r in events if r[4] != "ok"),
    }
    home = {
        "summary": summary,
        "views": views,
        "cards": cards,
        "dyn": {str(s): v for s, v in sorted(dynamic.items())},
        "ents": [
            [e["ref"], e["kind"], e["name"], e["st"], e["rp"], e["nw"], e["nr"], e["m"], e["own"],
             e["ownw"], e["nwh"], e["nrh"]]
            for e in tables
        ],
        "ncols": len(columns),
        "topics": topic_list,
    }
    return home, chunks


# ── the graph the engine reads, in columns ──────────────────────────────────

ENGINE_EDGES = ("calls", "triggers", "reads", "writes", "enqueues", "feeds", "calls_http")
# copies (`derived`) of these are skipped by every query; evidence is only read on these
_NEVER_DERIVED = ("calls", "reads", "writes", "enqueues")
_EDGE_EVIDENCE = ("reads", "writes", "calls_http", "feeds")
NODE_SLICE, EDGE_SLICE = 20_000, 100_000  # per chunk: none is large to parse
_SPARSE = ("table", "kind", "method", "path", "topic", "symbol", "handler", "db", "engine", "start_line", "repo")


def engine_data(ix: Index, data: dict[str, Any]) -> dict[str, Any]:
    """Exactly what :class:`bizmap.query.Index` keeps and the queries read,
    raw values included (``None`` stays ``null``): ``query.js`` rebuilds the
    same index from it. Node / edge order is the graph's."""
    ids: list[str] = list(ix.nodes)
    pos = {nid: k for k, nid in enumerate(ids)}
    types: list[str] = []
    tpos: dict[str, int] = {}
    files: list[str] = []
    fpos: dict[str, int] = {}

    def code(table: list[Any], index: dict[Any, int], v: Any) -> int:
        k = index.get(v)
        if k is None:
            k = index[v] = len(table)
            table.append(v)
        return k

    def ref(nid: str) -> int:
        k = pos.get(nid)
        if k is None:  # an edge to a node the graph does not list
            k = pos[nid] = len(ids)
            ids.append(nid)
        return k

    ty, nm, ef, el = [], [], [], []
    sparse: dict[str, dict[str, Any]] = {f: {} for f in (*_SPARSE, "uq")}
    for k, nid in enumerate(list(ids)):
        n = ix.nodes[nid]
        ty.append(code(types, tpos, n.get("type")))
        nm.append(n.get("name"))
        ev = n.get("evidence") or {}
        f = ev.get("file") if isinstance(ev, dict) else None
        ef.append(code(files, fpos, f) if f else -1)
        el.append(ev.get("line") if isinstance(ev, dict) else None)
        for field in _SPARSE:
            if field in n:
                sparse[field][str(k)] = n[field]
        uq = n.get("unresolved_queues")
        if uq:
            sparse["uq"][str(k)] = [{x: q[x] for x in ("env", "reason", "kind") if isinstance(q, dict) and x in q} for q in uq]
    etypes: list[str] = []
    etpos: dict[str, int] = {}
    confs: list[Any] = []
    cpos: dict[Any, int] = {}
    ea, eb, et, ec, ed, eef, eel = [], [], [], [], [], [], []
    extra: dict[str, Any] = {}
    for e in data.get("edges") or []:
        if not isinstance(e, dict) or "from" not in e or "to" not in e or e.get("type") not in ENGINE_EDGES:
            continue
        if e.get("derived") and e["type"] in _NEVER_DERIVED:  # every query skips these copies
            continue
        k = len(ea)
        ea.append(ref(e["from"]))
        eb.append(ref(e["to"]))
        et.append(code(etypes, etpos, e["type"]))
        ec.append(code(confs, cpos, e.get("confidence")))
        ed.append(1 if e.get("derived") else 0)
        ev = e.get("evidence") if e["type"] in _EDGE_EVIDENCE else None
        if isinstance(ev, dict) and ev.get("file"):
            eef.append(code(files, fpos, ev["file"]))
            eel.append(ev.get("line"))
        else:
            eef.append(-1)
            eel.append(None)
        if e["type"] == "feeds":
            via = (e.get("via") or [{}])[0] or {}
            extra[str(k)] = [
                [ref(p) for p in via.get("path") or []],
                [[c.get("from", ""), c.get("to", ""), c.get("confidence")] for c in e.get("column_map") or []],
            ]
    for _k in range(len(ty), len(ids)):  # listed only by an edge
        ty.append(-1)
        nm.append(None)
        ef.append(-1)
        el.append(None)
    return {
        "repo": ix.repo,
        "ids": ids, "T": types, "ty": ty, "nm": nm, "F": files, "ef": ef, "el": el,
        "x": {f: v for f, v in sparse.items() if v},
        "ET": etypes, "C": confs,
        "e": {"a": ea, "b": eb, "t": et, "c": ec, "d": ed, "f": eef, "l": eel},
        "fe": extra,
        "uh": [[u.get("from"), u.get("method"), u.get("url"), u.get("reason")] for u in ix.unresolved_http],
    }


# ── helpers ──────────────────────────────────────────────────────────────────


def _accesses(ix: Index, e: dict[str, Any]) -> dict[str, list[Any]]:
    """fn -> [rw bits, best read rank, best write rank, columns]: the functions
    `who-writes` / `who-reads` list for the entity (own access, no derived copies)."""
    out: dict[str, list[Any]] = {}
    ent = Entity(kind=e["kind"], name=e["name"], nodes=e["ids"])
    for tid in _data_targets(ix, ent):
        for edge in ix.inc.get(tid, []):
            t = edge.get("type")
            if t not in ("reads", "writes") or edge.get("derived"):
                continue
            row = out.setdefault(edge["from"], [0, 0, 0, set()])
            bit = 2 if t == "writes" else 1
            row[0] |= bit
            k = 2 if bit == 2 else 1
            row[k] = max(row[k], _rank(edge.get("confidence")))
            if bit == 2 and ix.kind(tid) == "column":
                row[3].add(str(ix.nodes[tid].get("name")))
    return out


def _entity(ix: Index, g: list[str]) -> dict[str, Any]:
    kind = str(ix.nodes[g[0]].get("type"))
    stores = sorted({ix.store(n) for n in g})
    return {
        "kind": kind,
        "name": ix.label(g[0]),
        "ids": g,
        "st": " / ".join(stores),
        "rp": sorted({r for r in (ix.repo_of(n) for n in g) if r}),
    }


def _item(ix: Index, fn: str, s: int, hid: bool, book_index: dict[str, int], detail: str) -> list[Any]:
    """[label, evidence, service, book index, hidden, detail, kind]"""
    return [ix.label(fn), ix.evidence(fn) or "", s, book_index.get(fn, -1), 1 if hid else 0, detail, ix.kind(fn)]


def _ep_kind(node: dict[str, Any]) -> str:
    k = str(node.get("kind") or "other")
    if k == "http" or k.startswith("firebase_http") or k == "firebase_call":
        return "http"
    if k in ("pubsub", "celery_task", "worker", "cloud_task", "topic") or "queue" in k:
        return "queue"
    if k in ("scheduled_job", "job", "cron") or "cron" in k or "schedule" in k:
        return "cron"
    if k in ("firestore", "db_trigger") or "trigger" in k:
        return "trigger"
    return "other"


def _owner(ix: Index, e: dict[str, Any], svc, writers: dict[int, int]) -> tuple[int, str]:
    ddl = set()
    for nid in e["ids"]:
        node = ix.nodes[nid]
        if node.get("type") not in ("table", "view") or node.get("kind") in ("unresolved_relation", "external_relation"):
            continue
        ev = node.get("evidence") if isinstance(node.get("evidence"), dict) else {}
        f = str(ev.get("file") or "")
        typed = any((ix.nodes.get(c) or {}).get("data_type") for c in ix.columns.get(
            (_split(nid)[0], str(node.get("name") or "").lower()), []))
        if f.endswith(".sql") or "migration" in f or typed:
            s = svc(nid)
            if s >= 0:
                ddl.add(s)
    if len(ddl) == 1:
        return next(iter(ddl)), "ddl"
    ranked = sorted(writers.items(), key=lambda x: -x[1])
    if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]) and ranked[0][0] >= 0:
        return ranked[0][0], "most"
    return -1, ""


def _unresolved_list(ix: Index, data: dict[str, Any], svc, hidden, book_index: dict[str, int]) -> list[list[Any]]:
    out = []
    seen = set()
    for u in data.get("unresolved_http") or []:
        if not isinstance(u, dict):
            continue
        fn = str(u.get("from") or "")
        key = (fn, u.get("method"), u.get("url"))
        seen.add(key)
        out.append(_item(ix, fn, svc(fn), hidden(fn), book_index,
                         f"http:{u.get('method') or ''} {u.get('url') or ''} ({u.get('reason') or ''})") if fn in ix.nodes
                   else [fn, "", -1, -1, 0, f"http:{u.get('method') or ''} {u.get('url') or ''}", "function"])
    for nid, node in ix.nodes.items():
        for u in node.get("unresolved_http") or []:
            if isinstance(u, dict) and (nid, u.get("method"), u.get("url") or u.get("path")) not in seen:
                out.append(_item(ix, nid, svc(nid), hidden(nid), book_index,
                                 f"http:{u.get('method') or ''} {u.get('url') or u.get('path') or ''} ({u.get('reason') or ''})"))
        for q in node.get("unresolved_queues") or []:
            if isinstance(q, dict):
                out.append(_item(ix, nid, svc(nid), hidden(nid), book_index,
                                 f"queue:{q.get('env') or ', '.join(map(str, q.get('candidates') or [])) or q.get('reason') or ''}"))
    return out


def _count_kinds(items: list[list[Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for it in items:
        out[str(it[5]).split(":", 1)[0]] += 1
    return dict(sorted(out.items()))


def _by_svc(items: list[list[Any]]) -> list[list[int]]:
    """[service, visible count, hidden count], most first."""
    per: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for it in items:
        per[it[2]][1 if it[4] else 0] += 1
    return sorted(([s, v[0], v[1]] for s, v in per.items()), key=lambda x: (-x[1], -x[2], x[0]))


def _pairs(mirrors: list[list[str]]) -> list[list[Any]]:
    per: dict[tuple[str, str], int] = defaultdict(int)
    for a, b in mirrors:
        per[(a, b)] += 1
    return sorted(([a, b, n] for (a, b), n in per.items()), key=lambda x: -x[2])


def _map_graph(ix: Index, tables: list[dict[str, Any]], entities: list[dict[str, Any]], services: list[str], svc, hidden) -> dict[str, Any]:
    """Nodes ``[kind, key, label, extra]`` (kind: e = data, s = service, q = topic)
    and edges ``[a, b, rel, rw, confidence rank, hidden]`` (rel: rw, feeds, pub,
    sub, http) between them."""
    nodes: list[list[Any]] = []
    pos: dict[str, int] = {}

    def node(kind: str, key: str, label: str, extra: str) -> int:
        k = pos.get(kind + "\x00" + key)
        if k is None:
            k = pos[kind + "\x00" + key] = len(nodes)
            nodes.append([kind, key, label, extra])
        return k

    ent_of: dict[str, int] = {}
    for e in tables:
        k = node("e", e["ref"], e["name"], f"{e['kind']}·{e['st']}")
        for nid in e["ids"]:
            ent_of[nid] = k
    for s, name in enumerate(services):
        node("s", str(s), name, "")
    edges: dict[tuple[int, int, str], list[Any]] = {}

    def add(a: int, b: int, rel: str, rw: int, rank: int, hid: bool) -> None:
        cur = edges.get((a, b, rel))
        if cur is None:
            edges[(a, b, rel)] = [a, b, rel, rw, rank, 1 if hid else 0]
        else:
            cur[3] |= rw
            if not hid and cur[5]:  # a visible link wins over a hidden one
                cur[5], cur[4] = 0, rank
            elif bool(hid) == bool(cur[5]):
                cur[4] = max(cur[4], rank)

    for e in tables:
        k = ent_of[e["ids"][0]]
        for s, rw, rank, hrw, hrank in e["m"]:
            if s < 0:
                continue
            sk = pos["s\x00" + str(s)]
            if rw:
                add(sk, k, "rw", rw, rank, False)
            if hrw:
                add(sk, k, "rw", hrw, hrank, True)
    for edge in ix.out.edges:  # every edge once (see query._Adj)
        t = edge.get("type")
        a, b = edge["from"], edge["to"]
        if t == "feeds" and a in ent_of and b in ent_of:
            add(ent_of[a], ent_of[b], "feeds", 0, _rank(edge.get("confidence")), False)
        elif t == "enqueues" and not edge.get("derived") and ix.kind(b) == "queue" and svc(a) >= 0:
            q = node("q", b, str(ix.nodes[b].get("name") or ix.label(b)), "topic")
            add(pos["s\x00" + str(svc(a))], q, "pub", 0, _rank(edge.get("confidence")), hidden(a))
        elif t == "triggers" and ix.kind(a) == "queue" and svc(b) >= 0:
            q = node("q", a, str(ix.nodes[a].get("name") or ix.label(a)), "topic")
            add(q, pos["s\x00" + str(svc(b))], "sub", 0, _rank(edge.get("confidence")), hidden(b))
        elif t == "calls_http" and svc(a) >= 0 and svc(b) >= 0 and svc(a) != svc(b):
            add(pos["s\x00" + str(svc(a))], pos["s\x00" + str(svc(b))], "http", 0, _rank(edge.get("confidence")), hidden(a))
    return {"n": nodes, "e": sorted(edges.values())}


__all__ = ["build_home", "is_hidden"]
