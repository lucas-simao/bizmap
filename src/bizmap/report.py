"""Coverage report for a scanned graph, optionally redacted.

``bizmap report`` answers "how much of this codebase did BizMap understand?"
without anyone reading the graph: entrypoints that reach data, HTTP links
matched vs. unresolved (by reason), how links were derived, and confidence.

With ``--redact`` every name (function, route, table, URL, env key, file) is
replaced by a salted hash, so the report can be shared from a private
codebase. Counts and reasons stay readable.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from bizmap.dataflow import flow_summary

DATA_PREFIXES = ("table:", "column:", "view:", "collection:")

REACH_DEFINITION = (
    "an entrypoint reaches data when it has a reads/writes edge, of any confidence, to a "
    "named table, column, view, or collection node. Edges are propagated from the functions "
    "it triggers through the call graph (including DB-trigger side effects) but not across "
    "calls_http. Tables missing from the scanned DDL (unresolved_relation) count; "
    "table:unknown / collection:unknown do not (reported separately as reaching_only_unknown)."
)


# Fixed vocabulary that never carries a name from the code or schema.
# Everything else that reaches the output is hashed under --redact.
_PUBLIC_VIA = {"traced_client", "route_literal", "sql_reference", "sql_key"}
_PUBLIC_KINDS = {
    "http",
    "scheduled_job",
    "firestore",
    "celery_task",
    "worker",
    "pubsub",
    "cloud_task",
    "firebase_http",
    "firebase_call",
    "other",
}


class Redactor:
    def __init__(self, enabled: bool, salt: str = "") -> None:
        self.enabled = enabled
        self.salt = salt

    def __call__(self, value: str) -> str:
        if not self.enabled:
            return value
        digest = hashlib.sha256((self.salt + "\0" + value).encode("utf-8")).hexdigest()
        return f"h:{digest[:10]}"

    def via(self, value: str) -> str:
        """`db_trigger:audit_x` → `db_trigger:h:…`; known derivations stay readable."""
        if not self.enabled or value in _PUBLIC_VIA:
            return value
        kind, sep, rest = value.partition(":")
        if sep and kind.isidentifier() and kind.islower():
            return f"{kind}:{self(rest)}"
        return self(value)

    def kind(self, value: str) -> str:
        return value if (not self.enabled or value in _PUBLIC_KINDS) else self(value)


def _strip_repo(node_id: str) -> tuple[str, str]:
    """`web/function:x` → (`web`, `function:x`)."""
    head, sep, rest = node_id.partition("/")
    if sep and ":" in rest and ":" not in head:
        return head, rest
    return "", node_id


def _entry_label(node: dict[str, Any]) -> str:
    kind = node.get("kind") or "entrypoint"
    if kind == "http":
        return f"{node.get('method')} {node.get('path')}"
    return f"{kind} {node.get('symbol') or node.get('path') or node.get('id')}"


MAX_HTTP_HOPS = 5


def build_report(
    data: dict[str, Any], *, redact: bool = False, salt: str = "", cross_service: bool = False
) -> dict[str, Any]:
    """``cross_service`` counts entrypoints that reach data only through
    other scanned services (resolved `calls_http`, up to MAX_HTTP_HOPS) in
    `reaching_data` too; they are always reported as `reaching_data_via_http`."""
    r = Redactor(redact, salt)
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    edges = data.get("edges", [])

    entrypoints = [n for n in nodes.values() if n.get("type") == "entrypoint"]
    reach: dict[str, set[str]] = {n["id"]: set() for n in entrypoints}
    unknown_reach: set[str] = set()
    for e in edges:
        if e.get("type") in {"reads", "writes"} and e["from"] in reach:
            to = _strip_repo(e["to"])[1]
            if to in {"table:unknown", "collection:unknown"}:
                unknown_reach.add(e["from"])
            elif to.startswith(DATA_PREFIXES):
                reach[e["from"]].add(to.split(".", 1)[0] if to.startswith("column:") else to)

    # call graph, to say *why* an entrypoint reaches no data
    handlers: dict[str, list[str]] = {}
    forward: dict[str, list[str]] = {}
    http_out: dict[str, list[str]] = {}
    for e in edges:
        t = e.get("type")
        if t == "triggers" and e["from"] in reach:
            handlers.setdefault(e["from"], []).append(e["to"])
        elif t == "calls" and e.get("confidence") != "unknown":
            forward.setdefault(e["from"], []).append(e["to"])
        elif t == "calls_http":
            http_out.setdefault(e["from"], []).append(e["to"])
    unresolved_http_from = {str(u.get("from") or "") for u in data.get("unresolved_http") or []}

    def reached_functions(ep_id: str) -> list[str]:
        seen: list[str] = []
        stack = list(handlers.get(ep_id, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.append(cur)
            stack.extend(forward.get(cur, []))
        return seen

    http_memo: dict[str, tuple[int | None, str | None]] = {}

    def http_reach(ep_id: str, stack: frozenset[str] = frozenset()) -> tuple[int | None, str | None]:
        """(hops to the first service with data, None if none; why it stops):
        follows resolved `calls_http` from everything the entrypoint reaches,
        across services, without cycles, up to MAX_HTTP_HOPS."""
        if ep_id in http_memo:
            return http_memo[ep_id]
        fns = reached_functions(ep_id)
        targets = sorted({t for f in fns for t in http_out.get(f, [])} - stack - {ep_id})
        unresolved = any(f in unresolved_http_from for f in fns)
        if not targets:
            result: tuple[int | None, str | None] = (None, "http_unresolved" if unresolved else None)
        elif any(reach.get(t) for t in targets):
            result = (1, None)
        elif len(stack) + 1 >= MAX_HTTP_HOPS:
            result = (None, "hop_limit")
        else:
            best: int | None = None
            reasons: list[str] = []
            for t in targets:
                hops, why = http_reach(t, stack | {ep_id})
                if hops is not None and (best is None or hops + 1 < best):
                    best = hops + 1
                elif why:
                    reasons.append(why)
            if best is not None:
                result = (best, None)
            elif "hop_limit" in reasons:
                result = (None, "hop_limit")
            elif "http_unresolved" in reasons or unresolved:
                result = (None, "http_unresolved")
            else:
                result = (None, "target_without_data")
        if not stack:  # a result inside a cycle depends on the path: memoize only from the top
            http_memo[ep_id] = result
        return result

    def why_no_data(n: dict[str, Any]) -> tuple[str, int | None, str | None]:
        """(reason, hops to data through other services, why the HTTP chain stops)."""
        if not handlers.get(n["id"]):
            return "handler_not_resolved", None, None
        fns = reached_functions(n["id"])
        hops, stop = http_reach(n["id"])
        if any((nodes.get(f) or {}).get("unresolved_calls") for f in fns) or n.get("unresolved_calls"):
            return "unresolved_call", hops, stop
        if any(f in http_out or f in unresolved_http_from or _strip_repo(f)[1] in unresolved_http_from for f in fns):
            return "only_http", hops, stop
        return "no_data_access", hops, stop

    by_kind: Counter[str] = Counter()
    reaching: Counter[str] = Counter()
    no_data: list[dict[str, Any]] = []
    no_data_reasons: Counter[str] = Counter()
    reaching_via_http = 0
    for n in entrypoints:
        kind = str(n.get("kind") or "other")
        by_kind[kind] += 1
        if reach[n["id"]]:
            reaching[kind] += 1
        else:
            repo, _ = _strip_repo(n["id"])
            reason, hops, stop = why_no_data(n)
            via_http = hops is not None
            reaching_via_http += int(via_http)
            if via_http and cross_service:
                reaching[kind] += 1
                continue
            no_data_reasons[reason] += 1
            row: dict[str, Any] = {
                "repo": r(repo or str(n.get("repo") or data.get("repo") or "")),
                "entrypoint": r(_entry_label(n)),
                "reason": reason,
                "reaches_data_via_http": via_http,
                "dynamic_table": bool(n.get("dynamic_table")),
                "unresolved_calls": len(n.get("unresolved_calls") or []),
            }
            if via_http:
                row["http_hops"] = hops
            elif reason == "only_http" and stop:
                row["http_stop"] = stop
            no_data.append(row)

    calls_http = [e for e in edges if e.get("type") == "calls_http"]
    via: Counter[str] = Counter(r.via(str(e.get("via") or "traced_client")) for e in calls_http)
    cross_repo = 0
    for e in calls_http:
        a, _ = _strip_repo(e["from"])
        b, _ = _strip_repo(e["to"])
        if a and b and a != b:
            cross_repo += 1

    unresolved = data.get("unresolved_http") or []
    reasons: Counter[str] = Counter(str(u.get("reason") or "unknown") for u in unresolved)
    keys: Counter[str] = Counter()
    for u in unresolved:
        if u.get("reason") in {"target_not_scanned", "base_unknown"}:
            for k in u.get("keys") or []:
                keys[r(str(k))] += 1

    confidence: Counter[str] = Counter(str(e.get("confidence") or "") for e in edges)
    # data links: what the code itself does, copies up the call graph, DB triggers
    data_origin: dict[str, Any] = {"direct": Counter(), "derived_call": 0, "db_trigger": 0}
    for e in edges:
        if e.get("type") not in {"reads", "writes"}:
            continue
        if str(e.get("via") or "").startswith("db_trigger:"):
            data_origin["db_trigger"] += 1
        elif e.get("derived"):
            data_origin["derived_call"] += 1
        else:
            data_origin["direct"][str(e.get("confidence") or "")] += 1
    data_via: Counter[str] = Counter(
        r.via(str(e.get("via")))
        for e in edges
        if e.get("type") in {"reads", "writes"} and e.get("via")
    )
    functions = [n for n in nodes.values() if n.get("type") == "function"]  # entrypoints hold copies
    unresolved_calls = sum(len(n.get("unresolved_calls") or []) for n in functions)
    call_reasons: Counter[str] = Counter()
    for n in functions:
        names = n.get("unresolved_calls") or []
        why_not = n.get("unresolved_call_reasons") or {}
        for name in names:
            call_reasons[str(why_not.get(name) or "no_scanned_implementer")] += 1
    total_entry = len(entrypoints)
    total_reach = sum(reaching.values())
    unknown_only = sum(1 for n in entrypoints if not reach[n["id"]] and n["id"] in unknown_reach)

    flow = flow_summary(data)
    if flow is not None:
        for row in flow["pairs"]:
            row["from"], row["to"] = r(row["from"]), r(row["to"])
    rep = {
        "version": data.get("version"),
        "redacted": redact,
        "repos": sorted(r(x) for x in (data.get("repos") or ([data["repo"]] if data.get("repo") else []))),
        "entrypoints": {
            "total": total_entry,
            "reaching_data": total_reach,
            "reaching_data_pct": round(100.0 * total_reach / total_entry, 1) if total_entry else 0.0,
            "reaching_only_unknown": unknown_only,
            "definition": REACH_DEFINITION,
            "by_kind": {
                r.kind(k): {"total": by_kind[k], "reaching_data": reaching[k]} for k in sorted(by_kind)
            },
            "without_data": sorted(no_data, key=lambda x: (x["repo"], x["entrypoint"])),
            "without_data_by_reason": dict(sorted(no_data_reasons.items())),
            "reaching_data_via_http": reaching_via_http,
            "cross_service": cross_service,
        },
        "http": {
            "calls_http": len(calls_http),
            "cross_repo": cross_repo,
            "by_via": dict(sorted(via.items())),
            "unresolved": len(unresolved),
            "unresolved_by_reason": dict(sorted(reasons.items())),
            "unresolved_top_keys": [
                {"key": k, "count": c} for k, c in sorted(keys.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
            ],
        },
        "data_links_by_via": dict(sorted(data_via.items())),
        "edges_by_confidence": dict(sorted(confidence.items())),
        "data_links": {
            "direct": dict(sorted(data_origin["direct"].items())),
            "derived_call": data_origin["derived_call"],
            "db_trigger": data_origin["db_trigger"],
        },
        "unresolved_calls": unresolved_calls,
        "unresolved_calls_by_reason": dict(sorted(call_reasons.items())),
    }
    if flow is not None:
        rep["data_flow"] = flow  # only when the graph has `feeds` edges
    return rep


_NO_DATA_LABELS = {
    "handler_not_resolved": "handler not resolved",
    "unresolved_call": "stops at an unresolved call",
    "only_http": "only calls other services over HTTP",
    "no_data_access": "no data access found",
}

_HTTP_STOP_LABELS = {
    "http_unresolved": "its HTTP calls have no resolved target",
    "target_without_data": "the services it calls reach no data either",
    "hop_limit": f"no data within {MAX_HTTP_HOPS} HTTP hops",
}

_REASON_HINTS = {
    "route_not_found": "the service was scanned, but none of its routes matches — the caller calls something the API does not have",
    "target_not_scanned": "URL composed, but no scanned route matches — scan that service or ignore",
    "base_unknown": "path known, base URL not — map the env var under `services:` in bizmap.yaml",
    "path_dynamic": "path built at runtime — expected `unknown`",
    "client_unknown": "client not recognized — open an issue with the client pattern",
}


def format_report(rep: dict[str, Any], *, limit: int = 20) -> str:
    ep = rep["entrypoints"]
    http = rep["http"]
    out: list[str] = []
    out.append(f"bizmap report (graph {rep.get('version')}){'  [redacted]' if rep['redacted'] else ''}")
    if rep["repos"]:
        out.append(f"repos: {', '.join(rep['repos'])}")
    out.append("")
    out.append(
        f"entrypoints: {ep['total']}  reaching a table/collection: {ep['reaching_data']} "
        f"({ep['reaching_data_pct']}%)"
    )
    if ep.get("reaching_only_unknown"):
        out.append(f"  + {ep['reaching_only_unknown']} reaching only table:unknown (dynamic SQL / unscanned models)")
    for kind, row in ep["by_kind"].items():
        out.append(f"  {kind:<14} {row['reaching_data']:>5} / {row['total']}")
    if ep.get("reaching_data_via_http"):
        n_via = ep["reaching_data_via_http"]
        if ep.get("cross_service"):
            out.append(f"  ({n_via} of those reach data only through other services over HTTP; --cross-service)")
        else:
            out.append(
                f"  + {n_via} reaching data through other scanned services over HTTP "
                "(counted with --cross-service)"
            )
    if ep["without_data"]:
        reasons = ep.get("without_data_by_reason") or {}
        detail = ", ".join(f"{_NO_DATA_LABELS.get(k, k)} {v}" for k, v in reasons.items())
        out.append(f"  without data ({len(ep['without_data'])}): {detail}")
        for row in ep["without_data"][:limit]:
            flags = []
            if row["dynamic_table"]:
                flags.append("dynamic table")
            if row.get("reason"):
                flags.append(_NO_DATA_LABELS.get(row["reason"], row["reason"]))
            if row.get("reaches_data_via_http"):
                flags.append(f"data via HTTP, {row.get('http_hops')} hop(s)")
            elif row.get("http_stop"):
                flags.append(_HTTP_STOP_LABELS.get(row["http_stop"], row["http_stop"]))
            if row["unresolved_calls"]:
                flags.append(f"{row['unresolved_calls']} unresolved calls")
            suffix = f"  ({', '.join(flags)})" if flags else ""
            repo = f"{row['repo']}: " if row["repo"] else ""
            out.append(f"    - {repo}{row['entrypoint']}{suffix}")
        if len(ep["without_data"]) > limit:
            out.append(f"    … {len(ep['without_data']) - limit} more (use --json)")
    out.append("")
    out.append(f"http links: {http['calls_http']}  (cross-repo {http['cross_repo']})")
    for via, count in http["by_via"].items():
        out.append(f"  via {via:<14} {count}")
    out.append(f"unresolved http: {http['unresolved']}")
    for reason, count in http["unresolved_by_reason"].items():
        hint = _REASON_HINTS.get(reason, "")
        out.append(f"  {reason:<19} {count:>5}  {hint}")
    if http["unresolved_top_keys"]:
        keys = ", ".join(f"{k['key']}×{k['count']}" for k in http["unresolved_top_keys"])
        out.append(f"  top base/env keys: {keys}")
    if rep["data_links_by_via"]:
        out.append("")
        out.append("data links by derivation: " + ", ".join(f"{k} {v}" for k, v in rep["data_links_by_via"].items()))
    out.append("")
    out.append("edges by confidence: " + ", ".join(f"{k} {v}" for k, v in rep["edges_by_confidence"].items()))
    links = rep.get("data_links") or {}
    if links:
        direct = ", ".join(f"{k} {v}" for k, v in (links.get("direct") or {}).items())
        out.append(
            f"data links: direct ({direct or 'none'}), copied to callers/entrypoints {links.get('derived_call', 0)}, "
            f"via DB triggers {links.get('db_trigger', 0)}"
        )
    flow = rep.get("data_flow")
    if flow:
        cols = flow["columns"]
        pct = round(100.0 * flow["columns_exact"] / cols) if cols else 0
        out.append("")
        out.append(
            f"data flow between databases: {flow['feeds']} feeds, {flow['feeds_with_columns']} with columns mapped, "
            f"{cols} column {'mapping' if cols == 1 else 'mappings'} (exact {flow['columns_exact']}, "
            f"heuristic {flow['columns_heuristic']}{f', {pct}% exact' if cols else ''})"
        )
        for row in flow["pairs"][:limit]:
            out.append(
                f"  {row['from']} -> {row['to']}: {row['tables']} {'table' if row['tables'] == 1 else 'tables'}, "
                f"{row['columns']} {'column' if row['columns'] == 1 else 'columns'} "
                f"(exact {row['exact']}, heuristic {row['heuristic']})"
            )
        if len(flow["pairs"]) > limit:
            out.append(f"  … {len(flow['pairs']) - limit} more (use --json)")
    by_reason = rep.get("unresolved_calls_by_reason") or {}
    detail = ", ".join(f"{k} {v}" for k, v in by_reason.items())
    out.append(f"unresolved calls: {rep['unresolved_calls']}" + (f"  ({detail})" if detail else ""))
    out.append("")
    out.append("reach: " + ep.get("definition", REACH_DEFINITION))
    return "\n".join(out) + "\n"
