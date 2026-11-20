<!-- bizmap-impact -->
### BizMap impact

**10** changed functions (1 folded into another row) · **6** entrypoints · writes **3** tables · reads **2** tables

```mermaid
flowchart LR
  nshop_function_src_server_POST__orders__id_paid["shop: src.server.POST /orders/:id/paid"]:::changed
  npayments_entrypoint_http_POST__webhooks_provider(["payments: POST /webhooks/provider"])
  nshop_entrypoint_http_POST__orders__id_paid(["shop: POST /orders/:id/paid"])
  ntable_customers[("shop: customers")]
  ntable_order_status_history[("shop: order_status_history")]
  ntable_orders[("shop: orders")]
  nshop_function_src_orders_markPaid["shop: src.orders.markPaid"]:::changed
  nshop_function_src_orders_markShipped["shop: src.orders.markShipped"]:::changed
  nshop_entrypoint_http_POST__orders__id_deliver(["shop: POST /orders/:id/deliver"])
  nshop_entrypoint_http_POST__orders__id_ship(["shop: POST /orders/:id/ship"])
  nshop_function_src_customers_normalizeEmail["shop: src.customers.normalizeEmail"]:::changed
  nshop_function_src_orders_markDelivered["shop: src.orders.markDelivered"]:::changed
  nshop_function_src_orders_markReturned["shop: src.orders.markReturned"]:::changed
  nshop_entrypoint_http_POST__orders__id_return(["shop: POST /orders/:id/return"])
  nshop_function_src_server_POST__orders__id_return["shop: src.server.POST /orders/:id/return"]:::changed
  nshop_function_src_server_POST__orders__id_ship["shop: src.server.POST /orders/:id/ship"]:::changed
  nshop_function_src_orders_getOrder["shop: src.orders.getOrder"]:::changed
  nshop_entrypoint_http_GET__orders__id(["shop: GET /orders/:id"])
  npayments_entrypoint_http_POST__webhooks_provider --> nshop_function_src_server_POST__orders__id_paid
  nshop_entrypoint_http_POST__orders__id_paid --> nshop_function_src_server_POST__orders__id_paid
  nshop_function_src_server_POST__orders__id_paid -- writes --> ntable_customers
  nshop_function_src_server_POST__orders__id_paid -- writes --> ntable_order_status_history
  nshop_function_src_server_POST__orders__id_paid -- writes --> ntable_orders
  npayments_entrypoint_http_POST__webhooks_provider --> nshop_function_src_orders_markPaid
  nshop_entrypoint_http_POST__orders__id_paid --> nshop_function_src_orders_markPaid
  nshop_function_src_orders_markPaid -- writes --> ntable_order_status_history
  nshop_function_src_orders_markPaid -- writes --> ntable_orders
  nshop_entrypoint_http_POST__orders__id_deliver --> nshop_function_src_orders_markShipped
  nshop_entrypoint_http_POST__orders__id_ship --> nshop_function_src_orders_markShipped
  nshop_function_src_orders_markShipped -- writes --> ntable_order_status_history
  nshop_function_src_orders_markShipped -- writes --> ntable_orders
  npayments_entrypoint_http_POST__webhooks_provider --> nshop_function_src_customers_normalizeEmail
  nshop_entrypoint_http_POST__orders__id_paid --> nshop_function_src_customers_normalizeEmail
  nshop_function_src_customers_normalizeEmail -- writes --> ntable_customers
  nshop_entrypoint_http_POST__orders__id_deliver --> nshop_function_src_orders_markDelivered
  nshop_function_src_orders_markDelivered -- writes --> ntable_order_status_history
  nshop_function_src_orders_markDelivered -- writes --> ntable_orders
  nshop_entrypoint_http_POST__orders__id_return --> nshop_function_src_orders_markReturned
  nshop_function_src_orders_markReturned -- writes --> ntable_order_status_history
  nshop_function_src_orders_markReturned -- writes --> ntable_orders
  nshop_entrypoint_http_POST__orders__id_return --> nshop_function_src_server_POST__orders__id_return
  nshop_function_src_server_POST__orders__id_return -- writes --> ntable_order_status_history
  nshop_function_src_server_POST__orders__id_return -- writes --> ntable_orders
  nshop_entrypoint_http_POST__orders__id_ship --> nshop_function_src_server_POST__orders__id_ship
  nshop_function_src_server_POST__orders__id_ship -- writes --> ntable_order_status_history
  nshop_function_src_server_POST__orders__id_ship -- writes --> ntable_orders
  nshop_entrypoint_http_GET__orders__id --> nshop_function_src_orders_getOrder
  nshop_function_src_orders_getOrder -. reads .-> ntable_orders
  classDef changed fill:#fff3bf,stroke:#f08c00
```

| Changed function | Entrypoints | Writes | Reads |
|---|---|---|---|
| `shop: src.server.POST /orders/:id/paid` | `payments: POST /webhooks/provider`<br>`shop: POST /orders/:id/paid` | `shop: customers (email)`<br>`shop: order_status_history`<br>`shop: orders (created_at, status)` | `shop: customers (id)`<br>`shop: orders (id)` |
| `shop: src.orders.markPaid` | `payments: POST /webhooks/provider`<br>`shop: POST /orders/:id/paid` | `shop: order_status_history`<br>`shop: orders (created_at, status)` | `shop: orders (id)` |
| `shop: src.orders.markShipped` | `shop: POST /orders/:id/deliver`<br>`shop: POST /orders/:id/ship` | `shop: order_status_history`<br>`shop: orders (status)` | `shop: orders (id)` |
| `shop: src.customers.normalizeEmail` | `payments: POST /webhooks/provider`<br>`shop: POST /orders/:id/paid` | `shop: customers (email)` | `shop: customers (id)` |
| `shop: src.orders.markDelivered`<br><sub>also changed: `shop: src.server.POST /orders/:id/deliver` (same impact)</sub> | `shop: POST /orders/:id/deliver` | `shop: order_status_history`<br>`shop: orders (status)` | `shop: orders (id)` |
| `shop: src.orders.markReturned` | `shop: POST /orders/:id/return` | `shop: order_status_history`<br>`shop: orders (status)` | `shop: orders (id)` |
| `shop: src.server.POST /orders/:id/return` | `shop: POST /orders/:id/return` | `shop: order_status_history`<br>`shop: orders (status)` | `shop: orders (id)` |
| `shop: src.server.POST /orders/:id/ship` | `shop: POST /orders/:id/ship` | `shop: order_status_history`<br>`shop: orders (status)` | `shop: orders (id)` |
| `shop: src.orders.getOrder` | `shop: GET /orders/:id` | — | `shop: orders (created_at, customer_id, id, quantity, sku, status)` |

<sub>Deterministic static analysis: entrypoints that reach each changed function and the data it (or its callees) reads/writes. Edges marked `heuristic`/`unknown` in the graph are included.</sub>
