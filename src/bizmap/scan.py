from __future__ import annotations

import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from bizmap.detect.go_lang import GoAnalysis, GoAnalyzer, GoFunction
from bizmap.detect.js_lang import JsAnalysis, JsAnalyzer
from bizmap.detect.python_lang import PyAccess, PyAnalysis, PythonAnalyzer
from bizmap.detect.rules import load_rules
from bizmap.files import discover_sql_files, iter_files
from bizmap.graph import (
    GRAPH_VERSION,
    Confidence,
    Evidence,
    Graph,
    collection_id,
    column_id,
    entrypoint_id,
    function_id,
    queue_id,
    table_id,
    trigger_id,
    view_id,
)
from bizmap.http_match import (
    match_http,
    path_without_placeholders,
    strip_service_base,
    unresolved_http_keys,
)
from bizmap.overrides import ScanConfig, apply_overrides, load_scan_config
from bizmap.registry import merge_graphs
from bizmap.schema_replay import Schema, replay_files
from bizmap.sql_analyze import SqlAccess, analyze_sql, is_external_table, is_skipped_table_name


@dataclass
class ScanProfile:
    parse: float = 0.0
    resolve: float = 0.0
    sql: float = 0.0
    propagate: float = 0.0

    @property
    def total(self) -> float:
        return self.parse + self.resolve + self.sql + self.propagate

    def format_line(self) -> str:
        return (
            f"bizmap profile: parse={self.parse:.3f}s resolve={self.resolve:.3f}s "
            f"sql={self.sql:.3f}s propagate={self.propagate:.3f}s total={self.total:.3f}s"
        )


def _merge_schema(base: Schema, extra: Schema) -> None:
    for name, table in extra.tables.items():
        dest = base.tables.get(name.lower())
        if dest is None:
            base.tables[name.lower()] = table
            continue
        for cname, col in table.columns.items():
            dest.columns.setdefault(cname, col)
        dest.foreign_keys.extend(table.foreign_keys)
        dest.policies.extend(table.policies)
        dest.rls = dest.rls or table.rls
        dest.constraints.update(table.constraints)
    for name, fn in extra.functions.items():
        base.functions.setdefault(name, fn)
    for name, trig in extra.triggers.items():
        base.triggers.setdefault(name, trig)
    for name, view in extra.views.items():
        base.views.setdefault(name, view)


def _add_schema_nodes(graph: Graph, schema: Schema) -> None:
    graph.schema = schema.to_dict()
    for tname, table in schema.tables.items():
        tid = table_id(tname)
        extra: dict = {}
        if table.rls:
            extra["rls"] = True
        if table.policies:
            extra["policies"] = [p.name for p in table.policies]
        graph.add_node(tid, "table", name=tname, **extra)
        for col in table.columns.values():
            graph.add_node(
                column_id(tname, col.name),
                "column",
                table=tname,
                name=col.name,
                data_type=col.data_type,
                nullable=col.nullable,
            )
    for vname, view in schema.views.items():
        vid = view_id(vname)
        graph.add_node(
            vid,
            "view",
            name=vname,
            kind="view",
            materialized=view.materialized,
            evidence=view.evidence.to_dict() if view.evidence else None,
        )
        for tname in view.tables:
            if tname in schema.views:
                graph.add_edge(
                    "derives_from",
                    vid,
                    view_id(tname),
                    confidence=Confidence.EXACT,
                    evidence=view.evidence,
                )
            else:
                tid = table_id(tname)
                if tid not in graph.nodes and tname not in schema.tables:
                    graph.add_node(tid, "table", name=tname)
                elif tid not in graph.nodes:
                    graph.add_node(tid, "table", name=tname)
                graph.add_edge(
                    "derives_from",
                    vid,
                    tid,
                    confidence=Confidence.EXACT,
                    evidence=view.evidence,
                )


def _ensure_access_target(graph: Graph, schema: Schema, access: SqlAccess) -> str | None:
    """Create the node an SQL access should point at.

    Named columns on known (or clearly real) tables stay column-level.
    SELECT * / column-less INSERT stay table-level. CTE aliases are never
    emitted by sql_analyze; dynamic table names land on ``table:unknown``.
    """
    if access.unknown_table or not access.table or access.table.lower() == "unknown":
        graph.add_node(
            "table:unknown",
            "table",
            name="unknown",
            candidates=list(access.candidates or []),
            confidence=Confidence.UNKNOWN.value,
        )
        return "table:unknown"
    tname = access.table.lower()
    if is_skipped_table_name(tname):
        return None
    if tname in schema.views:
        vid = view_id(tname)
        view = schema.views[tname]
        graph.add_node(
            vid,
            "view",
            name=tname,
            kind="view",
            materialized=view.materialized,
        )
        if access.column:
            cid = column_id(tname, access.column)
            if cid not in graph.nodes:
                graph.add_node(cid, "column", table=tname, name=access.column.lower())
            return cid
        return vid
    ddl_known = {t.lower() for t in schema.tables} | {t.lower() for t in schema.views}
    known = tname in schema.tables or tname in schema.views
    external = access.external or is_external_table(tname)
    if external:
        tid = table_id(tname)
        graph.add_node(tid, "table", name=tname, external=True)
        if access.column:
            cid = column_id(tname, access.column)
            graph.add_node(cid, "column", table=tname, name=access.column.lower(), external=True)
            return cid
        return tid
    # With DDL, only schema tables/views become nodes (plus schema-qualified
    # names). Unknown identifiers are unresolved attributes, not nodes.
    if ddl_known and not known:
        if getattr(access, "schema_qualified", False):
            tid = table_id(tname)
            graph.add_node(tid, "table", name=tname)
            if access.column:
                cid = column_id(tname, access.column)
                if cid not in graph.nodes:
                    graph.add_node(cid, "column", table=tname, name=access.column.lower())
                return cid
            return tid
        return None
    tid = table_id(tname)
    if not known:
        graph.add_node(tid, "table", name=tname)
    if access.column:
        cid = column_id(tname, access.column)
        if cid not in graph.nodes:
            graph.add_node(cid, "column", table=tname, name=access.column.lower())
        return cid
    graph.add_node(tid, "table", name=tname)
    return tid


def _kind_edge(kind: str) -> str:
    return "writes" if kind.startswith("write") else "reads"


def _add_function_node_go(graph: Graph, fn: GoFunction) -> str:
    nid = function_id(fn.qualname)
    graph.add_node(
        nid,
        "function",
        name=fn.qualname,
        package=fn.package,
        import_path=fn.import_path,
        language="go",
        why=fn.why,
        evidence={"file": fn.file, "line": fn.line, "symbol": fn.qualname},
    )
    return nid


def _is_func_main(qualname: str) -> bool:
    """True for the ``func main`` entry (not methods named Main, not jobs)."""
    return qualname.endswith(".main") and qualname.count(".") == 1


def _callers_of(qn: str, reverse: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    queue = deque([qn])
    while queue:
        cur = queue.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        for parent in reverse.get(cur, set()):
            if parent not in seen:
                queue.append(parent)
    seen.discard(qn)
    return seen


def _propagate(
    graph: Graph,
    origin_fn: str,
    accesses: list[SqlAccess],
    reverse_calls: dict[str, set[str]],
    evidence_by_fn: dict[str, Evidence],
    entrypoints_by_handler: dict[str, list[str]],
    schema: Schema,
    job_functions: set[str],
) -> None:
    """Copy origin accesses up the call graph as **table-level** inferred edges.

    Dynamic-table / ``table:unknown`` accesses stay on the origin function
    only — they must not flood every caller and HTTP route.
    """
    table_accesses: list[tuple[str, SqlAccess]] = []
    seen_tables: set[tuple[str, str]] = set()
    for access in accesses:
        tname = "unknown" if access.unknown_table or not access.table else access.table.lower()
        if access.unknown_table or tname == "unknown":
            continue
        key = (_kind_edge(access.kind), tname)
        if key in seen_tables:
            continue
        seen_tables.add(key)
        rolled = SqlAccess(
            kind=access.kind,
            table=tname,
            column=None,
            confidence=Confidence.INFERRED,
            raw_sql=access.raw_sql,
            dynamic=access.dynamic,
            external=access.external,
            unknown_table=access.unknown_table or tname == "unknown",
            candidates=list(access.candidates or []),
        )
        table_accesses.append((_kind_edge(access.kind), rolled))

    ancestors = _callers_of(origin_fn, reverse_calls)
    for anc in ancestors:
        if _is_func_main(anc) and anc not in job_functions:
            continue
        fid = function_id(anc)
        if fid not in graph.nodes:
            continue
        ev = evidence_by_fn.get(anc) or evidence_by_fn.get(origin_fn)
        for kind, access in table_accesses:
            target = _ensure_access_target(graph, schema, access)
            if not target:
                continue
            graph.add_edge(kind, fid, target, confidence=Confidence.INFERRED, evidence=ev)
        for ep in entrypoints_by_handler.get(anc, []):
            for kind, access in table_accesses:
                target = _ensure_access_target(graph, schema, access)
                if not target:
                    continue
                graph.add_edge(kind, ep, target, confidence=Confidence.INFERRED, evidence=ev)
    for ep in entrypoints_by_handler.get(origin_fn, []):
        for kind, access in table_accesses:
            target = _ensure_access_target(graph, schema, access)
            if not target:
                continue
            graph.add_edge(
                kind,
                ep,
                target,
                confidence=Confidence.INFERRED,
                evidence=evidence_by_fn.get(origin_fn),
            )


def _apply_go(
    graph: Graph,
    analysis: GoAnalysis,
    schema: Schema,
    *,
    expand_star: bool = False,
) -> tuple[float, float]:
    colmap = schema.column_names()
    known = set(colmap) | set(schema.views)
    evidence_by_fn: dict[str, Evidence] = {}
    for fn in analysis.functions.values():
        _add_function_node_go(graph, fn)
        evidence_by_fn[fn.qualname] = Evidence(file=fn.file, line=fn.line, symbol=fn.qualname)

    reverse: dict[str, set[str]] = defaultdict(set)
    for hit in analysis.calls:
        extra: dict = {}
        if hit.candidates:
            extra["candidates"] = list(hit.candidates)
        graph.add_edge(
            "calls",
            function_id(hit.caller),
            function_id(hit.callee),
            confidence=hit.confidence,
            evidence=hit.evidence,
            **extra,
        )
        # Ambiguous/unknown call edges do not propagate table reads/writes.
        if hit.confidence != Confidence.UNKNOWN:
            reverse[hit.callee].add(hit.caller)

    entrypoints_by_handler: dict[str, list[str]] = defaultdict(list)
    job_functions: set[str] = {j.handler.qualname for j in analysis.jobs}

    for route in analysis.routes:
        handler = route.handler
        key = f"{route.method}:{route.path}"
        eid = entrypoint_id("http", key)
        conf = route.confidence
        if route.path_confidence == Confidence.UNKNOWN:
            conf = Confidence.UNKNOWN
        node_kwargs: dict = dict(
            kind="http",
            method=route.method,
            path=route.path,
            symbol=handler.qualname if handler else route.handler_expr,
            why=handler.why if handler else None,
            confidence=conf.value,
            candidates=list(route.candidates or []),
            evidence=route.evidence.to_dict(),
        )
        if getattr(route, "path_unresolved", False):
            node_kwargs["unresolved"] = True
        graph.add_node(eid, "entrypoint", **node_kwargs)
        if handler:
            graph.add_edge(
                "triggers",
                eid,
                function_id(handler.qualname),
                confidence=route.confidence,
                evidence=route.evidence,
            )
            entrypoints_by_handler[handler.qualname].append(eid)

    for job in analysis.jobs:
        handler = job.handler
        eid = entrypoint_id("job", handler.qualname)
        graph.add_node(
            eid,
            "entrypoint",
            kind="scheduled_job",
            symbol=handler.qualname,
            handler=handler.qualname,
            why=handler.why,
            evidence=job.evidence.to_dict(),
        )
        graph.add_edge(
            "triggers",
            eid,
            function_id(handler.qualname),
            confidence=Confidence.EXACT,
            evidence=job.evidence,
        )
        entrypoints_by_handler[handler.qualname].append(eid)
        job_functions.add(handler.qualname)

    sql_sites = list(analysis.sql_sites)
    t_sql = time.perf_counter()
    origin_accesses: list[tuple[str, list[SqlAccess], object]] = []
    for site in sql_sites:
        fid = function_id(site.function.qualname)
        accesses = analyze_sql(
            site.sql,
            schema_columns=colmap,
            dynamic=site.dynamic or not site.fully_static,
            expand_star=expand_star,
            known_tables=known,
        )
        situations = list(dict.fromkeys(analysis.situations.get(site.function.qualname) or []))
        sit_kwargs: dict = {}
        if situations:
            sit_kwargs["situation"] = situations
        for access in accesses:
            target = _ensure_access_target(graph, schema, access)
            if not target:
                continue
            graph.add_edge(
                _kind_edge(access.kind),
                fid,
                target,
                confidence=access.confidence,
                evidence=site.evidence,
                **sit_kwargs,
            )
        origin_accesses.append((site.function.qualname, accesses, site.evidence))
    sql_s = time.perf_counter() - t_sql
    t_prop = time.perf_counter()
    for origin_fn, accesses, _ev in origin_accesses:
        _propagate(
            graph,
            origin_fn,
            accesses,
            reverse,
            evidence_by_fn,
            entrypoints_by_handler,
            schema,
            job_functions,
        )
    prop_s = time.perf_counter() - t_prop
    return sql_s, prop_s


def _entrypoint_kind_key(route) -> tuple[str, str]:
    kind = route.kind or "http"
    if kind == "http":
        return "http", f"{route.method}:{route.path}"
    if kind in {"firebase_http", "firebase_call"}:
        return kind, f"{route.method}:{route.path}"
    if kind == "firestore":
        coll = (route.extra or {}).get("collection") or route.path
        event = (route.extra or {}).get("event") or route.method.lower()
        return "firestore", f"{coll}:{event}"
    if kind == "scheduled_job":
        return "job", route.handler.qualname
    if kind == "celery_task":
        return "celery", route.handler.qualname
    if kind == "cloud_task":
        return "cloud_task", route.handler.qualname
    if kind == "pubsub":
        return "pubsub", route.path or route.handler.qualname
    if kind == "worker":
        return "worker", route.handler.qualname
    return kind, f"{route.method}:{route.path}" if route.method else route.path or route.handler.qualname


def _ensure_collection(graph: Graph, name: str) -> str:
    cid = collection_id(name)
    graph.add_node(cid, "collection", name=name.lower(), kind="collection")
    return cid


def _apply_py_access(graph: Graph, schema: Schema, access: PyAccess, fid: str) -> SqlAccess | None:
    if access.target_kind == "collection":
        target = _ensure_collection(graph, access.table)
        graph.add_edge(
            _kind_edge(access.kind),
            fid,
            target,
            confidence=access.confidence,
            evidence=access.evidence,
        )
        return SqlAccess(
            kind=access.kind,
            table=access.table,
            column=None,
            confidence=Confidence.INFERRED,
            raw_sql="",
            unknown_table=False,
        )
    rolled = SqlAccess(
        kind=access.kind,
        table=access.table,
        column=access.column,
        confidence=access.confidence,
        raw_sql="",
    )
    target = _ensure_access_target(graph, schema, rolled)
    if not target:
        return None
    graph.add_edge(
        _kind_edge(access.kind),
        fid,
        target,
        confidence=access.confidence,
        evidence=access.evidence,
    )
    return rolled


def _apply_python(
    graph: Graph,
    analysis: PyAnalysis,
    schema: Schema,
    *,
    expand_star: bool = False,
) -> tuple[float, float]:
    colmap = schema.column_names()
    known = set(colmap) | set(schema.views)
    evidence_by_fn: dict[str, Evidence] = {}
    reverse: dict[str, set[str]] = defaultdict(set)
    job_functions: set[str] = set()
    for fn in analysis.functions.values():
        fid = function_id(fn.qualname)
        extra: dict = {}
        if fn.role:
            extra["role"] = fn.role
        graph.add_node(
            fid,
            "function",
            name=fn.qualname,
            language="python",
            why=fn.why,
            evidence={"file": fn.file, "line": fn.line, "symbol": fn.qualname},
            **extra,
        )
        evidence_by_fn[fn.qualname] = Evidence(file=fn.file, line=fn.line, symbol=fn.qualname)
    for caller, callee, ev, conf in analysis.calls:
        graph.add_edge("calls", function_id(caller), function_id(callee), confidence=conf, evidence=ev)
        reverse[callee].add(caller)

    entrypoints_by_handler: dict[str, list[str]] = defaultdict(list)
    for route in analysis.routes:
        kind_key, key = _entrypoint_kind_key(route)
        eid = entrypoint_id(kind_key, key)
        node_kwargs: dict = dict(
            kind=route.kind if route.kind != "http" else "http",
            method=route.method or None,
            path=route.path,
            symbol=route.handler.qualname,
            why=route.handler.why,
            confidence=(Confidence.UNKNOWN if route.path_unresolved else route.confidence).value,
            evidence=route.evidence.to_dict(),
        )
        if route.path_unresolved:
            node_kwargs["unresolved"] = True
        node_kwargs.update({k: v for k, v in (route.extra or {}).items() if v not in (None, "", [], {})})
        graph.add_node(eid, "entrypoint", **node_kwargs)
        graph.add_edge(
            "triggers",
            eid,
            function_id(route.handler.qualname),
            confidence=Confidence.UNKNOWN if route.path_unresolved else route.confidence,
            evidence=route.evidence,
        )
        entrypoints_by_handler[route.handler.qualname].append(eid)
        if route.kind in {"scheduled_job", "celery_task", "cloud_task", "pubsub", "worker", "firestore"}:
            job_functions.add(route.handler.qualname)

    for fn in analysis.workers:
        eid = entrypoint_id("worker", fn.qualname)
        ev = Evidence(file=fn.file, line=fn.line, symbol=fn.qualname)
        graph.add_node(
            eid,
            "entrypoint",
            kind="worker",
            symbol=fn.qualname,
            handler=fn.qualname,
            why=fn.why,
            evidence=ev.to_dict(),
        )
        graph.add_edge("triggers", eid, function_id(fn.qualname), confidence=Confidence.EXACT, evidence=ev)
        entrypoints_by_handler[fn.qualname].append(eid)
        job_functions.add(fn.qualname)

    t_sql = time.perf_counter()
    origin_accesses: list[tuple[str, list[SqlAccess]]] = []
    for site in analysis.sql_sites:
        fid = function_id(site.function.qualname)
        accesses = analyze_sql(
            site.sql,
            schema_columns=colmap,
            dynamic=site.dynamic,
            expand_star=expand_star,
            known_tables=known,
        )
        for access in accesses:
            target = _ensure_access_target(graph, schema, access)
            if not target:
                continue
            graph.add_edge(
                _kind_edge(access.kind),
                fid,
                target,
                confidence=access.confidence,
                evidence=site.evidence,
            )
        origin_accesses.append((site.function.qualname, accesses))

    for access in analysis.accesses:
        fid = function_id(access.function.qualname)
        rolled = _apply_py_access(graph, schema, access, fid)
        if rolled is None:
            continue
        if access.target_kind == "collection":
            # propagate collection table-level via a synthetic SqlAccess on a fake table name
            # handled separately after SQL propagate using collection nodes
            continue
        origin_accesses.append((access.function.qualname, [rolled]))
    sql_s = time.perf_counter() - t_sql
    t_prop = time.perf_counter()
    for origin_fn, accesses in origin_accesses:
        _propagate(
            graph,
            origin_fn,
            accesses,
            reverse,
            evidence_by_fn,
            entrypoints_by_handler,
            schema,
            job_functions,
        )
    _propagate_collections(graph, analysis, reverse, entrypoints_by_handler)
    _apply_queues(graph, analysis, entrypoints_by_handler)
    _apply_rpc(graph, analysis, schema)
    return sql_s, time.perf_counter() - t_prop


def _propagate_collections(
    graph: Graph,
    analysis: PyAnalysis,
    reverse: dict[str, set[str]],
    entrypoints_by_handler: dict[str, list[str]],
) -> None:
    by_origin: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for access in analysis.accesses:
        if access.target_kind != "collection":
            continue
        by_origin[access.function.qualname].add((_kind_edge(access.kind), access.table.lower()))
    for origin, items in by_origin.items():
        ancestors = _callers_of(origin, reverse)
        targets = []
        for kind, coll in items:
            cid = _ensure_collection(graph, coll)
            targets.append((kind, cid))
        for anc in {origin, *ancestors}:
            fid = function_id(anc)
            if fid not in graph.nodes:
                continue
            for kind, cid in targets:
                if anc != origin:
                    graph.add_edge(kind, fid, cid, confidence=Confidence.INFERRED)
            for ep in entrypoints_by_handler.get(anc, []):
                for kind, cid in targets:
                    graph.add_edge(kind, ep, cid, confidence=Confidence.INFERRED)


def _apply_queues(graph: Graph, analysis: PyAnalysis, entrypoints_by_handler: dict[str, list[str]]) -> None:
    consumers: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ev in analysis.queue_events:
        if ev.role != "consume":
            continue
        qid = queue_id(ev.queue_kind, ev.name)
        graph.add_node(qid, "queue", kind=ev.queue_kind, name=ev.name)
        graph.add_edge(
            "triggers",
            qid,
            function_id(ev.function.qualname),
            confidence=Confidence.EXACT,
            evidence=ev.evidence,
        )
        consumers[(ev.queue_kind, ev.name)].append(ev.function.qualname)
        for eid in entrypoints_by_handler.get(ev.function.qualname, []):
            graph.add_edge("triggers", qid, eid, confidence=Confidence.INFERRED, evidence=ev.evidence)
    for ev in analysis.queue_events:
        if ev.role != "produce":
            continue
        name = ev.name
        if ev.queue_kind == "celery" and ev.target_symbol:
            # match consume events whose function name/qualname matches
            for (kind, cname), _fns in list(consumers.items()):
                if kind != "celery":
                    continue
                if ev.target_symbol == cname or cname.endswith("." + ev.target_symbol.split(".")[-1]) or ev.target_symbol.endswith(
                    "." + cname.split(".")[-1]
                ):
                    name = cname
                    break
        qid = queue_id(ev.queue_kind, name)
        graph.add_node(qid, "queue", kind=ev.queue_kind, name=name)
        graph.add_edge(
            "enqueues",
            function_id(ev.function.qualname),
            qid,
            confidence=Confidence.INFERRED,
            evidence=ev.evidence,
        )


def _queue_hints(raw: dict | None) -> dict[str, str]:
    """Flatten bizmap.yaml ``queues:`` to key → consumer hint (path or symbol)."""
    out: dict[str, str] = {}
    for key, spec in (raw or {}).items():
        if isinstance(spec, str):
            out[str(key)] = spec
            continue
        if not isinstance(spec, dict):
            continue
        hint = spec.get("consumer") or spec.get("entrypoint") or spec.get("path") or spec.get("url")
        if hint:
            out[str(key)] = str(hint)
        path = spec.get("path") or spec.get("url")
        if path:
            out[str(path)] = str(hint or path)
    return out


def _match_queue_consumer(
    graph: Graph,
    *,
    url: str | None,
    topic: str | None,
    kind: str,
    services: dict,
    hints: dict[str, str],
) -> list[str]:
    """Entrypoint ids that should consume this enqueue."""
    hits: list[str] = []
    suffix = ""
    last = ""
    if url:
        suffix = path_without_placeholders(strip_service_base(url, services))
        last = suffix.rstrip("/").split("/")[-1] if suffix else ""
    if topic:
        last = last or topic.split("/")[-1]
    candidates = [last, suffix, url or "", topic or ""]
    for key in list(candidates):
        if key and key in hints:
            candidates.append(hints[key])
        if key:
            for hkey, hint in hints.items():
                if str(hkey).rstrip("/") == str(key).rstrip("/") or str(hkey).endswith("/" + last):
                    candidates.append(hint)
    wanted = {c.lower().rstrip("/") for c in candidates if c}
    for node in graph.nodes.values():
        if node.type != "entrypoint":
            continue
        kind_n = str(node.attrs.get("kind") or "")
        if kind == "cloud_tasks" and kind_n not in {
            "http",
            "firebase_http",
            "firebase_call",
            "cloud_task",
        }:
            continue
        if kind == "pubsub" and kind_n not in {"pubsub", "cloud_event"}:
            continue
        path = str(node.attrs.get("path") or "")
        symbol = str(node.attrs.get("symbol") or "")
        name = symbol.split(".")[-1]
        topic_n = str(node.attrs.get("topic") or "")
        checks = {
            path.lower().rstrip("/"),
            path_without_placeholders(path).lower().rstrip("/"),
            name.lower(),
            symbol.lower(),
            topic_n.lower(),
            (path.rstrip("/").split("/")[-1] if path else "").lower(),
        }
        if checks & wanted:
            hits.append(node.id)
    return hits


def _link_queue_consumers(graph: Graph, analysis: PyAnalysis, cfg: ScanConfig) -> None:
    hints = _queue_hints((cfg.raw or {}).get("queues") if cfg else None)
    services = cfg.services if cfg else {}
    for ev in analysis.queue_events:
        if ev.role != "produce":
            continue
        if ev.queue_kind not in {"cloud_tasks", "pubsub"}:
            continue
        url = ev.http_url
        topic = ev.name if ev.queue_kind == "pubsub" else None
        if ev.queue_kind == "cloud_tasks" and not url:
            continue
        consumers = _match_queue_consumer(
            graph,
            url=url,
            topic=topic,
            kind=ev.queue_kind,
            services=services,
            hints=hints,
        )
        produce_qid = None
        for edge in graph.outgoing(function_id(ev.function.qualname), "enqueues"):
            produce_qid = edge.to_id
            break
        if produce_qid is None:
            produce_qid = queue_id(ev.queue_kind, ev.name)
            graph.add_node(produce_qid, "queue", kind=ev.queue_kind, name=ev.name)
        for eid in consumers:
            graph.add_edge(
                "triggers",
                produce_qid,
                eid,
                confidence=Confidence.INFERRED,
                evidence=ev.evidence,
            )
            # Share the consumer queue id when one already exists for that handler.
            node = graph.get(eid)
            symbol = str((node.attrs if node else {}).get("symbol") or "")
            for qid, qnode in list(graph.nodes.items()):
                if qnode.type != "queue":
                    continue
                if qnode.attrs.get("kind") != ev.queue_kind:
                    continue
                if symbol and (
                    qnode.attrs.get("name") == symbol
                    or str(qnode.attrs.get("name") or "").endswith("." + symbol.split(".")[-1])
                    or str(qnode.attrs.get("name") or "") == symbol.split(".")[-1]
                ):
                    graph.add_edge(
                        "enqueues",
                        function_id(ev.function.qualname),
                        qid,
                        confidence=Confidence.INFERRED,
                        evidence=ev.evidence,
                    )


def _apply_rpc(graph: Graph, analysis: PyAnalysis, schema: Schema) -> None:
    for rpc in analysis.rpc_calls:
        fname = rpc.name.split(".")[-1].lower()
        sql_fn = schema.functions.get(fname)
        target = function_id(f"sql.{fname}")
        if sql_fn:
            graph.add_node(
                target,
                "function",
                name=sql_fn.name,
                language=sql_fn.language,
                evidence=sql_fn.evidence.to_dict() if sql_fn.evidence else None,
            )
        else:
            graph.add_node(target, "function", name=fname, language="sql")
        graph.add_edge(
            "calls",
            function_id(rpc.function.qualname),
            target,
            confidence=Confidence.INFERRED if sql_fn else Confidence.UNKNOWN,
            evidence=rpc.evidence,
        )


def _apply_js(graph: Graph, analysis: JsAnalysis, schema: Schema) -> None:
    for route in analysis.routes:
        if route.method == "USE":
            continue
        eid = entrypoint_id("http", f"{route.method}:{route.path}")
        kwargs: dict = dict(
            kind="http",
            method=route.method,
            path=route.path,
            symbol=route.handler,
            language="javascript",
            evidence={"file": route.file, "line": route.line, "symbol": route.handler},
        )
        if route.unresolved:
            kwargs["unresolved"] = True
            kwargs["confidence"] = Confidence.UNKNOWN.value
        graph.add_node(eid, "entrypoint", **kwargs)
        hid = function_id(route.handler)
        graph.add_node(
            hid,
            "function",
            name=route.handler,
            language="javascript",
            evidence={"file": route.file, "line": route.line, "symbol": route.handler},
        )
        graph.add_edge(
            "triggers",
            eid,
            hid,
            confidence=Confidence.UNKNOWN if route.unresolved else Confidence.INFERRED,
        )
    for rpc in analysis.rpc_calls:
        fname = rpc.name.split(".")[-1].lower()
        target = function_id(f"sql.{fname}")
        if fname in schema.functions:
            fn = schema.functions[fname]
            graph.add_node(
                target,
                "function",
                name=fn.name,
                language=fn.language,
                evidence=fn.evidence.to_dict() if fn.evidence else None,
            )
        else:
            graph.add_node(target, "function", name=fname, language="sql")
        src = function_id(rpc.symbol)
        graph.add_node(
            src,
            "function",
            name=rpc.symbol,
            language="javascript",
            evidence={"file": rpc.file, "line": rpc.line, "symbol": rpc.symbol},
        )
        graph.add_edge("calls", src, target, confidence=Confidence.INFERRED)


def _link_http(
    graph: Graph,
    py: PyAnalysis | None,
    js: JsAnalysis | None,
    services: dict[str, str],
) -> list[dict]:
    """Match HTTP client calls to routes. Return unmatched calls for the scan summary."""
    routes: list[tuple[str, str, str]] = []
    for node in graph.nodes.values():
        if node.type != "entrypoint":
            continue
        if node.attrs.get("kind") not in {None, "http", "firebase_http", "cloud_task"}:
            continue
        method = str(node.attrs.get("method") or "")
        path = str(node.attrs.get("path") or "")
        if method and path:
            routes.append((node.id, method, path))
    calls: list[tuple[str, str, str, Evidence, Confidence]] = []
    if py:
        for c in py.http_calls:
            calls.append(
                (
                    function_id(c.function.qualname),
                    c.method,
                    c.url,
                    c.evidence,
                    c.confidence,
                )
            )
    if js:
        for c in js.http_calls:
            ev = Evidence(file=c.file, line=c.line, symbol=c.symbol)
            calls.append((function_id(c.symbol), c.method, c.url, ev, Confidence.INFERRED))
    unmatched: list[dict] = []
    for fid, method, url, ev, conf in calls:
        hits = match_http(method, url, routes, services)
        if hits:
            for eid in hits:
                graph.add_edge("calls_http", fid, eid, confidence=conf, evidence=ev)
            continue
        keys = unresolved_http_keys(url)
        if keys:
            unmatched.append({"url": url, "method": method, "keys": keys, "from": fid})
    graph.attrs["unresolved_http"] = unmatched
    return unmatched


def _link_firestore_triggers(graph: Graph) -> None:
    writers: dict[str, list[tuple[str, Evidence | None]]] = defaultdict(list)
    for edge in graph.edges.values():
        if edge.type != "writes" or not edge.to_id.startswith("collection:"):
            continue
        coll = edge.to_id.split(":", 1)[1]
        writers[coll].append((edge.from_id, edge.evidence))
    for node in list(graph.nodes.values()):
        if node.type != "entrypoint" or node.attrs.get("kind") != "firestore":
            continue
        coll = str(node.attrs.get("collection") or "")
        if not coll:
            continue
        for writer_id, ev in writers.get(coll.lower(), []):
            if writer_id == node.id:
                continue
            graph.add_edge("triggers", writer_id, node.id, confidence=Confidence.INFERRED, evidence=ev)


def _apply_sql_functions(graph: Graph, schema: Schema) -> None:
    for fn in schema.functions.values():
        fid = function_id(f"sql.{fn.name}")
        extra: dict = {}
        if fn.unresolved:
            extra["unresolved"] = list(fn.unresolved)
        graph.add_node(
            fid,
            "function",
            name=fn.name,
            language=fn.language,
            why=None,
            evidence=fn.evidence.to_dict() if fn.evidence else None,
            **extra,
        )
        for access in fn.accesses:
            target = _ensure_access_target(graph, schema, access)
            if not target:
                if access.table and not access.unknown_table:
                    node = graph.get(fid)
                    if node is not None:
                        refs = list(node.attrs.get("unresolved") or [])
                        leaf = access.table.lower()
                        if leaf not in refs:
                            refs.append(leaf)
                        node.attrs["unresolved"] = refs
                continue
            graph.add_edge(
                _kind_edge(access.kind),
                fid,
                target,
                confidence=access.confidence,
                evidence=fn.evidence,
            )


def _apply_db_triggers(graph: Graph, schema: Schema) -> None:
    writers_by_table: dict[str, list[tuple[str, Evidence | None]]] = defaultdict(list)
    for edge in list(graph.edges.values()):
        if edge.type != "writes":
            continue
        to = edge.to_id
        table = None
        if to.startswith("table:"):
            table = to.split(":", 1)[1]
        elif to.startswith("column:"):
            table = to.split(":", 1)[1].split(".", 1)[0]
        if table:
            writers_by_table[table.lower()].append((edge.from_id, edge.evidence))

    for trig in schema.triggers.values():
        tid = trigger_id(trig.name)
        graph.add_node(
            tid,
            "db_trigger",
            name=trig.name,
            table=trig.table,
            timing=trig.timing,
            events=trig.events,
            function=trig.function,
            evidence=trig.evidence.to_dict() if trig.evidence else None,
        )
        fn = schema.functions.get(trig.function)
        if fn:
            fid = function_id(f"sql.{fn.name}")
            graph.add_node(
                fid,
                "function",
                name=fn.name,
                language=fn.language,
                why=None,
                evidence=fn.evidence.to_dict() if fn.evidence else None,
            )
            graph.add_edge(
                "triggers",
                tid,
                fid,
                confidence=Confidence.EXACT,
                evidence=trig.evidence,
            )
            for access in fn.accesses:
                target = _ensure_access_target(graph, schema, access)
                if not target:
                    continue
                graph.add_edge(
                    _kind_edge(access.kind),
                    fid,
                    target,
                    confidence=access.confidence,
                    evidence=fn.evidence,
                )
                graph.add_edge(
                    _kind_edge(access.kind),
                    tid,
                    target,
                    confidence=Confidence.EXACT,
                    evidence=trig.evidence,
                )
                if access.kind.startswith("write"):
                    for writer_id, ev in writers_by_table.get(trig.table.lower(), []):
                        if writer_id in {fid, tid}:
                            continue
                        graph.add_edge(
                            "writes",
                            writer_id,
                            target,
                            confidence=Confidence.INFERRED,
                            evidence=ev or trig.evidence,
                            via=f"db_trigger:{trig.name}",
                        )
        elif trig.table:
            graph.add_edge(
                "triggers",
                tid,
                table_id(trig.table),
                confidence=Confidence.EXACT,
                evidence=trig.evidence,
            )


def scan_path(
    root: Path,
    *,
    extra_rules: list[Path] | None = None,
    expand_star: bool | None = None,
    exclude: list[str] | None = None,
    config: Path | None = None,
    profile: bool = False,
    _follow_repos: bool = True,
) -> Graph:
    root = root.resolve()
    cfg = load_scan_config(root, config=config)
    if _follow_repos and cfg.repos:
        roots = [root / r if not Path(r).is_absolute() else Path(r) for r in cfg.repos]
        roots = [p.resolve() for p in roots if p.exists()]
        if roots:
            return scan_paths(
                roots,
                extra_rules=extra_rules,
                expand_star=expand_star,
                exclude=exclude,
                config=config or cfg.path,
                profile=profile,
            )
    return _scan_one(
        root,
        extra_rules=extra_rules,
        expand_star=expand_star,
        exclude=exclude,
        config=config,
        profile=profile,
        cfg=cfg,
    )


def scan_paths(
    roots: list[Path],
    *,
    extra_rules: list[Path] | None = None,
    expand_star: bool | None = None,
    exclude: list[str] | None = None,
    config: Path | None = None,
    profile: bool = False,
) -> Graph:
    graphs: list[Graph] = []
    services: dict[str, str] = {}
    for root in roots:
        root = root.resolve()
        cfg = load_scan_config(root, config=config)
        services.update(cfg.services)
        graphs.append(
            _scan_one(
                root,
                extra_rules=extra_rules,
                expand_star=expand_star,
                exclude=exclude,
                config=config,
                profile=profile,
                cfg=cfg,
            )
        )
    merged = merge_graphs(graphs)
    parent_cfg = load_scan_config(roots[0].parent if roots else Path("."), config=config)
    services.update(parent_cfg.services)
    merged.attrs["services"] = services
    _relink_http_from_graph(merged, services)
    if profile:
        sys.stderr.write(f"bizmap profile: merged {len(roots)} repos nodes={len(merged.nodes)}\n")
        sys.stderr.flush()
    return merged


def _relink_http_from_graph(graph: Graph, services: dict[str, str]) -> None:
    """Match functions that already have no calls_http using stored http path attrs.

    Intra-repo linking happens in `_scan_one`. After merge, qualify_graph rewrites
    ids; existing calls_http edges are preserved. This pass matches any
    `http_call` attrs left on functions (none today) and is a no-op besides
    matching calls_http targets that were intra-repo-only when a service base
    now maps onto another repo's route — handled by scanning `calls_http`
    evidence URLs is not stored. Instead, re-match using entrypoint paths vs
    edges already present is sufficient when each scan recorded calls_http
    against local routes only.

    Cross-repo: look at function nodes with attr `http_calls` if present.
    """
    routes = [
        (n.id, str(n.attrs.get("method") or ""), str(n.attrs.get("path") or ""))
        for n in graph.nodes.values()
        if n.type == "entrypoint"
        and n.attrs.get("kind") in {None, "http", "firebase_http", "cloud_task"}
        and n.attrs.get("method")
        and n.attrs.get("path")
    ]
    unmatched: list[dict] = []
    for node in graph.nodes.values():
        pending = node.attrs.get("http_calls") or []
        if not pending:
            continue
        for item in pending:
            method = str(item.get("method") or "GET")
            url = str(item.get("url") or "")
            ev = Evidence.from_dict(item.get("evidence")) if item.get("evidence") else None
            hits = match_http(method, url, routes, services)
            if hits:
                for eid in hits:
                    graph.add_edge(
                        "calls_http", node.id, eid, confidence=Confidence.INFERRED, evidence=ev
                    )
                continue
            keys = unresolved_http_keys(url)
            if keys:
                unmatched.append(
                    {"url": url, "method": method, "keys": keys, "from": node.id}
                )
    graph.attrs["unresolved_http"] = unmatched


def _scan_one(
    root: Path,
    *,
    extra_rules: list[Path] | None = None,
    expand_star: bool | None = None,
    exclude: list[str] | None = None,
    config: Path | None = None,
    profile: bool = False,
    cfg: ScanConfig | None = None,
) -> Graph:
    timings = ScanProfile()
    cfg = cfg or load_scan_config(root, config=config)
    exclude_pats = list(cfg.exclude)
    if exclude:
        exclude_pats.extend(exclude)
    expand = cfg.expand_star if expand_star is None else expand_star
    rules = load_rules(extra_rules)
    graph = Graph()
    graph.repo = cfg.name or root.name
    graph.database = cfg.database
    graph.attrs["repo"] = graph.repo
    graph.attrs["database"] = graph.database
    graph.attrs["services"] = cfg.services
    graph.version = GRAPH_VERSION

    t0 = time.perf_counter()
    sql_files = discover_sql_files(root, exclude=exclude_pats)
    schema = replay_files(sql_files, root)
    go_files = iter_files(root, [".go"], exclude=exclude_pats)
    py_files = iter_files(root, [".py"], exclude=exclude_pats)
    js_files = iter_files(root, [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"], exclude=exclude_pats)
    timings.parse += time.perf_counter() - t0

    go_analysis = GoAnalyzer(rules).analyze(go_files, root, profile=timings)
    t_py = time.perf_counter()
    py_analysis = PythonAnalyzer(rules).analyze(py_files, root)
    timings.resolve += time.perf_counter() - t_py
    js_analysis = JsAnalyzer(rules).analyze(js_files, root)

    _merge_schema(schema, py_analysis.schema)
    _add_schema_nodes(graph, schema)

    go_sql, go_prop = _apply_go(graph, go_analysis, schema, expand_star=expand)
    py_sql, py_prop = _apply_python(graph, py_analysis, schema, expand_star=expand)
    _apply_js(graph, js_analysis, schema)
    timings.sql += go_sql + py_sql
    t_prop = time.perf_counter()
    _apply_sql_functions(graph, schema)
    _apply_db_triggers(graph, schema)
    _link_http(graph, py_analysis, js_analysis, cfg.services)
    _link_queue_consumers(graph, py_analysis, cfg)
    # stash client calls on function nodes so merge can re-match cross-repo
    for call in py_analysis.http_calls:
        fid = function_id(call.function.qualname)
        node = graph.get(fid)
        if node is None:
            continue
        bucket = list(node.attrs.get("http_calls") or [])
        bucket.append(
            {
                "method": call.method,
                "url": call.url,
                "evidence": call.evidence.to_dict(),
            }
        )
        node.attrs["http_calls"] = bucket
    for call in js_analysis.http_calls:
        fid = function_id(call.symbol)
        node = graph.get(fid)
        if node is None:
            graph.add_node(
                fid,
                "function",
                name=call.symbol,
                language="javascript",
                evidence={"file": call.file, "line": call.line, "symbol": call.symbol},
            )
            node = graph.get(fid)
        if node is None:
            continue
        bucket = list(node.attrs.get("http_calls") or [])
        bucket.append(
            {
                "method": call.method,
                "url": call.url,
                "evidence": {"file": call.file, "line": call.line, "symbol": call.symbol},
            }
        )
        node.attrs["http_calls"] = bucket
    _link_firestore_triggers(graph)
    if cfg.path:
        apply_overrides(graph, cfg.path)
    timings.propagate += go_prop + py_prop + (time.perf_counter() - t_prop)

    if profile:
        sys.stderr.write(timings.format_line() + "\n")
        sys.stderr.flush()
    return graph
