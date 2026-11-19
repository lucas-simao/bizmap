# BizMap graph schema (v0.1)

`bizmap scan` writes `bizmap.graph.json`. The graph is fully deterministic: every
node and edge is produced from source text (SQL, tree-sitter ASTs, comments, or
an explicit `bizmap.yaml` override). There is no LLM in the loop.

## Top-level object

| Field | Type | Meaning |
| --- | --- | --- |
| `version` | string | Graph format version (`"0.1"`). |
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
| `entrypoint` | `entrypoint:http:METHOD:/path` or `entrypoint:job:<symbol>` | `kind` (`http` \| `scheduled_job`), `method`, `path`, `symbol`, `why`, `evidence` |
| `function` | `function:<qualname>` | `name`, `package`, `language`, `why`, `evidence` |
| `table` | `table:<name>` | `name` |
| `column` | `column:<table>.<column>` | `table`, `name`, `data_type`, `nullable`, optional `why` |
| `db_trigger` | `db_trigger:<name>` | `name`, `table`, `timing`, `events`, `function`, `evidence` |

`why` is copied only from a comment or docstring immediately above the function,
or from `bizmap.yaml`. BizMap never invents business intent.

## Edges

| `type` | From | To | Meaning |
| --- | --- | --- | --- |
| `calls` | `function` | `function` | Intra-project call (handler → service → repository). |
| `reads` | `function` / `entrypoint` / `db_trigger` | `column` or `table` | SQL (or trigger function) reads this data. |
| `writes` | `function` / `entrypoint` / `db_trigger` | `column` or `table` | SQL (or trigger function) writes this data. |
| `triggers` | `entrypoint` or `db_trigger` | `function` | HTTP route / ticker job / DB trigger invokes a function. |

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

## Confidence

| Value | When |
| --- | --- |
| `exact` | Fully static SQL (string literals / same-package const concatenation) parsed by sqlglot, or a route/job registration seen in the AST. |
| `inferred` | Call-graph propagation (a caller of the SQL site), `SELECT *` expanded from the reconstructed schema, INSERT without a column list, or a writer of table T also writing what a trigger on T writes. |
| `unknown` | Dynamically built SQL (`"UPDATE t SET " + col`), unparsed SQL, or a table-level access when the column list could not be determined. |

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

## Overrides (`bizmap.yaml`)

Uncertain bits can be completed manually next to the scanned tree:

```yaml
overrides:
  columns:
    inventory.qty:
      why: "On-hand units; decremented when an order item is inserted."
      writers:
        - symbol: repository.Repo.InsertOrder
          confidence: inferred
```
