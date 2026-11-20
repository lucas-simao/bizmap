<!-- bizmap-impact -->
### BizMap impact

No mapped function changed in this diff.

**Schema changes**

| Change | Used by entrypoints | Functions |
|---|---|---|
| ⚠️ `inventory.updated_at` **dropped** — still used <sub>shop/db/migrations/002_shipping.sql:4</sub> | `shop: POST /orders` | `shop: src.inventory.reserveStock`<br>`shop: src.orders.placeOrder`<br>`shop: src.server.POST /orders` |
| ⚠️ `customers.name` **renamed → `full_name`** — still used <sub>shop/db/migrations/002_shipping.sql:5</sub> | `shop: GET /customers/:id`<br>`shop: POST /customers` | `shop: src.customers.createCustomer`<br>`shop: src.customers.getCustomer`<br>`shop: src.server.GET /customers/:id`<br>`shop: src.server.POST /customers` |
| `orders.tracking_code` added <sub>shop/db/migrations/002_shipping.sql:2</sub> | — (not used yet) | — |
| `orders.quantity` altered <sub>shop/db/migrations/002_shipping.sql:3</sub> | `shop: GET /orders/:id`<br>`shop: POST /orders` | `shop: src.orders.getOrder`<br>`shop: src.orders.placeOrder`<br>`shop: src.server.GET /orders/:id`<br>`shop: src.server.POST /orders` |
| new table `shipments` (3 columns) <sub>shop/db/migrations/002_shipping.sql:7</sub> | — (not used yet) | — |
| new table `warehouses` (2 columns) <sub>shop/db/migrations/002_shipping.sql:14</sub> | — (not used yet) | — |
| `orders.coupon` added <sub>shop/db/migrations/003_misc.sql:1</sub> | — (not used yet) | — |
| `orders.gift_note` added <sub>shop/db/migrations/003_misc.sql:2</sub> | — (not used yet) | — |
| `orders.gift_wrap` added <sub>shop/db/migrations/003_misc.sql:2</sub> | — (not used yet) | — |
| `customers.email` altered <sub>shop/db/migrations/003_misc.sql:3</sub> | `shop: GET /customers/:id`<br>`shop: POST /customers` | `shop: src.customers.createCustomer`<br>`shop: src.customers.getCustomer`<br>`shop: src.server.GET /customers/:id`<br>`shop: src.server.POST /customers` |
| `shipments.carrier` **dropped** <sub>shop/db/migrations/003_misc.sql:4</sub> | — | — |
