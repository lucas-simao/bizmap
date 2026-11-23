"""Architecture and event-flow views (bizmap/views.py): the elements and their
layout, Mermaid, the CLI, the PR comment and the HTML page data.

The fictional workspace is ``tests/fixtures/arch_demo.py``. The element
snapshots only move when the views change. Refresh with::

    BIZMAP_UPDATE_SNAPSHOTS=1 pytest tests/test_views.py

and review the diff of ``tests/snapshots/views_*.json`` in the PR.
"""

from __future__ import annotations

import copy
import json
import os
import random
import re
from pathlib import Path

import pytest

from bizmap import views
from bizmap.export import export_graph
from bizmap.impact import compute_impact, render_markdown
from bizmap.registry import graph_from_json
from fixtures.arch_demo import arch_graph

SNAP_DIR = Path(__file__).parent / "snapshots"


def _snapshot(name: str, obj: object) -> None:
    text = json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path = SNAP_DIR / f"views_{name}.json"
    if os.environ.get("BIZMAP_UPDATE_SNAPSHOTS") == "1" or not path.exists():
        path.write_text(text, encoding="utf-8")
        if os.environ.get("BIZMAP_UPDATE_SNAPSHOTS") != "1":
            pytest.fail(f"snapshot {path.name} was missing and has been written; re-run")
    assert text == path.read_text(encoding="utf-8"), f"views changed; refresh {path.name} (see module docstring)"


def _ctx(data: dict | None = None) -> views.Ctx:
    return views.context_from_data(data or arch_graph())


def _flows(data: dict | None = None) -> dict[str, dict]:
    return {f["key"]: f for f in views.event_flows(_ctx(data))}


# ── elements ─────────────────────────────────────────────────────────────────


def test_architecture_elements_snapshot() -> None:
    _snapshot("architecture", views.architecture(_ctx()))


def test_event_flow_elements_snapshot() -> None:
    _snapshot("flows", views.event_flows(_ctx()))


def test_architecture_layers_and_aggregated_edges() -> None:
    arch = views.architecture(_ctx())
    nodes = {n["id"]: n for n in arch["nodes"]}
    by_label = {n["label"]: n for n in arch["nodes"]}
    # front ends → APIs → topics → workers → stores, left to right
    assert by_label["shop-web"]["layer"] == 0 and by_label["shop-api"]["layer"] == 1
    assert by_label["order-placed"]["layer"] == 2 and by_label["billing-worker"]["layer"] == 3
    assert by_label["notify-fn"]["layer"] == 3 and by_label["Postgres"]["layer"] == 4
    xs = [by_label[k]["x"] for k in ("shop-web", "shop-api", "order-placed", "billing-worker", "Postgres")]
    assert xs == sorted(xs) and len(set(xs)) == 5
    # one box per store with its count, never one node per table
    assert by_label["Postgres"]["count"] == {"table": 4} and by_label["Firestore"]["count"] == {"collection": 2}
    assert not any(n["kind"] == "table" for n in arch["nodes"])
    edges = {(nodes[e["from"]]["label"], nodes[e["to"]]["label"], e["rel"]): e for e in arch["edges"]}
    api_pg = edges[("shop-api", "Postgres", "rw")]
    assert api_pg["rw"] == 3 and api_pg["n"] == 6  # 6 function × table accesses, reads and writes
    assert {it[0] for it in api_pg["items"]} == {"orders.Service.PlaceOrder", "orders.Service.Cancel", "orders.Service.Get"}
    assert edges[("shop-web", "shop-api", "http")]["n"] == 2
    assert edges[("shop-api", "order-placed", "pub")]["n"] == 1
    assert edges[("order-placed", "billing-worker", "sub")]["n"] == 1
    assert edges[("Firestore", "notify-fn", "trigger")]["n"] == 1
    assert edges[("billing-worker", "Firestore", "rw")]["rw"] == 2
    # the widest line is the pair with the most accesses
    assert max(arch["edges"], key=lambda e: e["w"]) is api_pg
    assert by_label["order-cancelled"]["status"] == "no_consumer"


def test_heuristic_only_edges_are_dashed() -> None:
    data = arch_graph()
    for e in data["edges"]:
        if e["type"] == "calls_http":
            e["confidence"] = "heuristic"
    arch = views.architecture(_ctx(data))
    http = [e for e in arch["edges"] if e["rel"] == "http"]
    assert http and all(e["guess"] == 1 for e in http)
    assert all(e["guess"] == 0 for e in arch["edges"] if e["rel"] != "http")


def test_topic_without_consumer_is_flagged() -> None:
    flows = _flows()
    cancelled = flows["order-cancelled"]
    assert cancelled["status"] == "no_consumer" and "no_consumer" in cancelled["warnings"]
    assert cancelled["consumers"] == [] and len(cancelled["publishers"]) == 1
    assert flows["order-placed"]["status"] == "ok"
    rows = {r[0]: r for r in views.queue_rows(list(flows.values()))}
    assert rows["order-cancelled"][4] == "no_consumer" and rows["order-placed"][4] == "ok"
    md = views.mermaid_flow(cancelled)
    assert "⚠ no consumer" in md


def test_consumer_without_publisher_is_flagged() -> None:
    data = arch_graph()
    data["edges"] = [e for e in data["edges"] if not (e["type"] == "enqueues" and e["to"].endswith("order-placed"))]
    placed = _flows(data)["order-placed"]
    assert placed["status"] == "no_publisher" and placed["publishers"] == []


def test_flow_follows_the_chain_and_the_message_shape() -> None:
    f = _flows()["order-placed"]
    cols: dict[int, list[str]] = {}
    for n in f["nodes"]:
        cols.setdefault(n["layer"], []).append(n["label"])
    # publisher reads → publisher → topic → consumer → writes → trigger → function → writes
    assert cols[0] == ["customers"]
    assert cols[1] == ["events.Publisher.OrderPlaced"]
    assert cols[2] == ["order-placed"]
    assert cols[3] == ["billing.handlers.on_order_placed"]
    assert sorted(cols[4]) == ["invoices", "payment_events"]
    assert cols[5] == ["payment_events"] and cols[6] == ["index.notifyPayment"] and cols[7] == ["notifications"]
    assert f["fields"] == {"pub": ["customerId", "orderId", "totalCents"], "read": ["couponCode", "orderId", "totalCents"],
                           "missing": ["couponCode"]}
    assert "unpublished_fields" in f["warnings"] and "cross_service" in f["warnings"]
    assert [views_svc for views_svc in f["publishers"][0]["entry"]] == ["POST /orders"]
    fs = _flows()["firestore:payment_events"]
    assert fs["kind"] == "firestore" and fs["status"] == "ok"
    assert [p["fn"][0] for p in fs["publishers"]] == ["billing.handlers.on_order_placed"]


def test_hops_stop_after_three() -> None:
    data = arch_graph()
    # notify-fn publishes on to a chain of topics a → b → c → d, each consumed by a worker
    prev = "notify-fn/function:index.notifyPayment"
    for k, name in enumerate("abcd"):
        q = f"queue:pubsub:{name}"
        data["nodes"].append({"id": q, "type": "queue", "kind": "pubsub", "name": name})
        data["edges"].append({"id": f"enq{k}", "type": "enqueues", "from": prev, "to": q, "confidence": "exact"})
        fn = f"chain/function:step{name}"
        data["nodes"].append({"id": fn, "type": "function", "name": f"step{name}", "evidence": {"file": "c.py", "line": k + 1}})
        data["edges"].append({"id": f"trg{k}", "type": "triggers", "from": q, "to": fn, "confidence": "exact"})
        prev = fn
    f = _flows(data)["order-placed"]
    events = sorted({n["label"] for n in f["nodes"] if n["kind"] == "event"})
    assert events == ["a", "order-placed", "payment_events"]  # 3 hops: the topic, the trigger, the next topic


# ── layout ───────────────────────────────────────────────────────────────────


def _positions(obj: dict) -> dict[str, tuple[int, int]]:
    return {n["id"]: (n["x"], n["y"]) for n in obj["nodes"]}


def test_layout_is_stable() -> None:
    data = arch_graph()
    a1, f1 = views.architecture(_ctx(data)), views.event_flows(_ctx(data))
    a2 = views.architecture(_ctx(copy.deepcopy(data)))
    assert a1 == a2
    # the same graph in another order: the same drawing
    shuffled = copy.deepcopy(data)
    rnd = random.Random(3)
    rnd.shuffle(shuffled["nodes"])
    rnd.shuffle(shuffled["edges"])
    a3, f3 = views.architecture(_ctx(shuffled)), views.event_flows(_ctx(shuffled))
    assert _positions(a1) == _positions(a3)
    assert [_positions(f) for f in f1] == [_positions(f) for f in f3]
    assert json.dumps(a1, sort_keys=True) == json.dumps(a3, sort_keys=True)


def test_layout_orders_by_neighbours_and_staggers_middle_columns() -> None:
    arch = views.architecture(_ctx())
    by_label = {n["label"]: n for n in arch["nodes"]}
    # a service → store line passes between the topic boxes, not through one
    api_y = by_label["shop-api"]["y"]
    for n in arch["nodes"]:
        if n["layer"] in (2, 3):
            assert abs(n["y"] - api_y) >= views.ROW_H / 2 - 1, n
    nodes = [{"id": f"n{k}", "label": f"n{k}", "layer": k % 2} for k in range(6)]
    edges = [{"from": "n0", "to": "n5"}, {"from": "n2", "to": "n1"}, {"from": "n4", "to": "n3"}]
    views.layout(nodes, edges)
    y = {n["id"]: n["y"] for n in nodes}
    # each right node sits level with the left node it is linked to
    assert y["n5"] == y["n0"] and y["n1"] == y["n2"] and y["n3"] == y["n4"]


def test_big_graph_aggregates_by_store() -> None:
    from fixtures.big_graph import build

    data = build(services=15, tables=800, functions=8000)
    ctx = views.context_from_data(data)
    arch = views.architecture(ctx)
    kinds: dict[str, int] = {}
    for n in arch["nodes"]:
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    assert kinds["service"] == 15 and kinds["store"] <= 3
    assert len(arch["nodes"]) < 60 and len(arch["edges"]) < 200  # never one node per table
    stores = [n for n in arch["nodes"] if n["kind"] == "store"]
    assert sum(n["n"] for n in stores) >= 800 and all(not n["ents"] for n in stores if n["n"] > views.MAX_EXPAND)
    assert all(len(e["items"]) <= views.MAX_ITEMS for e in arch["edges"])


# ── Mermaid and the CLI ──────────────────────────────────────────────────────


def test_mermaid_blocks() -> None:
    ctx = _ctx()
    arch = views.mermaid_architecture(views.architecture(ctx))
    assert arch.startswith("flowchart LR\n")
    assert '[("Postgres · 4 tables")]' in arch and '[("Firestore · 2 collections")]' in arch
    assert re.search(r's_s3 -->\|"HTTP 2"\| s_s2', arch), arch
    assert "⚠ no consumer" in arch
    md = views.mermaid_markdown(ctx)
    assert md.count("```mermaid") == 4  # the architecture and 3 event sources
    assert "| `order-cancelled` | topic | no consumer | 1 | 0 |" in md
    assert "⚠ read but never published: `couponCode`" in md
    for block in re.findall(r"```mermaid\n(.*?)```", md, re.S):
        for line in block.strip().splitlines()[1:]:
            assert not re.search(r"[<>]small", line), line


def test_cli_export_mermaid_and_report(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")  # the CLI module loads the parsers
    from click.testing import CliRunner

    from bizmap.cli import main

    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(arch_graph()), encoding="utf-8")
    runner = CliRunner()
    out = tmp_path / "arch.md"
    res = runner.invoke(main, ["export", "--graph", str(graph), "--format", "mermaid", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert out.read_text(encoding="utf-8").startswith("# Architecture\n\n```mermaid\nflowchart LR")
    res = runner.invoke(main, ["report", "--graph", str(graph), "--mermaid"])
    assert res.exit_code == 0, res.output
    assert "```mermaid" in res.output and "order-cancelled" in res.output
    res = runner.invoke(main, ["report", "--graph", str(graph), "--mermaid", "--redact", "--salt", "x"])
    assert res.exit_code != 0 and "--redact" in res.output


# ── PR comment ───────────────────────────────────────────────────────────────


def _diff(path: str, line: int) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{line},2 +{line},3 @@\n x\n+y\n z\n"


def test_pr_comment_shows_the_flow_of_a_touched_topic() -> None:
    g = graph_from_json(arch_graph())
    md = render_markdown(g, compute_impact(g, _diff("internal/orders/orders.go", 61)))  # Service.Cancel
    assert "<b>Event flow</b> · topic <code>order-cancelled</code> ⚠" in md
    assert "has no consumer in the scanned repos" in md
    block = md.split("<b>Event flow</b>", 1)[1]
    assert "```mermaid\nflowchart LR" in block
    # a consumer change brings its topic (and the chain behind it)
    md = render_markdown(g, compute_impact(g, _diff("billing/handlers.py", 15)))
    assert "<code>order-placed</code>" in md and "read but never published: `couponCode`" in md
    # no queue, no trigger touched: no section
    md = render_markdown(g, compute_impact(g, _diff("internal/orders/orders.go", 81)))  # Service.Get
    assert "Event flow" not in md
    # --no-mermaid keeps the notes, drops the diagram
    md = render_markdown(g, compute_impact(g, _diff("internal/orders/orders.go", 61)), mermaid=False)
    assert "Event flow" in md and "```mermaid" not in md


# ── HTML page data ───────────────────────────────────────────────────────────


def _blocks(html: str) -> dict:
    return {m.group(1): json.loads(m.group(2))
            for m in re.finditer(r'<script id="bm-([\w-]+)" type="application/json">(.*?)</script>', html, re.S)}


def test_html_embeds_the_views() -> None:
    html = export_graph(arch_graph(), "html")
    b = _blocks(html)
    ix = b["index"]
    assert {"ar", "ev"} <= set(ix["chunks"])
    v = ix["home"]["views"]
    assert (v["services"], v["stores"], v["topics"], v["problems"]) == (4, 2, 2, 1)
    assert [r[0] for r in v["events"]] == ["order-cancelled", "order-placed", "firestore:payment_events"]
    # the page draws the export's positions (same as the CLI's)
    assert _positions(b["ar"]) == _positions(views.architecture(_ctx()))
    assert {f["key"] for f in b["ev"]["flows"]} == {"order-cancelled", "order-placed", "firestore:payment_events"}
    # function links point at the book's pages
    placed = next(f for f in b["ev"]["flows"] if f["key"] == "order-placed")
    assert placed["publishers"][0]["fn"][2] >= 0
    for route in ('h[0] === "architecture"', 'h[0] === "queues"', "pageEvent("):
        assert route in html


def test_inlined_scripts_declare_each_name_once() -> None:
    """book.js, home.js and views.js share one scope: a second `function x` or
    `var x` silently replaces the first (a flow page once lost its labels)."""
    export = Path(views.__file__).parent / "export"
    seen: dict[str, str] = {}
    dupes = []
    for name in ("book.js", "home.js", "views.js"):
        for m in re.finditer(r"^  (?:function (\w+)\(|var (\w+)\b)", (export / name).read_text(encoding="utf-8"), re.M):
            ident = m.group(1) or m.group(2)
            if ident in seen:
                dupes.append((ident, seen[ident], name))
            seen.setdefault(ident, name)
    assert not dupes, dupes
