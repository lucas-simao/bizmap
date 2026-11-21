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

func NewAnnouncer(fs *firestore.Client, db *pgxpool.Pool, pub Publisher) *Announcer { return &Announcer{fs: fs, db: db, pub: pub} }

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


def test_one_wrapper_with_two_topics_publishes_to_both(tmp_path: Path) -> None:
    # the wrapper's Publish loops over every topic it was built with: two real edges
    files = dict(GO)
    files["cmd/api/main.go"] = MAIN.replace("[]string{cfg.OrderTopic}", '[]string{cfg.OrderTopic, "audit"}')
    g, _ = _go_graph(tmp_path / "two", files, edges=A_EDGES)
    for topic in ("audit", "order-announce"):
        edge = g.edges[f"enqueues:function:{ORD}Announcer.Announce->queue:pubsub:{topic}"]
        assert "candidates" not in edge.attrs
        assert edge.confidence.value == "exact"


CONFIG_FANOUT = CONFIG.replace(
    "type Config struct { ProjectID string; OrderTopic string }",
    "type Config struct { ProjectID string; OrderTopic string; InvoiceTopic string }",
).replace(
    'OrderTopic: getEnv("ORDER_ANNOUNCE_TOPIC", "order-announce")}',
    'OrderTopic: getEnv("ORDER_ANNOUNCE_TOPIC", "order-announce"), InvoiceTopic: getEnv("INVOICE_TOPIC", "invoice-ready")}',
)
INVOICES = """package invoices

import (
\t"context"
\t"encoding/json"
)

type Publisher interface { Publish(ctx context.Context, data []byte, attrs map[string]string) error }
type Service struct{ pub Publisher }

func New(pub Publisher) *Service { return &Service{pub: pub} }

func (s *Service) Ready(ctx context.Context, id string) error {
\tbody, _ := json.Marshal(map[string]string{"invoiceId": id})
\treturn s.pub.Publish(ctx, body, nil)
}
"""
CATALOG = """package catalog

import "context"

// a domain verb, not Pub/Sub
type Store interface { Publish(ctx context.Context, id string) error }
type Controller struct{ store Store }

func (c *Controller) PublishDraft(ctx context.Context, id string) error { return c.store.Publish(ctx, id) }
"""
MAIN_FANOUT = """package main

func main() {
\tctx := context.Background()
\tcfg := config.Load()
\tpub, _ := pubsub.NewPublisher(ctx, cfg.ProjectID, []string{cfg.OrderTopic})
\tann := orders.NewAnnouncer(fs, db, pub)
\tinvPub, _ := pubsub.NewPublisher(ctx, cfg.ProjectID, []string{cfg.InvoiceTopic})
\tinv := invoices.New(invPub)
\t_, _ = ann, inv
}
"""
FANOUT = {**GO, "internal/config/config.go": CONFIG_FANOUT, "internal/invoices/invoices.go": INVOICES,
          "internal/catalog/catalog.go": CATALOG, "cmd/api/main.go": MAIN_FANOUT}


def _enqueues(g: Graph) -> set[tuple[str, str]]:
    return {(e.from_id.rsplit(".", 1)[-1], e.to_id) for e in g.edges.values() if e.type == "enqueues"}


def test_each_wrapper_instance_keeps_its_topic(tmp_path: Path) -> None:
    g, _ = _go_graph(tmp_path / "fanout", FANOUT, edges=A_EDGES)
    assert _enqueues(g) == {
        ("Announce", "queue:pubsub:order-announce"),
        ("Ready", "queue:pubsub:invoice-ready"),
        ("PublishRefund", "queue:pubsub:refund-requested"),
    }  # nothing for catalog.PublishDraft: `Store.Publish(ctx, id)` is not the wrapper's signature
    ready = g.edges["enqueues:function:internal/invoices.Service.Ready->queue:pubsub:invoice-ready"]
    assert ready.attrs["fields"] == ["invoiceId"]
    assert ready.confidence.value == "exact"


MAIN_MAP = """package main

type Kind int

const (
\tKindOrder Kind = iota
\tKindInvoice
)

func main() {
\tctx := context.Background()
\tcfg := config.Load()
\ttopics := map[Kind]string{KindOrder: cfg.OrderTopic, KindInvoice: cfg.InvoiceTopic}
\tpubs := map[Kind]*pubsub.Publisher{}
\tfor _, k := range []Kind{KindOrder, KindInvoice} {
\t\tp, _ := pubsub.NewPublisher(ctx, cfg.ProjectID, []string{topics[k]})
\t\tpubs[k] = p
\t}
\tann := orders.NewAnnouncer(fs, db, pubs[KindOrder])
\tinv := invoices.New(pubs[KindInvoice])
\t_, _ = ann, inv
}
"""


def test_publishers_built_from_a_map_of_topics(tmp_path: Path) -> None:
    g, _ = _go_graph(tmp_path / "map", {**FANOUT, "cmd/api/main.go": MAIN_MAP}, edges=A_EDGES)
    assert _enqueues(g) == {
        ("Announce", "queue:pubsub:order-announce"),
        ("Ready", "queue:pubsub:invoice-ready"),
        ("PublishRefund", "queue:pubsub:refund-requested"),
    }


def test_unknown_map_key_gives_candidates_not_edges(tmp_path: Path) -> None:
    main = MAIN_MAP.replace("orders.NewAnnouncer(fs, db, pubs[KindOrder])", "orders.NewAnnouncer(fs, db, pubs[kind])")
    g, _ = _go_graph(tmp_path / "mapk", {**FANOUT, "cmd/api/main.go": main}, edges=A_EDGES)
    assert ("Announce", "queue:pubsub:order-announce") not in _enqueues(g)
    assert ("Announce", "queue:pubsub:invoice-ready") not in _enqueues(g)
    node = g.nodes[f"function:{ORD}Announcer.Announce"]
    assert [q["candidates"] for q in node.attrs["unresolved_queues"]] == [["invoice-ready", "order-announce"]]
    assert ("Ready", "queue:pubsub:invoice-ready") in _enqueues(g)


def test_env_without_default_takes_the_config_default(tmp_path: Path) -> None:
    worker = """package main

import (
\t"context"
\t"os"

\tgcppubsub "cloud.google.com/go/pubsub/v2"
)

func run(ctx context.Context, client *gcppubsub.Client, body []byte) error {
\ttopic := os.Getenv("ORDER_ANNOUNCE_TOPIC")
\t_, err := client.Publisher(topic).Publish(ctx, &gcppubsub.Message{Data: body}).Get(ctx)
\treturn err
}
"""
    g, _ = _go_graph(tmp_path / "cmd", {**GO, "cmd/worker/main.go": worker}, edges=A_EDGES)
    assert ("run", "queue:pubsub:order-announce") in _enqueues(g)


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


ANNOUNCE_PKG = """package announce

import (
\t"context"
\t"encoding/json"

\t"example.com/gopublisher/internal/orders"
)

type Publisher interface { Publish(ctx context.Context, data []byte, attrs map[string]string) error }
type Payload struct { Body []byte }

func mustJSON(v any) []byte {
\tb, _ := json.Marshal(v)
\treturn b
}

func Build(r orders.MirrorRow) (Payload, error) {
\treturn Payload{Body: mustJSON(orders.OrderAnnouncement{OrderID: r.ID, CustomerID: r.CustomerID, Total: r.Total})}, nil
}

func Send(ctx context.Context, pub Publisher, p Payload) error { return pub.Publish(ctx, p.Body, nil) }
"""
REREAD_PKG = """package orders

import "context"

func (a *Announcer) AnnounceFromMirror(ctx context.Context, id string) error {
\tvar row MirrorRow
\tif err := a.db.QueryRow(ctx, `SELECT id, customer_id, total FROM orders_mirror WHERE id = $1`, id).Scan(&row.ID, &row.CustomerID, &row.Total); err != nil {
\t\treturn err
\t}
\tpayload, err := announce.Build(row)
\tif err != nil {
\t\treturn err
\t}
\treturn announce.Send(ctx, a.pub, payload)
}
"""


def test_build_and_send_in_other_packages(tmp_path: Path) -> None:
    files = {**GO, "internal/orders/reread.go": REREAD_PKG, "internal/announce/announce.go": ANNOUNCE_PKG}
    files["internal/orders/announce.go"] = ANNOUNCE.replace(
        "\treturn a.pub.Publish(ctx, body, map[string]string{\"orderId\": id})", "\t_ = body\n\treturn nil"
    )
    rr, an = "internal/orders/reread.go", "internal/announce/announce.go"
    edges = (
        *A_EDGES,
        ("reads", ORD + "Announcer.AnnounceFromMirror", "column:orders_mirror.id", rr, "a.db.QueryRow"),
        ("calls", ORD + "Announcer.AnnounceFromMirror", "internal/announce.Build", rr, "announce.Build"),
        ("calls", ORD + "Announcer.AnnounceFromMirror", "internal/announce.Send", rr, "announce.Send"),
        ("calls", "internal/announce.Build", "internal/announce.mustJSON", an, "mustJSON(orders"),
    )
    gg, _ = _go_graph(tmp_path / "go", files, repo="go-publisher", edges=edges)
    assert ("Send", "queue:pubsub:order-announce") in _enqueues(gg)
    assert ("Announce", "queue:pubsub:order-announce") not in _enqueues(gg)
    gw = _warehouse_graph(tmp_path / "warehouse", {"push_subscriptions": {"order-announce": "POST /pubsub/orders"}})
    merged = merge_graphs([gg, gw])
    dataflow.link_queue_flows(merged)
    feeds = _feeds(merged)
    assert feeds[("go-publisher/table:orders_mirror", "warehouse/table:order_summary")][1] == {
        ("customer_id", "customer_id", "heuristic", False),
        ("total", "total_amount", "heuristic", False),
        ("id", "order_id", "heuristic", False),
    }


SYNC = """package orders

import (
\t"context"

\t"cloud.google.com/go/firestore"
\t"github.com/jackc/pgx/v5/pgxpool"
)

type FsRepo struct{ client *firestore.Client }

func (r *FsRepo) GetOrder(ctx context.Context, id string) (OrderDoc, error) {
\tsnap, err := r.client.Collection("orders").Doc(id).Get(ctx)
\tif err != nil {
\t\treturn OrderDoc{}, err
\t}
\tvar d OrderDoc
\terr = snap.DataTo(&d)
\treturn d, err
}

type PgRepo struct{ db *pgxpool.Pool }

func (r *PgRepo) Upsert(ctx context.Context, id string, d OrderDoc) error {
\t_, err := r.db.Exec(ctx, `INSERT INTO orders_mirror (id, customer_id, total) VALUES ($1, $2, $3)`, id, d.CustomerID, d.Total)
\treturn err
}

type Sync struct{ fs *FsRepo; pg *PgRepo }

func (s *Sync) Mirror(ctx context.Context, id string) error {
\td, err := s.fs.GetOrder(ctx, id)
\tif err != nil {
\t\treturn err
\t}
\treturn s.pg.Upsert(ctx, id, d)
}
"""


def test_mirror_through_two_repositories(tmp_path: Path) -> None:
    files = {k: v for k, v in GO.items() if k != "internal/orders/announce.go"}
    files["internal/orders/types.go"] = ANNOUNCE.split("func (a *Announcer) Mirror")[0]
    files["internal/orders/sync.go"] = SYNC
    sy = "internal/orders/sync.go"
    edges = (
        ("writes", ORD + "PgRepo.Upsert", "column:orders_mirror.total", sy, "r.db.Exec"),
        ("calls", ORD + "Sync.Mirror", ORD + "FsRepo.GetOrder", sy, "s.fs.GetOrder"),
        ("calls", ORD + "Sync.Mirror", ORD + "PgRepo.Upsert", sy, "s.pg.Upsert"),
    )
    g, _ = _go_graph(tmp_path / "sync", files, edges=edges)
    conf, cols = _feeds(g)[("collection:orders", "table:orders_mirror")]
    assert conf == "heuristic"  # across calls, as SQL -> SQL
    assert {(c[0], c[1]) for c in cols} == {("customerId", "customer_id"), ("total", "total"), ("__id__", "id")}


REFS = """package customers

import (
\t"context"

\t"cloud.google.com/go/firestore"
)

const NotesCollection = "notes"

type Repo struct{ client *firestore.Client; collection string }

func NewRepo(c *firestore.Client) *Repo { return &Repo{client: c, collection: "customers"} }

func (r *Repo) Many(ctx context.Context, ids []string) error {
\trefs := make([]*firestore.DocumentRef, len(ids))
\tfor i, id := range ids {
\t\trefs[i] = r.client.Collection("customers").Doc(id)
\t}
\t_, err := r.client.GetAll(ctx, refs)
\treturn err
}

func (r *Repo) Touch(ctx context.Context, ids []string) error {
\tbatch := r.client.Batch()
\trefs := make([]*firestore.DocumentRef, len(ids))
\tfor i, id := range ids {
\t\trefs[i] = r.client.Collection("archive").Doc(id)
\t\tbatch.Update(refs[i], []firestore.Update{{Path: "seen", Value: true}})
\t}
\t_, err := batch.Commit(ctx)
\treturn err
}

func (r *Repo) One(ctx context.Context, id string) error {
\t_, err := r.client.Collection(r.collection).Doc(id).Get(ctx)
\treturn err
}

func (r *Repo) Notes(ctx context.Context, id string) error {
\t_, err := r.client.Collection(NotesCollection).Doc(id).Delete(ctx)
\treturn err
}
"""


def test_firestore_refs_in_loops_and_collection_fields() -> None:
    files = {"internal/customers/repo.go": REFS}
    res = go_cloud.scan_go(files, _go_functions(files))
    assert {(a.symbol.rsplit(".", 1)[-1], a.kind, a.collection) for a in res.accesses} == {
        ("Many", "read", "customers"),
        ("Touch", "write", "archive"),
        ("One", "read", "customers"),
        ("Notes", "write", "notes"),
    }
    touch = next(a for a in res.accesses if a.symbol.endswith("Touch"))
    assert touch.fields == ["seen"]


def test_relink_after_merge_adds_a_queue_without_crashing() -> None:
    pytest.importorskip("tree_sitter_go")
    from bizmap.scan import relink_merged_graph

    g = Graph()
    for k in range(3):
        g.add_node(f"svc/function:p{k}", "function", name=f"p{k}", queue_events=[
            {"role": "produce", "kind": "pubsub", "name": f"topic-{k}"},
            {"role": "produce", "kind": "pubsub", "name": ""},
            {"role": "produce", "kind": "pubsub", "name": "{live[:10]}"},
        ])
    relink_merged_graph(g)
    assert {n for n in g.nodes if n.startswith("queue:")} == {"queue:pubsub:topic-0", "queue:pubsub:topic-1", "queue:pubsub:topic-2"}


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
