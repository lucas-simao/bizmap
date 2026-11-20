<!-- bizmap-impact -->
<!-- bizmap-risk: medium -->
### BizMap impact · 🟠 Medium risk

**2** entrypoints (2 routes) · writes **1** table · reads **1** table · **2** columns — from **2** changed functions

**Why medium:**
- changes the handler of `GET /orders/:id`, `POST /orders/:id/paid`
- writes 1 table reached from 2 entrypoints

<details>
<summary><b>Entrypoints</b> · 2</summary>

| Kind | Entrypoint | Where | Reaches changed |
|---|---|---|---|
| route | `GET /orders/:id` · **handler changed** | [src/server.ts:12](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/src/server.ts#L12) | `orders.getOrder` |
| route | `POST /orders/:id/paid` · **handler changed** | [src/server.ts:14](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/src/server.ts#L14) | `orders.markPaid` |

</details>

<details>
<summary><b>Data</b> · writes 1 · reads 1 · 2 columns</summary>

| Table | Writes | Reads | Changed functions |
|---|---|---|---|
| **`orders`** | _table_ | id, status | `orders.getOrder`, `orders.markPaid` |

</details>

<details>
<summary><b>Changed functions</b> · 2</summary>

**2** changed functions · **2** entrypoints · writes **1** table · reads **1** table

| Changed function | Where | Entrypoints | Writes | Reads |
|---|---|---|---|---|
| `src.orders.getOrder` | [src/orders.ts:20](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/src/orders.ts#L20-L24) | `GET /orders/:id` | — | `orders (id, status)` |
| `src.orders.markPaid` | [src/orders.ts:28](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/src/orders.ts#L28-L29) | `POST /orders/:id/paid` | `orders` | — |

</details>

<details>
<summary><b>Unresolved &amp; notes</b> · 1</summary>

- 1 link in the reach is `heuristic`/`unknown` (route literals, dynamic SQL): included, worth a look

</details>

<sub>[BizMap](https://github.com/lucas-simao/bizmap/blob/main/docs/action.md) X.Y.Z · deterministic static analysis, no AI · commit [`0123456`](https://github.com/acme/shop/commit/0123456789abcdef0123456789abcdef01234567) · updated on every push</sub>
