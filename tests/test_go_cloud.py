"""Firestore and Pub/Sub publishes in Go; topic and queue names in Python.

Repros (fictional) A–C from the request. The unit tests build the graph the
detectors would emit and run the text passes (no tree-sitter needed); the
`test_scan_*` tests run the whole scan and need the parsers installed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bizmap import dataflow, dbident, fsapply
from bizmap.detect import go_cloud
from bizmap.graph import Evidence, Graph
from bizmap.registry import merge_graphs
from test_dataflow import _feeds, _line
from test_firestore_flow import _warehouse_graph, _write_all

CONFIG = """package config

import "os"

func getEnv(k, def string) string { if v := os.Getenv(k); v != "" { return v }; return def }

type Config struct { ProjectID string; OrderTopic string }

func Load() Config { return Config{ProjectID: getEnv("GCP_PROJECT", "demo-shop"), OrderTopic: getEnv("ORDER_ANNOUNCE_TOPIC", "order-announce")} }
"""
PUBLISHER = """package pubsub

import (
\t"context"

\tgcppubsub "cloud.google.com/go/pubsub/v2"
)

type Publisher struct { client *gcppubsub.Client; pubs []*gcppubsub.Publisher }

func NewPublisher(ctx context.Context, projectID string, topicIDs []string) (*Publisher, error) {
\tclient, err := gcppubsub.NewClient(ctx, projectID); if err != nil { return nil, err }
\tpubs := make([]*gcppubsub.Publisher, len(topicIDs))
\tfor i, id := range topicIDs { pubs[i] = client.Publisher(id) }
\treturn &Publisher{client: client, pubs: pubs}, nil
}

func (p *Publisher) Publish(ctx context.Context, data []byte, attrs map[string]string) error {
\tfor _, pub := range p.pubs { if _, err := pub.Publish(ctx, &gcppubsub.Message{Data: data, Attributes: attrs}).Get(ctx); err != nil { return err } }
\treturn nil
}
"""
ANNOUNCE = """package orders

import (
\t"context"
\t"encoding/json"

\t"cloud.google.com/go/firestore"
\t"github.com/jackc/pgx/v5/pgxpool"
)

type OrderDoc struct { CustomerID string `firestore:"customerId"`; Total float64 `firestore:"total"`; Status string `firestore:"status"` }
type OrderAnnouncement struct { OrderID string `json:"orderId"`; CustomerID string `json:"customerId"`; Total float64 `json:"total"`; Status string `json:"status,omitempty"` }
type Publisher interface { Publish(ctx context.Context, data []byte, attrs map[string]string) error }
type Announcer struct { fs *firestore.Client; db *pgxpool.Pool; pub Publisher }
type MirrorRow struct { ID string; CustomerID string; Total float64 }

func (a *Announcer) Mirror(ctx context.Context, id string) error {
\tsnap, err := a.fs.Collection("orders").Doc(id).Get(ctx); if err != nil { return err }
\tvar d OrderDoc; if err := snap.DataTo(&d); err != nil { return err }
\t_, err = a.db.Exec(ctx, `INSERT INTO orders_mirror (id, customer_id, total) VALUES ($1, $2, $3)
\t\tON CONFLICT (id) DO UPDATE SET total = EXCLUDED.total`, id, d.CustomerID, d.Total)
\treturn err
}

func build(id string, d OrderDoc) OrderAnnouncement { return OrderAnnouncement{OrderID: id, CustomerID: d.CustomerID, Total: d.Total, Status: d.Status} }

func (a *Announcer) Announce(ctx context.Context, id string) error {
\tsnap, err := a.fs.Collection("orders").Doc(id).Get(ctx); if err != nil { return err }
\tvar d OrderDoc; if err := snap.DataTo(&d); err != nil { return err }
\tbody, err := json.Marshal(build(id, d)); if err != nil { return err }
\treturn a.pub.Publish(ctx, body, map[string]string{"orderId": id})
}
"""
REREAD = """package orders

import (
\t"context"
\t"encoding/json"
)

func (a *Announcer) AnnounceFromMirror(ctx context.Context, id string) error {
\tvar row MirrorRow
\tif err := a.db.QueryRow(ctx, `SELECT id, customer_id, total FROM orders_mirror WHERE id = $1`, id).Scan(&row.ID, &row.CustomerID, &row.Total); err != nil {
\t\treturn err
\t}
\tmsg := OrderAnnouncement{OrderID: row.ID, CustomerID: row.CustomerID, Total: row.Total}
\tbody, err := json.Marshal(msg)
\tif err != nil {
\t\treturn err
\t}
\treturn a.pub.Publish(ctx, body, nil)
}
"""
DIRECT = """package orders

import (
\t"context"
\t"encoding/json"

\tgcppubsub "cloud.google.com/go/pubsub/v2"
)

const RefundTopic = "refund-requested"

func PublishRefund(ctx context.Context, client *gcppubsub.Client, orderID string, amount float64) error {
\tbody, _ := json.Marshal(map[string]any{"orderId": orderID, "amount": amount})
\t_, err := client.Publisher(RefundTopic).Publish(ctx, &gcppubsub.Message{Data: body}).Get(ctx)
\treturn err
}
"""
MAIN = """package main

func main() {
\tctx := context.Background()
\tcfg := config.Load()
\tpub, err := pubsub.NewPublisher(ctx, cfg.ProjectID, []string{cfg.OrderTopic})
\tann := orders.NewAnnouncer(fs, db, pub)
\t_, _ = ann, err
}
"""
GO = {"internal/config/config.go": CONFIG, "internal/pubsub/publisher.go": PUBLISHER,
      "internal/orders/announce.go": ANNOUNCE, "internal/orders/direct.go": DIRECT, "cmd/api/main.go": MAIN}
MIRROR = ["id", "customer_id", "total"]


def _go_functions(files: dict[str, str]) -> list[tuple[str, str, int, int]]:
    out = []
    for rel, text in files.items():
        pkg = rel.rsplit("/", 1)[0]
        lines = text.split("\n")
        for k, line in enumerate(lines):
            m = re.match(r"func\s+(?:\(\s*\w+\s+\*?(\w+)\s*\)\s*)?(\w+)\s*\(", line)
            if not m:
                continue
            depth, seen, end = 0, False, k
            for j in range(k, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                seen = seen or "{" in lines[j]
                if seen and depth <= 0:
                    end = j
                    break
            recv = f"{m.group(1)}." if m.group(1) else ""
            out.append((f"{pkg}.{recv}{m.group(2)}", rel, k + 1, end + 1))
    return out


def _go_graph(root: Path, files: dict[str, str], *, repo: str | None = None,
              edges: tuple[tuple[str, str, str, str, str], ...] = ()) -> tuple[Graph, object]:
    """Graph as the Go detector emits it (functions, calls, SQL edges), then the passes."""
    _write_all(root, files)
    g = Graph()
    g.repo = repo
    g.attrs["repo"] = repo
    g.schema = {"tables": {"orders_mirror": {"columns": {c: {"type": "text"} for c in MIRROR}}}}
    g.add_node("table:orders_mirror", "table", name="orders_mirror")
    for c in MIRROR:
        g.add_node(f"column:orders_mirror.{c}", "column", table="orders_mirror", name=c)
    for qual, rel, start, end in _go_functions(files):
        g.add_node(f"function:{qual}", "function", name=qual, language="go",
                   evidence={"file": rel, "line": start, "symbol": qual}, end_line=end)
    for etype, frm, to, rel, needle in edges:
        g.add_edge(etype, f"function:{frm}", to if ":" in to else f"function:{to}",
                   evidence=Evidence(rel, _line(files[rel], needle), frm))
    meta = fsapply.apply_go_cloud(g, files, {})
    idx = dbident.build_index(files, {})
    dbident.assign_dbs(g, files, idx)
    dataflow.apply(g, root, dict(files), firestore=idx.firestore or False, go_meta=meta)
    return g, meta


ORD = "internal/orders."
A_EDGES = (
    ("writes", ORD + "Announcer.Mirror", "column:orders_mirror.total", "internal/orders/announce.go", "a.db.Exec"),
    ("calls", ORD + "Announcer.Announce", ORD + "build", "internal/orders/announce.go", "json.Marshal(build"),
)


# ── A ─────────────────────────────────────────────────────────────────────────


def test_scan_go_finds_firestore_and_publishes() -> None:
    res = go_cloud.scan_go(GO, _go_functions(GO))
    assert {(a.symbol.rsplit(".", 1)[-1], a.kind, a.collection) for a in res.accesses} == {
        ("Mirror", "read", "orders"), ("Announce", "read", "orders"),
    }
    pubs = {p.symbol.rsplit(".", 1)[-1]: p for p in res.publishes}
    assert [(t.name, t.env) for t in pubs["Announce"].topics] == [("order-announce", "ORDER_ANNOUNCE_TOPIC")]
    assert pubs["Announce"].fields == ["customerId", "orderId", "status", "total"]
    assert pubs["Announce"].via == "Publisher"
    assert [(t.name, t.env) for t in pubs["PublishRefund"].topics] == [("refund-requested", None)]
    assert pubs["PublishRefund"].fields == ["amount", "orderId"]
    assert res.fs_tags["OrderDoc"] == {"CustomerID": "customerId", "Total": "total", "Status": "status"}


def test_go_firestore_mirror_feeds_and_enqueues(tmp_path: Path) -> None:
    g, _ = _go_graph(tmp_path / "go", GO, edges=A_EDGES)
    assert f"reads:function:{ORD}Announcer.Mirror->collection:orders" in g.edges
    assert f"reads:function:{ORD}Announcer.Announce->collection:orders" in g.edges
    assert _feeds(g) == {
        ("collection:orders", "table:orders_mirror"): (
            "exact",
            {("customerId", "customer_id", "exact", False), ("total", "total", "exact", False), ("__id__", "id", "exact", False)},
        ),
    }
    enq = g.edges[f"enqueues:function:{ORD}Announcer.Announce->queue:pubsub:order-announce"]
    assert enq.attrs["fields"] == ["customerId", "orderId", "status", "total"]
    assert g.nodes["queue:pubsub:order-announce"].attrs["env"] == "ORDER_ANNOUNCE_TOPIC"
    refund = g.edges[f"enqueues:function:{ORD}PublishRefund->queue:pubsub:refund-requested"]
    assert refund.attrs["fields"] == ["amount", "orderId"]
    out = g.nodes[f"function:{ORD}Announcer.Announce"].attrs["dataflow_out"][0]
    assert out["topic"] == "order-announce"
    assert out["fields"]["customerId"] == [{"table": "collection:orders", "column": "customerId", "hops": [f"function:{ORD}Announcer.Announce"]}]


def test_env_topic_without_default_is_dynamic(tmp_path: Path) -> None:
    files = dict(GO)
    files["internal/config/config.go"] = CONFIG.replace('getEnv("ORDER_ANNOUNCE_TOPIC", "order-announce")', 'os.Getenv("ORDER_ANNOUNCE_TOPIC")')
    g, _ = _go_graph(tmp_path / "dyn", files, edges=A_EDGES)
    assert "queue:pubsub:order-announce" not in g.nodes
    node = g.nodes[f"function:{ORD}Announcer.Announce"]
    assert [q["env"] for q in node.attrs["unresolved_queues"]] == ["ORDER_ANNOUNCE_TOPIC"]
    res = go_cloud.scan_go(files, _go_functions(files), {"ORDER_ANNOUNCE_TOPIC": "orders-v2"})
    pub = next(p for p in res.publishes if p.symbol.endswith("Announce"))
    assert [(t.name, t.env) for t in pub.topics] == [("orders-v2", "ORDER_ANNOUNCE_TOPIC")]


def test_two_wrapper_topics_are_candidates(tmp_path: Path) -> None:
    files = dict(GO)
    files["cmd/api/main.go"] = MAIN.replace("[]string{cfg.OrderTopic}", '[]string{cfg.OrderTopic, "audit"}')
    g, _ = _go_graph(tmp_path / "two", files, edges=A_EDGES)
    edge = g.edges[f"enqueues:function:{ORD}Announcer.Announce->queue:pubsub:audit"]
    assert edge.attrs["candidates"] == ["audit", "order-announce"]
    assert "dataflow_out" not in g.nodes[f"function:{ORD}Announcer.Announce"].attrs  # not linked: ambiguous


def test_valid_topic_names() -> None:
    assert go_cloud.valid_topic("order-announce")
    for bad in ("{live[:10]}", "a or b", "x\ny", "{'a': 1}", "", None):
        assert not go_cloud.valid_topic(bad)


# ── B ─────────────────────────────────────────────────────────────────────────


def test_go_publisher_to_python_push_route(tmp_path: Path) -> None:
    gg, _ = _go_graph(tmp_path / "go", GO, repo="go-publisher", edges=A_EDGES)
    gw = _warehouse_graph(tmp_path / "warehouse", {"push_subscriptions": {"order-announce": "POST /pubsub/orders"}})
    merged = merge_graphs([gg, gw])
    dataflow.link_queue_flows(merged)
    assert "queue:pubsub:order-announce" in merged.nodes  # one node: the Go enqueue and the push subscription
    assert f"enqueues:go-publisher/function:{ORD}Announcer.Announce->queue:pubsub:order-announce" in merged.edges
    assert "triggers:queue:pubsub:order-announce->warehouse/entrypoint:http:POST:/pubsub/orders" in merged.edges
    feeds = _feeds(merged)
    assert feeds[("go-publisher/collection:orders", "warehouse/table:order_summary")] == (
        "heuristic",
        {
            ("customerId", "customer_id", "heuristic", False),
            ("total", "total_amount", "heuristic", False),
            ("status", "status", "heuristic", False),
            ("__id__", "order_id", "heuristic", False),
        },
    )
    edge = merged.edges["feeds:go-publisher/collection:orders->warehouse/table:order_summary"]
    assert edge.attrs["via"][0]["path"] == [
        f"go-publisher/function:{ORD}Announcer.Announce",
        "queue:pubsub:order-announce",
        "warehouse/function:warehouse.app.announce_order",
        "warehouse/function:warehouse.store.upsert_order",
    ]


def test_go_rereads_its_mirror_then_publishes(tmp_path: Path) -> None:
    files = {**GO, "internal/orders/reread.go": REREAD}
    files["internal/orders/announce.go"] = ANNOUNCE.replace(
        "\treturn a.pub.Publish(ctx, body, map[string]string{\"orderId\": id})", "\t_ = body\n\treturn nil"
    )
    edges = (
        *A_EDGES,
        ("reads", ORD + "Announcer.AnnounceFromMirror", "column:orders_mirror.id", "internal/orders/reread.go", "a.db.QueryRow"),
    )
    gg, _ = _go_graph(tmp_path / "go", files, repo="go-publisher", edges=edges)
    gw = _warehouse_graph(tmp_path / "warehouse", {"push_subscriptions": {"order-announce": "POST /pubsub/orders"}})
    merged = merge_graphs([gg, gw])
    dataflow.link_queue_flows(merged)
    feeds = _feeds(merged)
    assert feeds[("go-publisher/table:orders_mirror", "warehouse/table:order_summary")][1] == {
        ("customer_id", "customer_id", "heuristic", False),
        ("total", "total_amount", "heuristic", False),
        ("id", "order_id", "heuristic", False),
    }
    # and the mirror itself is fed from Firestore (A): the two chain
    assert ("go-publisher/collection:orders", "go-publisher/table:orders_mirror") in feeds


# ── whole scan (needs the tree-sitter parsers) ────────────────────────────────

GO_MOD = """module example.com/gopublisher

go 1.22

require (
\tcloud.google.com/go/firestore v1.15.0
\tcloud.google.com/go/pubsub/v2 v2.0.0
\tgithub.com/jackc/pgx/v5 v5.6.0
)
"""


def test_scan_go_publisher(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    from bizmap.scan import scan_path

    _write_all(tmp_path, {**GO, "go.mod": GO_MOD,
                          "schema.sql": "CREATE TABLE orders_mirror (id text PRIMARY KEY, customer_id text, total numeric);\n"})
    g = scan_path(tmp_path)
    reads = {e.from_id.rsplit(".", 1)[-1] for e in g.edges.values()
             if e.type == "reads" and e.to_id == "collection:orders" and not e.attrs.get("derived")}
    assert {"Mirror", "Announce"} <= reads
    cols = _feeds(g)[("collection:orders", "table:orders_mirror")][1]
    assert {("customerId", "customer_id"), ("total", "total"), ("__id__", "id")} <= {(c[0], c[1]) for c in cols}
    enq = {e.to_id: e for e in g.edges.values() if e.type == "enqueues"}
    assert enq["queue:pubsub:order-announce"].attrs["fields"] == ["customerId", "orderId", "status", "total"]
    assert enq["queue:pubsub:refund-requested"].attrs["fields"] == ["amount", "orderId"]
    assert g.nodes["queue:pubsub:order-announce"].attrs["env"] == "ORDER_ANNOUNCE_TOPIC"


PY_JUNK = '''import json
import os

from google.cloud import pubsub_v1
from rq import Queue

SHIPPED_TOPIC = "order-shipped"


def build_shipped_publisher():
    topic = os.environ.get("SHIPPED_TOPIC_NAME", "")
    if not topic:
        return None
    client = pubsub_v1.PublisherClient()
    topic_path = topic if "/" in topic else client.topic_path("demo-shop", topic)

    def publish(order_id: str) -> None:
        client.publish(topic_path, json.dumps({"orderId": order_id}).encode()).result()

    return publish


class InvoicePublisher:
    def __init__(self, project: str, topic: str = "invoice-ready"):
        self._client = pubsub_v1.PublisherClient()
        self.topic_path = self._client.topic_path(project, topic)

    def __call__(self, invoice_id: str) -> None:
        self._client.publish(self.topic_path, json.dumps({"invoiceId": invoice_id}).encode())


def publish_shipped(order_id: str) -> None:
    client = pubsub_v1.PublisherClient()
    client.publish(client.topic_path("demo-shop", SHIPPED_TOPIC), json.dumps({"orderId": order_id}).encode())


def resend(pending: list, publish) -> int:
    live = [p for p in pending if p["live"]]
    return len(list(publish(live[:10])))


class Stager:
    def publish(self, message: dict) -> None: ...

    def stage(self, order_id: str) -> None:
        self.publish({"orderId": order_id, "stage": "packed"})


def schedule(conn):
    q = Queue("emails", connection=conn)
    q.enqueue("jobs.send_email", "x")


def requeue(enqueue, cx, order_id: str) -> None:
    enqueue(cx, target_id=order_id)


def redo(repo, row) -> None:
    repo.enqueue(row["kind"], row["id"])
'''


def _queues(g: Graph) -> set[str]:
    return {n.id for n in g.nodes.values() if n.type == "queue"}


def test_scan_python_topic_and_queue_names(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_python")
    from bizmap.scan import scan_path

    _write_all(tmp_path, {"pyjunk/__init__.py": "", "pyjunk/pub.py": PY_JUNK})
    g = scan_path(tmp_path)
    assert _queues(g) == {"queue:pubsub:order-shipped", "queue:pubsub:invoice-ready", "queue:rq:emails"}
    assert next(e for e in g.edges.values() if e.to_id == "queue:rq:emails").attrs["task"] == "jobs.send_email"
    dyn = [n for n in g.nodes.values() if n.attrs.get("unresolved_queues")]
    assert [q["env"] for n in dyn for q in n.attrs["unresolved_queues"]] == ["SHIPPED_TOPIC_NAME"]


def test_scan_python_env_topic_from_bizmap_yaml(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_python")
    from bizmap.scan import scan_path

    _write_all(tmp_path, {"pyjunk/__init__.py": "", "pyjunk/pub.py": PY_JUNK,
                          "bizmap.yaml": "env:\n  SHIPPED_TOPIC_NAME: order-shipped\n"})
    g = scan_path(tmp_path)
    shipped = {e.from_id.rsplit(".", 1)[-1] for e in g.edges.values() if e.to_id == "queue:pubsub:order-shipped"}
    assert {"publish", "publish_shipped"} <= shipped
    assert g.nodes["queue:pubsub:order-shipped"].attrs["env"] == "SHIPPED_TOPIC_NAME"
