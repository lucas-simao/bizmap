from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from bizmap.detect.go_lang import GoAnalysis, GoAnalyzer, GoFunction
from bizmap.detect.python_lang import PyAnalysis, PythonAnalyzer
from bizmap.detect.rules import load_rules
from bizmap.files import discover_sql_files, iter_files
from bizmap.graph import (
    Confidence,
    Evidence,
    Graph,
    column_id,
    entrypoint_id,
    function_id,
    table_id,
    trigger_id,
)
from bizmap.overrides import apply_overrides, load_scan_config
from bizmap.schema_replay import Schema, replay_files
from bizmap.sql_analyze import SqlAccess, analyze_sql, is_external_table


def _add_schema_nodes(graph: Graph, schema: Schema) -> None:
    graph.schema = schema.to_dict()
    for tname, table in schema.tables.items():
        tid = table_id(tname)
        graph.add_node(tid, "table", name=tname)
        for col in table.columns.values():
            graph.add_node(
                column_id(tname, col.name),
                "column",
                table=tname,
                name=col.name,
                data_type=col.data_type,
                nullable=col.nullable,
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
    known = tname in {t.lower() for t in schema.tables}
    external = access.external or is_external_table(tname)
    if external:
        tid = table_id(tname)
        graph.add_node(tid, "table", name=tname, external=True)
        if access.column:
            cid = column_id(tname, access.column)
            graph.add_node(cid, "column", table=tname, name=access.column.lower(), external=True)
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
    """Copy origin accesses up the call graph as **table-level** inferred edges."""
    table_accesses: list[tuple[str, SqlAccess]] = []
    seen_tables: set[tuple[str, str]] = set()
    for access in accesses:
        tname = "unknown" if access.unknown_table or not access.table else access.table.lower()
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
) -> None:
    colmap = schema.column_names()
    known = set(colmap)
    evidence_by_fn: dict[str, Evidence] = {}
    for fn in analysis.functions.values():
        _add_function_node_go(graph, fn)
        evidence_by_fn[fn.qualname] = Evidence(file=fn.file, line=fn.line, symbol=fn.qualname)

    reverse: dict[str, set[str]] = defaultdict(set)
    for caller, callee, ev, conf in analysis.calls:
        graph.add_edge(
            "calls",
            function_id(caller),
            function_id(callee),
            confidence=conf,
            evidence=ev,
        )
        reverse[callee].add(caller)

    entrypoints_by_handler: dict[str, list[str]] = defaultdict(list)
    job_functions: set[str] = {j.handler.qualname for j in analysis.jobs}

    for route in analysis.routes:
        handler = route.handler
        key = f"{route.method}:{route.path}"
        eid = entrypoint_id("http", key)
        conf = route.confidence
        if route.path_confidence == Confidence.UNKNOWN:
            conf = Confidence.UNKNOWN
        graph.add_node(
            eid,
            "entrypoint",
            kind="http",
            method=route.method,
            path=route.path,
            symbol=handler.qualname if handler else route.handler_expr,
            why=handler.why if handler else None,
            confidence=conf.value,
            candidates=list(route.candidates or []),
            evidence=route.evidence.to_dict(),
        )
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

    for site in analysis.sql_sites:
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
        _propagate(
            graph,
            site.function.qualname,
            accesses,
            reverse,
            evidence_by_fn,
            entrypoints_by_handler,
            schema,
            job_functions,
        )


def _apply_python(
    graph: Graph,
    analysis: PyAnalysis,
    schema: Schema,
    *,
    expand_star: bool = False,
) -> None:
    colmap = schema.column_names()
    known = set(colmap)
    evidence_by_fn: dict[str, Evidence] = {}
    reverse: dict[str, set[str]] = defaultdict(set)
    for fn in analysis.functions.values():
        fid = function_id(fn.qualname)
        graph.add_node(
            fid,
            "function",
            name=fn.qualname,
            language="python",
            why=fn.why,
            evidence={"file": fn.file, "line": fn.line, "symbol": fn.qualname},
        )
        evidence_by_fn[fn.qualname] = Evidence(file=fn.file, line=fn.line, symbol=fn.qualname)
    for caller, callee, ev, conf in analysis.calls:
        graph.add_edge("calls", function_id(caller), function_id(callee), confidence=conf, evidence=ev)
        reverse[callee].add(caller)

    entrypoints_by_handler: dict[str, list[str]] = defaultdict(list)
    for route in analysis.routes:
        eid = entrypoint_id("http", f"{route.method}:{route.path}")
        graph.add_node(
            eid,
            "entrypoint",
            kind="http",
            method=route.method,
            path=route.path,
            symbol=route.handler.qualname,
            why=route.handler.why,
            evidence=route.evidence.to_dict(),
        )
        graph.add_edge(
            "triggers",
            eid,
            function_id(route.handler.qualname),
            confidence=Confidence.EXACT,
            evidence=route.evidence,
        )
        entrypoints_by_handler[route.handler.qualname].append(eid)

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
        _propagate(
            graph,
            site.function.qualname,
            accesses,
            reverse,
            evidence_by_fn,
            entrypoints_by_handler,
            schema,
            set(),
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
) -> Graph:
    root = root.resolve()
    cfg = load_scan_config(root)
    exclude_pats = list(cfg.exclude)
    if exclude:
        exclude_pats.extend(exclude)
    expand = cfg.expand_star if expand_star is None else expand_star
    rules = load_rules(extra_rules)
    graph = Graph()

    sql_files = discover_sql_files(root, exclude=exclude_pats)
    schema = replay_files(sql_files, root)
    _add_schema_nodes(graph, schema)

    go_files = iter_files(root, [".go"], exclude=exclude_pats)
    py_files = iter_files(root, [".py"], exclude=exclude_pats)

    go_analysis = GoAnalyzer(rules).analyze(go_files, root)
    py_analysis = PythonAnalyzer(rules).analyze(py_files, root)

    _apply_go(graph, go_analysis, schema, expand_star=expand)
    _apply_python(graph, py_analysis, schema, expand_star=expand)
    _apply_db_triggers(graph, schema)

    if cfg.path:
        apply_overrides(graph, cfg.path)
    return graph
