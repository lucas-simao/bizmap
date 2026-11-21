"""Fictional multi-service graph for `bizmap export` (README GIF, docs, tests).

    python scripts/export_demo_graph.py examples/export/shop.graph.json
    bizmap export --graph examples/export/shop.graph.json --out /tmp/shop.html

An invented shop: an API (orders, users, projects, cart), billing, catalog,
shipping, a worker and a web front end whose pages call the API, some of them
on URLs that match no route. All names are made up; no real code is involved.
"""

from __future__ import annotations

import json
import sys

TABLES = {
    "orders": ["id", "user_id", "status", "total_cents", "created_at", "paid_at"],
    "order_items": ["id", "order_id", "product_id", "qty", "price_cents"],
    "users": ["id", "email", "name", "created_at"],
    "projects": ["id", "owner_id", "name", "archived_at"],
    "tasks": ["id", "project_id", "title", "done"],
    "invoices": ["id", "order_id", "status", "amount_cents"],
    "payments": ["id", "invoice_id", "status", "captured_at"],
    "products": ["id", "sku", "name", "price_cents", "active"],
    "inventory": ["product_id", "qty", "reserved"],
    "audit_log": ["id", "actor", "action", "entity", "created_at"],
    "shipments": ["id", "order_id", "carrier", "tracking", "status"],
}

# service -> [(method, path, handler, [(r|w, table, [columns])], why)]
ROUTES = {
    "api": [
        ("GET", "/api/v1/orders", "Orders.List", [("r", "orders", ["id", "status", "total_cents"])], ""),
        ("POST", "/api/v1/orders", "Orders.Create", [("w", "orders", ["user_id", "status", "total_cents"]),
                                                      ("w", "order_items", ["order_id", "product_id", "qty"]),
                                                      ("r", "products", ["price_cents"])], "Creates an order and reserves stock."),
        ("GET", "/api/v1/orders/:id", "Orders.Get", [("r", "orders", ["id", "status"]), ("r", "order_items", ["qty"])], ""),
        ("POST", "/api/v1/orders/:id/paid", "Orders.MarkPaid", [("w", "orders", ["status", "paid_at"]), ("w", "audit_log", ["action"])], ""),
        ("POST", "/api/v1/orders/:id/cancel", "Orders.Cancel", [("w", "orders", ["status"]), ("w", "inventory", ["reserved"])], ""),
        ("GET", "/api/v1/users/me", "Users.Me", [("r", "users", ["id", "email", "name"])], ""),
        ("POST", "/api/v1/users", "Users.Create", [("w", "users", ["email", "name"])], "Signs a user up."),
        ("PATCH", "/api/v1/users/:id", "Users.Update", [("w", "users", ["name"])], ""),
        ("DELETE", "/api/v1/users/:id", "Users.Delete", [("w", "users", ["id"]), ("w", "audit_log", ["action"])], ""),
        ("GET", "/api/v1/projects", "Projects.List", [("r", "projects", ["id", "name"])], ""),
        ("POST", "/api/v1/projects", "Projects.Create", [("w", "projects", ["owner_id", "name"])], ""),
        ("GET", "/api/v1/projects/:id", "Projects.Get", [("r", "projects", ["id", "name"]), ("r", "tasks", ["title", "done"])], ""),
        ("POST", "/api/v1/projects/:id/archive", "Projects.Archive", [("w", "projects", ["archived_at"]), ("w", "audit_log", ["action", "entity"])],
         "Archives a project; its tasks stay readable."),
        ("GET", "/api/v1/projects/:id/tasks", "Tasks.List", [("r", "tasks", ["id", "title", "done"])], ""),
        ("POST", "/api/v1/projects/:id/tasks", "Tasks.Create", [("w", "tasks", ["project_id", "title"])], ""),
        ("PATCH", "/api/v1/projects/:id/tasks/:taskId", "Tasks.Toggle", [("w", "tasks", ["done"])], ""),
        ("GET", "/api/v1/cart", "Cart.Get", [("r", "products", ["sku", "name", "price_cents"])], ""),
        ("POST", "/api/v1/cart/items", "Cart.Add", [("r", "products", ["price_cents"]), ("r", "inventory", ["qty"])], ""),
        ("DELETE", "/api/v1/cart/items/:sku", "Cart.Remove", [], ""),
        ("GET", "/api/v1/health", "Health", [], ""),
    ],
    "billing": [
        ("POST", "/invoices", "create_invoice", [("w", "invoices", ["order_id", "status", "amount_cents"]), ("r", "orders", ["total_cents"])], ""),
        ("GET", "/invoices/:id", "get_invoice", [("r", "invoices", ["id", "status"])], ""),
        ("POST", "/webhooks/payment", "payment_webhook", [("w", "payments", ["status", "captured_at"]), ("r", "invoices", ["id"])], ""),
        ("POST", "/refunds", "create_refund", [("w", "payments", ["status"]), ("w", "audit_log", ["action", "entity"])], ""),
    ],
    "catalog": [
        ("GET", "/products", "list_products", [("r", "products", ["id", "sku", "name", "price_cents", "active"])], ""),
        ("GET", "/products/:sku", "get_product", [("r", "products", ["sku"]), ("r", "inventory", ["qty"])], ""),
        ("PUT", "/products/:sku", "update_product", [("w", "products", ["name", "price_cents"]), ("w", "audit_log", ["action"])], ""),
        ("POST", "/inventory/reserve", "reserve_stock", [("w", "inventory", ["reserved"])], ""),
    ],
    "shipping": [
        ("POST", "/shipments", "create_shipment", [("w", "shipments", ["order_id", "carrier"]), ("r", "orders", ["status"])], ""),
        ("GET", "/shipments/:id", "track", [("r", "shipments", ["tracking", "status"])], ""),
    ],
}

# web page -> [(function, (service, method, path) | None for an unmatched call, url if unmatched, reason)]
PAGES = {
    "src/pages/orders/OrdersPage.tsx": [("loadOrders", ("api", "GET", "/api/v1/orders"))],
    "src/pages/orders/OrderDetail.tsx": [("loadOrder", ("api", "GET", "/api/v1/orders/:id")),
                                         ("cancelOrder", ("api", "POST", "/api/v1/orders/:id/cancel")),
                                         ("loadInvoice", ("billing", "GET", "/invoices/:id"))],
    "src/pages/checkout/Checkout.tsx": [("placeOrder", ("api", "POST", "/api/v1/orders")),
                                        ("loadCart", ("api", "GET", "/api/v1/cart")),
                                        ("applyCoupon", None, "/api/v1/coupons/apply", "target_not_scanned")],
    "src/pages/cart/CartDrawer.tsx": [("addItem", ("api", "POST", "/api/v1/cart/items")),
                                      ("removeItem", ("api", "DELETE", "/api/v1/cart/items/:sku"))],
    "src/pages/projects/ProjectsPage.tsx": [("loadProjects", ("api", "GET", "/api/v1/projects")),
                                            ("createProject", ("api", "POST", "/api/v1/projects"))],
    "src/pages/projects/ProjectBoard.tsx": [("loadProject", ("api", "GET", "/api/v1/projects/:id")),
                                            ("loadTasks", ("api", "GET", "/api/v1/projects/:id/tasks")),
                                            ("addTask", ("api", "POST", "/api/v1/projects/:id/tasks")),
                                            ("toggleTask", ("api", "PATCH", "/api/v1/projects/:id/tasks/:taskId")),
                                            ("archive", ("api", "POST", "/api/v1/projects/:id/archive")),
                                            ("exportCsv", None, "`${base}/projects/${id}/export`", "path_dynamic")],
    "src/pages/account/Profile.tsx": [("loadMe", ("api", "GET", "/api/v1/users/me")),
                                      ("saveProfile", ("api", "PATCH", "/api/v1/users/:id")),
                                      ("loadAvatar", None, "/avatars/me", "base_unknown")],
    "src/pages/account/SignUp.tsx": [("signUp", ("api", "POST", "/api/v1/users"))],
    "src/pages/catalog/ProductPage.tsx": [("loadProduct", ("catalog", "GET", "/products/:sku"))],
    "src/pages/catalog/Catalog.tsx": [("loadCatalog", ("catalog", "GET", "/products"))],
    "src/pages/shipping/Tracking.tsx": [("track", ("shipping", "GET", "/shipments/:id"))],
}


def build() -> dict:
    nodes: list[dict] = []
    edges: dict[str, dict] = {}
    ids: set[str] = set()

    def n(i: str, t: str, **a) -> None:
        if i not in ids:
            ids.add(i)
            nodes.append({"id": i, "type": t, **a})

    def e(t: str, a: str, b: str, c: str = "exact", **x) -> None:
        edges[f"{t}:{a}->{b}"] = {"id": f"{t}:{a}->{b}", "type": t, "from": a, "to": b, "confidence": c, **x}

    for t, cols in TABLES.items():
        n(f"table:{t}", "table", name=t)
        for c in cols:
            n(f"column:{t}.{c}", "column", table=t, name=c, data_type="TEXT")

    def fn(svc: str, qual: str, file: str, line: int, why: str = "") -> str:
        i = f"{svc}/function:{qual}"
        n(i, "function", name=qual.rsplit(".", 1)[-1], evidence={"file": file, "line": line}, **({"why": why} if why else {}))
        return i

    for svc, routes in ROUTES.items():
        ext = "go" if svc == "api" else "py"
        for k, (m, path, handler, data, why) in enumerate(routes):
            ep = f"{svc}/entrypoint:http:{m}:{path}"
            n(ep, "entrypoint", kind="http", method=m, path=path, evidence={"file": f"router.{ext}", "line": 10 + k})
            area = handler.split(".")[0].lower()
            h = fn(svc, f"handlers.{handler}", f"internal/handlers/{area}.{ext}", 20 + 10 * k, why)
            e("triggers", ep, h)
            v = fn(svc, f"validate.{handler}", f"internal/validate/{area}.{ext}", 5 + 4 * k)
            e("calls", h, v)
            if data:
                svc_fn = fn(svc, f"service.{handler}", f"internal/service/{area}.{ext}", 12 + 9 * k)
                repo = fn(svc, f"repository.{handler}", f"internal/repository/{area}.{ext}", 30 + 12 * k)
                e("calls", h, svc_fn)
                e("calls", svc_fn, repo)
                for rw, t, cs in data:
                    kind = "writes" if rw == "w" else "reads"
                    for c in cs:
                        e(kind, repo, f"column:{t}.{c}", ops=(["UPDATE"] if rw == "w" else []))
                    for src in (svc_fn, h, ep):
                        e(kind, src, f"table:{t}", "inferred", derived="call")
            if svc == "api":
                log = fn(svc, "audit.Log", "internal/audit/log.go", 3)
                e("calls", h, log)

    def ep_id(svc: str, m: str, p: str) -> str:
        return f"{svc}/entrypoint:http:{m}:{p}"

    # service-to-service calls
    def client(svc: str, caller: str, name: str, target: tuple[str, str, str]) -> None:
        c = fn(svc, f"clients.{name}", f"internal/clients/{name.split('.')[0].lower()}.go", 8)
        e("calls", f"{svc}/function:{caller}", c)
        e("calls_http", c, ep_id(*target), "inferred")

    client("api", "service.Orders.Create", "Billing.CreateInvoice", ("billing", "POST", "/invoices"))
    client("api", "service.Orders.Create", "Catalog.Reserve", ("catalog", "POST", "/inventory/reserve"))
    client("api", "service.Orders.MarkPaid", "Shipping.Create", ("shipping", "POST", "/shipments"))
    notify = fn("billing", "clients.notify_paid", "app/clients.py", 4)
    e("calls", "billing/function:handlers.payment_webhook", notify)
    e("calls_http", notify, ep_id("api", "POST", "/api/v1/orders/:id/paid"), "inferred")

    # worker jobs
    for k, (job, data, why) in enumerate([
        ("expire_pending", [("w", "orders", ["status"]), ("w", "inventory", ["reserved"])], "Cancels orders unpaid for 24 h."),
        ("nightly_invoices", [("r", "orders", ["paid_at"]), ("w", "invoices", ["status"])], "Runs every night at 02:00."),
    ]):
        ep = f"worker/entrypoint:scheduled_job:{job}"
        n(ep, "entrypoint", kind="scheduled_job", symbol=job, evidence={"file": "worker/jobs.py", "line": 4 + 10 * k})
        f = fn("worker", f"jobs.{job}", "worker/jobs.py", 5 + 10 * k, why)
        e("triggers", ep, f)
        for rw, t, cs in data:
            for c in cs:
                e("writes" if rw == "w" else "reads", f, f"column:{t}.{c}")
            e("writes" if rw == "w" else "reads", ep, f"table:{t}", "inferred", derived="call")

    # web front end
    unresolved = []
    for file, calls in PAGES.items():
        comp = file.rsplit("/", 1)[-1].split(".")[0]
        page = fn("web", f"{file[:-4].replace('/', '.')}.{comp}", file, 1)
        for k, call in enumerate(calls):
            f = fn("web", f"{file[:-4].replace('/', '.')}.{call[0]}", file, 10 + 6 * k)
            e("calls", page, f)
            if call[1]:
                e("calls_http", f, ep_id(*call[1]), "inferred")
            else:
                unresolved.append({"from": f, "method": "POST" if "apply" in call[2] else "GET",
                                   "url": call[2], "keys": [], "reason": call[3]})
    hook = fn("web", "src.lib.api.useApi", "src/lib/api.ts", 3, "Shared fetch wrapper.")
    e("calls", "web/function:src.pages.checkout.Checkout.placeOrder", hook)

    return {
        "version": "0.5.1",
        "generated_at": "2026-11-20T12:00:00Z",
        "database": "shop",
        "repos": ["api", "billing", "catalog", "shipping", "web", "worker"],
        "nodes": nodes,
        "edges": list(edges.values()),
        "unresolved_http": sorted(unresolved, key=lambda u: (u["from"], u["url"])),
    }


if __name__ == "__main__":
    data = build()
    with open(sys.argv[1] if len(sys.argv) > 1 else "shop.graph.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(len(data["nodes"]), "nodes", len(data["edges"]), "edges")
