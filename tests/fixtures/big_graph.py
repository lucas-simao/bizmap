"""A large synthetic multi-repo graph (tens of thousands of nodes) for the HTML
start screen's size checks. Shape: per service, HTTP routes → handlers →
chains of functions → functions that read / write tables (by column) and
Firestore collections, a few cross-service writers, queues, feeds, dynamic
table names and functions nothing reaches. Deterministic."""

from __future__ import annotations

import random
from typing import Any


def build(services: int = 16, tables: int = 1200, cols: int = 8, functions: int = 50_000, seed: int = 7) -> dict[str, Any]:
    rnd = random.Random(seed)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    eid = 0

    def edge(t: str, a: str, b: str, conf: str = "exact", **attrs: Any) -> None:
        nonlocal eid
        eid += 1
        edges.append({"id": f"{t}:{eid}", "type": t, "from": a, "to": b, "confidence": conf, **attrs})

    svcs = [f"svc{k:02d}" for k in range(services)]
    tab_of: dict[int, str] = {}
    for k in range(tables):
        s = svcs[k % services]
        kind = "collection" if k % 5 == 0 else "table"
        tid = f"{s}/{kind}:t{k}"
        tab_of[k] = tid
        attrs: dict[str, Any] = {"name": f"t{k}"}
        if kind == "collection":
            attrs["db"] = "firestore:default"
        else:
            attrs["engine"] = "postgres"
            attrs["evidence"] = {"file": f"migrations/{k:04d}.sql", "line": 1}
        nodes.append({"id": tid, "type": kind, **attrs})
        for c in range(cols):
            nodes.append({"id": f"{s}/column:t{k}.c{c}", "type": "column", "table": f"t{k}", "name": f"c{c}",
                          "data_type": "text" if kind == "table" else ""})
    per_svc = functions // services
    for si, s in enumerate(svcs):
        fns = [f"{s}/function:pkg{j % 40}.F{j}" for j in range(per_svc)]
        for j, f in enumerate(fns):
            file = f"pkg{j % 40}/f{j}.go" if j % 97 else f"pkg{j % 40}/f{j}_test.go"
            nodes.append({"id": f, "type": "function", "name": f"F{j}", "evidence": {"file": file, "line": 1 + j % 300}})
        routes = per_svc // 25
        for r in range(routes):
            ep = f"{s}/entrypoint:http:GET:/r{r}"
            nodes.append({"id": ep, "type": "entrypoint", "kind": "http", "method": "GET", "path": f"/r{r}",
                          "evidence": {"file": f"routes/r{r}.go", "line": 1}})
            edge("triggers", ep, fns[r])
        # call chains: each function calls 2 later ones (a DAG), 5% left unreached
        for j in range(per_svc):
            if j % 20 == 19:
                continue
            for _ in range(2):
                k = j + 1 + rnd.randrange(60)
                if k < per_svc:
                    edge("calls", fns[j], fns[k], "exact" if rnd.random() < 0.8 else "inferred")
        # data access: 1 in 6 functions
        for j in range(0, per_svc, 6):
            k = rnd.randrange(tables) if rnd.random() < 0.15 else rnd.randrange(si, tables, services)
            t = tab_of[k]
            tsvc, ttype = t.split("/", 1)[0], t.split("/", 1)[1].split(":", 1)[0]
            target_repo = s
            if tsvc != s:  # a table of another service: this repo sees it under its own prefix
                tid = f"{s}/{ttype}:t{k}"
                if not any(n["id"] == tid for n in nodes[-1:]):
                    nodes.append({"id": tid, "type": ttype, "name": f"t{k}", "kind": "unresolved_relation"} if ttype == "table"
                                 else {"id": tid, "type": ttype, "name": f"t{k}", "db": "firestore:default"})
                t = tid
            et = "writes" if rnd.random() < 0.4 else "reads"
            if ttype == "table" and rnd.random() < 0.7:
                c = rnd.randrange(cols)
                col = f"{target_repo}/column:t{k}.c{c}"
                nodes.append({"id": col, "type": "column", "table": f"t{k}", "name": f"c{c}"})
                edge(et, fns[j], col, "exact" if rnd.random() < 0.7 else "heuristic", evidence={"file": "x.go", "line": j})
            else:
                edge(et, fns[j], t, "exact", evidence={"file": "x.go", "line": j})
        for j in range(3, per_svc, 997):
            nodes.append({"id": f"{s}/table:unknown", "type": "table", "name": "unknown"})
            edge("writes", fns[j], f"{s}/table:unknown", "unknown")
        q = f"queue:pubsub:topic{si}"
        nodes.append({"id": q, "type": "queue", "kind": "pubsub", "name": f"topic{si}"})
        edge("enqueues", fns[5], q)
        nxt = svcs[(si + 1) % services]
        edge("triggers", q, f"{nxt}/function:pkg0.F1", "inferred")
    for k in range(0, tables, 25):
        if tab_of[k].split("/", 1)[1].startswith("collection") and k + 1 in tab_of:
            edge("feeds", tab_of[k], tab_of[k + 1], "heuristic")
    # one node per id (the generator may add a column or table twice)
    uniq: dict[str, dict[str, Any]] = {}
    for n in nodes:
        uniq.setdefault(n["id"], n)
    return {"version": "test", "nodes": list(uniq.values()), "edges": edges}
