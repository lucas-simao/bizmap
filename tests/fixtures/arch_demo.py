"""Fictional workspace for the architecture and event-flow views (tests, docs).

- shop-api (Go, Postgres `orders`, `customers`, `order_items`): routes to place,
  cancel and read an order; publishes `order-placed` and `order-cancelled`.
- shop-web (TS): pages calling shop-api over HTTP.
- billing-worker (Python): consumes `order-placed`, writes `invoices`
  (Postgres) and `payment_events` (Firestore).
- notify-fn (Cloud Function): Firestore trigger on `payment_events`, writes
  `notifications`.

`order-cancelled` has no consumer. All names are made up.
"""

from __future__ import annotations

from typing import Any


def _n(nid: str, ntype: str, **attrs: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, **attrs}


def _e(etype: str, a: str, b: str, conf: str = "exact", **attrs: Any) -> dict[str, Any]:
    return {"id": f"{etype}:{a}->{b}", "type": etype, "from": a, "to": b, "confidence": conf, **attrs}


def _fn(repo: str, name: str, file: str, line: int, **attrs: Any) -> dict[str, Any]:
    return _n(f"{repo}/function:{name}", "function", name=name, evidence={"file": file, "line": line},
              start_line=line, end_line=line + 9, **attrs)


API, WEB, BILL, FN = "shop-api", "shop-web", "billing-worker", "notify-fn"
Q_PLACED, Q_CANCEL = "queue:pubsub:order-placed", "queue:pubsub:order-cancelled"


def arch_graph() -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    # shop-api
    for t, cols in {"orders": ["id", "customer_id", "status", "total_cents"], "customers": ["id", "email", "tier"],
                    "order_items": ["id", "order_id", "sku", "qty"]}.items():
        nodes.append(_n(f"{API}/table:{t}", "table", name=t, engine="postgres", db="shop"))
        for c in cols:
            nodes.append(_n(f"{API}/column:{t}.{c}", "column", table=t, name=c))
    for method, path, handler in (("POST", "/orders", "handlers.Orders.Create"), ("POST", "/orders/{id}/cancel", "handlers.Orders.Cancel"),
                                  ("GET", "/orders/{id}", "handlers.Orders.Get")):
        eid = f"{API}/entrypoint:http:{method}:{path}"
        nodes.append(_n(eid, "entrypoint", kind="http", method=method, path=path, handler=handler,
                        evidence={"file": "internal/http/routes.go", "line": 10}))
        nodes.append(_fn(API, handler, "internal/http/orders.go", 20))
        edges.append(_e("triggers", eid, f"{API}/function:{handler}"))
    svc = [("orders.Service.PlaceOrder", 30), ("orders.Service.Cancel", 60), ("orders.Service.Get", 80),
           ("events.Publisher.OrderPlaced", 12), ("events.Publisher.OrderCancelled", 30)]
    for name, line in svc:
        nodes.append(_fn(API, name, f"internal/{name.split('.')[0]}/{name.split('.')[0]}.go", line))
    nodes.append(_n(Q_PLACED, "queue", kind="pubsub", name="order-placed"))
    nodes.append(_n(Q_CANCEL, "queue", kind="pubsub", name="order-cancelled"))
    f = lambda n: f"{API}/function:{n}"  # noqa: E731
    edges += [
        _e("calls", f("handlers.Orders.Create"), f("orders.Service.PlaceOrder")),
        _e("calls", f("handlers.Orders.Cancel"), f("orders.Service.Cancel")),
        _e("calls", f("handlers.Orders.Get"), f("orders.Service.Get")),
        _e("calls", f("orders.Service.PlaceOrder"), f("events.Publisher.OrderPlaced")),
        _e("calls", f("orders.Service.Cancel"), f("events.Publisher.OrderCancelled")),
        _e("reads", f("orders.Service.PlaceOrder"), f"{API}/table:customers", evidence={"file": "internal/orders/orders.go", "line": 33}),
        _e("writes", f("orders.Service.PlaceOrder"), f"{API}/column:orders.total_cents", evidence={"file": "internal/orders/orders.go", "line": 40}),
        _e("writes", f("orders.Service.PlaceOrder"), f"{API}/table:order_items", evidence={"file": "internal/orders/orders.go", "line": 44}),
        _e("writes", f("orders.Service.Cancel"), f"{API}/column:orders.status", evidence={"file": "internal/orders/orders.go", "line": 62}),
        _e("reads", f("orders.Service.Get"), f"{API}/table:orders", evidence={"file": "internal/orders/orders.go", "line": 82}),
        _e("reads", f("orders.Service.Get"), f"{API}/table:order_items", "inferred", evidence={"file": "internal/orders/orders.go", "line": 84}),
        _e("enqueues", f("events.Publisher.OrderPlaced"), Q_PLACED, fields=["customerId", "orderId", "totalCents"],
           evidence={"file": "internal/events/events.go", "line": 14}),
        _e("enqueues", f("events.Publisher.OrderCancelled"), Q_CANCEL, fields=["orderId", "reason"],
           evidence={"file": "internal/events/events.go", "line": 32}),
    ]
    # shop-web
    nodes.append(_fn(WEB, "src.api.orders.placeOrder", "src/api/orders.ts", 5))
    nodes.append(_fn(WEB, "src.api.orders.getOrder", "src/api/orders.ts", 12))
    nodes.append(_fn(WEB, "src.pages.Checkout", "src/pages/Checkout.tsx", 8))
    nodes.append(_fn(WEB, "src.pages.OrderPage", "src/pages/OrderPage.tsx", 8))
    edges += [
        _e("calls", f"{WEB}/function:src.pages.Checkout", f"{WEB}/function:src.api.orders.placeOrder"),
        _e("calls", f"{WEB}/function:src.pages.OrderPage", f"{WEB}/function:src.api.orders.getOrder"),
        _e("calls_http", f"{WEB}/function:src.api.orders.placeOrder", f"{API}/entrypoint:http:POST:/orders", "inferred"),
        _e("calls_http", f"{WEB}/function:src.api.orders.getOrder", f"{API}/entrypoint:http:GET:/orders/{{id}}", "inferred"),
    ]
    # billing-worker
    ep = f"{BILL}/entrypoint:pubsub:order-placed"
    on = f"{BILL}/function:billing.handlers.on_order_placed"
    nodes += [
        _n(ep, "entrypoint", kind="pubsub", topic="order-placed", handler="billing.handlers.on_order_placed",
           evidence={"file": "billing/main.py", "line": 9}),
        _fn(BILL, "billing.handlers.on_order_placed", "billing/handlers.py", 14,
            dataflow_in=[{"topic": "order-placed", "fields": {
                "orderId": [{"table": "table:invoices", "column": "order_id"}],
                "totalCents": [{"table": "table:invoices", "column": "amount_cents"}],
                "couponCode": [{"table": "collection:payment_events", "column": "coupon"}],
            }, "evidence": {"file": "billing/handlers.py", "line": 20}}]),
        _n(f"{BILL}/table:invoices", "table", name="invoices", engine="postgres", db="billing"),
        _n(f"{BILL}/collection:payment_events", "collection", name="payment_events", db="firestore:default"),
    ]
    edges += [
        _e("triggers", Q_PLACED, ep),
        _e("triggers", ep, on),
        _e("writes", on, f"{BILL}/table:invoices", evidence={"file": "billing/handlers.py", "line": 22}),
        _e("writes", on, f"{BILL}/collection:payment_events", evidence={"file": "billing/handlers.py", "line": 26},
           fields=["invoiceId", "orderId", "status"]),
    ]
    # notify-fn
    tep = f"{FN}/entrypoint:firestore:payment_events:create"
    nf = f"{FN}/function:index.notifyPayment"
    nodes += [
        _n(tep, "entrypoint", kind="firestore", method="CREATE", path="payment_events/{id}", symbol="index.notifyPayment",
           collection="payment_events", document="payment_events/{id}", event="create", evidence={"file": "src/index.ts", "line": 6}),
        _fn(FN, "index.notifyPayment", "src/index.ts", 6),
        _n(f"{FN}/collection:payment_events", "collection", name="payment_events", db="firestore:default"),
        _n(f"{FN}/collection:notifications", "collection", name="notifications", db="firestore:default"),
    ]
    edges += [
        _e("triggers", tep, nf),
        _e("reads", nf, f"{FN}/collection:payment_events", evidence={"file": "src/index.ts", "line": 7}, fields=["orderId", "status"]),
        _e("writes", nf, f"{FN}/collection:notifications", evidence={"file": "src/index.ts", "line": 12}),
    ]
    return {"version": "test", "nodes": nodes, "edges": edges}


def front_functions_graph() -> dict[str, Any]:
    """shop-api (Go) and shop-web (TS) whose repo also holds a Cloud Function:
    `notifyOnOrderEvent`, a Firestore trigger on `order_events/{id}` that
    writes `notifications`. shop-api writes `order_events`."""
    nodes: list[dict[str, Any]] = [
        _n(f"{API}/table:orders", "table", name="orders", engine="postgres", db="shop"),
        _n(f"{API}/entrypoint:http:POST:/orders", "entrypoint", kind="http", method="POST", path="/orders",
           handler="handlers.Create", evidence={"file": "internal/http/routes.go", "line": 10}),
        _fn(API, "handlers.Create", "internal/http/orders.go", 20),
        _n(f"{API}/collection:order_events", "collection", name="order_events", db="firestore:default"),
        _fn(WEB, "src.api.orders.placeOrder", "src/api/orders.ts", 5),
        _fn(WEB, "src.pages.Checkout", "src/pages/Checkout.tsx", 8),
        _n(f"{WEB}/entrypoint:firestore:order_events:create", "entrypoint", kind="firestore", method="CREATE",
           path="order_events/{id}", symbol="functions.notifyOnOrderEvent", collection="order_events",
           document="order_events/{id}", event="create", evidence={"file": "functions/src/index.ts", "line": 4}),
        _fn(WEB, "functions.notifyOnOrderEvent", "functions/src/index.ts", 4),
        _n(f"{WEB}/collection:order_events", "collection", name="order_events", db="firestore:default"),
        _n(f"{WEB}/collection:notifications", "collection", name="notifications", db="firestore:default"),
    ]
    edges = [
        _e("triggers", f"{API}/entrypoint:http:POST:/orders", f"{API}/function:handlers.Create"),
        _e("writes", f"{API}/function:handlers.Create", f"{API}/table:orders", evidence={"file": "internal/http/orders.go", "line": 22}),
        _e("writes", f"{API}/function:handlers.Create", f"{API}/collection:order_events", evidence={"file": "internal/http/orders.go", "line": 25}),
        _e("calls", f"{WEB}/function:src.pages.Checkout", f"{WEB}/function:src.api.orders.placeOrder"),
        _e("calls_http", f"{WEB}/function:src.api.orders.placeOrder", f"{API}/entrypoint:http:POST:/orders", "inferred"),
        _e("triggers", f"{WEB}/entrypoint:firestore:order_events:create", f"{WEB}/function:functions.notifyOnOrderEvent"),
        _e("reads", f"{WEB}/function:functions.notifyOnOrderEvent", f"{WEB}/collection:order_events", evidence={"file": "functions/src/index.ts", "line": 5}),
        _e("writes", f"{WEB}/function:functions.notifyOnOrderEvent", f"{WEB}/collection:notifications", evidence={"file": "functions/src/index.ts", "line": 9}),
    ]
    return {"version": "test", "nodes": nodes, "edges": edges}


def mixed_kinds_graph() -> dict[str, Any]:
    """Entrypoint kinds a real workspace mixes (all names made up):

    - `ingest` (Python): only HTTP Cloud Functions (`firebase_http`) and a
      Firestore trigger the scan also stored as a `firestore` queue node; two
      Cloud Tasks producers on a queue named `cloud-tasks`.
    - `assistant` (Python): HTTP routes; its own `cloud-tasks` queue.
    - `portal` (TS front end): screens, plus an HTTP Cloud Function.
    - `core` (Go): routes, and a goroutine loop the scan marks `scheduled_job`
      with no schedule.
    """
    ex, asst, portal, core = "ingest", "assistant", "portal", "core"
    nodes: list[dict[str, Any]] = [
        _n(f"{ex}/entrypoint:firebase_http:POST:/produce", "entrypoint", kind="firebase_http", path="/produce", symbol="main.produce",
           evidence={"file": "src/main.py", "line": 3}),
        _fn(ex, "main.produce", "src/main.py", 3),
        _fn(ex, "tasks.enqueue", "src/tasks.py", 10),
        _n(f"{ex}/entrypoint:firestore:jobs:written", "entrypoint", kind="firestore", collection="jobs", event="written",
           path="jobs/{id}", symbol="main.on_job", evidence={"file": "src/main.py", "line": 20}),
        _fn(ex, "main.on_job", "src/main.py", 20),
        _n(f"{ex}/queue:firestore:jobs", "queue", kind="firestore", name="jobs", repo=ex),
        _n(f"{ex}/queue:cloud_tasks:cloud-tasks", "queue", kind="cloud_tasks", name="cloud-tasks", repo=ex),
        _n(f"{ex}/collection:jobs", "collection", name="jobs", db="firestore:default"),
        _n(f"{asst}/entrypoint:http:POST:/ask", "entrypoint", kind="http", method="POST", path="/ask", handler="app.ask",
           evidence={"file": "app.py", "line": 5}),
        _fn(asst, "app.ask", "app.py", 5),
        _n(f"{asst}/queue:cloud_tasks:cloud-tasks", "queue", kind="cloud_tasks", name="cloud-tasks", repo=asst),
        _fn(portal, "src.pages.Home", "src/pages/Home.tsx", 3),
        _fn(portal, "src.api.ask", "src/api/ask.ts", 3),
        _n(f"{portal}/entrypoint:firebase_http:rss", "entrypoint", kind="firebase_http", path="/rss", symbol="functions.rss",
           evidence={"file": "functions/src/rss.ts", "line": 2}),
        _fn(portal, "functions.rss", "functions/src/rss.ts", 2),
        _n(f"{portal}/collection:posts", "collection", name="posts", db="firestore:default"),
        _n(f"{core}/entrypoint:http:GET:/health", "entrypoint", kind="http", method="GET", path="/health", handler="health",
           evidence={"file": "main.go", "line": 4}),
        _fn(core, "health", "main.go", 4),
        _n(f"{core}/entrypoint:job:hub.Run", "entrypoint", kind="scheduled_job", symbol="hub.Run", handler="hub.Run",
           why="Run pumps messages until ctx ends.", evidence={"file": "hub.go", "line": 9}),
        _fn(core, "hub.Run", "hub.go", 9),
    ]
    edges = [
        _e("triggers", f"{ex}/entrypoint:firebase_http:POST:/produce", f"{ex}/function:main.produce"),
        _e("calls", f"{ex}/function:main.produce", f"{ex}/function:tasks.enqueue"),
        _e("enqueues", f"{ex}/function:tasks.enqueue", f"{ex}/queue:cloud_tasks:cloud-tasks", "inferred"),
        _e("writes", f"{ex}/function:main.produce", f"{ex}/collection:jobs", evidence={"file": "src/main.py", "line": 6}),
        _e("triggers", f"{ex}/entrypoint:firestore:jobs:written", f"{ex}/function:main.on_job"),
        _e("triggers", f"{ex}/queue:firestore:jobs", f"{ex}/entrypoint:firestore:jobs:written", "inferred"),
        _e("reads", f"{ex}/function:main.on_job", f"{ex}/collection:jobs", evidence={"file": "src/main.py", "line": 21}),
        _e("triggers", f"{asst}/entrypoint:http:POST:/ask", f"{asst}/function:app.ask"),
        _e("enqueues", f"{asst}/function:app.ask", f"{asst}/queue:cloud_tasks:cloud-tasks", "inferred"),
        _e("calls", f"{portal}/function:src.pages.Home", f"{portal}/function:src.api.ask"),
        _e("calls_http", f"{portal}/function:src.api.ask", f"{asst}/entrypoint:http:POST:/ask", "inferred"),
        _e("triggers", f"{portal}/entrypoint:firebase_http:rss", f"{portal}/function:functions.rss"),
        _e("reads", f"{portal}/function:functions.rss", f"{portal}/collection:posts", evidence={"file": "functions/src/rss.ts", "line": 4}),
        _e("reads", f"{portal}/function:src.pages.Home", f"{portal}/collection:posts", evidence={"file": "src/pages/Home.tsx", "line": 5}),
        _e("triggers", f"{core}/entrypoint:http:GET:/health", f"{core}/function:health"),
        _e("triggers", f"{core}/entrypoint:job:hub.Run", f"{core}/function:hub.Run"),
    ]
    return {"version": "test", "nodes": nodes, "edges": edges}
