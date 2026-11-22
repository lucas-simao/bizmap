from __future__ import annotations

import os
import re
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bizmap import dataflow, dbident, fsapply, progress
from bizmap.detect import go_helpers, js_firebase
from bizmap.detect.go_lang import GoAnalysis, GoAnalyzer, GoFunction
from bizmap.detect.js_lang import JsAnalysis, JsAnalyzer
from bizmap.detect.python_lang import PyAccess, PyAnalysis, PythonAnalyzer, valid_queue_name
from bizmap.detect.rules import load_rules
from bizmap.detect.signatures import go_signature, py_signature
from bizmap.files import WalkOptions, discover_sql_files, iter_files, rel
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
    _service_fields,
    base_is_known,
    classify_unresolved_http,
    drop_leading_placeholders,
    has_explicit_base,
    leading_env_key,
    match_http,
    normalize_path,
    path_template_match,
    path_without_placeholders,
    route_literal_hits,
    service_for_url,
    strip_service_base,
    suffix_template_match,
    unresolved_http_keys,
)
from bizmap.overrides import ScanConfig, apply_overrides, load_scan_config
from bizmap.registry import merge_graphs
from bizmap.schema_replay import DbTrigger, Schema, replay_files
from bizmap.spans import (
    fill_spans,
    split_factory_members,
    split_go_route_closures,
    split_js_inline_handlers,
)
from bizmap.sql_analyze import (
    SqlAccess,
    analyze_sql,
    drain_sql_failures,
    ident_leaf,
    is_external_table,
    is_skipped_table_name,
)
from bizmap.srctext import source_texts


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
        if table.dropped:
            continue
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


# id(schema) → (schema, (tables, views) sizes, live tables, live tables + views).
# Built once per schema instead of on every access target (called for every
# ancestor × access during propagation).
_SCHEMA_NAMES: dict[int, tuple[Schema, tuple[int, int], frozenset[str], frozenset[str]]] = {}


def _schema_names(schema: Schema) -> tuple[frozenset[str], frozenset[str]]:
    # the schema is complete (replayed) before any access is linked; the sizes
    # guard against a schema object that was extended afterwards
    sig = (len(schema.tables), len(schema.views))
    cached = _SCHEMA_NAMES.get(id(schema))
    if cached is None or cached[0] is not schema or cached[1] != sig:
        live = frozenset(t.lower() for t, tbl in schema.tables.items() if not tbl.dropped)
        known = live | frozenset(t.lower() for t in schema.views)
        if len(_SCHEMA_NAMES) > 16:
            _SCHEMA_NAMES.clear()
        cached = (schema, sig, live, known)
        _SCHEMA_NAMES[id(schema)] = cached
    return cached[2], cached[3]


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
    live_tables, ddl_known = _schema_names(schema)
    table_obj = schema.tables.get(tname) or schema.tables.get(ident_leaf(tname))
    dropped = bool(table_obj and table_obj.dropped)
    known = (tname in live_tables) or tname in schema.views
    external = access.external or is_external_table(tname)
    if external:
        tid = table_id(tname)
        graph.add_node(
            tid,
            "table",
            name=tname,
            external=True,
            kind="external_relation",
        )
        if access.column:
            cid = column_id(tname, access.column)
            graph.add_node(
                cid, "column", table=tname, name=access.column.lower(), external=True
            )
            return cid
        return tid
    if dropped:
        tid = table_id(tname)
        graph.add_node(tid, "table", name=tname, dropped=True)
        if access.column:
            cid = column_id(tname, access.column)
            if cid not in graph.nodes:
                graph.add_node(cid, "column", table=tname, name=access.column.lower())
            return cid
        return tid
    if ddl_known and not known:
        # Named FROM/JOIN/INSERT/UPDATE relation missing from DDL.
        if access.kind == "read":
            vid = view_id(tname)
            graph.add_node(vid, "view", name=tname, kind="unresolved_relation")
            if access.column:
                cid = column_id(tname, access.column)
                if cid not in graph.nodes:
                    graph.add_node(cid, "column", table=tname, name=access.column.lower())
                return cid
            return vid
        tid = table_id(tname)
        graph.add_node(tid, "table", name=tname, kind="unresolved_relation")
        if access.column:
            cid = column_id(tname, access.column)
            if cid not in graph.nodes:
                graph.add_node(cid, "column", table=tname, name=access.column.lower())
            return cid
        return tid
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


_WRITE_OPS = re.compile(r"\b(INSERT\s+INTO|UPDATE\s|DELETE\s+FROM|MERGE\s+INTO|UPSERT)\b", re.IGNORECASE)


def sql_write_ops(sql: str | None) -> list[str]:
    """INSERT / UPDATE / DELETE in a statement (an upsert is INSERT + UPDATE)."""
    ops: set[str] = set()
    text_ = sql or ""
    for m in _WRITE_OPS.finditer(text_):
        word = m.group(1).split()[0].upper()
        if word in {"MERGE", "UPSERT"}:
            ops |= {"INSERT", "UPDATE"}
        else:
            ops.add(word)
    if re.search(r"\bON\s+CONFLICT\b[\s\S]*\bDO\s+UPDATE\b|\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", text_, re.I):
        ops.add("UPDATE")
    return sorted(ops)


def _direct_data_edge(
    graph: Graph, kind: str, fid: str, target: str, access: SqlAccess, **kwargs: object
) -> None:
    """A read/write the function itself runs. Writes remember which SQL
    operations they come from (`ops`), so DB triggers fire only on their event."""
    edge = graph.add_edge(kind, fid, target, **kwargs)  # type: ignore[arg-type]
    if kind != "writes":
        return
    ops = sql_write_ops(access.raw_sql)
    if ops:
        edge.attrs["ops"] = sorted(set(edge.attrs.get("ops") or []) | set(ops))


def _end_line(node: object) -> int | None:
    """Last line (1-based) of a tree-sitter node, for diff → function mapping."""
    point = getattr(node, "end_point", None)
    if point is None:
        return None
    try:
        return int(point[0]) + 1
    except (TypeError, ValueError, IndexError):
        return None


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
        end_line=None if fn.synthetic else _end_line(fn.node),
        evidence={"file": fn.file, "line": fn.line, "symbol": fn.qualname},
        **({} if fn.synthetic else go_signature(fn.node)),
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
    callers_cache: dict[str, set[str]] | None = None,
) -> None:
    """Copy origin accesses up the call graph as **table-level** inferred edges.

    ``callers_cache`` memoizes the callers of each origin across calls with the
    same (unchanged) ``reverse_calls``.

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

    if callers_cache is None:
        ancestors = _callers_of(origin_fn, reverse_calls)
    else:
        ancestors = callers_cache.get(origin_fn)
        if ancestors is None:
            ancestors = callers_cache[origin_fn] = _callers_of(origin_fn, reverse_calls)
    # each access's target is created on first use (as before), then reused
    targets: dict[int, str | None] = {}

    def target_of(i: int, access: SqlAccess) -> str | None:
        if i not in targets:
            targets[i] = _ensure_access_target(graph, schema, access)
        return targets[i]

    for anc in ancestors:
        if _is_func_main(anc) and anc not in job_functions:
            continue
        fid = function_id(anc)
        if fid not in graph.nodes:
            continue
        ev = evidence_by_fn.get(anc) or evidence_by_fn.get(origin_fn)
        for i, (kind, access) in enumerate(table_accesses):
            target = target_of(i, access)
            if not target:
                continue
            graph.add_edge(kind, fid, target, confidence=Confidence.INFERRED, evidence=ev, derived="call")
        for ep in entrypoints_by_handler.get(anc, []):
            for i, (kind, access) in enumerate(table_accesses):
                target = target_of(i, access)
                if not target:
                    continue
                graph.add_edge(kind, ep, target, confidence=Confidence.INFERRED, evidence=ev, derived="call")
    for ep in entrypoints_by_handler.get(origin_fn, []):
        for i, (kind, access) in enumerate(table_accesses):
            target = target_of(i, access)
            if not target:
                continue
            graph.add_edge(
                kind,
                ep,
                target,
                confidence=Confidence.INFERRED,
                evidence=evidence_by_fn.get(origin_fn),
                derived="call",
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
    _attach_unresolved_calls(
        graph,
        [
            (u.caller, u.callee, getattr(u, "reason", None))
            for u in getattr(analysis, "unresolved_calls", []) or []
        ],
    )

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
            for part in getattr(route, "factory_parts", None) or []:
                graph.add_edge(
                    "triggers",
                    eid,
                    function_id(part.qualname),
                    confidence=Confidence.INFERRED,
                    evidence=route.evidence,
                    via="handler_factory",
                )
                if eid not in entrypoints_by_handler[part.qualname]:
                    entrypoints_by_handler[part.qualname].append(eid)

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
            _direct_data_edge(
                graph,
                _kind_edge(access.kind),
                fid,
                target,
                access,
                confidence=access.confidence,
                evidence=site.evidence,
                **sit_kwargs,
            )
        origin_accesses.append((site.function.qualname, accesses, site.evidence))
    sql_s = time.perf_counter() - t_sql
    t_prop = time.perf_counter()
    origin_unknown: dict[str, int] = defaultdict(int)
    callers_cache: dict[str, set[str]] = {}
    for prop_n, (origin_fn, accesses, _ev) in enumerate(origin_accesses):
        progress.counter("graph: propagating Go data accesses", prop_n, len(origin_accesses))
        unknown_n = sum(
            1 for a in accesses if a.unknown_table or (a.table or "").lower() == "unknown"
        )
        if unknown_n:
            origin_unknown[origin_fn] += unknown_n
        _propagate(
            graph,
            origin_fn,
            accesses,
            reverse,
            evidence_by_fn,
            entrypoints_by_handler,
            schema,
            job_functions,
            callers_cache,
        )
    _copy_unresolved_to_entrypoints(graph, entrypoints_by_handler, reverse)
    _copy_dynamic_table_to_entrypoints(graph, origin_unknown, reverse, entrypoints_by_handler)
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
    edge = graph.add_edge(
        _kind_edge(access.kind),
        fid,
        target,
        confidence=access.confidence,
        evidence=access.evidence,
    )
    op = getattr(access, "op", None)
    if op and edge.type == "writes":
        edge.attrs["ops"] = sorted(set(edge.attrs.get("ops") or []) | {op})
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
            end_line=_end_line(fn.node),
            evidence={"file": fn.file, "line": fn.line, "symbol": fn.qualname},
            **py_signature(fn.node),
            **extra,
        )
        evidence_by_fn[fn.qualname] = Evidence(file=fn.file, line=fn.line, symbol=fn.qualname)
    _attach_unresolved_calls(
        graph,
        [
            (u.caller, u.callee)
            for u in getattr(analysis, "unresolved_calls", []) or []
        ],
    )
    for (caller, callee), reason in sorted((getattr(analysis, "unresolved_reasons", None) or {}).items()):
        node = graph.get(function_id(caller))
        if node is not None:
            reasons = dict(node.attrs.get("unresolved_call_reasons") or {})
            reasons[callee] = reason
            node.attrs["unresolved_call_reasons"] = reasons
    resolved_by_caller: dict[str, set[str]] = defaultdict(set)
    for caller, callee, _ev, _conf in analysis.calls:
        resolved_by_caller[caller].add(callee)
    for caller, targets in resolved_by_caller.items():
        node = graph.get(function_id(caller))
        if node is None or not node.attrs.get("unresolved_calls"):
            continue
        # a `target_not_scanned` label naming a callee we did resolve is a
        # duplicate. (Protocol/interface notes stay: a stub is not an
        # implementation.)
        reasons_now = node.attrs.get("unresolved_call_reasons") or {}
        kept = [
            name
            for name in node.attrs["unresolved_calls"]
            if not (
                reasons_now.get(name) == "target_not_scanned"
                and any(t == name or t.endswith("." + name) for t in targets)
            )
        ]
        if len(kept) != len(node.attrs["unresolved_calls"]):
            node.attrs["unresolved_calls"] = kept
            reasons = node.attrs.get("unresolved_call_reasons")
            if isinstance(reasons, dict):
                node.attrs["unresolved_call_reasons"] = {k: v for k, v in reasons.items() if k in kept}
    for caller, callee, ev, conf in analysis.calls:
        via = (getattr(analysis, "call_vias", None) or {}).get((caller, callee))
        graph.add_edge(
            "calls",
            function_id(caller),
            function_id(callee),
            confidence=conf,
            evidence=ev,
            **({"via": via} if via else {}),
        )
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
            conf = getattr(site, "confidence", None) or access.confidence
            via = getattr(site, "via", None)
            extra_attrs: dict = {}
            if via:
                extra_attrs["via"] = via
            if getattr(site, "sink_not_scanned", False):
                extra_attrs["sink_not_scanned"] = True
            _direct_data_edge(
                graph,
                _kind_edge(access.kind),
                fid,
                target,
                access,
                confidence=conf,
                evidence=site.evidence,
                **extra_attrs,
            )
            note = getattr(site, "note", None)
            if note:
                node = graph.get(fid)
                if node is not None:
                    notes = list(node.attrs.get("notes") or [])
                    if note not in notes:
                        notes.append(note)
                    node.attrs["notes"] = notes
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
    origin_unknown: dict[str, int] = defaultdict(int)
    callers_cache: dict[str, set[str]] = {}
    for prop_n, (origin_fn, accesses) in enumerate(origin_accesses):
        progress.counter("graph: propagating Python data accesses", prop_n, len(origin_accesses))
        unknown_n = sum(
            1 for a in accesses if a.unknown_table or (a.table or "").lower() == "unknown"
        )
        if unknown_n:
            origin_unknown[origin_fn] += unknown_n
        _propagate(
            graph,
            origin_fn,
            accesses,
            reverse,
            evidence_by_fn,
            entrypoints_by_handler,
            schema,
            job_functions,
            callers_cache,
        )
    _propagate_collections(graph, analysis, reverse, entrypoints_by_handler)
    _apply_queues(graph, analysis, entrypoints_by_handler)
    _apply_rpc(graph, analysis, schema)
    _copy_unresolved_to_entrypoints(graph, entrypoints_by_handler, reverse)
    _copy_dynamic_table_to_entrypoints(graph, origin_unknown, reverse, entrypoints_by_handler)
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
                    graph.add_edge(kind, fid, cid, confidence=Confidence.INFERRED, derived="call")
            for ep in entrypoints_by_handler.get(anc, []):
                for kind, cid in targets:
                    graph.add_edge(kind, ep, cid, confidence=Confidence.INFERRED, derived="call")


def _apply_queues(graph: Graph, analysis: PyAnalysis, entrypoints_by_handler: dict[str, list[str]]) -> None:
    consumers: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ev in analysis.queue_events:
        if ev.role != "consume" or not ev.name or not valid_queue_name(ev.name):
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
    env_values = getattr(analysis, "env_values", None) or {}  # bizmap.yaml `env:`
    # a publish inside a closure is seen by the closure and by the function around
    # it: it belongs to the innermost one
    innermost: dict[tuple[str, int, str, str], str] = {}
    for ev in analysis.queue_events:
        if ev.role == "produce":
            key = (ev.evidence.file, ev.evidence.line, ev.queue_kind, ev.name or ev.env or "")
            cur = innermost.get(key)
            if cur is None or len(ev.function.qualname) > len(cur):
                innermost[key] = ev.function.qualname
    for ev in analysis.queue_events:
        if ev.role != "produce":
            continue
        key = (ev.evidence.file, ev.evidence.line, ev.queue_kind, ev.name or ev.env or "")
        if innermost.get(key) != ev.function.qualname:
            continue
        name = ev.name or (env_values.get(ev.env) if ev.env else "") or ""
        if not name:
            # a topic held in an env var without a default: a dynamic queue on the function
            node = graph.get(function_id(ev.function.qualname))
            if node is not None and ev.env:
                bucket = list(node.attrs.get("unresolved_queues") or [])
                item = {"kind": ev.queue_kind, "env": ev.env, "evidence": ev.evidence.to_dict()}
                same = [q for q in bucket if q.get("env") == ev.env and (q.get("evidence") or {}).get("line") == ev.evidence.line
                        and (q.get("evidence") or {}).get("file") == ev.evidence.file]
                if not same:
                    bucket.append(item)
                node.attrs["unresolved_queues"] = bucket
            continue
        if not valid_queue_name(name):
            continue
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
        if ev.env:
            graph.nodes[qid].attrs["env"] = ev.env
        edge = graph.add_edge(
            "enqueues",
            function_id(ev.function.qualname),
            qid,
            confidence=Confidence.INFERRED,
            evidence=ev.evidence,
        )
        if ev.queue_kind == "rq" and ev.target_symbol:
            edge.attrs["task"] = ev.target_symbol


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
                if str(hkey).rstrip("/") == str(key).rstrip("/") or (
                    last and str(hkey).endswith("/" + last)
                ):
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
            node.id.lower(),
            node.id.split("/")[-1].lower(),
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
        if not ev.name or not valid_queue_name(ev.name):
            continue
        url = ev.http_url
        topic = ev.name if ev.queue_kind == "pubsub" else ev.name
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


def _attach_unresolved_calls(graph: Graph, pairs: list[tuple]) -> None:
    """``(caller, callee)`` or ``(caller, callee, reason)``; the reason lands
    in ``unresolved_call_reasons`` (default: no_scanned_implementer)."""
    by_caller: dict[str, list[str]] = defaultdict(list)
    reasons: dict[str, dict[str, str]] = defaultdict(dict)
    for item in pairs:
        caller, callee = item[0], item[1]
        reason = item[2] if len(item) > 2 else None
        if callee and callee not in by_caller[caller]:
            by_caller[caller].append(callee)
        if callee and reason:
            reasons[caller].setdefault(callee, reason)
    for caller, names in by_caller.items():
        fid = function_id(caller)
        node = graph.get(fid)
        if node is None:
            graph.add_node(
                fid,
                "function",
                name=caller,
                unresolved_calls=list(names),
                confidence=Confidence.UNKNOWN.value,
            )
            node = graph.get(fid)
        if node is None:
            continue
        existing = list(node.attrs.get("unresolved_calls") or [])
        for name in names:
            if name not in existing:
                existing.append(name)
        node.attrs["unresolved_calls"] = existing
        node.attrs.setdefault("confidence", Confidence.UNKNOWN.value)
        if reasons.get(caller):
            merged = dict(node.attrs.get("unresolved_call_reasons") or {})
            for name, reason in reasons[caller].items():
                merged.setdefault(name, reason)
            node.attrs["unresolved_call_reasons"] = merged


def _copy_dynamic_table_to_entrypoints(
    graph: Graph,
    origin_unknown: dict[str, int],
    reverse_calls: dict[str, set[str]],
    entrypoints_by_handler: dict[str, list[str]],
) -> None:
    """Expose ``table:unknown`` at entrypoints as a flag/count, never as a reach edge."""
    if not origin_unknown:
        return
    for origin, count in origin_unknown.items():
        reached = _callers_of(origin, reverse_calls) | {origin}
        for anc in reached:
            for eid in entrypoints_by_handler.get(anc, []):
                ep = graph.get(eid)
                if ep is None:
                    continue
                ep.attrs["dynamic_table"] = True
                ep.attrs["dynamic_table_unknown"] = int(ep.attrs.get("dynamic_table_unknown") or 0) + count


def _copy_unresolved_to_entrypoints(
    graph: Graph,
    entrypoints_by_handler: dict[str, list[str]],
    reverse_calls: dict[str, set[str]] | None = None,
) -> None:
    """Show on each entrypoint the unresolved calls of its handler and, with
    ``reverse_calls``, of every function the handler reaches."""
    forward: dict[str, set[str]] = defaultdict(set)
    for callee, callers_ in (reverse_calls or {}).items():
        for caller in callers_:
            forward[caller].add(callee)
    for qual, eids in entrypoints_by_handler.items():
        reached = [qual]
        seen = {qual}
        queue = deque([qual])
        while queue:
            cur = queue.popleft()
            for nxt in sorted(forward.get(cur, ())):
                if nxt not in seen:
                    seen.add(nxt)
                    reached.append(nxt)
                    queue.append(nxt)
        names: list[str] = []
        reasons: dict[str, str] = {}
        for fq in reached:
            fn_node = graph.get(function_id(fq))
            if fn_node is None:
                continue
            why = fn_node.attrs.get("unresolved_call_reasons") or {}
            for name in fn_node.attrs.get("unresolved_calls") or []:
                if name not in names:
                    names.append(name)
                if name in why:
                    reasons.setdefault(name, why[name])
        if not names:
            continue
        for eid in eids:
            ep = graph.get(eid)
            if ep is None:
                continue
            existing = list(ep.attrs.get("unresolved_calls") or [])
            for name in names:
                if name not in existing:
                    existing.append(name)
            ep.attrs["unresolved_calls"] = existing
            ep.attrs.setdefault("confidence", Confidence.UNKNOWN.value)
            if reasons:
                merged = dict(ep.attrs.get("unresolved_call_reasons") or {})
                for name, reason in reasons.items():
                    merged.setdefault(name, reason)
                ep.attrs["unresolved_call_reasons"] = merged


def _apply_js(graph: Graph, analysis: JsAnalysis, schema: Schema) -> None:
    ends = getattr(analysis, "function_ends", None) or {}
    whys = getattr(analysis, "function_why", None) or {}
    sigs = getattr(analysis, "function_sigs", None) or {}
    for qual, (file, line) in (analysis.functions or {}).items():
        graph.add_node(
            function_id(qual),
            "function",
            name=qual,
            language="javascript",
            why=whys.get(qual),
            end_line=ends.get(qual),
            evidence={"file": file, "line": line, "symbol": qual},
            **(sigs.get(qual) or {}),
        )
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
        handler_why = whys.get(route.handler_qual or "") or whys.get(route.handler)
        if handler_why:
            kwargs["why"] = handler_why  # like Python/Go: the route shows its handler's doc
        if route.unresolved:
            kwargs["unresolved"] = True
            kwargs["confidence"] = Confidence.UNKNOWN.value
        graph.add_node(eid, "entrypoint", **kwargs)
        hid = function_id(route.handler)
        span_attrs: dict = {}
        if getattr(route, "span", None):
            # the inline handler's own lines: in a chain
            # (`new Hono().post(a).post(b)`) the call starts where the chain does
            span_attrs = {"start_line": route.span[0], "end_line": route.span[1]}
        graph.add_node(
            hid,
            "function",
            name=route.handler,
            language="javascript",
            evidence={"file": route.file, "line": route.line, "symbol": route.handler},
            **span_attrs,
        )
        graph.add_edge(
            "triggers",
            eid,
            hid,
            confidence=Confidence.UNKNOWN if route.unresolved else Confidence.INFERRED,
        )
    reverse: dict[str, set[str]] = defaultdict(set)
    for call in getattr(analysis, "calls", []) or []:
        graph.add_node(
            function_id(call.callee),
            "function",
            name=call.callee,
            language="javascript",
            evidence={"file": call.file, "line": call.line, "symbol": call.callee},
        )
        graph.add_edge(
            "calls",
            function_id(call.caller),
            function_id(call.callee),
            confidence=Confidence.EXACT if getattr(call, "exact", False) else Confidence.INFERRED,
            evidence=Evidence(file=call.file, line=call.line, symbol=call.caller),
        )
        reverse[call.callee].add(call.caller)
    for access in getattr(analysis, "accesses", []) or []:
        rolled = SqlAccess(
            kind=access.kind,
            table=access.table,
            column=None,
            confidence=Confidence.EXACT,
            raw_sql="",
            unknown_table=access.table == "unknown",
        )
        if access.target_kind == "collection":
            target = _ensure_collection(graph, access.table)
        else:
            target = _ensure_access_target(graph, schema, rolled)
        if not target:
            continue
        fid = function_id(access.symbol)
        graph.add_node(
            fid,
            "function",
            name=access.symbol,
            language="javascript",
            evidence={"file": access.file, "line": access.line, "symbol": access.symbol},
        )
        graph.add_edge(
            _kind_edge(access.kind),
            fid,
            target,
            confidence=Confidence.INFERRED if access.table == "unknown" else Confidence.EXACT,
            evidence=Evidence(file=access.file, line=access.line, symbol=access.symbol),
        )
        # Copy table-level edges onto callers and HTTP entrypoints.
        for caller in {access.symbol, *_callers_of(access.symbol, reverse)}:
            for node in graph.nodes.values():
                if node.type != "entrypoint":
                    continue
                if node.attrs.get("symbol") == caller:
                    graph.add_edge(
                        _kind_edge(access.kind),
                        node.id,
                        target,
                        confidence=Confidence.INFERRED,
                        derived="call",
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
    colmap = schema.column_names()
    known = set(colmap) | set(schema.views)
    for site in analysis.sql_sites:
        fid = function_id(site.symbol)
        graph.add_node(
            fid,
            "function",
            name=site.symbol,
            language="javascript",
            evidence={"file": site.file, "line": site.line, "symbol": site.symbol},
        )
        accesses = analyze_sql(
            site.sql,
            schema_columns=colmap,
            dynamic=site.dynamic,
            known_tables=known,
        )
        for access in accesses:
            target = _ensure_access_target(graph, schema, access)
            if not target:
                continue
            _direct_data_edge(
                graph,
                _kind_edge(access.kind),
                fid,
                target,
                access,
                confidence=access.confidence,
                evidence=Evidence(file=site.file, line=site.line, symbol=site.symbol),
            )
            for caller in {site.symbol, *_callers_of(site.symbol, reverse)}:
                if caller != site.symbol:
                    graph.add_edge(
                        _kind_edge(access.kind),
                        function_id(caller),
                        target,
                        confidence=Confidence.INFERRED,
                        derived="call",
                    )
                for node in graph.nodes.values():
                    if node.type != "entrypoint":
                        continue
                    if node.attrs.get("symbol") == caller:
                        graph.add_edge(
                            _kind_edge(access.kind),
                            node.id,
                            target,
                            confidence=Confidence.INFERRED,
                            derived="call",
                        )
    for acc in analysis.table_accesses:
        fid = function_id(acc.symbol)
        graph.add_node(
            fid,
            "function",
            name=acc.symbol,
            language="javascript",
            evidence={"file": acc.file, "line": acc.line, "symbol": acc.symbol},
        )
        rolled = SqlAccess(
            kind=acc.kind,
            table=acc.table,
            column=None,
            confidence=Confidence.INFERRED,
            raw_sql="",
        )
        target = _ensure_access_target(graph, schema, rolled)
        if not target:
            continue
        graph.add_edge(
            _kind_edge(acc.kind),
            fid,
            target,
            confidence=Confidence.INFERRED,
            evidence=Evidence(file=acc.file, line=acc.line, symbol=acc.symbol),
        )


def _stash_http_call(
    graph: Graph,
    fid: str,
    method: str,
    url: str,
    evidence: dict,
    *,
    name: str,
    language: str,
    client_unknown: bool = False,
) -> None:
    node = graph.get(fid)
    if node is None:
        graph.add_node(
            fid,
            "function",
            name=name,
            language=language,
            evidence=evidence,
        )
        node = graph.get(fid)
    if node is None:
        return
    bucket = list(node.attrs.get("http_calls") or [])
    item: dict = {"method": method, "url": url, "evidence": evidence}
    if client_unknown:
        item["client_unknown"] = True
    if item not in bucket:
        bucket.append(item)
    node.attrs["http_calls"] = bucket


_UI_SOURCE_EXTS = (".tsx", ".jsx", ".vue", ".svelte")


_JS_TEST_DIRS = ("__tests__", "__mocks__", "mocks", "__fixtures__", "e2e", "cypress", "playwright")


def _is_js_test_source(path: str) -> bool:
    """`__tests__/`, `mocks/`, `*.test.ts`, `*.spec.tsx`… (JS/TS only)."""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    if not name.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        return False
    if ".test." in name or ".spec." in name or name.startswith("setupTests"):
        return True
    return any(part in _JS_TEST_DIRS for part in parts[:-1])


# a call that sends a request: `apiGet(uid, '/x')`, `fetchJson`, `doRequest`,
# `httpClient`, `axios(...)` — not `push`, `navigate`, `open`, `hyperlink`
_REQUEST_WORDS = frozenset(
    {"get", "post", "put", "patch", "delete", "del", "fetch", "request", "api", "http",
     "send", "query", "mutate", "axios", "ky", "ofetch", "got"}
)
_WORD_SPLIT = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def _literal_is_sent(method: str | None, call: str | None) -> bool:
    """A path literal counts as a request only with a method hint (client
    method, `fetch`, config with url and method) or as an argument of a call
    whose name says it sends one. Anything else (a returned URL, a route
    template, a menu constant, a hyperlink) is navigation."""
    if method:
        return True
    name = (call or "").rsplit(".", 1)[-1]
    if not name:
        return False
    return any(w.lower() in _REQUEST_WORDS for w in _WORD_SPLIT.findall(name))


def _is_ui_source(path: str | None) -> bool:
    """A component file (`.tsx`, `.jsx`, `.vue`, `.svelte`)."""
    return str(path or "").lower().endswith(_UI_SOURCE_EXTS)


def _stash_route_literal(
    graph: Graph,
    fid: str,
    item: dict,
    *,
    name: str,
    language: str,
) -> None:
    node = graph.get(fid)
    if node is None:
        graph.add_node(fid, "function", name=name, language=language, evidence=item.get("evidence"))
        node = graph.get(fid)
    if node is None:
        return
    bucket = list(node.attrs.get("route_literals") or [])
    clean = {k: v for k, v in item.items() if v is not None}
    if clean not in bucket:
        bucket.append(clean)
    node.attrs["route_literals"] = bucket


def _literal_covers(note_url: str, path: str) -> bool:
    """True when an unresolved client URL is the same call as a matched literal."""
    stripped = drop_leading_placeholders(normalize_path(note_url))
    return path_template_match(stripped, path) or suffix_template_match(stripped, path)


def _drop_route_literal_edges(graph: Graph) -> None:
    """Forget literal links from an earlier pass (e.g. one repo alone).

    A literal unique within its own repo may be ambiguous across all scanned
    services; the pass on the merged graph recomputes every literal link.
    """
    for eid in [e.id for e in graph.edges.values() if e.type == "calls_http" and e.attrs.get("via") == "route_literal"]:
        del graph.edges[eid]


def _link_route_literals(graph: Graph) -> None:
    """Link API-looking string literals to routes when the client was not traced.

    Fallback for front-end clients built through factories / DI / wrappers the
    detectors cannot follow: a literal like ``/orders/${id}`` inside
    ``loadOrder`` becomes ``calls_http`` (``heuristic``, ``via: route_literal``)
    to the *single* route it matches, only when the call's base is unknown.
    A traced call with a known base (env var / absolute URL) is never
    rerouted; ambiguous literals link to nothing. Both cases are recorded in
    the function's ``route_literal_skipped`` with the discarded candidates.
    Literals never link to a route registered in the same file.
    """
    routes: list[tuple[str, str, str]] = []
    route_home: dict[str, tuple[str, str]] = {}
    for node in graph.nodes.values():
        if node.type != "entrypoint":
            continue
        if node.attrs.get("kind") not in {None, "http", "firebase_http"}:
            continue
        if node.attrs.get("unresolved"):
            continue
        method = str(node.attrs.get("method") or "")
        path = str(node.attrs.get("path") or "")
        if not method or not path:
            continue
        routes.append((node.id, method, path))
        ev = node.attrs.get("evidence") or {}
        ev_file = ev.get("file") if isinstance(ev, dict) else getattr(ev, "file", "")
        route_home[node.id] = (str(node.attrs.get("repo") or graph.repo or ""), str(ev_file or ""))
    _drop_route_literal_edges(graph)
    if not routes:
        return
    existing: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges.values():
        if edge.type == "calls_http":
            existing[edge.from_id].add(edge.to_id)
    covered: dict[str, list[str]] = defaultdict(list)
    for node in list(graph.nodes.values()):
        literals = node.attrs.get("route_literals") or []
        if not literals:
            continue
        repo = str(node.attrs.get("repo") or graph.repo or "")
        traced = [
            str(item.get("url") or "")
            for item in (node.attrs.get("http_calls") or [])
            if isinstance(item, dict)
        ]
        skipped: list[dict] = []
        for lit in literals:
            path = str(lit.get("path") or "")
            ev_raw = lit.get("evidence") or {}
            lit_file = str(ev_raw.get("file") or "")
            # Only the final URL counts: if a traced call of this function
            # covers the literal, match its full path; an explicit base (even
            # unknown) means the path must match whole, never by its tail.
            lit_traced = list(traced)
            if lit.get("bound_base"):
                # every caller passes this concrete base for the URL's base
                # parameter: treat it as the call's own base
                lit_traced.append(str(lit["bound_base"]).rstrip("/") + path)
            covering = [u for u in lit_traced if _literal_covers(u, path)]
            explicit = bool(lit.get("explicit_base")) or any(has_explicit_base(u) for u in covering)
            match_path = path
            for u in covering:
                full = drop_leading_placeholders(normalize_path(strip_service_base(u, graph.attrs.get("services") or {})))
                if len(full) > len(match_path):
                    match_path = full
            hits = [
                eid
                for eid in route_literal_hits(
                    match_path, lit.get("method"), routes, allow_suffix=not explicit
                )
                if route_home.get(eid) != (repo, lit_file)
                and not (lit.get("same_repo") and route_home.get(eid, ("", ""))[0] != repo)
            ]
            if not hits:
                continue
            # A traced client call for this path with a concrete base (env var
            # or absolute URL) already says where it goes. If that base names a
            # scanned service and exactly one of *its* routes matches, link it
            # as `inferred` (same evidence level as a traced call); otherwise the
            # literal must not reroute the call.
            confidence = Confidence.HEURISTIC
            known = sorted({u for u in lit_traced if base_is_known(u) and _literal_covers(u, path)})
            if known:
                service = service_for_url(known[0], graph.attrs.get("services") or {})
                own = [eid for eid in hits if service and route_home.get(eid, ("", ""))[0] == service]
                same_repo_hits = [eid for eid in hits if route_home.get(eid, ("", ""))[0] == repo]
                if len(own) == 1:
                    hits = own
                    confidence = Confidence.INFERRED
                elif service is None and len(same_repo_hits) == 1:
                    # An env base not declared in `services:` says nothing about
                    # which service it is; a single match in the caller's own repo
                    # (a self-call) is kept as heuristic. Other repos stay out.
                    hits = same_repo_hits
                else:
                    skipped.append(
                        {
                            "path": path,
                            "reason": "base_known",
                            "base": known[0],
                            "service": service,
                            "discarded": sorted(hits),
                        }
                    )
                    continue
            if len(hits) != 1:
                skipped.append({"path": path, "reason": "ambiguous", "discarded": sorted(hits)})
                continue
            target = hits[0]
            covered[node.id].append(path)
            if target in existing[node.id]:
                continue
            rpath = next((p for eid, _m, p in routes if eid == target), "")
            graph.add_edge(
                "calls_http",
                node.id,
                target,
                confidence=confidence,
                evidence=Evidence.from_dict(ev_raw),
                via="route_literal",
                match="full" if path_template_match(rpath, path) else "suffix",
                **({"via_helper": lit["via_helper"]} if lit.get("via_helper") else {}),
                **({"origin": lit["origin"]} if lit.get("origin") else {}),
            )
            existing[node.id].add(target)
        if skipped:
            node.attrs["route_literal_skipped"] = [
                {k: v for k, v in item.items() if v not in (None, "", [])} for item in skipped
            ]
        else:
            node.attrs.pop("route_literal_skipped", None)
    if not covered:
        return
    for fid, paths in covered.items():
        node = graph.get(fid)
        if node is None:
            continue
        notes = node.attrs.get("unresolved_http") or []
        kept = [
            n
            for n in notes
            if not (isinstance(n, dict) and any(_literal_covers(str(n.get("url") or ""), p) for p in paths))
        ]
        if len(kept) != len(notes):
            node.attrs["unresolved_http"] = kept
    summary = graph.attrs.get("unresolved_http") or []
    graph.attrs["unresolved_http"] = [
        u
        for u in summary
        if not (
            u.get("from") in covered
            and any(_literal_covers(str(u.get("url") or ""), p) for p in covered[u["from"]])
        )
    ]


def _link_http(
    graph: Graph,
    py: PyAnalysis | None,
    js: JsAnalysis | None,
    services: dict[str, str],
    go: GoAnalysis | None = None,
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
    calls: list[tuple[str, str, str, Evidence, Confidence, bool, str]] = []
    if py:
        for c in py.http_calls:
            calls.append(
                (
                    function_id(c.function.qualname),
                    c.method,
                    c.url,
                    c.evidence,
                    c.confidence,
                    bool(getattr(c, "client_unknown", False)),
                    str(getattr(c, "via", "") or ""),
                )
            )
    if js:
        for c in js.http_calls:
            if _is_js_test_source(c.file):
                continue
            ev = Evidence(file=c.file, line=c.line, symbol=c.symbol)
            calls.append(
                (
                    function_id(c.symbol),
                    c.method,
                    c.url,
                    ev,
                    Confidence.INFERRED,
                    bool(getattr(c, "client_unknown", False)),
                    str(getattr(c, "via", "") or ""),
                )
            )
    if go:
        for c in getattr(go, "http_calls", None) or []:
            calls.append(
                (
                    function_id(c.function.qualname),
                    c.method,
                    c.url,
                    c.evidence,
                    Confidence.INFERRED,
                    bool(getattr(c, "client_unknown", False)),
                    str(getattr(c, "via", "") or ""),
                )
            )
    unmatched: list[dict] = []
    route_repos = {
        n.id: str(n.attrs.get("repo") or graph.repo or "")
        for n in graph.nodes.values()
        if n.type == "entrypoint"
    }
    for fid, method, url, ev, conf, client_unknown, via in calls:
        caller_repo = None
        node = graph.get(fid)
        if node is not None:
            caller_repo = str(node.attrs.get("repo") or graph.repo or "") or None
        hits = match_http(
            method,
            url,
            routes,
            services,
            caller_repo=caller_repo,
            route_repos=route_repos,
        )
        if hits:
            for eid in hits:
                graph.add_edge("calls_http", fid, eid, confidence=conf, evidence=ev)
            continue
        keys = unresolved_http_keys(url)
        reason = classify_unresolved_http(url, client_unknown=client_unknown)
        service = service_for_url(url, services)
        node = graph.get(fid)
        if node is not None:
            notes = list(node.attrs.get("unresolved_http") or [])
            note = {"url": url, "method": method, "keys": keys, "reason": reason}
            if service:
                note["service"] = service
            if note not in notes:
                notes.append(note)
            node.attrs["unresolved_http"] = notes
            node.attrs.setdefault("unresolved", True)
        item = {"url": url, "method": method, "keys": keys, "from": fid, "reason": reason}
        if service:
            item["service"] = service
        if via:
            item["via"] = via
        unmatched.append(item)
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


# `bizmap scan --include-tests` sets this for the scan it runs
INCLUDE_TESTS = [False]

_TRIGGER_CHAIN_DEPTH = 4


def _data_table(to_id: str) -> str | None:
    if to_id.startswith("table:"):
        return to_id.split(":", 1)[1].lower()
    if to_id.startswith("column:"):
        return to_id.split(":", 1)[1].split(".", 1)[0].lower()
    return None


def _trigger_fires(trig: DbTrigger, ops: set[str] | None, columns: set[str] | None) -> bool:
    """Does a write with these SQL ``ops`` (None: unknown) to these
    ``columns`` (None: the whole row / unknown) fire ``trig``?"""
    if not trig.events_known or not ops:
        return True
    events = {e.upper() for e in trig.events}
    hit = ops & events
    if not hit:
        return False
    if hit - {"UPDATE"}:
        return True  # INSERT / DELETE fire whatever the columns
    wanted = {c.lower() for c in trig.update_columns}
    if not wanted or columns is None:
        return True
    return bool(columns & wanted)


def _apply_db_triggers(graph: Graph, schema: Schema) -> None:
    """DB triggers: link each trigger to its function and the data it
    writes, then give that effect to the code whose own SQL fires it.

    Only *direct* writes fire a trigger (not copies propagated to callers),
    the SQL operation must match the trigger event (INSERT / UPDATE /
    DELETE), and `UPDATE OF col` needs a write to one of those columns. The
    effect is table-level: on the firing function (`via: db_trigger:NAME`)
    and on its callers and entrypoints (also `derived: call`). Triggers whose
    function writes another table with triggers are followed, up to
    _TRIGGER_CHAIN_DEPTH levels."""
    effects: dict[str, list[tuple[str, str]]] = {}  # trigger fn id → (edge type, table target)
    fn_of_trigger: dict[str, str] = {}
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
            # optional, additive: only for `UPDATE OF col, ...` (others unchanged)
            **({"columns": list(trig.update_columns)} if trig.update_columns else {}),
        )
        fn = schema.functions.get(trig.function)
        if not fn:
            if trig.table:
                graph.add_edge(
                    "triggers",
                    tid,
                    table_id(trig.table),
                    confidence=Confidence.EXACT,
                    evidence=trig.evidence,
                )
            continue
        fid = function_id(f"sql.{fn.name}")
        fn_of_trigger[trig.name] = fid
        graph.add_node(
            fid,
            "function",
            name=fn.name,
            language=fn.language,
            why=None,
            evidence=fn.evidence.to_dict() if fn.evidence else None,
        )
        graph.add_edge("triggers", tid, fid, confidence=Confidence.EXACT, evidence=trig.evidence)
        out: list[tuple[str, str]] = []
        for access in fn.accesses:
            target = _ensure_access_target(graph, schema, access)
            if not target:
                continue
            edge = graph.add_edge(
                _kind_edge(access.kind), fid, target, confidence=access.confidence, evidence=fn.evidence
            )
            if edge.type == "writes":
                ops = sql_write_ops(access.raw_sql)
                if ops:
                    edge.attrs["ops"] = sorted(set(edge.attrs.get("ops") or []) | set(ops))
            graph.add_edge(
                _kind_edge(access.kind), tid, target, confidence=Confidence.EXACT, evidence=trig.evidence
            )
            if access.kind.startswith("write"):
                table = (access.table or "").lower()
                if table and not access.unknown_table and table != "unknown":
                    tgt = _ensure_access_target(
                        graph,
                        schema,
                        SqlAccess(kind=access.kind, table=table, column=None, confidence=Confidence.INFERRED, raw_sql=""),
                    )
                    if tgt and ("writes", tgt) not in out:
                        out.append(("writes", tgt))
        effects[fid] = out

    if not effects:
        return

    # direct writers per table: (function id) → ops / columns / evidence
    writers: dict[str, dict[str, dict]] = defaultdict(dict)
    for edge in graph.edges.values():
        if edge.type != "writes" or edge.attrs.get("derived") or edge.attrs.get("via"):
            continue
        src = graph.nodes.get(edge.from_id)
        if src is None or src.type != "function":
            continue
        table = _data_table(edge.to_id)
        if not table:
            continue
        info = writers[table].setdefault(edge.from_id, {"ops": set(), "any_op": False, "cols": set(), "row": False, "ev": edge.evidence})
        ops = edge.attrs.get("ops")
        if ops:
            info["ops"].update(str(o).upper() for o in ops)
        else:
            info["any_op"] = True
        if edge.to_id.startswith("column:"):
            info["cols"].add(edge.to_id.split(".", 1)[1].lower())
        else:
            info["row"] = True

    # who fires which trigger function
    fired_by: dict[str, dict[str, tuple[str, Evidence | None]]] = defaultdict(dict)  # fid → writer → (trigger, ev)
    for trig in sorted(schema.triggers.values(), key=lambda t: t.name):
        fid = fn_of_trigger.get(trig.name)
        if not fid or not trig.table:
            continue
        for writer, info in sorted(writers.get(trig.table.lower(), {}).items()):
            if writer == fid:
                continue
            ops = None if info["any_op"] else set(info["ops"])
            cols = None if info["row"] else set(info["cols"])
            if _trigger_fires(trig, ops, cols):
                fired_by[fid].setdefault(writer, (trig.name, info["ev"]))

    # call graph for the effect on callers and entrypoints
    reverse: dict[str, set[str]] = defaultdict(set)
    entry_of: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges.values():
        if edge.type == "calls" and edge.confidence != Confidence.UNKNOWN:
            reverse[edge.to_id].add(edge.from_id)
        elif edge.type == "triggers":
            src = graph.nodes.get(edge.from_id)
            if src is not None and src.type == "entrypoint":
                entry_of[edge.to_id].append(edge.from_id)

    def callers(fid: str) -> set[str]:
        seen: set[str] = set()
        queue = deque([fid])
        while queue:
            cur = queue.popleft()
            for parent in reverse.get(cur, ()):
                if parent not in seen and parent != fid:
                    seen.add(parent)
                    queue.append(parent)
        return seen

    for fid in sorted(effects):
        targets = effects[fid]
        if not targets:
            continue
        # writers that fire it, and (for chained triggers) whoever fires those
        causers: dict[str, tuple[str, Evidence | None]] = {}
        frontier = [(fid, 0)]
        while frontier:
            cur, depth = frontier.pop()
            if depth >= _TRIGGER_CHAIN_DEPTH:
                continue
            for writer, (tname, ev) in sorted(fired_by.get(cur, {}).items()):
                if writer in causers or writer == fid:
                    continue
                causers[writer] = (tname, ev)
                if writer in effects:  # a trigger function itself
                    frontier.append((writer, depth + 1))
        for writer, (tname, ev) in sorted(causers.items()):
            via = f"db_trigger:{tname}"
            for kind, target in targets:
                graph.add_edge(kind, writer, target, confidence=Confidence.INFERRED, evidence=ev, via=via)
            for anc in sorted(callers(writer)):
                anc_node = graph.nodes.get(anc)
                if anc_node is None or anc_node.type != "function":
                    continue
                if _is_func_main(str(anc_node.attrs.get("name") or anc.split(":", 1)[-1])):
                    continue
                for kind, target in targets:
                    graph.add_edge(
                        kind, anc, target, confidence=Confidence.INFERRED, evidence=ev, via=via, derived="call"
                    )
            for holder in {writer, *callers(writer)}:
                for ep in entry_of.get(holder, []):
                    for kind, target in targets:
                        graph.add_edge(
                            kind, ep, target, confidence=Confidence.INFERRED, evidence=ev, via=via, derived="call"
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
                base=root,
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
    base: Path | None = None,
    names: dict[Path, str] | None = None,
) -> Graph:
    """Scan several repos jointly. ``base`` is the directory ``repo_roots`` are
    relative to (the workspace root for `repos:` in bizmap.yaml, else cwd).
    ``names`` gives a repo name per root, used when its bizmap.yaml sets none."""
    graphs: list[Graph] = []
    services: dict[str, str] = {}
    roots_by_repo: dict[str, str] = {}
    base_dir = (base or Path.cwd()).resolve()
    for repo_n, root in enumerate(roots, 1):
        root = root.resolve()
        if len(roots) > 1:
            progress.set_prefix(f"[repo {repo_n}/{len(roots)}] ")
        default_name = (names or {}).get(root)
        cfg = _config_for_root(root, config=config, shared_name=False, default_name=default_name)
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
        roots_by_repo[str(graphs[-1].repo)] = Path(os.path.relpath(root, base_dir)).as_posix()
    progress.set_prefix("")
    t_merge = time.perf_counter()
    merged = merge_graphs(graphs)
    if merged.attrs.get("repo_roots_base") != "git":
        merged.attrs["repo_roots"] = roots_by_repo
        merged.attrs["repo_roots_base"] = "scan"
    parent_dir = base.resolve() if base is not None else (roots[0].parent if roots else Path("."))
    parent_cfg = load_scan_config(parent_dir, config=config)
    services.update(parent_cfg.services)
    merged.attrs["services"] = services
    _relink_from_graph(merged, services)
    progress.done(f"merge + cross-service links ({len(roots)} repos)", t_merge)
    if profile:
        sys.stderr.write(f"bizmap profile: merged {len(roots)} repos nodes={len(merged.nodes)}\n")
        sys.stderr.flush()
    return merged


def _config_for_root(
    root: Path, *, config: Path | None, shared_name: bool, default_name: str | None = None
) -> ScanConfig:
    """Load scan config, keeping each repo's own name on multi-root scans."""
    local = load_scan_config(root, config=None)
    cfg = load_scan_config(root, config=config)
    if config is None or shared_name:
        if not cfg.name or (default_name and not local.name):
            cfg.name = default_name or root.name
        return cfg
    name = local.name or default_name or root.name
    services = dict(cfg.services)
    services.update(local.services)
    exclude = list(dict.fromkeys([*cfg.exclude, *local.exclude]))
    raw = dict(cfg.raw or {})
    raw.update(local.raw or {})
    constants = dict(cfg.constants)
    constants.update(local.constants)
    return ScanConfig(
        include=list(dict.fromkeys([*cfg.include, *local.include])),
        gitignore=cfg.gitignore and local.gitignore,
        skip_generated=cfg.skip_generated and local.skip_generated,
        exclude=exclude,
        expand_star=bool(cfg.expand_star or local.expand_star),
        path=local.path or cfg.path,
        raw=raw,
        name=name,
        database=local.database or cfg.database,
        services=services,
        repos=local.repos or cfg.repos,
        constants=constants,
    )


def relink_merged_graph(graph: Graph, services: dict[str, str] | None = None) -> None:
    """Re-run HTTP and queue matching on a (merged) graph using stored call info."""
    services = services or graph.attrs.get("services") or {}
    _warn_unscanned_services(graph, services)
    _drop_route_literal_edges(graph)
    _relink_http_from_graph(graph, services)
    fsapply.link_firebase_callables(graph)
    _link_route_literals(graph)
    _relink_queues_from_graph(graph, services)
    dataflow.link_queue_flows(graph)
    dbident.link_same_tables(graph)


def _warn_unscanned_services(graph: Graph, services: dict) -> None:
    """Warn when a `services:` entry names no scanned repo (multi-repo graphs).

    Calls to such a service stay `target_not_scanned`; if it *is* scanned
    under another name, the entry needs `repo: <name>`.
    """
    repos = {
        str(n.attrs.get("repo"))
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("repo")
    }
    if len(repos) < 1 or not graph.attrs.get("repos"):
        return
    warnings = list(graph.attrs.get("warnings") or [])
    for name, spec in sorted((services or {}).items()):
        target = str(spec.get("repo") or name) if isinstance(spec, dict) else str(name)
        if target in repos or target in (graph.attrs.get("repos") or []):
            continue
        msg = (
            f"services: {name} matches no scanned repo; calls to it stay "
            f"target_not_scanned (add `repo: <name>` if it is scanned under another name)"
        )
        if msg not in warnings:
            warnings.append(msg)
    if warnings:
        graph.attrs["warnings"] = warnings


def _relink_from_graph(graph: Graph, services: dict[str, str] | None = None) -> None:
    relink_merged_graph(graph, services)


def _relink_http_from_graph(graph: Graph, services: dict[str, str]) -> None:
    """Match stored ``http_calls`` against every scanned route in the graph.

    Intra-repo linking happens in `_scan_one`. After merge, qualify_graph rewrites
    ids; this pass re-matches using each function's ``http_calls`` /
    ``unresolved_http`` so cross-repo ``calls_http`` edges match a joint scan.

    Env keys no ``services:`` entry maps (`{VITE_API_URL}/x`) are linked to a
    repo when every call through that key that did match points at that one
    repo: the key's other calls are matched against that repo only
    (`via: env_inferred`). Keys left with unmatched calls get a
    ``services:`` hint (graph attr ``service_hints``)."""
    routes = [
        (n.id, str(n.attrs.get("method") or ""), str(n.attrs.get("path") or ""))
        for n in graph.nodes.values()
        if n.type == "entrypoint"
        and n.attrs.get("kind") in {None, "http", "firebase_http", "cloud_task"}
        and n.attrs.get("method")
        and n.attrs.get("path")
    ]
    route_repos = {
        n.id: str(n.attrs.get("repo") or graph.repo or "")
        for n in graph.nodes.values()
        if n.type == "entrypoint"
    }
    mapped_envs: set[str] = set()
    for name, spec in (services or {}).items():
        mapped_envs.update(_service_fields(str(name), spec)[1])

    # pass 1: match everything; note which repo each unmapped env key reaches
    results: list[tuple[object, list[tuple[str, str, Evidence | None, dict, list[str]]]]] = []
    votes: dict[str, set[str]] = defaultdict(set)
    for node in graph.nodes.values():
        pending = list(node.attrs.get("http_calls") or [])
        if not pending:
            for note in node.attrs.get("unresolved_http") or []:
                if isinstance(note, dict) and note.get("url"):
                    pending.append(note)
        if not pending:
            continue
        caller_repo = str(node.attrs.get("repo") or graph.repo or "") or None
        rows = []
        for item in pending:
            method = str(item.get("method") or "GET")
            url = str(item.get("url") or "")
            if not url:
                continue
            ev = Evidence.from_dict(item.get("evidence")) if item.get("evidence") else None
            hits = match_http(method, url, routes, services, caller_repo=caller_repo, route_repos=route_repos)
            key = leading_env_key(url)
            if hits and key and key not in mapped_envs:
                for eid in hits:
                    target = route_repos.get(eid) or ""
                    if target and target != caller_repo:
                        votes[key].add(target)
            rows.append((method, url, ev, item, hits))
        results.append((node, rows))
    inferred = {key: next(iter(repos)) for key, repos in votes.items() if len(repos) == 1}

    unmatched: list[dict] = []
    other_repos = sorted({r for r in route_repos.values() if r})
    inferred_counts: dict[tuple[str, str], int] = defaultdict(int)
    hint_votes: dict[str, dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))
    prefixes_by_repo: dict[str, list[str]] = {}

    def repo_prefixes(repo: str) -> list[str]:
        """Leading one- and two-segment literal prefixes of a repo's routes
        (`/api`, `/api/v1`), most common first."""
        if repo not in prefixes_by_repo:
            counts: dict[str, int] = defaultdict(int)
            for eid, _m, rpath in routes:
                if route_repos.get(eid) != repo:
                    continue
                parts = [x for x in rpath.split("/") if x]
                for k in (1, 2):
                    head = parts[:k]
                    if len(head) == k and len(parts) > k and all(
                        not x.startswith((":", "{", "<", "*")) for x in head
                    ):
                        counts["/" + "/".join(head)] += 1
            prefixes_by_repo[repo] = [p for p, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:6]
        return prefixes_by_repo[repo]
    hint_calls: dict[str, int] = defaultdict(int)
    for node, rows in results:
        caller_repo = str(node.attrs.get("repo") or graph.repo or "") or None
        still: list[dict] = []
        for method, url, ev, item, hits in rows:
            via = None
            key = leading_env_key(url)
            if not hits and key in inferred:
                repo = inferred[key]
                extra = dict(services or {})
                extra[f"__env_{key}"] = {"env": key, "repo": repo}
                hits = match_http(method, url, routes, extra, caller_repo=caller_repo, route_repos=route_repos)
                if hits:
                    via = "env_inferred"
                    inferred_counts[(key, repo)] += 1
            if hits:
                for eid in hits:
                    attrs = {"via": via} if via else {}
                    graph.add_edge("calls_http", node.id, eid, confidence=Confidence.INFERRED, evidence=ev, **attrs)
                continue
            keys = unresolved_http_keys(url)
            reason = classify_unresolved_http(url, client_unknown=bool(item.get("client_unknown")))
            service = service_for_url(url, services)
            if reason == "target_not_scanned" and key and key not in mapped_envs:
                hint_calls[key] += 1
                for repo in other_repos:
                    if repo == caller_repo:
                        continue
                    trial = {"__hint": {"env": key, "repo": repo}}
                    if match_http(method, url, routes, trial, caller_repo=caller_repo, route_repos=route_repos):
                        hint_votes[key][(repo, "")] += 1
                        continue
                    for prefix in repo_prefixes(repo):
                        trial = {"__hint": {"env": key, "repo": repo, "prefix": prefix}}
                        if match_http(method, url, routes, trial, caller_repo=caller_repo, route_repos=route_repos):
                            hint_votes[key][(repo, prefix)] += 1
                            break
            note = {"url": url, "method": method, "keys": keys, "reason": reason}
            if service:
                note["service"] = service
            if note not in still:
                still.append(note)
            summary = {"url": url, "method": method, "keys": keys, "from": node.id, "reason": reason}
            if service:
                summary["service"] = service
            unmatched.append(summary)
        if still:
            node.attrs["unresolved_http"] = still
            node.attrs.setdefault("unresolved", True)
        elif "unresolved_http" in node.attrs:
            node.attrs["unresolved_http"] = []
    graph.attrs["unresolved_http"] = unmatched
    graph.attrs["inferred_services"] = [
        {"env": key, "repo": repo, "calls": n} for (key, repo), n in sorted(inferred_counts.items())
    ]
    hints = []
    external = []
    for key, n in sorted(hint_calls.items()):
        votes_ = hint_votes.get(key) or {}
        if not votes_:
            # no scanned route matches any of its calls: an outside service
            # (SUPABASE_URL, a SaaS API…), not a repo to map it to
            external.append({"env": key, "calls": n})
            continue
        ranked = sorted(votes_.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            continue  # ambiguous
        (repo, prefix), matching = ranked[0]
        hint = {"env": key, "repo": repo, "calls": n, "matching": matching}
        if prefix:
            hint["prefix"] = prefix
        hints.append(hint)
    graph.attrs["service_hints"] = hints
    graph.attrs["external_envs"] = external


def _relink_queues_from_graph(graph: Graph, services: dict[str, str]) -> None:
    """Re-match Cloud Tasks / Pub/Sub producers to consumer entrypoints after merge."""
    hints = _queue_hints(graph.attrs.get("queues") or {})
    for node in list(graph.nodes.values()):  # the loop may add queue nodes
        for ev in node.attrs.get("queue_events") or []:
            if str(ev.get("role") or "") != "produce":
                continue
            kind = str(ev.get("kind") or ev.get("queue_kind") or "")
            if kind not in {"cloud_tasks", "pubsub"}:
                continue
            url = ev.get("http_url") or ev.get("url")
            topic = ev.get("name") if kind == "pubsub" else ev.get("name")
            consumers = _match_queue_consumer(
                graph,
                url=url,
                topic=topic,
                kind=kind,
                services=services,
                hints=hints,
            )
            produce_qid = None
            for edge in graph.outgoing(node.id, "enqueues"):
                produce_qid = edge.to_id
                break
            if produce_qid is None:
                name = str(ev.get("name") or "")
                if not valid_queue_name(name):
                    continue  # no name (an env var, a parameter) or an expression: no node
                produce_qid = queue_id(kind, name)
                graph.add_node(produce_qid, "queue", kind=kind, name=name)
            evidence = Evidence.from_dict(ev.get("evidence")) if ev.get("evidence") else None
            for eid in consumers:
                graph.add_edge(
                    "triggers",
                    produce_qid,
                    eid,
                    confidence=Confidence.INFERRED,
                    evidence=evidence,
                )


def _warn_graph(graph: Graph, msg: str) -> None:
    existing = list(graph.attrs.get("warnings") or [])
    if msg not in existing:
        existing.append(msg)
    graph.attrs["warnings"] = existing


def _apply_go_cloud(graph: Graph, root: Path, go_files: list[Path], cfg: ScanConfig) -> tuple[dict[str, str], Any]:
    """Firestore and Pub/Sub publishes in Go (a text pass). Returns the texts read
    and what the data-flow pass needs (struct tags, publish topics)."""
    if not go_files:
        return {}, None
    texts = source_texts(root, go_files)
    try:
        env_values = {str(k): str(v) for k, v in ((cfg.raw or {}).get("env") or {}).items()}
        return texts, fsapply.apply_go_cloud(graph, texts, env_values)
    except Exception as exc:  # noqa: BLE001 - never lose the scan over a text pass
        _warn_graph(graph, f"Go Firestore / Pub/Sub pass failed ({type(exc).__name__}: {exc}); skipped")
        return texts, None


def _apply_go_helpers(graph: Graph, root: Path, go_texts: dict[str, str]) -> None:
    """Routes a Go registration helper mounts from an interface method
    (`RegisterScheduled(e, auth, jobs.NewX(..))` → `POST /internal/jobs/x`):
    one entrypoint per call site, triggering the concrete type's methods,
    with their tables rolled up (see ``detect/go_helpers``)."""
    if not go_texts:
        return
    try:
        module = None
        gomod = root / "go.mod"
        if gomod.is_file():
            mm = re.search(r"(?m)^module\s+(\S+)", gomod.read_text(encoding="utf-8", errors="replace"))
            module = mm.group(1) if mm else None
        routes = go_helpers.scan_helpers(go_texts, module)
        if not routes:
            return
        from bizmap.spans import _OutIndex, _table_hits

        index = _OutIndex(graph)
        memo: dict[str, set[tuple[str, str]]] = {}
        for r in routes:
            eid = entrypoint_id("http", f"{r.verb}:{r.path}")
            ev = Evidence(r.file, r.line, r.helper)
            handlers = [
                fid for fid in (function_id(f"{r.type_pkg}.{r.type_name}.{m}") for m in r.handlers)
                if fid in graph.nodes
            ]
            graph.add_node(
                eid, "entrypoint", kind="http", method=r.verb, path=r.path, language="go",
                symbol=f"{r.type_pkg}.{r.type_name}", helper=r.helper,
                confidence=Confidence.INFERRED.value, evidence=ev.to_dict(),
            )
            for fid in handlers:
                index.add("triggers", eid, fid, confidence=Confidence.INFERRED, evidence=ev)
                for etype, target in sorted(_table_hits(graph, index, fid, memo)):
                    index.add(etype, eid, target, confidence=Confidence.INFERRED, evidence=ev, derived="call")
    except Exception as exc:  # noqa: BLE001 - never lose the scan over a text pass
        _warn_graph(graph, f"Go route-helper pass failed ({type(exc).__name__}: {exc}); skipped")


def _apply_js_firestore(graph: Graph, root: Path, js_files: list[Path], analysis: JsAnalysis) -> dict[str, str]:
    """Firestore reads / writes, triggers and Pub/Sub publishes in JS/TS (a
    text pass over the files that use Firestore). Returns the texts read."""
    if not js_files:
        return {}
    texts = source_texts(root, js_files)
    try:
        calls = [(c.caller, c.callee, c.file, c.line) for c in getattr(analysis, "calls", []) or []]
        uses_functions = any("firebase-functions" in t for t in texts.values())
        deployed = js_firebase.deployed_names(root, texts) if uses_functions else {}
        fsapply.apply_js_firestore(
            graph, texts, dict(analysis.functions or {}), dict(getattr(analysis, "function_ends", {}) or {}), calls,
            deployed,
        )
    except Exception as exc:  # noqa: BLE001 - never lose the scan over Firestore
        _warn_graph(graph, f"JS Firestore pass failed ({type(exc).__name__}: {exc}); skipped")
    try:
        fsapply.apply_js_callables(
            graph, texts, dict(analysis.functions or {}), dict(getattr(analysis, "function_ends", {}) or {})
        )
    except Exception as exc:  # noqa: BLE001 - never lose the scan over callables
        _warn_graph(graph, f"JS Firebase callable pass failed ({type(exc).__name__}: {exc}); skipped")
    return texts


def _apply_dataflow(
    graph: Graph, root: Path, cfg: ScanConfig, files: list[Path], known: dict[str, str] | None = None,
    go_meta: Any = None,
) -> None:
    """``db`` on tables and ``feeds`` between them (a text pass after the
    detectors; a failure there only adds a warning)."""
    try:
        known = known or {}
        texts = {
            **source_texts(root, [f for f in files if os.path.relpath(f, root).replace(os.sep, "/") not in known]),
            **known,
        }
        idx = dbident.build_index(texts, cfg.raw)
        idx.engines |= dbident.manifest_engines(root)
        dbident.assign_dbs(graph, texts, idx)
        dataflow.apply(graph, root, texts, firestore=idx.firestore or False, go_meta=go_meta)
    except Exception as exc:  # noqa: BLE001 - never lose the scan over data flow
        existing = list(graph.attrs.get("warnings") or [])
        existing.append(f"data-flow pass failed ({type(exc).__name__}: {exc}); feeds skipped")
        graph.attrs["warnings"] = existing


def _git_toplevel(path: Path) -> Path | None:
    for cand in (path, *path.parents):
        if (cand / ".git").exists():
            return cand
    return None


def _analyze_safely(label, analyze, files, root, failures):  # noqa: ANN001, ANN202
    """Run a detector over ``files``; if it crashes, find the files that make
    it crash (each analysed alone), skip them with a warning and analyse the
    rest, so one pathological file never aborts the whole scan."""
    try:
        return analyze(files)
    except Exception as exc:  # noqa: BLE001 - a detector bug must not kill the scan
        first_error = exc
    bad: list[Path] = []
    for path in files:
        try:
            analyze([path])
        except Exception:  # noqa: BLE001
            bad.append(path)
    good = [p for p in files if p not in bad]
    for path in bad:
        failures.append(
            f"{label} detector failed on {rel(path, root)} ({type(first_error).__name__}); file skipped"
        )
    if not bad:
        failures.append(f"{label} detector failed ({type(first_error).__name__}: {first_error}); results skipped")
        return analyze([])
    try:
        return analyze(good)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{label} detector failed ({type(exc).__name__}); results skipped")
        return analyze([])


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
    git_root = _git_toplevel(root)
    if git_root is not None:
        # relative to the git root, like diff paths: survives `registry merge`
        graph.attrs["repo_roots"] = {graph.repo: Path(os.path.relpath(root, git_root)).as_posix()}
        graph.attrs["repo_roots_base"] = "git"
    else:
        graph.attrs["repo_roots"] = {graph.repo: "."}
        graph.attrs["repo_roots_base"] = "scan"
    graph.attrs["services"] = cfg.services
    graph.attrs["queues"] = (cfg.raw or {}).get("queues") or {}
    graph.version = GRAPH_VERSION

    t0 = time.perf_counter()
    walk = WalkOptions(
        include=cfg.include,
        gitignore=cfg.gitignore,
        skip_generated=cfg.skip_generated,
        include_tests=cfg.include_tests or INCLUDE_TESTS[0],
    )
    sql_files = discover_sql_files(root, exclude=exclude_pats, options=walk)
    schema = replay_files(sql_files, root)
    go_files = iter_files(root, [".go"], exclude=exclude_pats, options=walk)
    py_files = iter_files(root, [".py"], exclude=exclude_pats, options=walk)
    js_files = iter_files(
        root, [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"], exclude=exclude_pats, options=walk
    )
    if walk.stats:
        # how much was left out (generated dirs/files, .gitignore), for `scan` output
        graph.attrs["skipped"] = dict(sorted(walk.stats.items()))
        graph.attrs["skipped_paths"] = walk.skipped_paths()
    timings.parse += time.perf_counter() - t0
    progress.done(
        f"{graph.repo}: files ({len(go_files)} go, {len(py_files)} py, {len(js_files)} js/ts, "
        f"{len(sql_files)} sql)",
        t0,
    )

    t_go = time.perf_counter()
    if go_files:
        progress.begin(f"{graph.repo}: go", len(go_files))

    failures: list[str] = []
    go_analysis = _analyze_safely(
        "Go",
        lambda files: GoAnalyzer(rules).analyze(files, root, profile=timings, constants=cfg.constants),
        go_files,
        root,
        failures,
    )
    if go_files:
        progress.done(f"{graph.repo}: go", t_go)
    warnings = list(getattr(go_analysis, "warnings", None) or []) + failures
    if warnings:
        existing = list(graph.attrs.get("warnings") or [])
        for item in warnings:
            if item not in existing:
                existing.append(item)
        graph.attrs["warnings"] = existing
    t_py = time.perf_counter()
    if py_files:
        progress.begin(f"{graph.repo}: python", len(py_files))
    py_failures: list[str] = []
    py_analysis = _analyze_safely(
        "Python",
        lambda files: PythonAnalyzer(rules).analyze(files, root, constants=cfg.constants),
        py_files,
        root,
        py_failures,
    )
    timings.resolve += time.perf_counter() - t_py
    if py_files:
        progress.done(f"{graph.repo}: python", t_py)
    t_js = time.perf_counter()
    if js_files:
        progress.begin(f"{graph.repo}: js/ts", len(js_files))
    js_failures: list[str] = []
    js_analysis = _analyze_safely(
        "JS/TS",
        lambda files: JsAnalyzer(rules).analyze(files, root, constants=cfg.constants),
        js_files,
        root,
        js_failures,
    )
    if js_files:
        progress.done(f"{graph.repo}: js/ts", t_js)
    t_graph = time.perf_counter()
    progress.begin(f"{graph.repo}: graph")
    if py_failures or js_failures:
        existing = list(graph.attrs.get("warnings") or [])
        for item in py_failures + js_failures:
            if item not in existing:
                existing.append(item)
        graph.attrs["warnings"] = existing

    _merge_schema(schema, py_analysis.schema)
    _add_schema_nodes(graph, schema)

    go_sql, go_prop = _apply_go(graph, go_analysis, schema, expand_star=expand)
    go_texts, go_meta = _apply_go_cloud(graph, root, go_files, cfg)
    py_analysis.env_values = {str(k): str(v) for k, v in ((cfg.raw or {}).get("env") or {}).items()}
    py_sql, py_prop = _apply_python(graph, py_analysis, schema, expand_star=expand)
    _apply_js(graph, js_analysis, schema)
    js_texts = _apply_js_firestore(graph, root, js_files, js_analysis)
    timings.sql += go_sql + py_sql
    t_prop = time.perf_counter()
    _apply_sql_functions(graph, schema)
    _apply_db_triggers(graph, schema)
    _apply_go_helpers(graph, root, go_texts)
    sql_failures = drain_sql_failures()
    if sql_failures:
        existing = list(graph.attrs.get("warnings") or [])
        first, error = sql_failures[0]
        msg = (
            f"SQL analysis failed for {len(sql_failures)} statement(s), skipped "
            f"(first: {first!r}: {error})"
        )
        if msg not in existing:
            existing.append(msg)
        graph.attrs["warnings"] = existing
    # TestClient / ASGITransport calls exercise the app in-process: they are
    # tests, not calls between services (no calls_http, not unresolved)
    in_process = [c for c in py_analysis.http_calls if getattr(c, "in_process", False)]
    if in_process:
        py_analysis.http_calls = [c for c in py_analysis.http_calls if not getattr(c, "in_process", False)]
        graph.attrs["in_process_http"] = len(in_process)
    _link_http(graph, py_analysis, js_analysis, cfg.services, go=go_analysis)
    _link_queue_consumers(graph, py_analysis, cfg)
    # stash client calls on function nodes so merge can re-match cross-repo
    for call in py_analysis.http_calls:
        _stash_http_call(
            graph,
            function_id(call.function.qualname),
            call.method,
            call.url,
            call.evidence.to_dict(),
            name=call.function.qualname,
            language="python",
            client_unknown=bool(getattr(call, "client_unknown", False)),
        )
    for call in js_analysis.http_calls:
        if _is_js_test_source(call.file):
            continue
        _stash_http_call(
            graph,
            function_id(call.symbol),
            call.method,
            call.url,
            {"file": call.file, "line": call.line, "symbol": call.symbol},
            name=call.symbol,
            language="javascript",
            client_unknown=bool(getattr(call, "client_unknown", False)),
        )
    callers_of: dict[str, list] = defaultdict(list)
    for hc in getattr(js_analysis, "helper_calls", None) or []:
        if hc.caller != hc.callee:
            callers_of[hc.callee].append(hc)
    for lit in js_analysis.route_literals:
        if _is_js_test_source(lit.file):
            continue  # a test or a mock (msw `rest.get('/x')`) is not the app calling the API
        if not getattr(lit, "helper", False) and not _literal_is_sent(lit.method, getattr(lit, "call", "")):
            # a path with nothing saying it is a request (no client method, no
            # fetch, no `url` + method, no request-like call) is navigation or
            # a label: not a call to the API
            continue
        base_item = {
            "path": lit.path,
            "method": lit.method,
            "same_repo": True if lit.same_repo else None,
            "evidence": {"file": lit.file, "line": lit.line, "symbol": lit.symbol},
        }
        base_item["explicit_base"] = True if getattr(lit, "explicit_base", False) else None
        base_item["bound_base"] = getattr(lit, "bound_base", None)
        is_fn = lit.symbol in js_analysis.functions or getattr(lit, "member_helper", False)
        wanted = "call" if is_fn else "ref"
        all_sites = (
            [hc for hc in callers_of.get(lit.symbol) or [] if getattr(hc, "kind", "call") == wanted]
            if getattr(lit, "helper", False)
            else None
        )
        sites = [hc for hc in all_sites or [] if getattr(hc, "direct", False)]
        # a path helper used in a component without any request hint
        # (`navigate(paths.widget(id))` through an unknown wrapper) is navigation
        sites = [hc for hc in sites if _literal_is_sent(hc.method or lit.method, getattr(hc, "call", ""))]
        if all_sites and not sites and any(getattr(hc, "direct", False) for hc in all_sites):
            continue
        if all_sites and not sites:
            # Only used inside larger URLs (`${base(id)}/photos`): those
            # composed URLs are literals of their own; this fragment is not a
            # request target.
            continue
        if sites:
            # Path helper / module const: the request is made by its direct user.
            for hc in sites:
                item = dict(base_item)
                item["method"] = hc.method or lit.method
                item["evidence"] = {"file": hc.file, "line": hc.line, "symbol": hc.caller}
                item["via_helper"] = lit.symbol
                _stash_route_literal(
                    graph, function_id(hc.caller), item, name=hc.caller, language="javascript"
                )
            continue
        if getattr(lit, "helper", False):
            # a path helper / module const nobody is seen sending (a URL
            # builder, a route template, a menu entry): navigation, not a call
            continue
        _stash_route_literal(
            graph,
            function_id(lit.symbol),
            base_item,
            name=lit.symbol,
            language="javascript",
        )
    _link_route_literals(graph)
    for call in getattr(go_analysis, "http_calls", None) or []:
        _stash_http_call(
            graph,
            function_id(call.function.qualname),
            call.method,
            call.url,
            call.evidence.to_dict(),
            name=call.function.qualname,
            language="go",
            client_unknown=bool(getattr(call, "client_unknown", False)),
        )
    for ev in py_analysis.queue_events:
        fid = function_id(ev.function.qualname)
        node = graph.get(fid)
        if node is None:
            continue
        bucket = list(node.attrs.get("queue_events") or [])
        bucket.append(
            {
                "kind": ev.queue_kind,
                "name": ev.name,
                "role": ev.role,
                "http_url": ev.http_url,
                "http_method": ev.http_method,
                "evidence": ev.evidence.to_dict(),
            }
        )
        node.attrs["queue_events"] = bucket
    _link_firestore_triggers(graph)
    fsapply.link_firebase_callables(graph)
    try:
        fsapply.apply_push_subscriptions(graph, root, cfg.raw)
    except Exception as exc:  # noqa: BLE001 - a config hint never costs the scan
        _warn_graph(graph, f"push subscriptions skipped ({type(exc).__name__}: {exc})")
    if cfg.path:
        apply_overrides(graph, cfg.path)
    span_cache: dict = {}
    fill_spans(graph, root, span_cache)
    split_factory_members(graph, root, span_cache)
    split_go_route_closures(graph, root, span_cache)  # its own edge index: after the JS moves
    split_js_inline_handlers(graph)
    _apply_dataflow(graph, root, cfg, [*go_files, *py_files, *js_files], {**go_texts, **js_texts}, go_meta)
    timings.propagate += go_prop + py_prop + (time.perf_counter() - t_prop)
    progress.done(f"{graph.repo}: graph", t_graph)

    if profile:
        sys.stderr.write(timings.format_line() + "\n")
        sys.stderr.flush()
    return graph
