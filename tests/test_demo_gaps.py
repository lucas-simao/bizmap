"""Gaps found on the public demo repos (items 3 and 5 of the request):

3. a pure rule (`DiscountCents`) shows where its result is written, by its
   direct callers; a changed package-level constant maps to the functions
   that use it;
5. Firestore field names keep their case in `feeds`; the repo shown for a
   shared table / collection does not depend on the repo order; `why
   TABLE.COLUMN` does not list writers of other columns of the table.
"""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import Evidence, Graph
from bizmap.impact import compute_impact, render_markdown
from bizmap.registry import merge_graphs
from bizmap.why import format_why
from test_action_comment import _diff, _ep, _fn
from test_dataflow import PEDIDOS, _feeds, _graph
from test_firestore_flow import _js_graph
from test_go_cloud import _go_graph

PRICING_GO = """package orders

const GoldDiscountPct = 12

const SilverDiscountPct = 5

func DiscountCents(subtotal int64, tier string) int64 {
	switch tier {
	case "gold":
		return subtotal * GoldDiscountPct / 100
	case "silver":
		return subtotal * SilverDiscountPct / 100
	}
	return 0
}

func Label(tier string) string { return tier }
"""


def _pricing_graph() -> Graph:
    g = Graph()
    g.attrs["repo_roots"] = {"api": "."}
    ep = _ep(g, "", "http", "POST:/orders", "main.go", 20, method="POST", path="/orders")
    disc = _fn(g, "", "orders.DiscountCents", "orders/pricing.go", 7, 15)
    _fn(g, "", "orders.Label", "orders/pricing.go", 17, 17)
    place = _fn(g, "", "orders.Service.PlaceOrder", "orders/service.go", 10, 40)
    g.add_node("table:orders", "table", name="orders")
    g.add_node("column:orders.discount_cents", "column", table="orders", name="discount_cents")
    g.add_edge("triggers", ep, place)
    g.add_edge("calls", place, disc, evidence=Evidence("orders/service.go", 20, ""))
    g.add_edge("writes", place, "column:orders.discount_cents", evidence=Evidence("orders/service.go", 30, ""))
    return g


def test_pure_rule_shows_what_its_callers_write() -> None:
    g = _pricing_graph()
    imp = compute_impact(g, _diff(("orders/pricing.go", 10)))
    (fi,) = imp.functions
    assert fi.writes == [] and fi.caller_writes == {"function:orders.Service.PlaceOrder": ["column:orders.discount_cents"]}
    assert fi.to_dict()["caller_writes"] == fi.caller_writes
    md = render_markdown(g, imp)
    assert "via `Service.PlaceOrder`: `orders` (discount_cents)" in md, md
    assert "writes nothing itself; its callers write" in md, md


def test_changed_constant_maps_to_the_functions_that_use_it(tmp_path: Path) -> None:
    (tmp_path / "orders").mkdir()
    (tmp_path / "orders" / "pricing.go").write_text(PRICING_GO, encoding="utf-8")
    g = _pricing_graph()
    diff = (
        "diff --git a/orders/pricing.go b/orders/pricing.go\n--- a/orders/pricing.go\n+++ b/orders/pricing.go\n"
        "@@ -3,1 +3,1 @@\n-const GoldDiscountPct = 10\n+const GoldDiscountPct = 12\n"
    )
    imp = compute_impact(g, diff, repo_root=tmp_path)
    assert [(f.function, f.via_consts, f.lines) for f in imp.functions] == [
        ("function:orders.DiscountCents", ["GoldDiscountPct"], []),
    ]
    assert imp.functions[0].entrypoints == ["entrypoint:http:POST:/orders"]
    md = render_markdown(g, imp)
    assert "No mapped function changed" not in md
    assert "`GoldDiscountPct` changed outside any function; used by `orders.DiscountCents`" in md \
        or "`GoldDiscountPct` changed outside any function" in md, md
    assert "uses changed `GoldDiscountPct`" in md


def test_constant_in_a_python_module_and_ts(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "rules.py").write_text("GOLD_PCT = 12\n\n\ndef discount(x):\n    return x * GOLD_PCT // 100\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "cfg.ts").write_text("export const MAX_ITEMS = 5;\n")
    (tmp_path / "web" / "cart.ts").write_text("import { MAX_ITEMS } from './cfg';\nexport function full(n: number) { return n >= MAX_ITEMS; }\n")
    g = Graph()
    g.attrs["repo_roots"] = {"x": "."}
    _fn(g, "", "app.rules.discount", "app/rules.py", 4, 5)
    _fn(g, "", "web.cart.full", "web/cart.ts", 2, 2)
    _fn(g, "", "web.cfg.module", "web/cfg.ts", 99, 99)  # a function span elsewhere in cfg.ts, so the file is known
    diff = (
        "diff --git a/app/rules.py b/app/rules.py\n--- a/app/rules.py\n+++ b/app/rules.py\n@@ -1,1 +1,1 @@\n-GOLD_PCT = 10\n+GOLD_PCT = 12\n"
        "diff --git a/web/cfg.ts b/web/cfg.ts\n--- a/web/cfg.ts\n+++ b/web/cfg.ts\n@@ -1,1 +1,1 @@\n-export const MAX_ITEMS = 4;\n+export const MAX_ITEMS = 5;\n"
    )
    imp = compute_impact(g, diff, repo_root=tmp_path)
    assert sorted((f.function, f.via_consts) for f in imp.functions) == [
        ("function:app.rules.discount", ["GOLD_PCT"]), ("function:web.cart.full", ["MAX_ITEMS"]),
    ]


def test_firestore_field_names_keep_their_case(tmp_path: Path) -> None:
    ts = """import { Pool } from 'pg';
import { getFirestore } from 'firebase-admin/firestore';

const a = new Pool({ connectionString: process.env.ORDERS_DATABASE_URL });

export async function mirrorOrders() {
  const { rows } = await a.query('SELECT id, valor_centavos, cliente_id FROM pedidos');
  for (const p of rows) {
    await getFirestore().collection('order_events').doc(p.id).set({ orderId: p.id, amountCents: p.valor_centavos });
  }
}
"""
    f, m = "src/sync.ts", "function:src/sync.mirrorOrders"
    g = _graph(tmp_path / "ts", {f: ts}, [(m, f, "export async function mirrorOrders")],
               [("reads", m, "column:pedidos.id", f, "a.query('SELECT id, valor_centavos")], {"pedidos": PEDIDOS})
    (conf, cols) = _feeds(g)[("table:pedidos", "collection:order_events")]
    assert {(a, b) for a, b, _c, _t in cols} == {("id", "orderId"), ("valor_centavos", "amountCents")}, cols


def _side(repo: str, writes: int, reads: int) -> Graph:
    g = Graph()
    g.repo = repo
    g.attrs["repo"] = repo
    g.database = "firestore:default"
    g.add_node("collection:order_events", "collection", name="order_events", db="firestore:default")
    for k in range(writes):
        g.add_node(f"function:w{k}", "function", name=f"w{k}")
        g.add_edge("writes", f"function:w{k}", "collection:order_events")
    for k in range(reads):
        g.add_node(f"function:r{k}", "function", name=f"r{k}")
        g.add_edge("reads", f"function:r{k}", "collection:order_events")
    return g


def test_shared_collection_owner_does_not_depend_on_repo_order() -> None:
    a = merge_graphs([_side("api", 2, 0), _side("web", 1, 3)])
    b = merge_graphs([_side("web", 1, 3), _side("api", 2, 0)])
    shared = [n for n in a.nodes if n.endswith("collection:order_events")]
    assert shared == ["collection:order_events"], sorted(a.nodes)
    for m in (a, b):
        node = m.nodes["collection:order_events"]
        assert node.attrs["repo"] == "api" and node.attrs["used_by"] == ["api", "web"]


def test_why_column_skips_writers_of_other_columns() -> None:
    g = Graph()
    for c in ("status", "discount_cents"):
        g.add_node(f"column:orders.{c}", "column", table="orders", name=c)
    g.add_node("table:orders", "table", name="orders")
    for fn in ("SetStatus", "PlaceOrder", "Purge"):
        g.add_node(f"function:orders.{fn}", "function", name=fn)
    g.add_node("entrypoint:http:PATCH:/orders/:id", "entrypoint", kind="http", method="PATCH", path="/orders/:id")
    g.add_edge("writes", "function:orders.SetStatus", "column:orders.status", evidence=Evidence("r.go", 3, ""))
    g.add_edge("writes", "function:orders.SetStatus", "table:orders", confidence="inferred", evidence=Evidence("r.go", 3, ""))
    g.add_edge("writes", "entrypoint:http:PATCH:/orders/:id", "table:orders", confidence="inferred", derived="call")
    g.add_edge("writes", "function:orders.PlaceOrder", "column:orders.discount_cents", evidence=Evidence("s.go", 9, ""))
    g.add_edge("writes", "function:orders.Purge", "table:orders", evidence=Evidence("p.go", 2, ""))  # DELETE: table level
    text = format_why(g, "orders.discount_cents")
    writers = text.split("writers:", 1)[1].split("readers:", 1)[0]
    assert "PlaceOrder" in writers and "SetStatus" not in writers, text
    assert "Purge" in writers  # a table-level DELETE still touches every column


# ── follow-up on #161 ─────────────────────────────────────────────────────────

ORDERS_API_GO = {"internal/orders/events.go": """package orders

import (
\t"context"
\t"cloud.google.com/go/firestore"
)

type OrderEvent struct {
\tOrderID string `firestore:"orderId"`
\tKind    string `firestore:"kind,omitempty"`
\tTotal   int64
}

type Repo struct{ db *sql.DB; client *firestore.Client }

func (r *Repo) Emit(ctx context.Context, id string) error {
\tvar total int64
\tif err := r.db.QueryRowContext(ctx, "SELECT id, total_cents FROM orders WHERE id = $1", id).Scan(&id, &total); err != nil { return err }
\t_, _, err := r.client.Collection("order_events").Add(ctx, OrderEvent{OrderID: id, Kind: "created", Total: total})
\treturn err
}
"""}

SHOP_FUNCTIONS_TS = {"src/index.ts": """import { onDocumentCreated } from 'firebase-functions/v2/firestore';
import { getFirestore } from 'firebase-admin/firestore';
const db = getFirestore();
export const notifyOrder = onDocumentCreated('order_events/{eventId}', async (event) => {
  const data = event.data?.data();
  if (!data) return;
  await db.collection('notifications').add({ orderId: data.orderId, sentAt: new Date() });
});
"""}


def test_go_struct_written_to_firestore_uses_its_firestore_tags(tmp_path: Path) -> None:
    g, _ = _go_graph(
        tmp_path / "orders-api", ORDERS_API_GO, repo="orders-api",
        edges=(("reads", "internal/orders.Repo.Emit", "column:orders.total_cents", "internal/orders/events.go", "r.db.QueryRowContext"),),
        schema={"orders": ["id", "total_cents"]},
    )
    (_conf, cols), = [v for k, v in _feeds(g).items() if k == ("table:orders", "collection:order_events")]
    # the tag names the document field; an untagged field keeps the Go name
    assert {(a, b) for a, b, _c, _t in cols} == {("id", "orderId"), ("total_cents", "Total")}, cols


def test_trigger_feed_between_collections_survives_an_id_only_copy(tmp_path: Path) -> None:
    g = _js_graph(tmp_path / "functions", SHOP_FUNCTIONS_TS, repo="functions")
    feeds = _feeds(g)
    assert ("collection:order_events", "collection:notifications") in feeds, feeds
    _conf, cols = feeds[("collection:order_events", "collection:notifications")]
    assert {(a, b) for a, b, _c, _t in cols} == {("orderId", "orderId")}, cols


def test_pure_rule_counts_callers_writes_in_summary_why_and_data() -> None:
    g = _pricing_graph()
    place = "function:orders.Service.PlaceOrder"
    g.add_node("table:audit_log", "table", name="audit_log")
    for c in ("actor", "action", "at", "entity"):
        g.add_node(f"column:audit_log.{c}", "column", table="audit_log", name=c)
        g.add_edge("writes", place, f"column:audit_log.{c}", evidence=Evidence("orders/service.go", 25, ""))
    for c in ("id", "subtotal_cents"):
        g.add_node(f"column:orders.{c}", "column", table="orders", name=c)
        g.add_edge("writes", place, f"column:orders.{c}", evidence=Evidence("orders/service.go", 30, ""))
    md = render_markdown(g, compute_impact(g, _diff(("orders/pricing.go", 10))))
    assert "writes **2** tables via callers" in md, md
    assert "writes **0** tables" not in md, md
    assert "no table" not in md, md
    assert "writes 2 tables via callers (`Service.PlaceOrder`) reached from 1 entrypoint" in md, md
    assert "<b>Data</b> · writes 2 tables via callers · reads 0" in md, md
    assert "| **`orders`** <sub>via `Service.PlaceOrder`</sub> | discount_cents, id, subtotal_cents |" in md, md
    # the note groups by table, the table the rule feeds first, nothing hidden by a column cap
    assert (
        "its callers write: via `Service.PlaceOrder`: `orders` (discount_cents, id, subtotal_cents), "
        "`audit_log` (action, actor, at, entity)"
    ) in md, md
