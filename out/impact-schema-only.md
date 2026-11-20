<!-- bizmap-impact -->
### BizMap impact

No mapped function changed in this diff.

**Schema changes**

| Change | Used by entrypoints | Functions |
|---|---|---|
| `customers.phone` added <sub>shop/db/schema.sql:5</sub> | — (not used yet) | — |
| `inventory.stock` changed <sub>shop/db/schema.sql:10</sub> | `shop: GET /inventory/:sku`<br>`shop: POST /orders` | `shop: src.inventory.getStock`<br>`shop: src.inventory.reserveStock`<br>`shop: src.orders.placeOrder`<br>`shop: src.server.GET /inventory/:sku`<br>+1 |
| `orders.coupon_code` added <sub>shop/db/schema.sql:19</sub> | — (not used yet) | — |
| `orders.status` changed <sub>shop/db/schema.sql:20</sub> | `payments: POST /webhooks/provider`<br>`shop: GET /orders/:id`<br>`shop: POST /orders`<br>`shop: POST /orders/:id/checkout`<br>`shop: POST /orders/:id/paid` | `shop: src.checkout.startCheckout`<br>`shop: src.orders.getOrder`<br>`shop: src.orders.markPaid`<br>`shop: src.orders.placeOrder`<br>+4 |
