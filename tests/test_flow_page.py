"""The central data-flow page (#/flow), kind / storage labels and the same
Firestore collection seen by several repos (export side: no scan needed)."""

from __future__ import annotations

import json
import re

from bizmap.dbident import ConnIndex, _collection_db, build_index, is_aux_file
from bizmap.export import export_graph
from bizmap.export.book import build_book, storage_of
from bizmap.export.services import assign_services


def _n(nid: str, ntype: str, **attrs) -> dict:
    return {"id": nid, "type": ntype, **attrs}


def _e(etype: str, a: str, b: str, conf: str = "exact", **attrs) -> dict:
    return {"id": f"{etype}:{a}->{b}", "type": etype, "from": a, "to": b, "confidence": conf, **attrs}


MIRROR = "orders-api/function:internal/mirror.Service.Mirror"


def repro_graph() -> dict:
    """orders-api mirrors the Firestore `orders` collection into a Postgres
    `orders` table; shop-web reads the same collection."""
    nodes = [
        _n("orders-api/collection:orders", "collection", name="orders", db="firestore:default",
           same_as=["shop-web/collection:orders"]),
        _n("orders-api/table:orders", "table", name="orders", engine="postgres"),
        _n("orders-api/table:order_items", "table", name="order_items", engine="postgres"),
        _n("shop-web/collection:orders", "collection", name="orders", db="firestore:default",
           same_as=["orders-api/collection:orders"]),
        _n(MIRROR, "function", name="Service.Mirror", evidence={"file": "internal/mirror/service.go", "line": 12}),
        _n("orders-api/function:internal/fsread.Reader.MirrorDocument", "function", name="Reader.MirrorDocument",
           evidence={"file": "internal/fsread/reader.go", "line": 3}),
        _n("orders-api/function:internal/pgstore.Tx.WriteOrder", "function", name="Tx.WriteOrder",
           evidence={"file": "internal/pgstore/store.go", "line": 40}),
        _n("shop-web/function:src.orders.listOrders", "function", name="listOrders", evidence={"file": "src/orders.ts", "line": 4}),
        _n("shop-web/function:src.orders.getOrder", "function", name="getOrder", evidence={"file": "src/orders.ts", "line": 5}),
    ]
    edges = [
        _e("reads", "orders-api/function:internal/fsread.Reader.MirrorDocument", "orders-api/collection:orders",
           evidence={"file": "internal/fsread/reader.go", "line": 4}),
        _e("writes", "orders-api/function:internal/pgstore.Tx.WriteOrder", "orders-api/table:orders",
           evidence={"file": "internal/pgstore/store.go", "line": 41}),
        _e("calls", MIRROR, "orders-api/function:internal/fsread.Reader.MirrorDocument"),
        _e("calls", MIRROR, "orders-api/function:internal/pgstore.Tx.WriteOrder"),
        _e("reads", "shop-web/function:src.orders.listOrders", "shop-web/collection:orders", evidence={"file": "src/orders.ts", "line": 4}),
        _e("reads", "shop-web/function:src.orders.getOrder", "shop-web/collection:orders", evidence={"file": "src/orders.ts", "line": 5}),
        _e("feeds", "orders-api/collection:orders", "orders-api/table:orders", "heuristic",
           via=[{"path": [MIRROR]}], column_map=[]),
    ]
    return {"version": "test", "nodes": nodes, "edges": edges}


def _book(data: dict) -> tuple[dict, dict]:
    return build_book(data, assign_services(data), lang="pt-BR")


def _blocks(html: str) -> dict:
    return {m.group(1): json.loads(m.group(2))
            for m in re.finditer(r'<script id="bm-([\w-]+)" type="application/json">(.*?)</script>', html, re.S)}


def test_flow_page_has_its_own_small_chunk_with_every_feed() -> None:
    ix, chunks = _book(repro_graph())
    assert ix["flows"] == 1 and "flow" in ix["chunks"]
    names = chunks["names"]["l"]
    (row,) = chunks["flow"]["rows"]
    src, dst, conf, cols, fns, ev, total = row
    assert (names[src], names[dst], conf, cols, total) == ("orders", "orders", "heuristic", [], 1)
    assert [names[f] for f in fns] == ["internal/mirror.Service.Mirror"]
    assert ev == "internal/mirror/service.go:12"  # the copying function when the edge has no evidence
    # the flow chunk holds only the flow rows (not spread over the table chunks)
    assert set(chunks["flow"]) == {"rows"}


def test_tables_carry_kind_storage_flow_count_and_same_collection() -> None:
    ix, chunks = _book(repro_graph())
    by = {(chunks["names"]["l"][r["i"]], r["kind"], ix["services"][r["users"][0]]["name"] if r["users"] else ""): r
          for r in ix["tables"]}
    coll = by[("orders", "collection", "orders-api")]
    table = by[("orders", "table", "orders-api")]
    web = by[("orders", "collection", "shop-web")]
    assert (coll["st"], table["st"], web["st"]) == ("Firestore", "Postgres", "Firestore")
    assert coll["fl"] == 1 and table["fl"] == 1 and "fl" not in web
    assert coll["same"] == [web["i"]] and web["same"] == [coll["i"]]


def test_graph_without_feeds_still_exports_an_empty_flow_page() -> None:
    data = repro_graph()
    data["edges"] = [e for e in data["edges"] if e["type"] != "feeds"]
    for n in data["nodes"]:
        n.pop("same_as", None)
        n.pop("engine", None)
    html = export_graph(data, "html")
    html = html if isinstance(html, str) else html[0]
    blocks = _blocks(html)
    assert blocks["index"]["flows"] == 0 and blocks["flow"] == {"rows": []}


def test_new_fields_are_escaped_in_the_page_data() -> None:
    data = repro_graph()
    bad = '<img src=x onerror="window.__alerted=1">'
    data["nodes"][0]["name"] = bad
    data["edges"][-1]["column_map"] = [{"from": f"orders.{bad}", "to": f"orders.{bad}", "confidence": "exact"}]
    data["nodes"][4]["evidence"] = {"file": f"{bad}.go", "line": 1}
    html = export_graph(data, "html")
    html = html if isinstance(html, str) else html[0]
    from importlib import resources

    # the inlined Mermaid bundle holds `<img` in its own strings; the page data must not
    html = html.replace(resources.files("bizmap.export").joinpath("vendor/mermaid.min.js").read_text(encoding="utf-8"), "")
    assert "<img" not in html  # names, column_map and evidence travel as escaped JSON
    assert "\\u003cimg" in html


def test_storage_labels() -> None:
    assert storage_of({"type": "collection"}, "") == "Firestore"
    assert storage_of({"type": "table", "engine": "postgres"}, "") == "Postgres"
    assert storage_of({"type": "table"}, "orders_pg") == "Postgres"
    assert storage_of({"type": "table", "engine": "mysql"}, "") == "MySQL"
    assert storage_of({"type": "table"}, "") == "SQL"


def test_big_graph_ships_flows_without_spreading_them() -> None:
    """~50k nodes, a few hundred feeds: one flow chunk, the page paginates (JS)."""
    nodes, edges = [], []
    for k in range(400):
        nodes.append(_n(f"svc{k % 16}/table:t{k}", "table", name=f"t{k}", engine="postgres"))
        nodes.append(_n(f"svc{k % 16}/collection:c{k}", "collection", name=f"c{k}", db="firestore:default"))
    for k in range(49_000):
        nodes.append(_n(f"svc{k % 16}/function:pkg{k % 300}.F{k}", "function", name=f"F{k}",
                        evidence={"file": f"pkg{k % 300}/f{k}.go", "line": 1}))
    for k in range(300):
        edges.append(_e("feeds", f"svc{k % 16}/collection:c{k}", f"svc{k % 16}/table:t{k}", "heuristic",
                        via=[{"path": [f"svc{k % 16}/function:pkg{k % 300}.F{k}"]}]))
    ix, chunks = _book({"nodes": nodes, "edges": edges})
    assert ix["flows"] == 300 and len(chunks["flow"]["rows"]) == 300
    assert len(json.dumps(chunks["flow"])) < 60_000


# ── Firestore base label: a helper command's project is not the repo's ──

SNAPSHOT = """package main

import "cloud.google.com/go/firestore"

func main() {
	client, err := firestore.NewClient(ctx, "demo-snap")
	docs, _ := client.Collection("orders").Documents(ctx).GetAll()
}
"""
MAIN = """package main

import "cloud.google.com/go/firestore"

func main() {
	client, _ := firestore.NewClient(ctx, firestore.DetectProjectID)
	_ = client
}
"""
READER = """package fsread

func (r *Reader) MirrorDocument(ctx context.Context, id string) (map[string]any, bool, error) {
	snap, err := r.client.Collection("orders").Doc(id).Get(ctx)
	return nil, false, err
}
"""


def test_aux_command_project_is_not_the_repo_base() -> None:
    texts = {"cmd/snapshot/main.go": SNAPSHOT, "cmd/api/main.go": MAIN, "internal/fsread/reader.go": READER}
    idx = build_index(texts, None)
    assert idx.firestore == "firestore:default"
    assert idx.fs_file_project == {"cmd/snapshot/main.go": "demo-snap"}
    # the service's reader (default client) and the tool both read `orders`: the service wins
    assert _collection_db({"internal/fsread/reader.go", "cmd/snapshot/main.go"}, idx) == "firestore:default"
    # a collection only the tool reads stays on the tool's project
    assert _collection_db({"cmd/snapshot/main.go"}, idx) == "firestore:demo-snap"


def test_a_service_literal_project_still_names_the_repo() -> None:
    texts = {"internal/store/client.go": SNAPSHOT.replace("package main", "package store").replace("demo-snap", "shop-prod")}
    idx = build_index(texts, None)
    assert idx.firestore == "firestore:shop-prod"
    assert _collection_db(set(), ConnIndex(firestore="firestore:shop-prod")) == "firestore:shop-prod"


def test_aux_files() -> None:
    assert is_aux_file("cmd/snapshot/main.go") and is_aux_file("scripts/seed.ts") and is_aux_file("internal/x_test.go")
    assert not is_aux_file("cmd/api/main.go") and not is_aux_file("internal/fsread/reader.go") and not is_aux_file("src/orders.ts")


# ── whole scan of the request's repro (needs the tree-sitter parsers) ──

ORDERS_API = {
    "bizmap.yaml": "name: orders-api\n",
    "go.mod": "module example.com/orders-api\n\ngo 1.22\n",
    "migrations/001_orders.sql": (
        "CREATE TABLE orders (id text PRIMARY KEY, customer_id text, total numeric);\n"
        "CREATE TABLE order_items (order_id text REFERENCES orders(id), sku text);\n"
    ),
    "internal/fsread/reader.go": """package fsread

import (
	"context"

	"cloud.google.com/go/firestore"
)

type Reader struct{ client *firestore.Client }

func New(client *firestore.Client) *Reader { return &Reader{client: client} }

func (r *Reader) MirrorDocument(ctx context.Context, id string) (map[string]any, bool, error) {
	snap, err := r.client.Collection("orders").Doc(id).Get(ctx)
	if err != nil {
		return nil, false, err
	}
	data, _ := normalize(snap.Data()).(map[string]any)
	return data, true, nil
}

func normalize(v any) any { return v }
""",
    # the mirror in one function (the shape the Go data-flow pass follows today);
    # the request's two-level version (reader + pgx batch) is the same feeds edge
    "internal/mirror/service.go": """package mirror

import (
	"context"

	"cloud.google.com/go/firestore"
	"github.com/jackc/pgx/v5/pgxpool"
)

type OrderDoc struct {
	CustomerID string  `firestore:"customerId"`
	Total      float64 `firestore:"total"`
}

type Service struct {
	fs *firestore.Client
	db *pgxpool.Pool
}

func (s *Service) Mirror(ctx context.Context, id string) error {
	snap, err := s.fs.Collection("orders").Doc(id).Get(ctx)
	if err != nil {
		return err
	}
	var d OrderDoc
	if err := snap.DataTo(&d); err != nil {
		return err
	}
	_, err = s.db.Exec(ctx, `INSERT INTO orders (id, customer_id, total) VALUES ($1, $2, $3)
		ON CONFLICT (id) DO UPDATE SET total = EXCLUDED.total`, id, d.CustomerID, d.Total)
	return err
}
""",
    "cmd/api/main.go": """package main

import (
	"context"

	"cloud.google.com/go/firestore"
	"example.com/orders-api/internal/fsread"
)

func main() {
	ctx := context.Background()
	client, _ := firestore.NewClient(ctx, firestore.DetectProjectID)
	_ = fsread.New(client)
}
""",
    "cmd/snapshot/main.go": """package main

import (
	"context"

	"cloud.google.com/go/firestore"
)

func main() {
	ctx := context.Background()
	client, err := firestore.NewClient(ctx, "demo-snap")
	if err != nil {
		return
	}
	docs, _ := client.Collection("orders").Documents(ctx).GetAll()
	_ = docs
}
""",
}
SHOP_WEB = {
    "bizmap.yaml": "name: shop-web\n",
    "package.json": '{"name": "shop-web", "dependencies": {"firebase": "^10.0.0"}}\n',
    "src/orders.ts": """import { getFirestore, collection, getDocs, doc, getDoc } from "firebase/firestore";
const db = getFirestore();
export async function listOrders() { return (await getDocs(collection(db, "orders"))).docs.map((d) => d.data()); }
export async function getOrder(id: string) { return (await getDoc(doc(db, "orders", id))).data(); }
""",
}


def test_scan_repro_labels_firestore_base_and_links_the_collection(tmp_path) -> None:
    import pytest

    pytest.importorskip("tree_sitter_go")
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_paths

    for repo, files in (("orders-api", ORDERS_API), ("shop-web", SHOP_WEB)):
        for rel, text in files.items():
            path = tmp_path / repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    graph = scan_paths([tmp_path / "orders-api", tmp_path / "shop-web"], base=tmp_path)
    api_coll = graph.nodes["orders-api/collection:orders"]
    web_coll = graph.nodes["shop-web/collection:orders"]
    # the helper command's literal project does not become the service's base
    assert api_coll.attrs.get("db") == "firestore:default", api_coll.attrs
    assert web_coll.attrs.get("db") == "firestore:default", web_coll.attrs
    assert api_coll.attrs.get("same_as") == ["shop-web/collection:orders"]
    assert graph.nodes["orders-api/table:orders"].attrs.get("engine") == "postgres"
    data = graph.to_dict()
    ix, chunks = _book(data)
    names = chunks["names"]["l"]
    rows = {(names[r[0]], names[r[1]]) for r in chunks["flow"]["rows"]}
    feeds = {(e["from"], e["to"]) for e in data["edges"] if e["type"] == "feeds"}
    assert ix["flows"] == len(feeds)
    assert ("orders-api/collection:orders", "orders-api/table:orders") in feeds, feeds
    assert ("orders", "orders") in rows
