"""The fictional three-repo system of the HTML start screen, in the shape
`bizmap scan --discover .` writes (multi-repo ids `<repo>/...`; queues shared).
Fictional code only.

- orders-api (Go, gin): ``POST /orders`` → ``OrdersHandler.Create`` →
  ``Service.Save`` writes the Firestore collection ``orders``; ``Create`` also
  publishes ``order-created``. ``PATCH /orders/:id/status`` →
  ``OrdersHandler.UpdateStatus`` → ``Repo.SetStatus`` → (2 functions) →
  ``Repo.storeStatus`` writes Postgres ``orders.status``. The ``cmd/mirror``
  job copies Firestore ``orders`` → Postgres ``orders`` (feeds). The migration
  ``CREATE TABLE orders (...)`` is here (columns with types).
  ``Repo.BulkFix`` writes a table whose name is built at runtime;
  ``Repo.CountByStatus`` reads ``orders`` and nothing calls it; a test writes
  ``orders.status`` too.
- shop-web (TS, Firebase Web SDK): ``listOrders`` reads the collection from
  the orders screen; ``saveDoc`` writes ``doc(db, name, id)`` (name from a
  parameter).
- billing-worker (Python, FastAPI + SQLAlchemy): the push route
  ``POST /pubsub/order-created`` (subscription to ``order-created``) runs
  ``UPDATE orders SET status = 'billed'`` — the forgotten worker — and
  ``touch(table_name, ...)`` builds its table name in an f-string.
"""

from __future__ import annotations

from typing import Any

API, WEB, BILL = "orders-api", "shop-web", "billing-worker"
Q = "queue:pubsub:order-created"
A = f"{API}/function:"
W_ = f"{WEB}/function:"
B = f"{BILL}/function:"
POST_ORDERS = f"{API}/entrypoint:http:POST:/orders"
PATCH_STATUS = f"{API}/entrypoint:http:PATCH:/orders/:id/status"
MIRROR_JOB = f"{API}/entrypoint:scheduled_job:cmd/mirror.main"
PUSH = f"{BILL}/entrypoint:http:POST:/pubsub/order-created"


def _n(nid: str, ntype: str, **attrs: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, **attrs}


def _e(etype: str, a: str, b: str, conf: str = "exact", file: str | None = None, line: int = 0, **attrs: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"id": f"{etype}:{a}->{b}", "type": etype, "from": a, "to": b, "confidence": conf}
    if file:
        e["evidence"] = {"file": file, "line": line, "symbol": ""}
    e.update(attrs)
    return e


def _fn(prefix: str, qual: str, name: str, file: str, line: int, **attrs: Any) -> dict[str, Any]:
    return _n(prefix + qual, "function", name=name, evidence={"file": file, "line": line, "symbol": qual}, **attrs)


def build() -> dict[str, Any]:
    col_types = {"id": "uuid", "customer_id": "uuid", "total": "numeric", "status": "text"}
    nodes = [
        # ── orders-api ──
        _n(POST_ORDERS, "entrypoint", kind="http", method="POST", path="/orders",
           evidence={"file": "internal/http/routes.go", "line": 12}),
        _n(PATCH_STATUS, "entrypoint", kind="http", method="PATCH", path="/orders/:id/status",
           evidence={"file": "internal/http/routes.go", "line": 13}),
        _n(MIRROR_JOB, "entrypoint", kind="scheduled_job", symbol="cmd/mirror", evidence={"file": "cmd/mirror/main.go", "line": 9}),
        _fn(A, "internal/orders.OrdersHandler.Create", "OrdersHandler.Create", "internal/orders/handler.go", 20),
        _fn(A, "internal/orders.OrdersHandler.UpdateStatus", "OrdersHandler.UpdateStatus", "internal/orders/handler.go", 41),
        _fn(A, "internal/orders.Service.Save", "Service.Save", "internal/orders/service.go", 15),
        _fn(A, "internal/orders.Repo.SetStatus", "Repo.SetStatus", "internal/orders/repo.go", 30),
        _fn(A, "internal/orders.Repo.applyStatus", "Repo.applyStatus", "internal/orders/repo.go", 44),
        _fn(A, "internal/orders.Repo.storeStatus", "Repo.storeStatus", "internal/orders/repo.go", 58),
        _fn(A, "internal/orders.Repo.BulkFix", "Repo.BulkFix", "internal/orders/repo.go", 75),
        _fn(A, "internal/orders.Repo.CountByStatus", "Repo.CountByStatus", "internal/orders/repo.go", 90),
        _fn(A, "internal/orders.TestSetStatus", "TestSetStatus", "internal/orders/repo_test.go", 12),
        _fn(A, "internal/events.Publisher.OrderCreated", "Publisher.OrderCreated", "internal/events/publisher.go", 10),
        _fn(A, "cmd/mirror.Job.Run", "Job.Run", "cmd/mirror/job.go", 18),
        _n(f"{API}/collection:orders", "collection", name="orders", kind="collection", db="firestore:default",
           same_as=[f"{WEB}/collection:orders"]),
        _n(f"{API}/table:orders", "table", name="orders", engine="postgres",
           evidence={"file": "migrations/001_orders.sql", "line": 1}),
        *[_n(f"{API}/column:orders.{c}", "column", table="orders", name=c, data_type=t, nullable=c != "id")
          for c, t in col_types.items()],
        _n(f"{API}/table:unknown", "table", name="unknown"),
        # ── shop-web ──
        _fn(W_, "src.orders.listOrders", "listOrders", "src/orders.ts", 4),
        _fn(W_, "src.orders.createOrder", "createOrder", "src/orders.ts", 9),
        _fn(W_, "src.pages.Orders.OrdersPage", "OrdersPage", "src/pages/Orders.tsx", 3),
        _fn(W_, "src.lib.store.saveDoc", "saveDoc", "src/lib/store.ts", 7),
        _fn(W_, "src.pages.Admin.AdminPage", "AdminPage", "src/pages/Admin.tsx", 5),
        _n(f"{WEB}/collection:orders", "collection", name="orders", kind="collection", db="firestore:default",
           same_as=[f"{API}/collection:orders"]),
        _n(f"{WEB}/collection:unknown", "collection", name="unknown", db="firestore:default"),
        # ── billing-worker ──
        _n(PUSH, "entrypoint", kind="http", method="POST", path="/pubsub/order-created",
           evidence={"file": "billing/main.py", "line": 10}),
        _fn(B, "billing.main.on_order_created", "main.on_order_created", "billing/main.py", 11),
        _fn(B, "billing.repo.mark_billed", "repo.mark_billed", "billing/repo.py", 21),
        _fn(B, "billing.repo.touch", "repo.touch", "billing/repo.py", 40),
        _n(f"{BILL}/table:orders", "table", name="orders", engine="postgres", kind="unresolved_relation"),
        _n(f"{BILL}/column:orders.status", "column", table="orders", name="status"),
        _n(f"{BILL}/table:unknown", "table", name="unknown"),
        # ── shared ──
        _n(Q, "queue", kind="pubsub", name="order-created"),
    ]
    edges = [
        # POST /orders → Create → Save (collection) + publish
        _e("triggers", POST_ORDERS, A + "internal/orders.OrdersHandler.Create", file="internal/http/routes.go", line=12),
        _e("calls", A + "internal/orders.OrdersHandler.Create", A + "internal/orders.Service.Save", file="internal/orders/handler.go", line=25),
        _e("calls", A + "internal/orders.OrdersHandler.Create", A + "internal/events.Publisher.OrderCreated", file="internal/orders/handler.go", line=28),
        _e("writes", A + "internal/orders.Service.Save", f"{API}/collection:orders", file="internal/orders/service.go", line=18),
        # the scan also copies a handler's access to its entrypoint / callers (derived): never a row
        _e("writes", POST_ORDERS, f"{API}/collection:orders", "inferred", derived="call"),
        _e("writes", A + "internal/orders.OrdersHandler.Create", f"{API}/collection:orders", "inferred", derived="call"),
        _e("enqueues", A + "internal/events.Publisher.OrderCreated", Q, file="internal/events/publisher.go", line=14),
        # PATCH /orders/:id/status → UpdateStatus → SetStatus → applyStatus → storeStatus
        _e("triggers", PATCH_STATUS, A + "internal/orders.OrdersHandler.UpdateStatus", file="internal/http/routes.go", line=13),
        _e("calls", A + "internal/orders.OrdersHandler.UpdateStatus", A + "internal/orders.Repo.SetStatus", file="internal/orders/handler.go", line=47),
        _e("calls", A + "internal/orders.Repo.SetStatus", A + "internal/orders.Repo.applyStatus", file="internal/orders/repo.go", line=33),
        _e("calls", A + "internal/orders.Repo.applyStatus", A + "internal/orders.Repo.storeStatus", file="internal/orders/repo.go", line=50),
        _e("writes", A + "internal/orders.Repo.storeStatus", f"{API}/column:orders.status", file="internal/orders/repo.go", line=61, ops=["UPDATE"]),
        _e("writes", PATCH_STATUS, f"{API}/column:orders.status", "inferred", derived="call"),
        _e("calls", A + "internal/orders.OrdersHandler.UpdateStatus", A + "internal/orders.Repo.BulkFix", "inferred", file="internal/orders/handler.go", line=50),
        _e("writes", A + "internal/orders.Repo.BulkFix", f"{API}/table:unknown", "unknown", file="internal/orders/repo.go", line=80),
        _e("reads", A + "internal/orders.Repo.CountByStatus", f"{API}/column:orders.status", file="internal/orders/repo.go", line=92),
        _e("writes", A + "internal/orders.TestSetStatus", f"{API}/column:orders.status", file="internal/orders/repo_test.go", line=15),
        # cmd/mirror: Firestore orders → Postgres orders
        _e("triggers", MIRROR_JOB, A + "cmd/mirror.Job.Run", file="cmd/mirror/main.go", line=9),
        _e("reads", A + "cmd/mirror.Job.Run", f"{API}/collection:orders", file="cmd/mirror/job.go", line=22),
        *[_e("writes", A + "cmd/mirror.Job.Run", f"{API}/column:orders.{c}", "exact", file="cmd/mirror/job.go", line=30, ops=["INSERT"])
          for c in ("id", "customer_id", "total")],
        _e("feeds", f"{API}/collection:orders", f"{API}/table:orders", "heuristic",
           via=[{"path": [A + "cmd/mirror.Job.Run"]}], via_total=1,
           column_map=[{"from": "orders.customerId", "to": "orders.customer_id", "confidence": "heuristic", "fn": A + "cmd/mirror.Job.Run"},
                       {"from": "orders.total", "to": "orders.total", "confidence": "heuristic", "fn": A + "cmd/mirror.Job.Run"}]),
        # shop-web
        _e("reads", W_ + "src.orders.listOrders", f"{WEB}/collection:orders", file="src/orders.ts", line=5),
        _e("calls", W_ + "src.pages.Orders.OrdersPage", W_ + "src.orders.listOrders", file="src/pages/Orders.tsx", line=6),
        _e("calls", W_ + "src.pages.Orders.OrdersPage", W_ + "src.orders.createOrder", file="src/pages/Orders.tsx", line=8),
        _e("calls_http", W_ + "src.orders.createOrder", POST_ORDERS, file="src/orders.ts", line=10),
        _e("writes", W_ + "src.lib.store.saveDoc", f"{WEB}/collection:unknown", "unknown", file="src/lib/store.ts", line=8),
        _e("calls", W_ + "src.pages.Admin.AdminPage", W_ + "src.lib.store.saveDoc", file="src/pages/Admin.tsx", line=9),
        # billing-worker: the push subscription of order-created
        _e("triggers", Q, PUSH, "inferred", file="infra/pubsub.sh", line=3),
        _e("triggers", PUSH, B + "billing.main.on_order_created", file="billing/main.py", line=10),
        _e("calls", B + "billing.main.on_order_created", B + "billing.repo.mark_billed", file="billing/main.py", line=14),
        _e("writes", B + "billing.repo.mark_billed", f"{BILL}/column:orders.status", "inferred", file="billing/repo.py", line=24, ops=["UPDATE"]),
        _e("writes", B + "billing.repo.touch", f"{BILL}/table:unknown", "unknown", file="billing/repo.py", line=42),
    ]
    return {
        "version": "test",
        "repos": [API, WEB, BILL],
        "nodes": nodes,
        "edges": edges,
        "unresolved_http": [
            {"from": A + "internal/orders.OrdersHandler.Create", "method": "POST", "url": "{FRAUD_API_URL}/check",
             "reason": "base_unknown"},
        ],
    }
