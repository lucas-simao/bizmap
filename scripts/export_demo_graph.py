"""Fictional multi-service graph used for the `bizmap export` GIF in the README.

    python scripts/export_demo_graph.py /tmp/demo.json
    bizmap export --graph /tmp/demo.json --out /tmp/demo.html
    node scripts/export_gif.js /tmp/demo.html /tmp/frames   # needs `npm i playwright`
    ffmpeg -framerate 10 -i /tmp/frames/f%04d.png -vf "fps=8,scale=960:-1" docs/img/export-html.gif

All names are invented; no real code is involved.
"""

import json
import random
import sys

rnd = random.Random(3)
T = {
    "orders": ["id", "user_id", "status", "total_cents", "created_at", "paid_at"],
    "order_items": ["id", "order_id", "product_id", "qty", "price_cents"],
    "users": ["id", "email", "name", "created_at"],
    "invoices": ["id", "order_id", "status", "amount_cents", "provider_ref"],
    "payments": ["id", "invoice_id", "status", "provider", "captured_at"],
    "products": ["id", "sku", "name", "price_cents", "active"],
    "inventory": ["product_id", "qty", "reserved"],
    "audit_log": ["id", "actor", "action", "entity", "created_at"],
    "shipments": ["id", "order_id", "carrier", "tracking", "status"],
}
nodes, edges = [], []


def n(i, t, **a):
    nodes.append({"id": i, "type": t, **a})


def e(t, a, b, c="exact", **x):
    edges.append({"id": f"{t}:{a}->{b}", "type": t, "from": a, "to": b, "confidence": c, **x})


for t, cols in T.items():
    n(f"table:{t}", "table", name=t)
    for c in cols:
        n(f"column:{t}.{c}", "column", table=t, name=c, data_type="TEXT")
SV = {
    "api": [
        (
            "POST",
            "/api/orders",
            "Orders.Create",
            [
                ("w", "orders", ["user_id", "status", "total_cents"]),
                ("w", "order_items", ["order_id", "product_id", "qty"]),
                ("r", "products", ["price_cents"]),
            ],
        ),
        (
            "GET",
            "/api/orders/:id",
            "Orders.Get",
            [("r", "orders", ["id", "status", "total_cents"]), ("r", "order_items", ["qty"])],
        ),
        (
            "POST",
            "/api/orders/:id/paid",
            "Orders.MarkPaid",
            [("w", "orders", ["status", "paid_at"]), ("w", "audit_log", ["action"])],
        ),
        (
            "POST",
            "/api/orders/:id/cancel",
            "Orders.Cancel",
            [("w", "orders", ["status"]), ("w", "inventory", ["reserved"])],
        ),
        ("GET", "/api/users/me", "Users.Me", [("r", "users", ["id", "email", "name"])]),
        ("POST", "/api/users", "Users.Create", [("w", "users", ["email", "name"])]),
        ("GET", "/api/cart", "Cart.Get", [("r", "products", ["sku", "name", "price_cents"])]),
    ],
    "billing": [
        (
            "POST",
            "/invoices",
            "create_invoice",
            [
                ("w", "invoices", ["order_id", "status", "amount_cents"]),
                ("r", "orders", ["total_cents"]),
            ],
        ),
        (
            "POST",
            "/webhooks/payment",
            "payment_webhook",
            [("w", "payments", ["status", "captured_at"]), ("r", "invoices", ["id"])],
        ),
        ("GET", "/invoices/:id", "get_invoice", [("r", "invoices", ["id", "status"])]),
        (
            "POST",
            "/refunds",
            "create_refund",
            [("w", "payments", ["status"]), ("w", "audit_log", ["action", "entity"])],
        ),
    ],
    "catalog": [
        (
            "GET",
            "/products",
            "list_products",
            [("r", "products", ["id", "sku", "name", "price_cents", "active"])],
        ),
        (
            "GET",
            "/products/:sku",
            "get_product",
            [("r", "products", ["sku"]), ("r", "inventory", ["qty"])],
        ),
        (
            "PUT",
            "/products/:sku",
            "update_product",
            [("w", "products", ["name", "price_cents"]), ("w", "audit_log", ["action"])],
        ),
        ("POST", "/inventory/reserve", "reserve_stock", [("w", "inventory", ["reserved"])]),
    ],
    "shipping": [
        (
            "POST",
            "/shipments",
            "create_shipment",
            [("w", "shipments", ["order_id", "carrier"]), ("r", "orders", ["status"])],
        ),
        ("GET", "/shipments/:id", "track", [("r", "shipments", ["tracking", "status"])]),
    ],
    "admin": [
        (
            "GET",
            "/admin/orders",
            "list_orders",
            [("r", "orders", ["id", "status"]), ("r", "users", ["email"])],
        ),
        ("POST", "/admin/orders/:id/refund", "refund_order", []),
        ("GET", "/admin/audit", "audit", [("r", "audit_log", ["actor", "action", "entity"])]),
    ],
}
for svc, routes in SV.items():
    for k, (m, path, fn, data) in enumerate(routes):
        ep = f"{svc}/entrypoint:http:{m}:{path}"
        n(
            ep,
            "entrypoint",
            kind="http",
            method=m,
            path=path,
            evidence={"file": f"{svc}/routes.py", "line": 10 + k},
        )
        h = f"{svc}/function:handlers.{fn}"
        n(h, "function", name=fn, evidence={"file": f"{svc}/handlers.py", "line": 20 + 15 * k})
        e("triggers", ep, h)
        if data:
            repo = f"{svc}/function:repository.{fn}_query"
            n(
                repo,
                "function",
                name=f"{fn}_query",
                evidence={"file": f"{svc}/repository.py", "line": 30 + 12 * k},
            )
            e("calls", h, repo)
            for rw, t, cs in data:
                kind = "writes" if rw == "w" else "reads"
                for c in cs:
                    e(kind, repo, f"column:{t}.{c}")
                e(kind, h, f"table:{t}", "inferred", derived="call")
                e(kind, ep, f"table:{t}", "inferred", derived="call")


def fn(svc, name, f="client.py"):
    i = f"{svc}/function:clients.{name}"
    if not any(x["id"] == i for x in nodes):
        n(i, "function", name=name, evidence={"file": f"{svc}/{f}", "line": 5})
    return i


# web front end
for name, target in [
    ("placeOrder", "api/entrypoint:http:POST:/api/orders"),
    ("pollOrder", "api/entrypoint:http:GET:/api/orders/:id"),
    ("loadMe", "api/entrypoint:http:GET:/api/users/me"),
    ("signUp", "api/entrypoint:http:POST:/api/users"),
    ("loadCatalog", "catalog/entrypoint:http:GET:/products"),
    ("loadProduct", "catalog/entrypoint:http:GET:/products/:sku"),
    ("loadCart", "api/entrypoint:http:GET:/api/cart"),
    ("trackShipment", "shipping/entrypoint:http:GET:/shipments/:id"),
]:
    i = f"web/function:src.pages.{name}"
    n(i, "function", name=name, evidence={"file": "web/src/pages.ts", "line": 3})
    e("calls_http", i, target, "inferred")
# cross-service
e("calls", "api/function:handlers.Orders.Create", fn("api", "BillingClient.CreateInvoice"))
e(
    "calls_http",
    fn("api", "BillingClient.CreateInvoice"),
    "billing/entrypoint:http:POST:/invoices",
    "inferred",
)
e("calls", "api/function:handlers.Orders.Create", fn("api", "CatalogClient.Reserve"))
e(
    "calls_http",
    fn("api", "CatalogClient.Reserve"),
    "catalog/entrypoint:http:POST:/inventory/reserve",
    "inferred",
)
e("calls", "api/function:handlers.Orders.MarkPaid", fn("api", "ShippingClient.Create"))
e(
    "calls_http",
    fn("api", "ShippingClient.Create"),
    "shipping/entrypoint:http:POST:/shipments",
    "inferred",
)
e("calls", "billing/function:handlers.payment_webhook", fn("billing", "notify_paid"))
e(
    "calls_http",
    fn("billing", "notify_paid"),
    "api/entrypoint:http:POST:/api/orders/:id/paid",
    "inferred",
)
e("calls", "admin/function:handlers.refund_order", fn("admin", "billing_refund"))
e("calls_http", fn("admin", "billing_refund"), "billing/entrypoint:http:POST:/refunds", "inferred")
e(
    "calls_http",
    fn("admin", "catalog_edit"),
    "catalog/entrypoint:http:PUT:/products/:sku",
    "heuristic",
)
e(
    "calls_http",
    fn("shipping", "order_status"),
    "api/entrypoint:http:GET:/api/orders/:id",
    "inferred",
)
# worker jobs
for k, (job, data) in enumerate(
    [
        ("expire_pending", [("w", "orders", ["status"]), ("w", "inventory", ["reserved"])]),
        ("nightly_invoices", [("r", "orders", ["paid_at"]), ("w", "invoices", ["status"])]),
    ]
):
    ep = f"worker/entrypoint:scheduled_job:{job}"
    n(
        ep,
        "entrypoint",
        kind="scheduled_job",
        symbol=job,
        evidence={"file": "worker/jobs.py", "line": 4 + 10 * k},
    )
    f = f"worker/function:jobs.{job}"
    n(
        f,
        "function",
        name=job,
        evidence={"file": "worker/jobs.py", "line": 5 + 10 * k},
        why="Runs every night at 02:00.",
    )
    e("triggers", ep, f)
    for rw, t, cs in data:
        for c in cs:
            e("writes" if rw == "w" else "reads", f, f"column:{t}.{c}")
json.dump(
    {
        "version": "0.4.2",
        "generated_at": "2026-11-20T12:00:00Z",
        "database": "shop",
        "nodes": nodes,
        "edges": list({x["id"]: x for x in edges}.values()),
        "repos": sorted(set(SV) | {"web", "worker"}),
    },
    open(sys.argv[1], "w"),
)
print(len(nodes), len(edges))
