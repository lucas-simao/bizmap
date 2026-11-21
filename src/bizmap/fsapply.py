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

from bizmap.detect.js_firestore import FS_FILE, FsResult, scan_files
from bizmap.graph import (
    Confidence,
    Evidence,
    Graph,
    collection_id,
    entrypoint_id,
    function_id,
    queue_id,
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


def _ensure_coll(graph: Graph, name: str, path: str | None = None) -> str:
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
) -> FsResult | None:
    if not any(FS_FILE.search(t) for t in texts.values()):
        return None
    res = scan_files(texts, functions, function_ends, calls)
    direct: list[tuple[str, str, str]] = []  # (edge type, function id, target id)
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
        edge = graph.add_edge(
            etype, fid, cid, confidence=Confidence.INFERRED if a.via else Confidence.EXACT,
            evidence=Evidence(a.file, a.line, a.symbol), **attrs,
        )
        if a.fields:
            edge.attrs["fields"] = sorted(set(edge.attrs.get("fields") or []) | set(a.fields))
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
