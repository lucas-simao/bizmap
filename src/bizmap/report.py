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

DATA_PREFIXES = ("table:", "column:", "view:", "collection:")


class Redactor:
    def __init__(self, enabled: bool, salt: str = "") -> None:
        self.enabled = enabled
        self.salt = salt

    def __call__(self, value: str) -> str:
        if not self.enabled:
            return value
        digest = hashlib.sha256((self.salt + "\0" + value).encode("utf-8")).hexdigest()
        return f"h:{digest[:10]}"


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


def build_report(data: dict[str, Any], *, redact: bool = False, salt: str = "") -> dict[str, Any]:
    r = Redactor(redact, salt)
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    edges = data.get("edges", [])

    entrypoints = [n for n in nodes.values() if n.get("type") == "entrypoint"]
    reach: dict[str, set[str]] = {n["id"]: set() for n in entrypoints}
    for e in edges:
        if e.get("type") in {"reads", "writes"} and e["from"] in reach:
            to = _strip_repo(e["to"])[1]
            if to.startswith(DATA_PREFIXES) and to != "table:unknown":
                reach[e["from"]].add(to.split(".", 1)[0] if to.startswith("column:") else to)

    by_kind: Counter[str] = Counter()
    reaching: Counter[str] = Counter()
    no_data: list[dict[str, Any]] = []
    for n in entrypoints:
        kind = str(n.get("kind") or "other")
        by_kind[kind] += 1
        if reach[n["id"]]:
            reaching[kind] += 1
        else:
            repo, _ = _strip_repo(n["id"])
            no_data.append(
                {
                    "repo": r(repo or str(n.get("repo") or data.get("repo") or "")),
                    "entrypoint": r(_entry_label(n)),
                    "dynamic_table": bool(n.get("dynamic_table")),
                    "unresolved_calls": len(n.get("unresolved_calls") or []),
                }
            )

    calls_http = [e for e in edges if e.get("type") == "calls_http"]
    via: Counter[str] = Counter(str(e.get("via") or "traced_client") for e in calls_http)
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
    data_via: Counter[str] = Counter(
        str(e.get("via"))
        for e in edges
        if e.get("type") in {"reads", "writes"} and e.get("via")
    )
    unresolved_calls = sum(len(n.get("unresolved_calls") or []) for n in nodes.values())
    total_entry = len(entrypoints)
    total_reach = sum(reaching.values())

    return {
        "version": data.get("version"),
        "redacted": redact,
        "repos": sorted(r(x) for x in (data.get("repos") or ([data["repo"]] if data.get("repo") else []))),
        "entrypoints": {
            "total": total_entry,
            "reaching_data": total_reach,
            "reaching_data_pct": round(100.0 * total_reach / total_entry, 1) if total_entry else 0.0,
            "by_kind": {k: {"total": by_kind[k], "reaching_data": reaching[k]} for k in sorted(by_kind)},
            "without_data": sorted(no_data, key=lambda x: (x["repo"], x["entrypoint"])),
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
        "unresolved_calls": unresolved_calls,
    }


_REASON_HINTS = {
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
    for kind, row in ep["by_kind"].items():
        out.append(f"  {kind:<14} {row['reaching_data']:>5} / {row['total']}")
    if ep["without_data"]:
        out.append(f"  without data ({len(ep['without_data'])}):")
        for row in ep["without_data"][:limit]:
            flags = []
            if row["dynamic_table"]:
                flags.append("dynamic table")
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
    out.append(f"unresolved calls (interface/injection with no scanned target): {rep['unresolved_calls']}")
    return "\n".join(out) + "\n"
