"""`bizmap export --format html`: one self-contained, offline HTML page.

The graph is embedded as compact JSON (arrays, integer indexes, nodes grouped
by service so a service is one contiguous slice). The page draws only the
service overview at first and builds each subgraph when it is opened, so a
~55k-node graph stays usable. Cytoscape.js (MIT) is inlined; no CDN, no server.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from bizmap.export.overview import TABLE_TYPES, build_overview, column_tables

# node type codes (kept in step with template.html)
T_ENTRY, T_FUNC, T_TABLE, T_COLUMN, T_QUEUE, T_TRIGGER = range(6)
TYPE_CODE = {
    "entrypoint": T_ENTRY,
    "function": T_FUNC,
    "table": T_TABLE,
    "view": T_TABLE,
    "collection": T_TABLE,
    "column": T_COLUMN,
    "queue": T_QUEUE,
    "db_trigger": T_TRIGGER,
}
EDGE_KINDS = ("calls", "triggers", "enqueues", "calls_http", "reads", "writes", "derives_from")
# confidence code = table_usage rank - 1
CONF_CODE = {"unknown": 0, "heuristic": 1, "inferred": 2, "exact": 3}
LANGS = ("en", "pt-BR")


def node_label(node: dict[str, Any]) -> str:
    nid = node["id"]
    leaf = nid.split("/", 1)[1] if "/" in nid.split(":", 1)[0] else nid
    kind = node.get("type")
    if kind == "entrypoint":
        if node.get("kind") == "http":
            return f"{node.get('method') or 'ANY'} {node.get('path') or ''}".strip()
        sym = node.get("symbol") or node.get("path") or leaf.split(":", 2)[-1]
        return f"{node.get('kind')} {sym}".strip()
    if kind == "function":
        return leaf.removeprefix("function:")
    if kind == "column":
        return f"{node.get('table')}.{node.get('name')}"
    if kind == "queue":
        return f"{node.get('kind')}:{node.get('name')}"
    return str(node.get("name") or leaf.split(":", 1)[-1])


def _evidence(node: dict[str, Any]) -> str:
    ev = node.get("evidence")
    if not isinstance(ev, dict) or not ev.get("file"):
        return ""
    line = node.get("start_line") or ev.get("line")
    return f"{ev['file']}:{line}" if line else str(ev["file"])


def _sub(node: dict[str, Any]) -> str:
    kind = node.get("type")
    if kind == "entrypoint":
        return str(node.get("kind") or "")
    if kind in ("view", "collection"):
        return kind
    if kind == "table" and node.get("external"):
        return "external"
    return ""


def node_order(data: dict[str, Any], svc_of: dict[str, str | None], services: list[str]) -> list[str]:
    """Node ids in payload order: each service's nodes contiguous (by
    service, type, label), then shared data nodes. Index = position."""
    rank = {s: i for i, s in enumerate(services)}
    tail = len(services)

    def key(node: dict[str, Any]) -> tuple[int, int, str, str]:
        svc = svc_of.get(node["id"])
        return (
            rank[svc] if svc is not None else tail,
            TYPE_CODE.get(node.get("type"), 9),
            node_label(node).lower(),
            node["id"],
        )

    return [n["id"] for n in sorted(data.get("nodes") or [], key=key)]


def build_payload(
    data: dict[str, Any], svc_of: dict[str, str | None], lang: str = "en"
) -> dict[str, Any]:
    overview = build_overview(data, svc_of)
    services = [s["name"] for s in overview["services"]]
    svc_index = {s: i for i, s in enumerate(services)}
    nodes = {n["id"]: n for n in data.get("nodes") or []}
    order = node_order(data, svc_of, services)
    index = {nid: i for i, nid in enumerate(order)}

    records: list[list[Any]] = []
    why: dict[str, str] = {}
    ranges = [[0, 0] for _ in services]
    seen: set[int] = set()
    for i, nid in enumerate(order):
        node = nodes[nid]
        svc = svc_of.get(nid)
        s = svc_index[svc] if svc is not None else -1
        if s >= 0:
            if s not in seen:
                ranges[s][0] = i
                seen.add(s)
            ranges[s][1] = i + 1
        records.append([TYPE_CODE.get(node.get("type"), T_TRIGGER), s, node_label(node), _evidence(node), _sub(node)])
        if node.get("why"):
            why[str(i)] = str(node["why"])

    col_tab = column_tables(nodes)
    col_table = []
    for cid, tid in col_tab.items():
        if cid in index and tid in index:
            col_table += [index[cid], index[tid]]

    edges: dict[str, list[int]] = {k: [] for k in EDGE_KINDS}
    for edge in data.get("edges") or []:
        kind = edge.get("type")
        if kind not in edges:
            continue
        a, b = index.get(edge["from"]), index.get(edge["to"])
        if a is None or b is None:
            continue
        flags = CONF_CODE.get(str(edge.get("confidence")), 0)
        if edge.get("derived"):
            flags |= 4
        edges[kind] += [a, b, flags]

    svc_rows = []
    for row, (start, end) in zip(overview["services"], ranges, strict=True):
        svc_rows.append({**row, "start": start, "end": end})
    risks = []
    for r in overview["risks"]:
        target = svc_index[r["id"]] if r["kind"] == "service" else index.get(r["id"], -1)
        risks.append({"kind": r["kind"], "i": target, "name": r["name"], "users": r["users"], "top": r["top"]})
    return {
        "v": 1,
        "lang": lang if lang in LANGS else "en",
        "meta": {
            "graph_version": data.get("version"),
            "generated_at": data.get("generated_at"),
            "repo": data.get("repo"),
            "nodes": len(order),
            "edges": sum(len(v) // 3 for v in edges.values()),
            "unresolved_http": len(data.get("unresolved_http") or []),
        },
        "services": svc_rows,
        "connections": [
            [svc_index[c["from"]], svc_index[c["to"]], c["links"]] for c in overview["connections"]
        ],
        "risks": risks,
        "nodes": records,
        "why": why,
        "colTable": col_table,
        "edges": edges,
        "tableTypes": sorted(TABLE_TYPES),
    }


def _asset(name: str) -> str:
    return resources.files("bizmap.export").joinpath(name).read_text(encoding="utf-8")


def _json_safe(blob: str) -> str:
    """JSON that cannot close the <script> element it sits in (`<` only
    appears inside strings, where `\\u003c` is the same character)."""
    return blob.replace("<", "\\u003c")


def _js_safe(name: str, code: str) -> str:
    low = code.lower()
    if "</script" in low or "<!--" in low:
        raise ValueError(f"{name} cannot be inlined in a <script> element")
    return code


def render_html(
    data: dict[str, Any], svc_of: dict[str, str | None], lang: str = "en", title: str | None = None
) -> str:
    payload = build_payload(data, svc_of, lang)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    name = title or data.get("repo") or ("+".join(s["name"] for s in payload["services"][:3]) or "graph")
    page = _asset("template.html")
    parts = {
        "__TITLE__": _html_escape(f"BizMap — {name}"),
        "__LANG__": "pt-BR" if payload["lang"] == "pt-BR" else "en",
        "/*__CYTOSCAPE__*/": _js_safe("cytoscape", _asset("vendor/cytoscape.min.js")),
        "/*__REACH__*/": _js_safe("reach.js", _asset("reach.js")),
        "/*__APP__*/": _js_safe("app.js", _asset("app.js")),
        "__DATA__": _json_safe(blob),
    }
    # one pass, so a placeholder-looking string inside the data is never expanded
    out: list[str] = []
    pos = 0
    while True:
        hits = [(page.find(k, pos), k) for k in parts]
        hits = [(i, k) for i, k in hits if i >= 0]
        if not hits:
            out.append(page[pos:])
            break
        i, k = min(hits)
        out.append(page[pos:i])
        out.append(parts[k])
        pos = i + len(k)
    return "".join(out)


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
