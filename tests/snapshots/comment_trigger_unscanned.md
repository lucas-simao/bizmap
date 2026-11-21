<!-- bizmap-impact -->
<!-- bizmap-risk: medium -->
### BizMap impact · 🟠 Medium risk

**1** entrypoint (1 route) · writes **1** table · reads **0** tables · **1** column — from **1** changed function

**Why medium:**
- changes the handler of `PATCH /orders/:id/status`
- writes 1 table reached from 1 entrypoint

<details open>
<summary><b>Diagram</b> · entrypoint → changed function → table</summary>

```mermaid
flowchart LR
  n1["Store.SetStatus"]:::changed
  n2(["PATCH /orders/:id/status"])
  n3[("orders")]
  n2 --> n1
  n1 -- writes --> n3
  classDef changed fill:#fff3bf,stroke:#f08c00,color:#1f2328
  classDef risky fill:#ffe3e3,stroke:#e03131,color:#1f2328
  classDef trigger fill:#f3f0ff,stroke:#7048e8,color:#1f2328
  classDef more fill:#f1f3f5,stroke:#adb5bd,color:#495057,stroke-dasharray:3 3
```

</details>

<details>
<summary><b>Entrypoints</b> · 1</summary>

| Kind | Entrypoint | Where | Reaches changed |
|---|---|---|---|
| route | `PATCH /orders/:id/status` · **handler changed** | [main.go:5](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/main.go#L5) | `Store.SetStatus` |

</details>

<details>
<summary><b>Data</b> · writes 1 · reads 0 · 1 column</summary>

| Table | Writes | Reads | Changed functions |
|---|---|---|---|
| **`orders`** | status | — | `Store.SetStatus` |

</details>

<details>
<summary><b>Triggers</b> · 1</summary>

| Trigger | On table | When | Fired by this change | Where |
|---|---|---|---|---|
| ⚡ `orders_stamp` | `orders` | AFTER UPDATE OF status | unknown (its function was not scanned or has no known effect) | [schema.sql:4](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/schema.sql#L4) |

</details>

<details>
<summary><b>Changed functions</b> · 1</summary>

**1** changed function · **1** entrypoint · writes **1** table · reads **0** tables

| Changed function | Where | Entrypoints | Writes | Reads |
|---|---|---|---|---|
| `store.Store.SetStatus` | [store/store.go:11](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/store/store.go#L11-L13) | `PATCH /orders/:id/status` | `orders (status)` | — |

</details>

<sub>[BizMap](https://github.com/lucas-simao/bizmap/blob/main/docs/action.md) X.Y.Z · deterministic static analysis, no AI · commit [`0123456`](https://github.com/acme/shop/commit/0123456789abcdef0123456789abcdef01234567) · updated on every push</sub>
