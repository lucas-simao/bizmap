<!-- bizmap-impact -->
<!-- bizmap-risk: high -->
### BizMap impact · 🔴 High risk

No mapped function changed in this diff · **2** schema changes.

**Why high:**
- dropped column `orders.total_cents` while 1 entrypoint still uses it
- 1 other schema change (added)

<details open>
<summary><b>Schema changes</b> · 2</summary>

| Change | Where | Used by entrypoints | Functions |
|---|---|---|---|
| ⚠️ `orders.total_cents` **dropped** — still used | [db/migrations/0042_drop_total.sql:3](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/db/migrations/0042_drop_total.sql#L3) | `billing: POST /charges` | `billing: app.create_charge` |
| `orders.note` added | [db/migrations/0042_drop_total.sql:4](https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567/db/migrations/0042_drop_total.sql#L4) | — (not used yet) | — |

</details>

<sub>[BizMap](https://github.com/lucas-simao/bizmap/blob/main/docs/action.md) X.Y.Z · deterministic static analysis, no AI · commit [`0123456`](https://github.com/acme/shop/commit/0123456789abcdef0123456789abcdef01234567) · updated on every push</sub>
