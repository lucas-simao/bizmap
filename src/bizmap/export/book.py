"""Data for the HTML "book": an index read when the page opens, plus chunks
read only when a level is opened.

Levels: system → service → domain (routes grouped by path) → route (call tree)
→ data access (table / column). Front-end services also get **screens**: the
files whose functions call the API, with the routes each one uses and the
calls that matched no route (``unresolved_http``).

Every node keeps its index in the graph's node order (``node_order``), so a
link from one chunk to another is just an integer. Chunks:

- ``d<N>``: one per domain — its routes and every function they reach inside
  the service (calls / triggers / enqueues), with own data access (derived
  edges are not shipped: the tree recomputes them), outbound HTTP, callers
  and callees, evidence and ``why``.
- ``w<S>``: screens of front-end service S.
- ``t<N>``: tables (columns, who reads / writes them, which routes reach them).
- ``f<N>``: functions no entrypoint reaches, by folder (search and screens
  link to them).
- ``names``: label, type, service and home chunk of every node (search, and
  names of nodes in chunks not loaded yet).

The graph is only read.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from typing import Any

from bizmap.export.domains import group_entrypoints
from bizmap.export.overview import TABLE_TYPES, build_overview, column_tables, data_target

# node type codes
T_ENTRY, T_FUNC, T_TABLE, T_COLUMN, T_QUEUE, T_OTHER = range(6)
TYPE_CODE = {
    "entrypoint": T_ENTRY,
    "function": T_FUNC,
    "table": T_TABLE,
    "view": T_TABLE,
    "collection": T_TABLE,
    "column": T_COLUMN,
    "queue": T_QUEUE,
}
CALL_EDGES = ("calls", "triggers", "enqueues")
FRONT_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
UI_EXT = (".tsx", ".jsx", ".vue", ".svelte")
SCREEN_DIRS = ("pages", "screens", "app", "views", "routes", "components", "features")
CALLER_CAP = 40
UI_DEPTH = 8  # calls followed up from an API call to the screen that triggers it
UI_VISIT = 4000
CONSUMER_CAP = 200
FOLDER_CHUNK = 2500
SERVICE_TABLES = 400
TABLE_CHUNK = 60
R, W = 1, 2
METHOD_ORDER = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}


NOISE = re.compile(
    r"(\.(test|spec|stories|e2e)\.[^/]+$|\.d\.ts$|(^|/)(__tests__|__mocks__|tests?|spec|e2e|fixtures|generated|__generated__)/)"
)


def is_noise(file: str) -> bool:
    """Tests, mocks, fixtures, type declarations and generated code: they call
    APIs too, but they are not who *uses* a route."""
    return bool(file) and bool(NOISE.search(file))


def is_ui_file(file: str) -> bool:
    return file.endswith(UI_EXT) and not is_noise(file)


def node_label(node: dict[str, Any]) -> str:
    nid = node["id"]
    head = nid.split(":", 1)[0]
    leaf = nid.split("/", 1)[1] if "/" in head else nid
    kind = node.get("type")
    if kind == "entrypoint":
        if node.get("kind") == "http":
            return f"{node.get('method') or 'ANY'} {node.get('path') or ''}".strip()
        sym = node.get("symbol") or node.get("path") or leaf.split(":", 2)[-1]
        return f"{node.get('kind')} {sym}".strip()
    if kind == "function":
        qual = leaf.removeprefix("function:")
        # module-level code (an API client object, a router) -> "pkg.file (module)"
        return f"{qual[: -len('.module')]} (module)" if qual.endswith(".module") else qual
    if kind == "column":
        return f"{node.get('table')}.{node.get('name')}"
    if kind == "queue":
        return f"{node.get('kind')}:{node.get('name')}"
    return str(node.get("name") or leaf.split(":", 1)[-1])


def _file(node: dict[str, Any]) -> str:
    ev = node.get("evidence")
    return str(ev.get("file")) if isinstance(ev, dict) and ev.get("file") else ""


def _evidence(node: dict[str, Any]) -> str:
    f = _file(node)
    if not f:
        return ""
    line = node.get("start_line") or (node.get("evidence") or {}).get("line")
    return f"{f}:{line}" if line else f


def path_params(path: str) -> list[str]:
    out = []
    for part in (path or "").split("/"):
        if part.startswith(":"):
            out.append(part[1:])
        elif part.startswith("{") and part.endswith("}"):
            out.append(part[1:-1].split(":", 1)[0])
        elif part.startswith("<") and part.endswith(">"):
            out.append(part[1:-1].split(":")[-1])
        elif part.startswith("[") and part.endswith("]"):
            out.append(part.strip("[].") or part)
    return out


def code_folder(file: str) -> str:
    """Folder a function is listed under on its service page: the first two
    directory levels (`src/api`, `internal/billing`)."""
    parts = file.split("/")[:-1]
    return "/".join(parts[:2]) if parts else "."


def screen_group(file: str) -> str:
    """Folder a screen file is listed under: the segment after a UI folder
    (`pages/`, `screens/`, `app/`…) when there is one, else its directory."""
    parts = file.split("/")
    for i, p in enumerate(parts[:-1]):
        if p in SCREEN_DIRS:
            return "/".join(parts[: min(i + 2, len(parts) - 1)])
    return posixpath.dirname(file) or "."


def node_order(data: dict[str, Any], svc_of: dict[str, str | None], services: list[str]) -> list[str]:
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


def build_book(
    data: dict[str, Any],
    svc_of: dict[str, str | None],
    *,
    domain_rules: dict[str, list[tuple[str, str, str]]] | None = None,
    domains_by: str = "path",
    lang: str = "en",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """-> (index, {chunk id: chunk})."""
    overview = build_overview(data, svc_of)
    services = [s["name"] for s in overview["services"]]
    svc_index = {s: i for i, s in enumerate(services)}
    raw_nodes = {n["id"]: n for n in data.get("nodes") or []}
    order = node_order(data, svc_of, services)
    index = {nid: i for i, nid in enumerate(order)}
    n_nodes = len(order)
    nodes = [raw_nodes[nid] for nid in order]
    ntype = [TYPE_CODE.get(n.get("type"), T_OTHER) for n in nodes]
    nsvc = [svc_index[svc_of[nid]] if svc_of.get(nid) is not None else -1 for nid in order]
    labels = [node_label(n) for n in nodes]
    col_tab = {index[c]: index[t] for c, t in column_tables(raw_nodes).items() if c in index and t in index}

    # ── edges (own access only; derived edges are recomputed by the page) ──
    out_calls: list[list[int]] = [[] for _ in range(n_nodes)]
    in_calls: list[list[int]] = [[] for _ in range(n_nodes)]
    http_out: list[list[int]] = [[] for _ in range(n_nodes)]
    http_in: list[list[int]] = [[] for _ in range(n_nodes)]
    http_guess: set[tuple[int, int]] = set()  # calls_http matched by heuristic (a route literal…)
    own: dict[int, dict[int, list[Any]]] = defaultdict(dict)  # fn -> table -> [rw, cols set, ops set]
    reach_tables: dict[int, dict[int, int]] = defaultdict(dict)  # entrypoint -> table -> rw (own + derived)
    for edge in data.get("edges") or []:
        etype = edge.get("type")
        a, b = index.get(edge["from"]), index.get(edge["to"])
        if a is None or b is None:
            continue
        if etype in CALL_EDGES:
            out_calls[a].append(b)
            in_calls[b].append(a)
        elif etype == "calls_http":
            http_out[a].append(b)
            http_in[b].append(a)
            if edge.get("confidence") in ("heuristic", "unknown"):
                http_guess.add((a, b))
        elif etype in ("reads", "writes"):
            t = b if ntype[b] == T_TABLE else col_tab.get(b)
            if t is None:
                continue
            bit = R if etype == "reads" else W
            if ntype[a] == T_ENTRY:
                reach_tables[a][t] = reach_tables[a].get(t, 0) | bit
            if edge.get("derived"):
                continue
            slot = own[a].setdefault(t, [0, set(), set()])
            slot[0] |= bit
            if ntype[b] == T_COLUMN:
                slot[1].add(str(raw_nodes[order[b]].get("name") or labels[b].rsplit(".", 1)[-1]))
            for op in edge.get("ops") or []:
                slot[2].add(str(op).upper())
    for lst in (out_calls, in_calls, http_out, http_in):
        for i, v in enumerate(lst):
            if len(v) > 1:
                lst[i] = sorted(set(v))

    # ── service kinds ──
    files_by_svc: dict[int, list[str]] = defaultdict(list)
    for i in range(n_nodes):
        if ntype[i] == T_FUNC and nsvc[i] >= 0:
            files_by_svc[nsvc[i]].append(_file(nodes[i]))
    unresolved_by: dict[int, list[list[str]]] = defaultdict(list)
    for u in data.get("unresolved_http") or []:
        src = index.get(str(u.get("from") or ""))
        if src is not None:
            unresolved_by[src].append([str(u.get("method") or ""), str(u.get("url") or ""), str(u.get("reason") or "")])
    for i in range(n_nodes):  # node-level notes (single-repo graphs)
        for u in nodes[i].get("unresolved_http") or []:
            if isinstance(u, dict) and i not in unresolved_by:
                unresolved_by[i].append([str(u.get("method") or ""), str(u.get("url") or u.get("path") or ""),
                                         str(u.get("reason") or "")])
    svc_rows = []
    for s, row in enumerate(overview["services"]):
        files = [f for f in files_by_svc.get(s, []) if f]
        front = sum(1 for f in files if f.endswith(FRONT_EXT))
        ui = any(f.endswith(UI_EXT) for f in files) or any(f"/{d}/" in f"/{f}" for f in files for d in ("pages", "screens", "components"))
        calls_out = row["calls_out"] + sum(1 for i in range(n_nodes) if nsvc[i] == s and i in unresolved_by)
        # a JS/TS code base that calls APIs is a front end (or client) when it has UI
        # files or no routes of its own (a Vite app with only .ts files counts)
        if files and front / len(files) >= 0.6 and calls_out and (ui or not row["routes"]):
            kind = "web"
        elif row["routes"]:
            kind = "api"
        elif row["jobs"]:
            kind = "worker"
        else:
            kind = "lib"
        svc_rows.append({**row, "kind": kind})

    files = [_file(n) for n in nodes]
    noise = [is_noise(f) for f in files]
    has_ui = {s for s, row in enumerate(svc_rows) if row["kind"] == "web"
              and any(is_ui_file(files[i]) for i in range(row_start(nsvc, s), row_end(nsvc, s)) if ntype[i] == T_FUNC)}

    # UI files (screens) that reach each HTTP-calling function through the calls
    # inside their service: a page -> a hook -> the API client -> the route
    ui_reach: dict[int, set[str]] = {}
    for i in range(n_nodes):
        if ntype[i] != T_FUNC or noise[i] or nsvc[i] not in has_ui or not (http_out[i] or i in unresolved_by):
            continue
        found = {files[i]} if is_ui_file(files[i]) else set()
        seen, frontier = {i}, [i]
        for _ in range(UI_DEPTH):
            nxt = []
            for u in frontier:
                for c in in_calls[u]:
                    if c in seen or nsvc[c] != nsvc[i] or noise[c]:
                        continue
                    seen.add(c)
                    if is_ui_file(files[c]):
                        found.add(files[c])
                    if len(seen) < UI_VISIT:
                        nxt.append(c)
            frontier = nxt
            if not frontier:
                break
        ui_reach[i] = found

    chunks: dict[str, Any] = {}
    home = [""] * n_nodes  # node -> chunk that holds its record
    svc_own_tables: dict[int, dict[int, int]] = defaultdict(dict)  # tables a service reads/writes itself
    for f, per in own.items():
        if nsvc[f] >= 0:
            for t, (rw, _c, _o) in per.items():
                svc_own_tables[nsvc[f]][t] = svc_own_tables[nsvc[f]].get(t, 0) | rw

    def fn_record(i: int, closure: set[int] | None = None) -> list[Any]:
        node = nodes[i]
        callers = in_calls[i]
        data_rows = [
            [t, rw, sorted(cols), sorted(ops)] for t, (rw, cols, ops) in sorted(own.get(i, {}).items())
        ]
        rec: list[Any] = [
            labels[i],
            _evidence(node),
            str(node.get("why") or ""),
            out_calls[i],
            callers[:CALLER_CAP],
            len(callers),
            data_rows,
            http_out[i],
            unresolved_by.get(i, []),
        ]
        sig: dict[str, Any] = {}
        if node.get("params") is not None:
            sig["params"] = node["params"]
        if node.get("returns") is not None:
            sig["returns"] = node["returns"]
        if sig:
            rec.append(sig)
        return rec

    # ── domains ──
    domains: list[dict[str, Any]] = []
    route_domain: dict[int, int] = {}
    for s, row in enumerate(svc_rows):
        row["domains"] = []
        eps = [i for i in range(row_start(nsvc, s), row_end(nsvc, s)) if ntype[i] == T_ENTRY]
        if not eps:
            continue
        handler_file = {}
        for e in eps:
            hs = [h for h in out_calls[e] if ntype[h] == T_FUNC]
            handler_file[e] = _file(nodes[hs[0]]) if hs else _file(nodes[e])
        groups = group_entrypoints(
            [(e, nodes[e], handler_file[e]) for e in eps], services[s], domain_rules, domains_by
        )
        by_name: dict[str, list[int]] = defaultdict(list)
        rule_of: dict[str, str] = {}
        for e in eps:
            name, rule = groups[e]
            by_name[name].append(e)
            rule_of.setdefault(name, rule)
        ordered = sorted(by_name, key=lambda n: (n == "other", rule_of[n] == "kind", n))
        for name in ordered:
            routes = by_name[name]
            did = len(domains)
            cid = f"d{did}"
            closure: set[int] = set(routes)
            stack = list(routes)
            while stack:
                u = stack.pop()
                for v in out_calls[u]:
                    if v not in closure and nsvc[v] == s:
                        closure.add(v)
                        stack.append(v)
            fns = {}
            for i in sorted(closure):
                if ntype[i] == T_ENTRY:
                    continue
                fns[i] = fn_record(i)
                if not home[i]:
                    home[i] = cid
            tables: dict[int, int] = defaultdict(int)
            route_rows = []
            consumers_total = 0
            for e in routes:
                route_domain[e] = did
                home[e] = cid
                node = nodes[e]
                rt = reach_tables.get(e, {})
                for t, rw in rt.items():
                    tables[t] |= rw
                callers_http = [c for c in http_in[e] if not noise[c]]
                hidden = len(http_in[e]) - len(callers_http)
                consumers_total += len(callers_http)
                screens_of = sorted({(nsvc[c], f) for c in callers_http for f in ui_reach.get(c, ())})
                route_rows.append(
                    {
                        "i": e,
                        "m": str(node.get("method") or node.get("kind") or ""),
                        "p": str(node.get("path") or node.get("symbol") or labels[e]),
                        "pp": path_params(str(node.get("path") or "")),
                        "h": [h for h in out_calls[e]],
                        "t": sorted([t, rw] for t, rw in rt.items()),
                        "c": [[c, 1 if (c, e) in http_guess else 0] for c in callers_http[:CONSUMER_CAP]],
                        "nc": len(callers_http),
                        "nh": hidden,
                        "sc": [[sv, f] for sv, f in screens_of[:CONSUMER_CAP]],
                        "ev": _evidence(node),
                        "why": str(node.get("why") or ""),
                    }
                )
            route_rows.sort(key=lambda r: (r["p"].lower(), METHOD_ORDER.get(r["m"], 9), r["m"]))
            chunks[cid] = {"routes": route_rows, "fns": {str(k): v for k, v in fns.items()}}
            domains.append(
                {
                    "svc": s,
                    "name": name,
                    "rule": rule_of[name],
                    "routes": len(routes),
                    "functions": len(fns),
                    "tables": sorted(([t, rw] for t, rw in tables.items()), key=lambda x: labels[x[0]])[:12],
                    "ntables": len(tables),
                    "consumers": consumers_total,
                    "chunk": cid,
                }
            )
        row["domains"] = [d for d in range(len(domains)) if domains[d]["svc"] == s]

    # ── outbound HTTP: screens (front-ends), API client code, calls with no route ──
    for s, row in enumerate(svc_rows):
        row["screens"] = 0
        row["clients"] = 0
        row["unmatched"] = 0
        row["hidden_calls"] = 0
        callers = [i for i in range(row_start(nsvc, s), row_end(nsvc, s))
                   if ntype[i] == T_FUNC and (http_out[i] or i in unresolved_by)]
        if not callers:
            continue
        real = [i for i in callers if not noise[i]]
        row["hidden_calls"] = sum(len(http_out[i]) + len(unresolved_by.get(i, [])) for i in callers if noise[i])
        cid = f"w{s}"
        fns: dict[str, Any] = {}

        def routes_of(group: list[int]) -> list[list[int]]:
            pairs = {(t, 1 if (i, t) in http_guess else 0) for i in group for t in http_out[i]}
            best: dict[int, int] = {}
            for t, guess in pairs:
                best[t] = min(best.get(t, 1), guess)
            return sorted([t, g] for t, g in best.items())

        by_file: dict[str, list[int]] = defaultdict(list)
        for i in real:
            by_file[files[i] or labels[i]].append(i)
        clients = []
        for f in sorted(by_file):
            group = by_file[f]
            clients.append({
                "f": f, "g": screen_group(f), "fns": group, "r": routes_of(group),
                "u": sum(len(unresolved_by.get(i, [])) for i in group),
                "ui": sorted({u for i in group for u in ui_reach.get(i, ())} - {f}),
            })
            for i in group:
                fns[str(i)] = fn_record(i)
                home[i] = home[i] or cid
        screens = []
        if s in has_ui:
            # a screen = a UI file that makes, or reaches through calls, an API call
            per_ui: dict[str, list[int]] = defaultdict(list)
            for i in real:
                for u in ui_reach.get(i, ()):
                    per_ui[u].append(i)
            for f in sorted(per_ui):
                via = sorted(set(per_ui[f]))
                screens.append({"f": f, "g": screen_group(f), "via": via, "r": routes_of(via),
                                "u": sum(len(unresolved_by.get(i, [])) for i in via)})
        elif row["kind"] == "web":
            # no UI files (a Vite app with only .ts): the calling files are the screens
            screens = [{"f": c["f"], "g": c["g"], "via": c["fns"], "r": c["r"], "u": c["u"]} for c in clients]
        linked = sum(1 for c in clients if c["ui"] or is_ui_file(c["f"]))
        chunks[cid] = {"screens": screens, "clients": clients, "fns": fns}
        row["screens"] = len(screens)
        row["clients"] = len(clients)
        row["clients_linked"] = linked
        row["unmatched"] = sum(c["u"] for c in clients)
        row["screen_chunk"] = cid

    # ── tables ──
    table_ids = [i for i in range(n_nodes) if ntype[i] == T_TABLE]
    users_by_table: dict[int, dict[int, list[int]]] = defaultdict(dict)  # table -> fn -> [rw, cols, ops]
    for f, per in own.items():
        for t, (rw, cols, ops) in per.items():
            users_by_table[t][f] = [rw, sorted(cols), sorted(ops)]
    routes_by_table: dict[int, list[list[int]]] = defaultdict(list)
    for e, per in reach_tables.items():
        for t, rw in per.items():
            routes_by_table[t].append([e, rw])
    cols_by_table: dict[int, list[int]] = defaultdict(list)
    for c, t in col_tab.items():
        cols_by_table[t].append(c)
    database = data.get("database")
    table_rows = []
    for k, t in enumerate(table_ids):
        cid = f"t{k // TABLE_CHUNK}"
        node = nodes[t]
        users = sorted({nsvc[f] for f in users_by_table.get(t, {}) if nsvc[f] >= 0})
        rws = [v[0] for v in users_by_table.get(t, {}).values()]
        table_rows.append(
            {
                "i": t,
                "kind": str(node.get("type") if node.get("type") != "table" else (node.get("kind") or "table")),
                "db": str(node.get("database") or database or (order[t].split("/", 1)[0] if "/" in order[t].split(":", 1)[0] else "")),
                "users": users,
                "r": sum(1 for v in rws if v & R),
                "w": sum(1 for v in rws if v & W),
                "chunk": cid,
            }
        )
        chunk = chunks.setdefault(cid, {"tables": {}, "fns": {}})
        chunk["tables"][str(t)] = {
            "cols": [[labels[c].rsplit(".", 1)[-1], str(nodes[c].get("data_type") or "")] for c in sorted(cols_by_table.get(t, []))],
            "users": sorted([f, *v] for f, v in users_by_table.get(t, {}).items()),
            "routes": sorted(routes_by_table.get(t, [])),
        }
        home[t] = cid
        for c in cols_by_table.get(t, []):
            home[c] = cid

    # ── code by folder: functions no route reaches (all of a library's code) ──
    rest: dict[tuple[int, str], list[int]] = defaultdict(list)
    for i in range(n_nodes):
        if ntype[i] in (T_FUNC, T_QUEUE, T_OTHER) and not home[i].startswith("d"):
            rest[(nsvc[i], code_folder(_file(nodes[i])))].append(i)
    k = 0
    folders: dict[int, list[list[Any]]] = defaultdict(list)
    for (svc, folder), items in sorted(rest.items()):
        cids = []
        for start in range(0, len(items), FOLDER_CHUNK):
            cid = f"f{k}"
            k += 1
            part = items[start : start + FOLDER_CHUNK]
            # label left out: the page takes it from `names`, which keeps the file smaller;
            # functions already filed with a front-end's screens are only listed here
            chunks[cid] = {
                "folder": folder,
                "items": part,
                "fns": {str(i): ["", *fn_record(i)[1:]] for i in part if not home[i]},
            }
            for i in part:
                home[i] = home[i] or cid
            cids.append(cid)
        tabs: dict[int, int] = defaultdict(int)
        for i in items:
            for t, (rw, _c, _o) in own.get(i, {}).items():
                tabs[t] |= rw
        folders[svc].append([
            folder,
            len(items),
            sum(len(http_out[i]) + len(unresolved_by.get(i, [])) for i in items),
            len(tabs),
            sorted(([t, rw] for t, rw in tabs.items()), key=lambda x: labels[x[0]])[:6],
            cids,
        ])
    for s, row in enumerate(svc_rows):
        row["folders"] = folders.get(s, [])
        tabs = svc_own_tables.get(s, {})
        row["tables"] = len(tabs)
        row["data"] = sorted(([t, rw] for t, rw in tabs.items()), key=lambda x: labels[x[0]])[:SERVICE_TABLES]

    chunk_ids = sorted(chunks, key=lambda c: (c[0], int(c[1:]) if c[1:].isdigit() else 0))
    chunk_no = {c: n for n, c in enumerate(chunk_ids)}
    chunks["names"] = {
        "l": labels,
        "t": ntype,
        "s": nsvc,
        "h": [chunk_no.get(h, -1) for h in home],
    }
    risks = []
    for r in overview["risks"]:
        target = svc_index[r["id"]] if r["kind"] == "service" else index.get(r["id"], -1)
        risks.append({"kind": r["kind"], "i": target, "name": r["name"], "users": r["users"], "top": r["top"]})
    index_doc = {
        "v": 2,
        "lang": lang,
        "meta": {
            "graph_version": data.get("version"),
            "generated_at": data.get("generated_at"),
            "nodes": n_nodes,
            "edges": len(data.get("edges") or []),
            "unresolved_http": sum(len(v) for v in unresolved_by.values()),
            "reached": sum(1 for i in range(n_nodes) if ntype[i] == T_FUNC and home[i].startswith("d")),
            "functions": sum(1 for t in ntype if t == T_FUNC),
        },
        "services": svc_rows,
        "domains": domains,
        "tables": table_rows,
        "tableNames": {str(r["i"]): labels[r["i"]] for r in table_rows},
        "connections": [[svc_index[c["from"]], svc_index[c["to"]], c["links"]] for c in overview["connections"]],
        "risks": risks,
        "chunks": chunk_ids,
        "routeDomain": {str(e): d for e, d in route_domain.items()},
    }
    return index_doc, chunks


def row_start(nsvc: list[int], s: int) -> int:
    lo, hi = 0, len(nsvc)
    while lo < hi:  # nodes are ordered by service, shared data (-1) last
        mid = (lo + hi) // 2
        v = nsvc[mid] if nsvc[mid] >= 0 else 1 << 30
        if v < s:
            lo = mid + 1
        else:
            hi = mid
    return lo


def row_end(nsvc: list[int], s: int) -> int:
    return row_start(nsvc, s + 1)


__all__ = ["TABLE_TYPES", "build_book", "data_target", "node_label", "node_order", "path_params", "screen_group"]
