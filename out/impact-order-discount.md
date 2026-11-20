<!-- bizmap-impact -->
### BizMap impact

**3** changed functions (1 folded into another row) · **2** entrypoints · writes **3** tables · reads **2** tables

```mermaid
flowchart LR
  nshop_function_src_orders_cancelOrder["shop: src.orders.cancelOrder"]:::changed
  nshop_entrypoint_http_POST__orders__id_cancel(["shop: POST /orders/:id/cancel"])
  ntable_inventory[("shop: inventory")]
  ntable_order_status_history[("shop: order_status_history")]
  ntable_orders[("shop: orders")]
  nshop_function_src_orders_placeOrder["shop: src.orders.placeOrder"]:::changed
  nshop_entrypoint_http_POST__orders(["shop: POST /orders"])
  nshop_entrypoint_http_POST__orders__id_cancel --> nshop_function_src_orders_cancelOrder
  nshop_function_src_orders_cancelOrder -- writes --> ntable_inventory
  nshop_function_src_orders_cancelOrder -- writes --> ntable_order_status_history
  nshop_function_src_orders_cancelOrder -- writes --> ntable_orders
  nshop_entrypoint_http_POST__orders --> nshop_function_src_orders_placeOrder
  nshop_function_src_orders_placeOrder -- writes --> ntable_inventory
  nshop_function_src_orders_placeOrder -- writes --> ntable_orders
  classDef changed fill:#fff3bf,stroke:#f08c00
```

| Changed function | Entrypoints | Writes | Reads |
|---|---|---|---|
| `shop: src.orders.cancelOrder`<br><sub>also changed: `shop: src.server.POST /orders/:id/cancel` (same impact)</sub> | `shop: POST /orders/:id/cancel` | `shop: inventory (stock)`<br>`shop: order_status_history`<br>`shop: orders (status)` | `shop: inventory (sku)`<br>`shop: orders (customer_id, id, quantity, sku)` |
| `shop: src.orders.placeOrder` | `shop: POST /orders` | `shop: inventory (stock, updated_at)`<br>`shop: orders (customer_id, discount_pct, quantity, sku, status)` | `shop: inventory (sku)` |

**Schema changes**

| Change | Used by entrypoints | Functions |
|---|---|---|
| `orders.discount_pct` added <sub>shop/db/schema.sql:19</sub> | `shop: POST /orders` | `shop: src.orders.placeOrder`<br>`shop: src.server.POST /orders` |

<sub>Deterministic static analysis: entrypoints that reach each changed function and the data it (or its callees) reads/writes. Edges marked `heuristic`/`unknown` in the graph are included.</sub>
