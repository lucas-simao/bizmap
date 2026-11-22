"""Put the JS/TS Firestore pass and Pub/Sub push subscriptions in the graph.

- ``apply_js_firestore``: collection nodes (``db`` comes later from
  ``dbident``), ``reads`` / ``writes`` from the function that runs the access
  (``fields`` on writes), copied onto its callers and entrypoints like SQL
  (``derived: call``); Firestore trigger entrypoints (``entrypoint:firestore:
  <collection>:<event>``) with their handler; ``enqueues`` to
  ``queue:pubsub:<topic>`` for ``pubsub.topic(t).publishMessage(…)``.
- ``apply_push_subscriptions``: a Pub/Sub push subscription delivers a topic
  to an HTTP route (``push_subscriptions:`` in bizmap.yaml, or
  ``gcloud pubsub subscriptions create … --topic=T --push-endpoint=URL`` in a
  shell / YAML / Makefile): ``triggers queue:pubsub:<topic> -> entrypoint``.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bizmap.detect.go_cloud import GoCloudResult, scan_go, valid_topic
from bizmap.detect.js_firebase import callable_sites
from bizmap.detect.js_firestore import FS_FILE, FsFunction, FsResult, scan_files
from bizmap.graph import (
    Confidence,
    Evidence,
    Graph,
    collection_id,
    entrypoint_id,
    function_id,
    queue_id,
    table_id,
)


def _warn(graph: Graph, msg: str) -> None:
    existing = list(graph.attrs.get("warnings") or [])
    if msg not in existing:
        existing.append(msg)
    graph.attrs["warnings"] = existing


def _ensure_fn(graph: Graph, qual: str, file: str, line: int) -> str:
    fid = function_id(qual)
    if fid not in graph.nodes:
        graph.add_node(fid, "function", name=qual, language="javascript",
                       evidence={"file": file, "line": line, "symbol": qual})
    return fid


UNKNOWN = "unknown"  # a collection whose name is built at runtime (`Collection(name)`)


def _ensure_coll(graph: Graph, name: str, path: str | None = None) -> str:
    if name == UNKNOWN:
        # like `table:unknown`: one node per repo, no path, never a feeds end
        cid = collection_id(UNKNOWN)
        graph.add_node(cid, "collection", name=UNKNOWN, kind="collection", confidence=Confidence.UNKNOWN.value)
        return cid
    cid = collection_id(name)
    graph.add_node(cid, "collection", name=name.lower(), kind="collection")
    if path and path.lower() != name.lower():
        node = graph.nodes[cid]
        paths = sorted(set(node.attrs.get("paths") or []) | {path})
        node.attrs["path"] = paths[0]
        if len(paths) > 1:
            node.attrs["paths"] = paths
    return cid


def apply_js_firestore(
    graph: Graph,
    texts: dict[str, str],
    functions: dict[str, tuple[str, int]],
    function_ends: dict[str, int],
    calls: list[tuple[str, str, str, int]],
    deployed: dict[tuple[str, str], str] | None = None,
) -> FsResult | None:
    if not any(FS_FILE.search(t) for t in texts.values()):
        return None
    res = scan_files(texts, functions, function_ends, calls)
    deployed = deployed or {}
    direct: list[tuple[str, str, str]] = []  # (edge type, function id, target id)
    for f in res.functions:
        _add_cloud_function(graph, f, deployed.get((f.file, f.name)))
    for t in res.triggers:
        fid = function_id(t.qual)
        graph.add_node(
            fid, "function", name=t.qual, language="javascript",
            evidence={"file": t.file, "line": t.start, "symbol": t.qual},
            start_line=t.start, end_line=t.end,
            firestore_trigger={"collection": t.collection, "document": t.document, "event": t.event,
                               **({"id_param": t.id_param} if t.id_param else {})},
        )
        eid = entrypoint_id("firestore", f"{t.collection}:{t.event}")
        ev = Evidence(t.file, t.start, t.qual)
        graph.add_node(
            eid, "entrypoint", kind="firestore", method=t.event.upper(), path=t.document, symbol=t.qual,
            language="javascript", collection=t.collection, document=t.document, event=t.event,
            confidence=Confidence.EXACT.value, evidence=ev.to_dict(),
        )
        name = deployed.get((t.file, t.qual.rsplit(".", 1)[-1]))
        if name:
            graph.nodes[eid].attrs.setdefault("deployed_name", name)
            graph.nodes[fid].attrs.setdefault("deployed_name", name)
        graph.add_edge("triggers", eid, fid, confidence=Confidence.EXACT, evidence=ev)
        cid = _ensure_coll(graph, t.collection, None)
        graph.add_edge("reads", fid, cid, confidence=Confidence.EXACT, evidence=ev)
        graph.add_edge("reads", eid, cid, confidence=Confidence.INFERRED, derived="call")
    for a in res.accesses:
        fid = _ensure_fn(graph, a.symbol, a.file, a.line)
        cid = _ensure_coll(graph, a.collection, a.path)
        etype = "writes" if a.kind == "write" else "reads"
        attrs: dict[str, Any] = {}
        if a.via:
            attrs["via"] = function_id(a.via)
        dynamic = a.collection == UNKNOWN
        edge = graph.add_edge(
            etype, fid, cid,
            confidence=Confidence.UNKNOWN if dynamic else Confidence.INFERRED if a.via else Confidence.EXACT,
            evidence=Evidence(a.file, a.line, a.symbol), **attrs,
        )
        if a.fields:
            edge.attrs["fields"] = sorted(set(edge.attrs.get("fields") or []) | set(a.fields))
        if not dynamic:  # a runtime name stays on the function: not copied to callers
            direct.append((etype, fid, cid))
    for p in res.publishes:
        fid = _ensure_fn(graph, p.symbol, p.file, p.line)
        qid = queue_id("pubsub", p.topic)
        graph.add_node(qid, "queue", kind="pubsub", name=p.topic)
        edge = graph.add_edge("enqueues", fid, qid, confidence=Confidence.EXACT, evidence=Evidence(p.file, p.line, p.symbol))
        if p.fields:
            edge.attrs["fields"] = sorted(set(edge.attrs.get("fields") or []) | set(p.fields))
    _propagate(graph, direct)
    return res


def _add_cloud_function(graph: Graph, f: FsFunction, deployed: str | None) -> None:
    """A callable / HTTPS / scheduled / Pub/Sub / auth Cloud Function: its
    handler function and an entrypoint named after the deployed name
    (`users-archiveUser`), which callers link to."""
    fid = function_id(f.qual)
    graph.add_node(
        fid, "function", name=f.qual, language="javascript",
        evidence={"file": f.file, "line": f.start, "symbol": f.qual}, start_line=f.start, end_line=f.end,
    )
    node = graph.nodes[fid]
    node.attrs.setdefault("start_line", f.start)
    node.attrs.setdefault("end_line", f.end)
    name = deployed or f.name
    attrs: dict[str, Any] = {
        "kind": f.kind, "symbol": f.qual, "language": "javascript", "name": name,
        "confidence": Confidence.EXACT.value, "evidence": Evidence(f.file, f.start, f.qual).to_dict(),
    }
    if deployed:
        attrs["deployed_name"] = deployed
        node.attrs.setdefault("deployed_name", deployed)
    if f.kind in {"firebase_call", "firebase_http", "firebase_task"}:
        # Firebase serves `https://<region>-<project>.cloudfunctions.net/<name>`
        attrs.update(method="POST" if f.kind != "firebase_http" else "ANY", path=f"/{name}")
    if f.schedule:
        attrs["schedule"] = f.schedule
    if f.topic:
        attrs["topic"] = f.topic
    if f.event:
        attrs["event"] = f.event
    eid = entrypoint_id(f.kind, name if deployed else f.qual)
    graph.add_node(eid, "entrypoint", **attrs)
    ev = Evidence(f.file, f.start, f.qual)
    graph.add_edge("triggers", eid, fid, confidence=Confidence.EXACT, evidence=ev)
    if f.kind == "pubsub" and f.topic:
        qid = queue_id("pubsub", f.topic)
        graph.add_node(qid, "queue", kind="pubsub", name=f.topic)
        graph.add_edge("triggers", qid, eid, confidence=Confidence.EXACT, evidence=ev)


def apply_js_callables(
    graph: Graph,
    texts: dict[str, str],
    functions: dict[str, tuple[str, int]],
    function_ends: dict[str, int],
) -> None:
    """Stash `httpsCallable(fns, '<name>')` calls on the calling function
    (`firebase_calls`); `link_firebase_callables` turns them into edges."""
    for site in callable_sites(texts, functions, function_ends):
        fid = _ensure_fn(graph, site.symbol, site.file, site.line)
        node = graph.nodes[fid]
        item = {"name": site.name, "evidence": {"file": site.file, "line": site.line, "symbol": site.symbol}}
        bucket = list(node.attrs.get("firebase_calls") or [])
        if item not in bucket:
            bucket.append(item)
        node.attrs["firebase_calls"] = bucket


_CALLABLE_KINDS = {"firebase_call", "firebase_http", "firebase_task"}


def link_firebase_callables(graph: Graph) -> None:
    """`calls_http` (via `firebase_callable`) from each stashed callable call to
    the Cloud Function entrypoint with that deployed name, in any scanned repo.
    Names no scanned function has go to `unresolved_http` as
    `callable_not_scanned`. Idempotent: safe to run again after a merge."""
    targets: dict[str, list[str]] = defaultdict(list)
    for node in graph.nodes.values():
        if node.type == "entrypoint" and node.attrs.get("kind") in _CALLABLE_KINDS and node.attrs.get("deployed_name"):
            targets[str(node.attrs["deployed_name"])].append(node.id)
    unresolved = [u for u in graph.attrs.get("unresolved_http") or [] if u.get("reason") != "callable_not_scanned"]
    for node in list(graph.nodes.values()):
        for item in node.attrs.get("firebase_calls") or []:
            name = str(item.get("name") or "")
            ev = Evidence.from_dict(item["evidence"]) if item.get("evidence") else None
            hits = targets.get(name) or []
            for eid in hits:
                graph.add_edge("calls_http", node.id, eid, confidence=Confidence.EXACT, evidence=ev,
                               via="firebase_callable")
            if not hits:
                note = {"url": name, "method": "CALL", "keys": [], "from": node.id, "reason": "callable_not_scanned"}
                if note not in unresolved:
                    unresolved.append(note)
    graph.attrs["unresolved_http"] = unresolved


def apply_go_cloud(
    graph: Graph, texts: dict[str, str], env_values: dict[str, str] | None = None
) -> GoCloudResult | None:
    """Firestore accesses and Pub/Sub publishes in Go (see ``detect/go_cloud``)."""
    fns: list[tuple[str, str, int, int]] = []
    for node in graph.nodes.values():
        if node.type != "function" or node.attrs.get("language") != "go":
            continue
        ev = node.attrs.get("evidence") or {}
        start = node.attrs.get("start_line") or ev.get("line")
        end = node.attrs.get("end_line")
        if ev.get("file") and isinstance(start, int) and isinstance(end, int):
            fns.append((str(node.attrs.get("name") or node.id.split(":", 1)[1]), str(ev["file"]), start, end))
    res = scan_go(texts, fns, env_values)
    for c in res.calls:
        fid, tid = function_id(c.symbol), function_id(c.target)
        if fid in graph.nodes and tid in graph.nodes and fid != tid:
            graph.add_edge("calls", fid, tid, confidence=Confidence.INFERRED, evidence=Evidence(c.file, c.line, c.symbol),
                           via="interface")
    for w in res.sql_writes:
        fid = function_id(w.symbol)
        if fid not in graph.nodes:
            continue
        tid = table_id(w.table)
        if tid not in graph.nodes:
            graph.add_node(tid, "table", name=w.table)
        attrs: dict[str, Any] = {"via": w.via} if w.via else {}
        graph.add_edge("writes", fid, tid, confidence=Confidence.INFERRED, evidence=Evidence(w.file, w.line, w.symbol), **attrs)
    if not res.accesses and not res.publishes:
        return res
    direct: list[tuple[str, str, str]] = []
    for a in res.accesses:
        fid = function_id(a.symbol)
        if fid not in graph.nodes:
            continue
        cid = _ensure_coll(graph, a.collection, a.path)
        etype = "writes" if a.kind == "write" else "reads"
        dynamic = a.collection == UNKNOWN
        edge = graph.add_edge(etype, fid, cid, confidence=Confidence.UNKNOWN if dynamic else Confidence.EXACT,
                              evidence=Evidence(a.file, a.line, a.symbol))
        if a.fields:
            edge.attrs["fields"] = sorted(set(edge.attrs.get("fields") or []) | set(a.fields))
        if not dynamic:
            direct.append((etype, fid, cid))
    for p in res.publishes:
        fid = function_id(p.symbol)
        node = graph.nodes.get(fid)
        if node is None:
            continue
        named = [t for t in p.topics if t.name and valid_topic(t.name)]
        dynamic = [t for t in p.topics if not t.name]
        for t in dynamic:
            bucket = list(node.attrs.get("unresolved_queues") or [])
            item = {"kind": "pubsub", "env": t.env, "evidence": Evidence(p.file, p.line, p.symbol).to_dict()}
            if item not in bucket:
                bucket.append(item)
            node.attrs["unresolved_queues"] = bucket
        names = sorted({t.name for t in named if t.name})
        if getattr(p, "unknown", False):
            bucket = list(node.attrs.get("unresolved_queues") or [])
            item = {"kind": "pubsub", "reason": "instance_unknown", "evidence": Evidence(p.file, p.line, p.symbol).to_dict()}
            if p.via:
                item["via"] = p.via
            if item not in bucket:
                bucket.append(item)
            node.attrs["unresolved_queues"] = bucket
            continue
        if p.candidates:
            # which of several wrapper instances is the receiver is unknown: no edges
            if names:
                bucket = list(node.attrs.get("unresolved_queues") or [])
                item = {"kind": "pubsub", "candidates": names, "evidence": Evidence(p.file, p.line, p.symbol).to_dict()}
                if item not in bucket:
                    bucket.append(item)
                node.attrs["unresolved_queues"] = bucket
            continue
        for t in named:
            qid = queue_id("pubsub", str(t.name))
            graph.add_node(qid, "queue", kind="pubsub", name=t.name)
            if t.env:
                graph.nodes[qid].attrs["env"] = t.env
            attrs: dict[str, Any] = {}
            if p.via:
                attrs["via"] = p.via
            edge = graph.add_edge(
                "enqueues", fid, qid,
                confidence=Confidence.EXACT if p.confidence == "exact" else Confidence.INFERRED,
                evidence=Evidence(p.file, p.line, p.symbol), **attrs,
            )
            if p.fields:
                edge.attrs["fields"] = sorted(set(edge.attrs.get("fields") or []) | set(p.fields))
    _propagate(graph, direct)
    return res


def _propagate(graph: Graph, direct: list[tuple[str, str, str]]) -> None:
    """Callers (and the entrypoints that run them) reach what a function reads / writes."""
    if not direct:
        return
    reverse: dict[str, set[str]] = defaultdict(set)
    eps: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges.values():
        if e.type == "calls":
            reverse[e.to_id].add(e.from_id)
        elif e.type == "triggers" and e.from_id.startswith("entrypoint:"):
            eps[e.to_id].add(e.from_id)
    by_symbol: dict[str, set[str]] = defaultdict(set)
    for node in graph.nodes.values():
        if node.type == "entrypoint" and node.attrs.get("symbol"):
            by_symbol[function_id(str(node.attrs["symbol"]))].add(node.id)
    for etype, fid, cid in direct:
        seen = {fid}
        todo = deque([fid])
        while todo:
            cur = todo.popleft()
            for ep in eps.get(cur, set()) | by_symbol.get(cur, set()):
                graph.add_edge(etype, ep, cid, confidence=Confidence.INFERRED, derived="call")
            for parent in reverse.get(cur, ()):
                if parent in seen:
                    continue
                seen.add(parent)
                graph.add_edge(etype, parent, cid, confidence=Confidence.INFERRED, derived="call")
                todo.append(parent)


# ---------------------------------------------------------------- push ----

_GCLOUD = re.compile(r"gcloud\s+(?:beta\s+)?pubsub\s+subscriptions\s+(?:create|update)\b([^\n]*)")
_FLAG = re.compile(r"--([a-z-]+)(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)")
_PUSH_FILES = (".sh", ".bash", ".yaml", ".yml", ".mk", ".tf", ".json", ".toml", ".ps1")
_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", "__pycache__", ".next", "vendor", "target"}


def _endpoint_path(url: str) -> str:
    url = url.strip().strip("\"'")
    if url.startswith("$("):  # $(gcloud run services describe … --format='value(status.url)')/path
        depth = 0
        for k, c in enumerate(url):
            depth += c == "("
            depth -= c == ")"
            if depth == 0 and k > 1:
                url = url[k + 1 :]
                break
    url = re.sub(r"^\$\{?\w+\}?", "", url)  # $URL/path, ${URL}/path
    if "://" in url:
        url = urlparse(url).path
    if not url.startswith("/"):
        url = "/" + url.split("/", 1)[1] if "/" in url else "/" + url
    return url.rstrip("/") or "/"


def push_specs(root: Path, raw_config: dict[str, Any] | None) -> list[tuple[str, str, str, str, int]]:
    """(topic, method, path, file, line) from bizmap.yaml and gcloud commands."""
    out: list[tuple[str, str, str, str, int]] = []
    spec = (raw_config or {}).get("push_subscriptions")
    items: list[tuple[str, Any]] = []
    if isinstance(spec, dict):
        items = list(spec.items())
    elif isinstance(spec, list):
        for it in spec:
            if isinstance(it, dict) and it.get("topic"):
                items.append((str(it["topic"]), it))
    for topic, val in items:
        if isinstance(val, str):
            parts = val.split()
            method, path = (parts[0].upper(), parts[1]) if len(parts) == 2 else ("POST", parts[0])
        elif isinstance(val, dict):
            method = str(val.get("method") or "POST").upper()
            path = str(val.get("path") or val.get("endpoint") or "")
        else:
            continue
        if path:
            out.append((str(topic), method, _endpoint_path(path), "bizmap.yaml", 1))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        if dirpath.count(os.sep) - str(root).count(os.sep) > 5:
            dirnames[:] = []
        for name in filenames:
            if not (name.endswith(_PUSH_FILES) or name in ("Makefile", "Dockerfile")):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > 300_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "pubsub" not in text or "subscriptions" not in text:
                continue
            joined = re.sub(r"\\\s*\n", " ", text)
            for m in _GCLOUD.finditer(joined):
                flags = {k: v.strip("\"'") for k, v in _FLAG.findall(m.group(1))}
                topic, endpoint = flags.get("topic"), flags.get("push-endpoint")
                if topic and endpoint:
                    topic = topic.rsplit("/", 1)[-1]  # projects/p/topics/name
                    line = joined.count("\n", 0, m.start()) + 1
                    out.append((topic, "POST", _endpoint_path(endpoint), os.path.relpath(path, root), line))
    return out


def apply_push_subscriptions(graph: Graph, root: Path, raw_config: dict[str, Any] | None) -> None:
    specs = push_specs(root, raw_config)
    if not specs:
        return
    routes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for node in graph.nodes.values():
        if node.type == "entrypoint" and node.attrs.get("kind") == "http":
            path = str(node.attrs.get("path") or "").rstrip("/") or "/"
            routes[(str(node.attrs.get("method") or "").upper(), path)].append(node.id)
    for topic, method, path, file, line in specs:
        hits = routes.get((method, path)) or []
        if not hits:
            _warn(graph, f"push subscription {topic} -> {method} {path}: no such route in this repo")
            continue
        qid = queue_id("pubsub", topic)
        graph.add_node(qid, "queue", kind="pubsub", name=topic)
        for eid in hits:
            graph.add_edge("triggers", qid, eid, confidence=Confidence.INFERRED,
                           evidence=Evidence(file, line, topic), via="push_subscription")
