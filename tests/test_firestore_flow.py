"""Firestore in JS/TS, Firestore as a `feeds` source, SQLAlchemy Core columns,
Pub/Sub push between repos.

Repros (fictional) A–E from the request. The unit tests build the graph the
detectors would emit and run the text passes (no tree-sitter needed); the
`test_scan_*` tests run the whole scan and need the parsers installed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bizmap import dataflow, dbident, fsapply
from bizmap.detect import js_firestore
from bizmap.graph import Evidence, Graph
from bizmap.registry import merge_graphs
from test_dataflow import _feeds, _graph, _line

# ── A: web modular SDK ────────────────────────────────────────────────────────

WEB_FIREBASE = """import { initializeApp } from 'firebase/app';
import { getFirestore } from 'firebase/firestore';
export const db = getFirestore(initializeApp({ projectId: 'demo-shop' }));
"""
WEB_CONSTANTS = """export const ORDERS_COLLECTION = 'orders';
export enum Collections { Customers = 'customers' }
"""
WEB_ORDERS = """import { collection, doc, getDoc, getDocs, query, where, updateDoc, addDoc, deleteDoc, setDoc, onSnapshot } from 'firebase/firestore';
import { db } from '../firebase';
import { ORDERS_COLLECTION, Collections } from './constants';
export async function listPaidOrders() {
  const snap = await getDocs(query(collection(db, 'orders'), where('status', '==', 'paid')));
  return snap.docs.map(d => d.data());
}
export async function getOrder(id: string) { return (await getDoc(doc(db, 'orders', id))).data(); }
export async function markShipped(id: string) { await updateDoc(doc(db, 'orders', id), { status: 'shipped', shippedAt: new Date() }); }
export async function createOrder(customerId: string, total: number) { return addDoc(collection(db, 'orders'), { customerId, total, status: 'new' }); }
export async function removeOrder(id: string) { await deleteDoc(doc(db, 'orders', id)); }
export async function countOrders() { return (await getDocs(collection(db, ORDERS_COLLECTION))).size; }
export async function saveCustomer(id: string, name: string) { await setDoc(doc(db, Collections.Customers, id), { name }, { merge: true }); }
export async function addNote(customerId: string, text: string) { await addDoc(collection(db, `customers/${customerId}/notes`), { text }); }
export async function getOrderByPath(id: string) { return (await getDoc(doc(db, `orders/${id}`))).data(); }
const ordersRef = collection(db, 'orders');
export async function touchOrder(id: string) { await updateDoc(doc(ordersRef, id), { touchedAt: Date.now() }); }
async function listAll(path: string) { return (await getDocs(collection(db, path))).docs.map(d => d.data()); }
export const listCustomers = () => listAll('customers');
export function watchOrder(id: string, cb: (o: unknown) => void) { return onSnapshot(doc(db, 'orders', id), s => cb(s.data())); }
"""
WEB_PAGE = """import { listPaidOrders } from '../api/orders';
export function OrdersPage() {
  const orders = listPaidOrders();
  return orders;
}
"""
WEB = {"src/firebase.ts": WEB_FIREBASE, "src/api/constants.ts": WEB_CONSTANTS, "src/api/orders.ts": WEB_ORDERS,
       "src/pages/OrdersPage.tsx": WEB_PAGE}

# ── B: admin SDK chains ───────────────────────────────────────────────────────

ADMIN = """import { getFirestore } from 'firebase-admin/firestore';
const db = getFirestore();
export async function getOrder(id: string) { return (await db.collection('orders').doc(id).get()).data(); }
export async function paidOrders() { return (await db.collection('orders').where('status', '==', 'paid').get()).docs; }
export async function closeOrder(id: string) { await db.collection('orders').doc(id).update({ status: 'closed' }); }
export async function copyCustomer(id: string) {
  const c = (await db.collection('customers').doc(id).get()).data()!;
  await db.collection('customer_snapshots').doc(id).set({ name: c.name, email: c.email });
}
"""

# ── E: functions (TS trigger + publish) and warehouse (push route) ────────────

FUNCTIONS = """import { onDocumentWritten } from 'firebase-functions/v2/firestore';
import { PubSub } from '@google-cloud/pubsub';
const pubsub = new PubSub();
export const announceOrder = onDocumentWritten('orders/{orderId}', async (event) => {
  const after = event.data?.after.data();
  if (!after) return;
  await pubsub.topic('order-announce').publishMessage({
    json: { orderId: event.params.orderId, customerId: after.customerId, total: after.total, status: after.status },
  });
});
"""
WAREHOUSE_APP = '''import base64
import json
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field
from warehouse.store import upsert_order, engine

app = FastAPI()


class OrderAnnouncement(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    order_id: str = Field(alias="orderId")
    customer_id: str | None = Field(default=None, alias="customerId")
    total: float | None = None
    status: str | None = None

    def to_record(self) -> OrderRecord:
        return OrderRecord(order_id=self.order_id, customer_id=self.customer_id, total=self.total, status=self.status)


class PushEnvelope(BaseModel):
    message: PushMessage = Field(default_factory=PushMessage)

    def order_record(self) -> OrderRecord | None:
        if not self.message.data:
            return None
        return OrderAnnouncement.model_validate(json.loads(base64.b64decode(self.message.data))).to_record()


@app.post("/pubsub/orders")
def announce_order(envelope: PushEnvelope) -> dict:
    record = envelope.order_record()
    with engine.begin() as conn:
        upsert_order(conn, record)
    return {"ok": True}
'''
WAREHOUSE_STORE = '''import os
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from warehouse.tables import order_summary

engine = create_engine(os.environ["WAREHOUSE_DATABASE_URL"])


def upsert_order(conn, record) -> None:
    stmt = pg_insert(order_summary).values({
        "order_id": record.order_id,
        "customer_id": record.customer_id,
        "total_amount": record.total,
        "status": record.status,
    })
    conn.execute(stmt.on_conflict_do_update(index_elements=["order_id"], set_={"total_amount": stmt.excluded.total_amount}))
'''
WAREHOUSE_TABLES = '''from sqlalchemy import Column, MetaData, Numeric, Table, Text

metadata = MetaData()
order_summary = Table(
    "order_summary",
    metadata,
    Column("order_id", Text, primary_key=True),
    Column("customer_id", Text),
    Column("total_amount", Numeric),
    Column("status", Text),
)
'''
DEPLOY = """#!/bin/sh
gcloud pubsub subscriptions create order-announce-push \\
  --topic=order-announce --push-endpoint="$URL/pubsub/orders"
"""

# ── helpers ───────────────────────────────────────────────────────────────────


def _js_functions(files: dict[str, str]) -> tuple[dict[str, tuple[str, int]], dict[str, int]]:
    """Functions as the JS detector reports them: qual → (file, line), end line."""
    out: dict[str, tuple[str, int]] = {}
    ends: dict[str, int] = {}
    for rel, text in files.items():
        mod = rel.replace("/", ".").rsplit(".", 1)[0]
        lines = text.split("\n")
        for k, line in enumerate(lines):
            m = re.search(r"(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\()", line)
            if not m or ("function" not in line and "=>" not in line):
                continue
            name = m.group(1) or m.group(2)
            depth, seen, end = 0, False, k
            for j in range(k, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                seen = seen or "{" in lines[j]
                if (seen and depth <= 0) or (not seen and "=>" in lines[j]):
                    end = j
                    break
            out[f"{mod}.{name}"] = (rel, k + 1)
            ends[f"{mod}.{name}"] = end + 1
    return out, ends


def _js_graph(root: Path, files: dict[str, str], *, repo: str | None = None,
              calls: tuple[tuple[str, str, str, str], ...] = ()) -> Graph:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    g = Graph()
    g.repo = repo
    g.attrs["repo"] = repo
    fns, ends = _js_functions(files)
    for qual, (rel, line) in fns.items():
        g.add_node(f"function:{qual}", "function", name=qual, language="javascript",
                   evidence={"file": rel, "line": line, "symbol": qual}, end_line=ends[qual])
    call_rows = []
    for caller, callee, rel, needle in calls:
        line = _line(files[rel], needle)
        g.add_edge("calls", f"function:{caller}", f"function:{callee}", evidence=Evidence(rel, line, caller))
        call_rows.append((caller, callee, rel, line))
    fsapply.apply_js_firestore(g, files, fns, ends, call_rows)
    idx = dbident.build_index(files, {})
    dbident.assign_dbs(g, files, idx)
    dataflow.apply(g, root, dict(files), firestore=idx.firestore or False)
    return g


def _direct(g: Graph, etype: str) -> set[tuple[str, str]]:
    return {
        (e.from_id.split("function:", 1)[-1], e.to_id)
        for e in g.edges.values()
        if e.type == etype and e.to_id.startswith("collection:") and not e.attrs.get("derived")
        and e.from_id.startswith("function:")
    }


# ── A ─────────────────────────────────────────────────────────────────────────


def test_web_modular_sdk_reads_and_writes(tmp_path: Path) -> None:
    g = _js_graph(tmp_path / "web", WEB, calls=(("src.pages.OrdersPage.OrdersPage", "src.api.orders.listPaidOrders",
                                                 "src/pages/OrdersPage.tsx", "listPaidOrders()"),))
    o = "src.api.orders."
    assert _direct(g, "reads") == {
        (o + "listPaidOrders", "collection:orders"),
        (o + "getOrder", "collection:orders"),
        (o + "countOrders", "collection:orders"),
        (o + "getOrderByPath", "collection:orders"),
        (o + "watchOrder", "collection:orders"),
        (o + "listCustomers", "collection:customers"),
    }
    assert _direct(g, "writes") == {
        (o + "markShipped", "collection:orders"),
        (o + "createOrder", "collection:orders"),
        (o + "removeOrder", "collection:orders"),
        (o + "touchOrder", "collection:orders"),
        (o + "saveCustomer", "collection:customers"),
        (o + "addNote", "collection:notes"),
    }
    assert g.edges[f"writes:function:{o}markShipped->collection:orders"].attrs["fields"] == ["shippedAt", "status"]
    assert g.edges[f"reads:function:{o}listCustomers->collection:customers"].attrs["via"] == f"function:{o}listAll"
    assert g.nodes["collection:notes"].attrs["path"] == "customers/{}/notes"
    assert {g.nodes[c].attrs["db"] for c in ("collection:orders", "collection:customers", "collection:notes")} == {
        "firestore:demo-shop"
    }
    page = g.edges["reads:function:src.pages.OrdersPage.OrdersPage->collection:orders"]
    assert page.attrs.get("derived") == "call"
    assert not any(e.type == "feeds" for e in g.edges.values())


def test_web_scan_files_resolution_forms() -> None:
    fns, ends = _js_functions(WEB)
    res = js_firestore.scan_files(WEB, fns, ends, calls=[])
    by_fn = {a.symbol.rsplit(".", 1)[-1]: (a.kind, a.collection) for a in res.accesses}
    assert by_fn["countOrders"] == ("read", "orders")  # constant
    assert by_fn["saveCustomer"] == ("write", "customers")  # enum member
    assert by_fn["getOrderByPath"] == ("read", "orders")  # template path to a document
    assert by_fn["touchOrder"] == ("write", "orders")  # doc(ref, id)
    assert by_fn["addNote"] == ("write", "notes")  # subcollection
    assert by_fn["listCustomers"] == ("read", "customers")  # the helper's argument
    assert "listAll" not in by_fn  # the helper alone does not know the collection


def test_files_without_firestore_are_left_alone() -> None:
    files = {"src/db.ts": "export async function f(db) { return db.collection('users').findOne({}); }\n"}
    fns, ends = _js_functions(files)
    assert js_firestore.scan_files(files, fns, ends).accesses == []


# ── B ─────────────────────────────────────────────────────────────────────────


def test_admin_chains_and_collection_to_collection_feeds(tmp_path: Path) -> None:
    g = _js_graph(tmp_path / "admin", {"src/orders.ts": ADMIN})
    o = "src.orders."
    assert _direct(g, "reads") == {
        (o + "getOrder", "collection:orders"),
        (o + "paidOrders", "collection:orders"),
        (o + "copyCustomer", "collection:customers"),
    }
    assert _direct(g, "writes") == {(o + "closeOrder", "collection:orders"), (o + "copyCustomer", "collection:customer_snapshots")}
    assert _feeds(g) == {
        ("collection:customers", "collection:customer_snapshots"): (
            "exact",
            {("name", "name", "exact", False), ("email", "email", "exact", False)},
        ),
    }


# ── C ─────────────────────────────────────────────────────────────────────────

PY_SOURCE = '''from google.cloud import firestore
fs = firestore.Client()


def load_order(order_id: str) -> dict:
    return fs.collection("orders").document(order_id).get().to_dict() or {}
'''
PY_SINK = '''import os
from sqlalchemy import create_engine, text
engine = create_engine(os.environ["WAREHOUSE_DATABASE_URL"])


def save_summary(conn, order_id: str, doc: dict) -> None:
    conn.execute(text("INSERT INTO order_summary (order_id, customer_id, total_amount) VALUES (:id, :cust, :total)"),
                 {"id": order_id, "cust": doc["customerId"], "total": doc["total"]})
'''
PY_JOB = '''from sync.source import load_order, fs
from sync.sink import save_summary, engine
from sqlalchemy import text


def sync_one(order_id: str) -> None:
    doc = load_order(order_id)
    with engine.begin() as conn:
        save_summary(conn, order_id, doc)


def sync_inline(order_id: str) -> None:
    snap = fs.collection("orders").document(order_id).get().to_dict()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO order_summary (order_id, customer_id, total_amount) VALUES (:id, :cust, :total)"),
                     {"id": order_id, "cust": snap["customerId"], "total": snap["total"]})


def sync_all() -> None:
    with engine.begin() as conn:
        for s in fs.collection("orders").where("status", "==", "paid").stream():
            o = s.to_dict()
            conn.execute(text("INSERT INTO order_summary (order_id, total_amount) VALUES (:id, :t)"), {"id": s.id, "t": o["total"]})
'''
SUMMARY = ["order_id", "customer_id", "total_amount", "status"]


def _py_direct_graph(root: Path) -> Graph:
    files = {"sync/source.py": PY_SOURCE, "sync/sink.py": PY_SINK, "sync/job.py": PY_JOB}
    lo, sv = "function:sync.source.load_order", "function:sync.sink.save_summary"
    one, inl, alls = "function:sync.job.sync_one", "function:sync.job.sync_inline", "function:sync.job.sync_all"
    return _graph(
        root,
        files,
        [(lo, "sync/source.py", "def load_order"), (sv, "sync/sink.py", "def save_summary"),
         (one, "sync/job.py", "def sync_one"), (inl, "sync/job.py", "def sync_inline"), (alls, "sync/job.py", "def sync_all")],
        [
            ("reads", lo, "collection:orders", "sync/source.py", 'fs.collection("orders")'),
            ("writes", sv, "column:order_summary.customer_id", "sync/sink.py", "conn.execute("),
            ("calls", one, lo, "sync/job.py", "doc = load_order"),
            ("calls", one, sv, "sync/job.py", "save_summary(conn"),
            ("reads", inl, "collection:orders", "sync/job.py", "snap = fs.collection"),
            ("writes", inl, "column:order_summary.customer_id", "sync/job.py", 'conn.execute(text("INSERT INTO order_summary (order_id, customer_id'),
            ("reads", alls, "collection:orders", "sync/job.py", "for s in fs.collection"),
            ("writes", alls, "column:order_summary.total_amount", "sync/job.py", '{"id": s.id'),
        ],
        {"order_summary": SUMMARY},
    )


def test_python_firestore_to_postgres(tmp_path: Path) -> None:
    g = _py_direct_graph(tmp_path / "pydirect")
    edge = g.edges["feeds:collection:orders->table:order_summary"]
    assert _feeds(g) == {
        ("collection:orders", "table:order_summary"): (
            "exact",
            {
                ("customerId", "customer_id", "exact", False),
                ("total", "total_amount", "exact", False),
                ("__id__", "order_id", "exact", False),  # the document id, a reference
            },
        ),
    }
    by_fn: dict[str, set[tuple[str, str]]] = {}
    for c in edge.attrs["column_map"]:
        by_fn.setdefault(c["fn"].rsplit(".", 1)[-1], set()).add((c["from"], c["to"]))
    assert {("orders.customerId", "order_summary.customer_id"), ("orders.total", "order_summary.total_amount")} <= by_fn["save_summary"]
    assert {("orders.customerId", "order_summary.customer_id"), ("orders.total", "order_summary.total_amount")} <= by_fn["sync_inline"]
    assert {("orders.total", "order_summary.total_amount"), ("orders.__id__", "order_summary.order_id")} <= by_fn["sync_all"]
    paths = [v["path"] for v in edge.attrs["via"]]
    assert ["function:sync.source.load_order", "function:sync.job.sync_one", "function:sync.sink.save_summary"] in paths
    assert g.nodes["collection:orders"].attrs["db"] == "firestore:default"
    assert g.nodes["table:order_summary"].attrs["db"] == "warehouse_db"


def test_firestore_fields_need_no_schema_but_sql_columns_still_do(tmp_path: Path) -> None:
    root = tmp_path / "noschema"
    g = _graph(
        root,
        {"sync/job.py": PY_JOB, "sync/source.py": PY_SOURCE, "sync/sink.py": PY_SINK},
        [("function:sync.job.sync_inline", "sync/job.py", "def sync_inline")],
        [
            ("reads", "function:sync.job.sync_inline", "collection:orders", "sync/job.py", "snap = fs.collection"),
            ("writes", "function:sync.job.sync_inline", "column:order_summary.total_amount", "sync/job.py", "conn.execute(text"),
        ],
        {"order_summary": ["order_id", "total_amount"]},  # no customer_id column in the schema
    )
    cols = _feeds(g)[("collection:orders", "table:order_summary")][1]
    assert ("total", "total_amount", "exact", False) in cols  # a collection field: no schema needed
    assert not any(c[1] == "customer_id" for c in cols)  # a destination column must exist


# ── E ─────────────────────────────────────────────────────────────────────────


def _warehouse_graph(root: Path, raw_config: dict | None) -> Graph:
    files = {"warehouse/app.py": WAREHOUSE_APP, "warehouse/store.py": WAREHOUSE_STORE, "warehouse/tables.py": WAREHOUSE_TABLES}
    a, u = "function:warehouse.app.announce_order", "function:warehouse.store.upsert_order"
    g = _graph(
        root, files,
        [(a, "warehouse/app.py", "def announce_order"), (u, "warehouse/store.py", "def upsert_order")],
        [
            ("calls", a, u, "warehouse/app.py", "upsert_order(conn"),
            ("writes", u, "column:order_summary.customer_id", "warehouse/store.py", "stmt = pg_insert"),
        ],
        {"order_summary": SUMMARY},
        repo="warehouse",
    )
    g.add_node("entrypoint:http:POST:/pubsub/orders", "entrypoint", kind="http", method="POST", path="/pubsub/orders",
               symbol="warehouse.app.announce_order")
    g.add_edge("triggers", "entrypoint:http:POST:/pubsub/orders", a)
    fsapply.apply_push_subscriptions(g, root, raw_config)
    dataflow.apply(g, root, dict(files))
    return g


def test_ts_trigger_and_publish(tmp_path: Path) -> None:
    g = _js_graph(tmp_path / "functions", {"src/index.ts": FUNCTIONS}, repo="functions")
    ep = g.nodes["entrypoint:firestore:orders:written"]
    assert (ep.attrs["kind"], ep.attrs["collection"], ep.attrs["event"]) == ("firestore", "orders", "written")
    assert "triggers:entrypoint:firestore:orders:written->function:src.index.announceOrder" in g.edges
    assert "reads:entrypoint:firestore:orders:written->collection:orders" in g.edges
    enq = g.edges["enqueues:function:src.index.announceOrder->queue:pubsub:order-announce"]
    assert enq.attrs["fields"] == ["customerId", "orderId", "status", "total"]
    out = g.nodes["function:src.index.announceOrder"].attrs["dataflow_out"][0]
    assert out["topic"] == "order-announce"
    assert out["fields"]["customerId"] == [{"table": "collection:orders", "column": "customerId"}]
    assert out["fields"]["orderId"] == [{"table": "collection:orders", "column": "__id__"}]
    assert g.nodes["collection:orders"].attrs["db"] == "firestore:default"


def test_pubsub_push_between_repos(tmp_path: Path) -> None:
    gf = _js_graph(tmp_path / "functions", {"src/index.ts": FUNCTIONS}, repo="functions")
    gw = _warehouse_graph(tmp_path / "warehouse", {"push_subscriptions": {"order-announce": "POST /pubsub/orders"}})
    assert "triggers:queue:pubsub:order-announce->entrypoint:http:POST:/pubsub/orders" in gw.edges
    merged = merge_graphs([gf, gw])
    dataflow.link_queue_flows(merged)
    assert _feeds(merged) == {
        ("functions/collection:orders", "warehouse/table:order_summary"): (
            "heuristic",
            {
                ("customerId", "customer_id", "heuristic", False),
                ("total", "total_amount", "heuristic", False),
                ("status", "status", "heuristic", False),
                ("__id__", "order_id", "heuristic", False),
            },
        ),
    }
    edge = merged.edges["feeds:functions/collection:orders->warehouse/table:order_summary"]
    assert edge.attrs["via"][0]["path"] == [
        "functions/function:src.index.announceOrder",
        "functions/queue:pubsub:order-announce",
        "warehouse/function:warehouse.app.announce_order",
        "warehouse/function:warehouse.store.upsert_order",
    ]


def test_push_subscription_from_gcloud_command(tmp_path: Path) -> None:
    root = tmp_path / "warehouse"
    root.mkdir()
    (root / "deploy.sh").write_text(DEPLOY, encoding="utf-8")
    assert fsapply.push_specs(root, {}) == [("order-announce", "POST", "/pubsub/orders", "deploy.sh", 2)]
    gw = _warehouse_graph(root, {})
    assert "triggers:queue:pubsub:order-announce->entrypoint:http:POST:/pubsub/orders" in gw.edges
    assert gw.nodes["function:warehouse.store.upsert_order"].attrs["dataflow_in"][0]["topic"] == "order-announce"


def test_push_subscription_without_route_warns(tmp_path: Path) -> None:
    g = Graph()
    fsapply.apply_push_subscriptions(g, tmp_path, {"push_subscriptions": {"t": "POST /nowhere"}})
    assert g.attrs["warnings"] == ["push subscription t -> POST /nowhere: no such route in this repo"]
    assert not any(n.type == "queue" for n in g.nodes.values())


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        ({"t": "POST /a/b/"}, [("t", "POST", "/a/b", "bizmap.yaml", 1)]),
        ({"t": {"path": "https://svc.run.app/a", "method": "put"}}, [("t", "PUT", "/a", "bizmap.yaml", 1)]),
        ([{"topic": "t", "endpoint": "/a"}], [("t", "POST", "/a", "bizmap.yaml", 1)]),
    ],
)
def test_push_subscriptions_config_forms(tmp_path: Path, spec: object, want: list) -> None:
    assert fsapply.push_specs(tmp_path, {"push_subscriptions": spec}) == want


# ── whole scan (needs the tree-sitter parsers) ────────────────────────────────


def _write_all(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _bare_direct(g: Graph, etype: str) -> set[tuple[str, str]]:
    return {
        (e.from_id.rsplit(".", 1)[-1], e.to_id.split("/")[-1])
        for e in g.edges.values()
        if e.type == etype and "collection:" in e.to_id and not e.attrs.get("derived") and "function:" in e.from_id
    }


def test_scan_web_modular(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_path

    _write_all(tmp_path, {**WEB, "package.json": '{"name": "web-modular", "dependencies": {"firebase": "^10"}}\n'})
    g = scan_path(tmp_path)
    reads = _bare_direct(g, "reads")
    writes = _bare_direct(g, "writes")
    for fn in ("listPaidOrders", "getOrder", "countOrders", "getOrderByPath", "watchOrder"):
        assert (fn, "collection:orders") in reads
    assert ("listCustomers", "collection:customers") in reads
    for fn in ("markShipped", "createOrder", "removeOrder", "touchOrder"):
        assert (fn, "collection:orders") in writes
    assert ("saveCustomer", "collection:customers") in writes
    assert ("addNote", "collection:notes") in writes
    assert g.nodes["collection:orders"].attrs["db"] == "firestore:demo-shop"


def test_scan_admin_chain(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_path

    _write_all(tmp_path, {"src/orders.ts": ADMIN, "package.json": '{"name": "admin-ts", "dependencies": {"firebase-admin": "^12"}}\n'})
    g = scan_path(tmp_path)
    assert _feeds(g) == {
        ("collection:customers", "collection:customer_snapshots"): (
            "exact",
            {("name", "name", "exact", False), ("email", "email", "exact", False)},
        ),
    }


def test_scan_python_firestore_to_postgres(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_python")
    from bizmap.scan import scan_path

    _write_all(tmp_path, {
        "sync/__init__.py": "", "sync/source.py": PY_SOURCE, "sync/sink.py": PY_SINK, "sync/job.py": PY_JOB,
        "schema.sql": "CREATE TABLE order_summary (order_id text PRIMARY KEY, customer_id text, total_amount numeric, status text);\n",
    })
    g = scan_path(tmp_path)
    cols = _feeds(g)[("collection:orders", "table:order_summary")][1]
    assert {("customerId", "customer_id", "exact", False), ("total", "total_amount", "exact", False)} <= cols
    assert not any(c[0] not in ("customerId", "total", "__id__") for c in cols)


SA_CORE = '''from sqlalchemy import Column, MetaData, Table, Text, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

metadata = MetaData()
order_summary = Table(
    "order_summary",
    metadata,
    Column("order_id", Text, primary_key=True),
    Column("customer_id", Text),
    Column("total_amount", Text),
    Column("status", Text),
)


def dict_var(conn, r):
    values = {"order_id": r.order_id, "status": r.status}
    conn.execute(pg_insert(order_summary).values(values))


def dict_inline(conn, r):
    conn.execute(insert(order_summary).values({"order_id": r.order_id, "status": r.status}))


def pg_kwargs(conn, r):
    conn.execute(pg_insert(order_summary).values(order_id=r.order_id, status=r.status))


def core_kwargs(conn, r):
    conn.execute(insert(order_summary).values(order_id=r.order_id, status=r.status))


def upsert(conn, r):
    stmt = pg_insert(order_summary).values(order_id=r.order_id)
    conn.execute(stmt.on_conflict_do_update(index_elements=["order_id"], set_={"total_amount": stmt.excluded.total_amount}))
'''


def test_scan_sqlalchemy_core_columns(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_python")
    from bizmap.scan import scan_path

    _write_all(tmp_path, {"app/__init__.py": "", "app/store.py": SA_CORE})
    g = scan_path(tmp_path)
    assert "table:unknown" not in g.nodes
    for fn in ("dict_var", "dict_inline", "pg_kwargs", "core_kwargs"):
        targets = {e.to_id for e in g.edges.values() if e.type == "writes" and e.from_id.endswith(f".{fn}")}
        assert {"column:order_summary.order_id", "column:order_summary.status"} <= targets, fn
        assert "table:unknown" not in targets
    up = {e.to_id for e in g.edges.values() if e.type == "writes" and e.from_id.endswith(".upsert")}
    assert {"column:order_summary.order_id", "column:order_summary.total_amount"} <= up


def test_scan_pubsub_push_between_repos(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_typescript")
    pytest.importorskip("tree_sitter_python")
    from bizmap.scan import scan_paths

    _write_all(tmp_path / "functions", {"src/index.ts": FUNCTIONS, "bizmap.yaml": "name: functions\n",
                                        "package.json": '{"name": "functions"}\n'})
    _write_all(tmp_path / "warehouse", {
        "warehouse/__init__.py": "", "warehouse/app.py": WAREHOUSE_APP, "warehouse/store.py": WAREHOUSE_STORE,
        "warehouse/tables.py": WAREHOUSE_TABLES, "deploy.sh": DEPLOY,
        "bizmap.yaml": "name: warehouse\npush_subscriptions:\n  order-announce: POST /pubsub/orders\n",
    })
    g = scan_paths([tmp_path / "functions", tmp_path / "warehouse"], base=tmp_path)
    feeds = {(a.split("/")[-1], b.split("/")[-1]): v for (a, b), v in _feeds(g).items()}
    cols = feeds[("collection:orders", "table:order_summary")][1]
    assert {("customerId", "customer_id"), ("total", "total_amount"), ("status", "status")} <= {(c[0], c[1]) for c in cols}
