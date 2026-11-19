# BizMap graph schema (v0.1.2)

`bizmap scan` writes `bizmap.graph.json`. The graph is fully deterministic: every
node and edge is produced from source text (SQL, tree-sitter ASTs, comments, or
an explicit `bizmap.yaml` override). There is no LLM in the loop.

## Top-level object

| Field | Type | Meaning |
| --- | --- | --- |
| `version` | string | Graph format version (`"0.1.2"`). |
| `generated_at` | string | UTC timestamp. |
| `schema` | object | Reconstructed database schema (`schema.tables`). |
| `nodes` | array | Graph nodes (see below). |
| `edges` | array | Graph edges (see below). |

### `schema.tables.<name>`

```json
{
  "columns": {
    "status": { "type": "TEXT", "nullable": false, "default": "'pending'" }
  },
  "foreign_keys": [
    { "name": "orders_user_id_fkey", "columns": ["user_id"], "ref_table": "users", "ref_columns": ["id"] }
  ]
}
```

## Nodes

Every node has `id` and `type`. Extra fields depend on `type`.

| `type` | `id` pattern | Extra fields |
| --- | --- | --- |
| `entrypoint` | `entrypoint:http:METHOD:/path` or `entrypoint:job:<symbol>` | `kind` (`http` \| `scheduled_job`), `method`, `path`, `symbol`, `why`, `evidence`, optional `confidence`, `candidates`, `unresolved` (const path not in the scan) |
| `function` | `function:<qualname>` | `name`, `package`, optional `import_path`, `language`, `why`, `evidence` |
| `table` | `table:<name>` | `name`; optional `external` (system catalogs), `candidates` (dynamic/`unknown`) |
| `column` | `column:<table>.<column>` | `table`, `name`, `data_type`, `nullable`, optional `why` |
| `db_trigger` | `db_trigger:<name>` | `name`, `table`, `timing`, `events`, `function`, `evidence` |

Go `qualname` is always the directory path relative to the scan root plus the
type and method (`cmd/server.main`, `controllers/orders.Handler.Create`,
`internal/services/appointments.Service.Run`). Functions also store the full
Go import path.

`why` is copied only from a comment or docstring immediately above the function,
or from `bizmap.yaml`. BizMap never invents business intent.

HTTP entrypoints that cannot be bound to a single handler (name collision, no
type information) keep `confidence: unknown` and list `candidates` instead of
picking an arbitrary match.

## Edges

| `type` | From | To | Meaning |
| --- | --- | --- | --- |
| `calls` | `function` | `function` | Intra-project call (handler → service → repository). |
| `reads` | `function` / `entrypoint` / `db_trigger` | `column` or `table` | SQL (or trigger function) reads this data. |
| `writes` | `function` / `entrypoint` / `db_trigger` | `column` or `table` | SQL (or trigger function) writes this data. |
| `triggers` | `entrypoint` or `db_trigger` | `function` | HTTP route / ticker job / DB trigger invokes a function. |

Column-level `reads`/`writes` are emitted only when the SQL names columns.
`SELECT *` and INSERT without a column list are **table-level** (unless
`bizmap scan --expand-star`, which expands `SELECT *` at the SQL origin only).
Call-graph propagation to callers and HTTP/job entrypoints is always
table-level, so a star query is not copied as every column to every caller.

Every edge has:

```json
{
  "id": "writes:function:repository.Repo.InsertOrder->column:orders.status",
  "type": "writes",
  "from": "function:repository.Repo.InsertOrder",
  "to": "column:orders.status",
  "confidence": "exact",
  "evidence": { "file": "repository/repository.go", "line": 112, "symbol": "repository.Repo.InsertOrder" }
}
```

Optional `via` records that a write was propagated through a database trigger.
Optional `situation` is a list of const/iota names observed at call sites
(for example `audit.AppointmentCanceled`) so `bizmap why` can say *when* a
column is written.

## Confidence

| Value | When |
| --- | --- |
| `exact` | Fully static SQL (string literals / const concatenation, including cross-package `pkg.Const`) parsed by sqlglot, or a route/job registration whose handler resolved via type. |
| `inferred` | Call-graph propagation (table-level), `SELECT *` (table-level, or columns with `--expand-star`), INSERT without a column list, or a writer of table T also writing what a trigger on T writes. |
| `unknown` | Dynamically built SQL (`"UPDATE t SET " + col` or `"UPDATE "+table+" SET"`), unparsed SQL, an unresolvable route path (`{otherpkg.PendingPath}/setup`, with `unresolved: true`), or an ambiguous handler (candidates listed, no arbitrary pick). FROM-less SELECT is not unknown: it emits no table edge. |

When the same edge is observed twice, the **highest** confidence is kept
(`exact` > `inferred` > `unknown`).

## Schema replay

DDL files (prefer `schema.sql`, then `**/migrations/**`, then other `*.sql`) are
applied in order. Supported PostgreSQL constructs:

- `CREATE TABLE IF NOT EXISTS`
- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `DROP COLUMN`
- `ALTER TABLE ... ADD/DROP CONSTRAINT` (including `FOREIGN KEY`)
- `DO $$ ... $$` blocks (inner `ALTER`/`CREATE` is applied)
- `CREATE TRIGGER` + `CREATE FUNCTION` (plpgsql body scanned for `INSERT`/`UPDATE`/`DELETE`/`SELECT`)

Table nodes are created for reconstructed DDL tables, for static SQL that
names a real identifier, and for system catalogs (`pg_roles`, …) marked
`external`. CTE / subquery / LATERAL aliases (`l`, `files.file_id`) and SQL
keywords are never tables. FROM-less SELECT (`SELECT now()`) touches no
table. Dynamic table names become `table:unknown` with `candidates` when
determinable, and stay on the origin function only.

## Overrides (`bizmap.yaml`)

Uncertain bits can be completed manually. `bizmap.yaml` is loaded from
`--config path`, else the scan root, else the git toplevel. Top-level
`exclude` globs skip files (seed/demo trees) inside the scan root.
`expand_star: true` expands `SELECT *` the same way as the CLI flag.

```yaml
exclude:
  - cmd/seed
  - demo/
expand_star: false
overrides:
  columns:
    inventory.qty:
      why: "On-hand units; decremented when an order item is inserted."
      writers:
        - symbol: repository.Repo.InsertOrder
          confidence: inferred
```

## Mermaid

`bizmap scan --mermaid` writes an **entrypoint → table** rollup (readable on
large graphs). `--mermaid-focus TABLE` shows functions, columns, and
entrypoints around one table. `--mermaid-limit N` caps node count.
