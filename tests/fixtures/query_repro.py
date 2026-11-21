"""The fictional three-repo system the query commands are tested on, in the
shape `bizmap scan` writes (multi-repo ids `<repo>/...`; queues are shared).

- orders-api (Go): POST /orders → OrdersHandler.Create → Service.Save writes
  Firestore `orders`; Create also calls Events.OrderCreated, which publishes
  `order-created`. A `mirror` job copies Firestore `orders` into Postgres
  `orders` (feeds). PATCH /orders/{id}/status → OrdersHandler.UpdateStatus →
  Repo.SetStatus writes `orders.status`. Repo.BulkFix writes a table whose
  name is built at runtime (`table:unknown`).
- shop-web (TS): reads Firestore `orders`; createOrder calls POST /orders.
- billing-worker (Python): consumes `order-created` and writes Postgres
  `orders.status` too (the "forgotten worker").

Names only known at runtime (counted as `table:unknown` / `collection:unknown`):
orders-api `Service.Archive` (Go `Collection(coll)`), shop-web `saveTo`
(TS `doc(db, name, id)`), billing-worker `touch` (SQL `UPDATE {table_name}`).
An event route registered through orders-api's own wrapper: `main →
buildRouter → platform.RegisterEvent → (closure) → sync.Handle →
Service.Sync → mirrorTx.WriteOrder` writes Postgres `orders`.
"""

from __future__ import annotations

from typing import Any

API, WEB, BILL = "orders-api", "shop-web", "billing-worker"
Q = "queue:pubsub:order-created"


def _n(nid: str, ntype: str, **attrs: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, **attrs}


def _e(etype: str, a: str, b: str, conf: str = "exact", file: str | None = None, line: int = 0, **attrs: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"id": f"{etype}:{a}->{b}", "type": etype, "from": a, "to": b, "confidence": conf}
    if file:
        e["evidence"] = {"file": file, "line": line, "symbol": ""}
    e.update(attrs)
    return e


def _fn(repo: str, qual: str, file: str, line: int, **attrs: Any) -> dict[str, Any]:
    return _n(f"{repo}/function:{qual}", "function", name=qual.split(".", 1)[-1] if "." in qual else qual,
              evidence={"file": file, "line": line, "symbol": qual}, **attrs)


def build() -> dict[str, Any]:
    a = f"{API}/function:"
    nodes = [
        # orders-api
        _n(f"{API}/entrypoint:http:POST:/orders", "entrypoint", kind="http", method="POST", path="/orders",
           evidence={"file": "internal/http/routes.go", "line": 12}),
        _n(f"{API}/entrypoint:http:PATCH:/orders/{{id}}/status", "entrypoint", kind="http", method="PATCH",
           path="/orders/{id}/status", evidence={"file": "internal/http/routes.go", "line": 13}),
        _n(f"{API}/entrypoint:job:internal/mirror.Job.Run", "entrypoint", kind="job", symbol="mirror",
           evidence={"file": "cmd/mirror/main.go", "line": 9}),
        _fn(API, "internal/orders.OrdersHandler.Create", "internal/orders/handler.go", 20),
        _fn(API, "internal/orders.OrdersHandler.UpdateStatus", "internal/orders/handler.go", 41),
        _fn(API, "internal/orders.Service.Save", "internal/orders/service.go", 15),
        _fn(API, "internal/orders.Repo.SetStatus", "internal/orders/repo.go", 30),
        _fn(API, "internal/orders.Repo.BulkFix", "internal/orders/repo.go", 55),
        _fn(API, "internal/events.Publisher.OrderCreated", "internal/events/publisher.go", 10),
        _fn(API, "internal/mirror.Job.Run", "internal/mirror/job.go", 18),
        _n(f"{API}/collection:orders", "collection", name="orders", kind="collection", db="firestore:default",
           same_as=[f"{WEB}/collection:orders"]),
        _n(f"{API}/table:orders", "table", name="orders", engine="postgres"),
        *[_n(f"{API}/column:orders.{c}", "column", table="orders", name=c) for c in ("id", "customer_id", "total", "status")],
        _n(f"{API}/table:unknown", "table", name="unknown"),
        _n(f"{API}/collection:unknown", "collection", name="unknown", kind="collection", confidence="unknown"),
        _fn(API, "internal/orders.Service.Archive", "internal/orders/service.go", 40),
        _fn(API, "cmd/api.main", "cmd/api/main.go", 10),
        _fn(API, "cmd/api.buildRouter", "cmd/api/main.go", 20),
        _fn(API, "internal/platform.RegisterEvent", "internal/platform/events.go", 8),
        _fn(API, "internal/platform.RegisterEvent.func1", "internal/platform/events.go", 9),
        _fn(API, "internal/sync.Handle", "internal/sync/handler.go", 12),
        _fn(API, "internal/sync.Service.Sync", "internal/sync/service.go", 30),
        _fn(API, "internal/pgstore.mirrorTx.WriteOrder", "internal/pgstore/store.go", 44),
        # shop-web
        _fn(WEB, "src.orders.listOrders", "src/orders.ts", 4),
        _fn(WEB, "src.orders.createOrder", "src/orders.ts", 9),
        _fn(WEB, "src.pages.Orders.OrdersPage", "src/pages/Orders.tsx", 3),
        _n(f"{WEB}/collection:orders", "collection", name="orders", kind="collection", db="firestore:default",
           same_as=[f"{API}/collection:orders"]),
        _n(f"{WEB}/collection:unknown", "collection", name="unknown", kind="collection", confidence="unknown"),
        _fn(WEB, "src.orders.saveTo", "src/orders.ts", 14),
        # billing-worker
        _n(f"{BILL}/entrypoint:pubsub:order-created", "entrypoint", kind="pubsub", path="order-created",
           evidence={"file": "billing/consumer.py", "line": 8}),
        _fn(BILL, "billing.consumer.handle_order_created", "billing/consumer.py", 9),
        _fn(BILL, "billing.repo.mark_billed", "billing/repo.py", 21),
        _n(f"{BILL}/table:orders", "table", name="orders", engine="postgres"),
        _n(f"{BILL}/column:orders.status", "column", table="orders", name="status"),
        _n(f"{BILL}/table:unknown", "table", name="unknown"),
        _fn(BILL, "billing.app.touch", "billing/app.py", 30),
        # shared
        _n(Q, "queue", kind="pubsub", name="order-created"),
    ]
    edges = [
        _e("triggers", f"{API}/entrypoint:http:POST:/orders", a + "internal/orders.OrdersHandler.Create", file="internal/http/routes.go", line=12),
        _e("calls", a + "internal/orders.OrdersHandler.Create", a + "internal/orders.Service.Save", file="internal/orders/handler.go", line=25),
        _e("calls", a + "internal/orders.OrdersHandler.Create", a + "internal/events.Publisher.OrderCreated", file="internal/orders/handler.go", line=28),
        _e("writes", a + "internal/orders.Service.Save", f"{API}/collection:orders", file="internal/orders/service.go", line=18),
        # the scan also copies a handler's access to its entrypoint / callers (derived): never a row
        _e("writes", f"{API}/entrypoint:http:POST:/orders", f"{API}/collection:orders", "inferred", derived="call"),
        _e("writes", a + "internal/orders.OrdersHandler.Create", f"{API}/collection:orders", "inferred", derived="call"),
        _e("enqueues", a + "internal/events.Publisher.OrderCreated", Q, file="internal/events/publisher.go", line=14),
        _e("triggers", f"{API}/entrypoint:http:PATCH:/orders/{{id}}/status", a + "internal/orders.OrdersHandler.UpdateStatus",
           file="internal/http/routes.go", line=13),
        _e("calls", a + "internal/orders.OrdersHandler.UpdateStatus", a + "internal/orders.Repo.SetStatus", file="internal/orders/handler.go", line=47),
        _e("writes", a + "internal/orders.Repo.SetStatus", f"{API}/column:orders.status", file="internal/orders/repo.go", line=33, ops=["UPDATE"]),
        _e("writes", a + "internal/orders.Repo.BulkFix", f"{API}/table:unknown", "unknown", file="internal/orders/repo.go", line=60),
        _e("calls", a + "internal/orders.OrdersHandler.UpdateStatus", a + "internal/orders.Repo.BulkFix", "inferred", file="internal/orders/handler.go", line=50),
        _e("triggers", f"{API}/entrypoint:job:internal/mirror.Job.Run", a + "internal/mirror.Job.Run", file="cmd/mirror/main.go", line=9),
        _e("reads", a + "internal/mirror.Job.Run", f"{API}/collection:orders", file="internal/mirror/job.go", line=22),
        *[_e("writes", a + "internal/mirror.Job.Run", f"{API}/column:orders.{c}", "exact", file="internal/mirror/job.go", line=30, ops=["INSERT"])
          for c in ("id", "customer_id", "total")],
        _e("feeds", f"{API}/collection:orders", f"{API}/table:orders", "heuristic",
           via=[{"path": [a + "internal/mirror.Job.Run"]}], via_total=1,
           column_map=[{"from": "orders.customerId", "to": "orders.customer_id", "confidence": "heuristic", "fn": a + "internal/mirror.Job.Run"},
                       {"from": "orders.total", "to": "orders.total", "confidence": "heuristic", "fn": a + "internal/mirror.Job.Run"}]),
        # shop-web
        _e("reads", f"{WEB}/function:src.orders.listOrders", f"{WEB}/collection:orders", file="src/orders.ts", line=5),
        _e("calls", f"{WEB}/function:src.pages.Orders.OrdersPage", f"{WEB}/function:src.orders.listOrders", file="src/pages/Orders.tsx", line=6),
        _e("calls_http", f"{WEB}/function:src.orders.createOrder", f"{API}/entrypoint:http:POST:/orders", file="src/orders.ts", line=10),
        # billing-worker
        _e("triggers", Q, f"{BILL}/entrypoint:pubsub:order-created", "inferred", file="billing/consumer.py", line=8),
        _e("triggers", Q, f"{BILL}/function:billing.consumer.handle_order_created", file="billing/consumer.py", line=8),
        _e("triggers", f"{BILL}/entrypoint:pubsub:order-created", f"{BILL}/function:billing.consumer.handle_order_created", file="billing/consumer.py", line=8),
        _e("calls", f"{BILL}/function:billing.consumer.handle_order_created", f"{BILL}/function:billing.repo.mark_billed", file="billing/consumer.py", line=14),
        _e("writes", f"{BILL}/function:billing.repo.mark_billed", f"{BILL}/column:orders.status", file="billing/repo.py", line=24, ops=["UPDATE"]),
        _e("writes", f"{BILL}/function:billing.app.touch", f"{BILL}/table:unknown", "unknown", file="billing/app.py", line=32),
        # names only known at runtime, in Go and TS
        _e("writes", a + "internal/orders.Service.Archive", f"{API}/collection:unknown", "unknown", file="internal/orders/service.go", line=41),
        _e("writes", f"{WEB}/function:src.orders.saveTo", f"{WEB}/collection:unknown", "unknown", file="src/orders.ts", line=15),
        # the event route behind orders-api's own wrapper
        _e("calls", a + "cmd/api.main", a + "cmd/api.buildRouter", file="cmd/api/main.go", line=12),
        _e("calls", a + "cmd/api.buildRouter", a + "internal/platform.RegisterEvent", file="cmd/api/main.go", line=22),
        _e("calls", a + "internal/platform.RegisterEvent", a + "internal/platform.RegisterEvent.func1", file="internal/platform/events.go", line=9),
        _e("calls", a + "internal/platform.RegisterEvent.func1", a + "internal/sync.Handle", "inferred", file="internal/platform/events.go", line=9),
        _e("calls", a + "internal/sync.Handle", a + "internal/sync.Service.Sync", file="internal/sync/handler.go", line=15),
        _e("calls", a + "internal/sync.Service.Sync", a + "internal/pgstore.mirrorTx.WriteOrder", file="internal/sync/service.go", line=38),
        _e("writes", a + "internal/pgstore.mirrorTx.WriteOrder", f"{API}/table:orders", file="internal/pgstore/store.go", line=47, ops=["INSERT"]),
    ]
    return {
        "version": "test",
        "repos": [API, WEB, BILL],
        "nodes": nodes,
        "edges": edges,
        "unresolved_http": [
            {"from": f"{API}/function:internal/orders.OrdersHandler.Create", "method": "POST", "url": "{FRAUD_API_URL}/check",
             "reason": "base_unknown"},
        ],
    }
