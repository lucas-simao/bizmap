"""Follow-ups of `bizmap who-writes` / `trace` (after 0.13.0), on the request's repro:

1. Firestore accesses whose collection name is only known at runtime, in Go
   (`Collection(coll)`, `CollectionGroup(name)`, `Doc(path)`) and TS
   (`doc(db, name, id)`, `collection(db, x)`, `db.collection(x)`), land on
   `collection:unknown` and are counted, never named.
3. The SQL engine from dependency manifests (`requirements.txt`, `pyproject.toml`).
5. A Go publisher whose topic sits in a struct field.

The text passes run without tree-sitter; `test_scan_*` need the parsers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bizmap import dataflow, dbident, fsapply
from bizmap.detect import go_cloud
from bizmap.detect.js_firestore import scan_files
from bizmap.export.book import _borrow_engines
from bizmap.graph import Graph
from bizmap.query import Index, render, trace, who
from test_go_cloud import _go_functions, _go_graph

# ── 1. Go ──────────────────────────────────────────────────────────────────

GO_SERVICE = {
    "internal/orders/service.go": """package orders

import (
	"context"

	"cloud.google.com/go/firestore"
)

type Service struct{ fs *firestore.Client }

func (s *Service) Save(ctx context.Context, id string, o map[string]any) error {
	_, err := s.fs.Collection("orders").Doc(id).Set(ctx, o)
	return err
}

func (s *Service) Archive(ctx context.Context, coll, id string) error {
	_, err := s.fs.Collection(coll).Doc(id).Set(ctx, map[string]any{"archived": true})
	return err
}

func (s *Service) ByPath(ctx context.Context, id string) error {
	_, err := s.fs.Doc("orders/" + id).Get(ctx)
	return err
}

func (s *Service) ByAnyPath(ctx context.Context, path string) error {
	_, err := s.fs.Doc(path).Delete(ctx)
	return err
}

func (s *Service) Everywhere(ctx context.Context, name string) error {
	_, err := s.fs.CollectionGroup(name).Documents(ctx).GetAll()
	return err
}

func (s *Service) Handle(ctx context.Context, id string) error {
	return s.Archive(ctx, "orders_archive", id)
}
""",
}


def test_go_dynamic_collection_names_are_unknown_not_dropped() -> None:
    res = go_cloud.scan_go(GO_SERVICE, _go_functions(GO_SERVICE))
    got = sorted((a.symbol.rsplit(".", 1)[-1], a.kind, a.collection) for a in res.accesses)
    assert got == [
        ("Archive", "write", "unknown"),
        ("ByAnyPath", "write", "unknown"),
        ("ByPath", "read", "orders"),
        ("Everywhere", "read", "unknown"),
        ("Save", "write", "orders"),
    ]


def test_go_unknown_collection_stays_on_the_function(tmp_path: Path) -> None:
    g, _meta = _go_graph(tmp_path, GO_SERVICE, schema={})
    g.add_edge("calls", "function:internal/orders.Service.Handle", "function:internal/orders.Service.Archive")
    unknown = [e for e in g.edges.values() if e.to_id == "collection:unknown"]
    assert {(e.type, e.from_id.rsplit(".", 1)[-1], e.confidence.value) for e in unknown} == {
        ("writes", "Archive", "unknown"), ("writes", "ByAnyPath", "unknown"), ("reads", "Everywhere", "unknown"),
    }
    assert all(e.evidence is not None and e.evidence.file == "internal/orders/service.go" for e in unknown)
    assert not any(e.attrs.get("derived") for e in unknown)  # not copied to callers / entrypoints
    node = g.nodes["collection:unknown"]
    assert node.attrs["name"] == "unknown" and "path" not in node.attrs
    assert not [n for n in g.nodes if n.startswith("collection:") and n not in ("collection:orders", "collection:unknown")]
    assert not any(e.type == "feeds" and "unknown" in (e.from_id + e.to_id) for e in g.edges.values())


# ── 1. TS ──────────────────────────────────────────────────────────────────

TS = """import { getFirestore, collection, getDocs, doc, setDoc } from "firebase/firestore";
const db = getFirestore();
export async function listOrders() { return (await getDocs(collection(db, "orders"))).docs.map((d) => d.data()); }
export async function saveTo(name: string, id: string, data: object) {
  await setDoc(doc(db, name, id), data);
}
export async function anyCollection(x: string) { const c = pick(x); return getDocs(collection(db, c)); }
"""
ADMIN = """import { getFirestore } from "firebase-admin/firestore";
const db = getFirestore();
export async function wipe(coll: string) { await db.collection(coll).doc("x").delete(); }
export async function wipeOrders() { await wipe("orders"); }
export async function wipeSome(which: string) { await wipe(which); }
"""


def _ts_functions(rel: str, text: str) -> tuple[dict[str, tuple[str, int]], dict[str, int]]:
    funcs, ends = {}, {}
    lines = text.split("\n")
    mod = rel.rsplit(".", 1)[0].replace("/", ".")
    for i, line in enumerate(lines, 1):
        if "function " in line:
            name = line.split("function ", 1)[1].split("(", 1)[0]
            end = i
            depth = 0
            for j in range(i, len(lines) + 1):
                depth += lines[j - 1].count("{") - lines[j - 1].count("}")
                if depth <= 0:
                    end = j
                    break
            funcs[f"{mod}.{name}"] = (rel, i)
            ends[f"{mod}.{name}"] = end
    return funcs, ends


def test_ts_dynamic_collection_names_are_unknown() -> None:
    funcs, ends = _ts_functions("src/orders.ts", TS)
    res = scan_files({"src/orders.ts": TS}, funcs, ends, [])
    got = sorted((a.symbol.rsplit(".", 1)[-1], a.kind, a.collection) for a in res.accesses)
    # saveTo: nobody in the scan names the collection → unknown on the helper
    assert got == [("anyCollection", "read", "unknown"), ("listOrders", "read", "orders"), ("saveTo", "write", "unknown")]


def test_ts_admin_helper_named_by_one_caller_unknown_by_the_other() -> None:
    funcs, ends = _ts_functions("src/admin.ts", ADMIN)
    calls = [("src.admin.wipeOrders", "src.admin.wipe", "src/admin.ts", 4), ("src.admin.wipeSome", "src.admin.wipe", "src/admin.ts", 5)]
    res = scan_files({"src/admin.ts": ADMIN}, funcs, ends, calls)
    got = sorted((a.symbol.rsplit(".", 1)[-1], a.kind, a.collection) for a in res.accesses)
    assert got == [("wipeOrders", "write", "orders"), ("wipeSome", "write", "unknown")]


def test_ts_unknown_collection_is_not_copied_to_callers() -> None:
    funcs, ends = _ts_functions("src/orders.ts", TS)
    g = Graph()
    for q, (rel, line) in funcs.items():
        g.add_node(f"function:{q}", "function", name=q, language="javascript", evidence={"file": rel, "line": line})
    g.add_node("function:src.page.Save", "function", name="src.page.Save", language="javascript")
    g.add_edge("calls", "function:src.page.Save", "function:src.orders.saveTo")
    fsapply.apply_js_firestore(g, {"src/orders.ts": TS}, funcs, ends, [])
    unknown = [e for e in g.edges.values() if e.to_id == "collection:unknown"]
    assert {(e.from_id, e.type) for e in unknown} == {
        ("function:src.orders.saveTo", "writes"), ("function:src.orders.anyCollection", "reads"),
    }
    assert all(e.evidence is not None and e.evidence.file == "src/orders.ts" for e in unknown)


def test_unknown_names_never_feed_and_never_link_across_repos() -> None:
    g = Graph()
    g.add_node("a/collection:unknown", "collection", name="unknown", db="firestore:default")
    g.add_node("b/collection:unknown", "collection", name="unknown", db="firestore:default")
    g.add_node("a/table:orders", "table", name="orders")
    dbident.link_same_tables(g)
    assert "same_as" not in g.nodes["a/collection:unknown"].attrs
    dataflow.add_feed(g, "a/collection:unknown", "a/table:orders", path=[], confidence="heuristic",
                      evidence={"file": "x.go", "line": 1}, entries=[])
    assert not [e for e in g.edges.values() if e.type == "feeds"]


# ── 3. engine from manifests, one label ─────────────────────────────────────


def test_engine_from_requirements_and_pyproject(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi==0.110\nSQLAlchemy>=2\npsycopg[binary]>=3.1\n")
    assert dbident.manifest_engines(tmp_path) == {"postgres"}
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi", "asyncpg>=0.29"]\n')
    assert dbident.manifest_engines(other) == {"postgres"}
    (other / "pyproject.toml").write_text('[project]\ndependencies = ["requests", "psycopgx"]\n')
    assert dbident.manifest_engines(other) == set()
    node_repo = tmp_path / "web"
    node_repo.mkdir()
    (node_repo / "package.json").write_text('{"dependencies": {"pg": "^8.11.0", "express": "^4"}}')
    assert dbident.manifest_engines(node_repo) == {"postgres"}


def test_html_rows_borrow_the_known_engine() -> None:
    rows = [
        {"i": 0, "kind": "table", "st": "Postgres", "_db": ""},
        {"i": 1, "kind": "table", "st": "SQL", "_db": ""},
        {"i": 2, "kind": "table", "st": "SQL", "_db": ""},  # another name: unchanged
    ]
    _borrow_engines(rows, ["orders", "orders", "invoices"])
    assert [r["st"] for r in rows] == ["Postgres", "Postgres", "SQL"]
    assert all("_db" not in r for r in rows)


# ── 5. Go topic in a struct field ───────────────────────────────────────────

GO_FIELD = {
    "internal/orders/announce.go": """package orders

import (
	"context"
	"encoding/json"

	"cloud.google.com/go/pubsub"
)

const refundTopic = "refund-requested"

type Service struct {
	topic  *pubsub.Topic
	refund *pubsub.Topic
}

func NewService(ps *pubsub.Client) *Service {
	s := &Service{topic: ps.Topic("order-created")}
	s.refund = ps.Topic(refundTopic)
	return s
}

func (s *Service) announce(ctx context.Context, o map[string]any) error {
	body, _ := json.Marshal(o)
	_, err := s.topic.Publish(ctx, &pubsub.Message{Data: body}).Get(ctx)
	return err
}

func (s *Service) askRefund(ctx context.Context) error {
	_, err := s.refund.Publish(ctx, &pubsub.Message{Data: nil}).Get(ctx)
	return err
}
""",
}


def test_go_topic_kept_in_a_struct_field_is_followed(tmp_path: Path) -> None:
    res = go_cloud.scan_go(GO_FIELD, _go_functions(GO_FIELD))
    assert sorted((p.symbol.rsplit(".", 1)[-1], [t.name for t in p.topics]) for p in res.publishes) == [
        ("announce", ["order-created"]), ("askRefund", ["refund-requested"]),
    ]
    g, _meta = _go_graph(tmp_path, GO_FIELD, schema={})
    enq = {(e.from_id.rsplit(".", 1)[-1], e.to_id) for e in g.edges.values() if e.type == "enqueues"}
    assert enq == {("announce", "queue:pubsub:order-created"), ("askRefund", "queue:pubsub:refund-requested")}


# ── whole scans (need the parsers) ──────────────────────────────────────────


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def test_scan_counts_dynamic_collection_writes_in_go_and_ts(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_paths

    api = tmp_path / "orders-api"
    _write(api, {"bizmap.yaml": "name: orders-api\n", "go.mod": "module example.com/orders-api\n\ngo 1.22\n", **GO_SERVICE})
    web = tmp_path / "shop-web"
    _write(web, {"bizmap.yaml": "name: shop-web\n", "package.json": '{"name": "shop-web"}\n', "src/orders.ts": TS})
    graph = scan_paths([api, web], base=tmp_path)
    data = graph.to_dict()
    out = tmp_path / "bizmap.graph.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    ix = Index.load(out)
    r = who(ix, "collection:orders", "writes")
    counts = {(u["repo"], u["count"]) for u in r["unresolved"] if u["kind"] == "dynamic_collection"}
    assert counts == {("orders-api", 2), ("shop-web", 1)}, r["unresolved"]
    fns = {f["label"].rsplit(".", 1)[-1] for u in r["unresolved"] for f in u["functions"]}
    assert {"Archive", "ByAnyPath", "saveTo"} <= fns
    reads = who(ix, "collection:orders", "reads")
    assert {(u["repo"], u["count"]) for u in reads["unresolved"]} == {("orders-api", 1), ("shop-web", 1)}
    t = trace(ix, "collection:orders")
    assert {(d["repo"], d["count"]) for d in t["unresolved"]["dynamic_writes"]} == {("orders-api", 2), ("shop-web", 1)}
    assert "could not be resolved" in render(t)
    # no collection node named after an expression
    assert not [n["id"] for n in data["nodes"] if n["type"] == "collection"
                and n["id"].rsplit(":", 1)[-1] not in ("orders", "unknown")]


def test_scan_python_engine_from_requirements(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_python")
    from bizmap.scan import scan_path

    _write(tmp_path, {
        "requirements.txt": "fastapi\nSQLAlchemy>=2\npsycopg[binary]>=3.1\n",
        "billing/app.py": (
            "import os\nfrom sqlalchemy import create_engine, text\n\n"
            "engine = create_engine(os.environ['BILLING_DATABASE_URL'])\n\n\n"
            "def bill(order_id: str) -> None:\n"
            "    with engine.begin() as conn:\n"
            "        conn.execute(text(\"UPDATE orders SET status = 'billed' WHERE id = :id\"), {'id': order_id})\n"
        ),
    })
    g = scan_path(tmp_path)
    tables = [n for n in g.nodes.values() if n.type == "table" and n.attrs.get("name") == "orders"]
    assert tables and all(n.attrs.get("engine") == "postgres" for n in tables), [n.attrs for n in tables]
