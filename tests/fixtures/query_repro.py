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
        # shop-web
        _fn(WEB, "src.orders.listOrders", "src/orders.ts", 4),
        _fn(WEB, "src.orders.createOrder", "src/orders.ts", 9),
        _fn(WEB, "src.pages.Orders.OrdersPage", "src/pages/Orders.tsx", 3),
        _n(f"{WEB}/collection:orders", "collection", name="orders", kind="collection", db="firestore:default",
           same_as=[f"{API}/collection:orders"]),
        # billing-worker
        _n(f"{BILL}/entrypoint:pubsub:order-created", "entrypoint", kind="pubsub", path="order-created",
           evidence={"file": "billing/consumer.py", "line": 8}),
        _fn(BILL, "billing.consumer.handle_order_created", "billing/consumer.py", 9),
        _fn(BILL, "billing.repo.mark_billed", "billing/repo.py", 21),
        _n(f"{BILL}/table:orders", "table", name="orders", engine="postgres"),
        _n(f"{BILL}/column:orders.status", "column", table="orders", name="status"),
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
