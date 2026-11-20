# BizMap graph schema (v0.3.9)

`bizmap scan` writes `bizmap.graph.json`. The graph is fully deterministic: every
node and edge is produced from source text (SQL, tree-sitter ASTs, comments, or
an explicit `bizmap.yaml` override). There is no LLM in the loop. List
attributes (`unresolved`, `unresolved_http`, `unresolved_calls`, `candidates`,
…) are sorted so identical input is byte-identical across `PYTHONHASHSEED`
values except `generated_at`.

## Top-level object

| Field | Type | Meaning |
| --- | --- | --- |
| `version` | string | Graph format version (`"0.3.9"`). |
| `generated_at` | string | UTC timestamp. |
| `schema` | object | Reconstructed database schema (`schema.tables`). |
| `nodes` | array | Graph nodes (see below). |
| `edges` | array | Graph edges (see below). |
| `repo` | string? | Scan-root name (`bizmap.yaml` `name:`). |
| `database` | string? | Shared-database key. Matching values share table/collection ids on merge. |
| `repos` | array? | Repo names included in a merged registry graph. |
| `repo_roots` | object? | Repo name → its directory. Inside a git work tree (`repo_roots_base: git`) the path is relative to the git root, like diff paths, so per-repo scans + `registry merge` equal the joint scan. Outside git (`repo_roots_base: scan`) it is relative to the scan path (`.` for each separately scanned repo, so merge == joint does **not** hold there and `bizmap impact --prefix` is needed). |
| `repo_roots_base` | string? | `git` or `scan`: what `repo_roots` is relative to. |
| `services` | object? | Service name → base URL, used to match HTTP client calls. |
| `queues` | object? | Queue name → consumer hint, copied from `bizmap.yaml` for rematch. |
| `unresolved_http` | array? | Unmatched outbound HTTP calls (`url`, `method`, `keys`, `from`, `reason`). `reason` is one of `target_not_scanned` (URL fully composed but no matching route in scanned repos), `base_unknown` (path known, base not resolvable), `path_dynamic` (path not composable), `client_unknown`. Stderr counts this list and prints per-reason totals. |
| `warnings` | array? | Non-fatal scan notes (for example Go import-path suffix fallback when `go.mod` is missing). Printed by `bizmap scan`. |

### `schema.tables.<name>`

```json
{
  "columns": {
    "status": { "type": "TEXT", "nullable": false, "default": "'pending'" }
  },
  "foreign_keys": [
    { "name": "orders_user_id_fkey", "columns": ["user_id"], "ref_table": "users", "ref_columns": ["id"] }
  ],
  "rls": true,
  "policies": [
    { "name": "orders_tenant_select", "command": "SELECT", "role": "app_user" }
  ]
}
```

## Nodes

Every node has `id` and `type`. Extra fields depend on `type`.

| `type` | `id` pattern | Extra fields |
| --- | --- | --- |
| `entrypoint` | `entrypoint:http:METHOD:/path` or `entrypoint:<kind>:<key>` | `kind` (`http` \| `scheduled_job` \| `firestore` \| `celery_task` \| `worker` \| `pubsub` \| `cloud_task` \| `firebase_http` \| `firebase_call`), `method`, `path`, `symbol`, `why`, `evidence`, optional `confidence`, `candidates`, `unresolved` (const path not in the scan), optional `unresolved_calls`, optional `collection` / `event`, optional `dynamic_table` / `dynamic_table_unknown` (flag/count when a reachable origin writes `table:unknown`; never a reach edge) |
| `function` | `function:<qualname>` | `name`, `package`, optional `import_path`, `language`, `why`, `evidence`, optional `end_line` (last line of the definition) and `start_line` (first line, when it differs from `evidence.line`; for an inline route handler `evidence.line` is where the registration call starts — in a chain like `new Hono().post(a).post(b)` or `router.route('/x').get(a)`, the chain's first line — and `start_line`/`end_line` are the handler function's own lines), optional `parent` (enclosing factory/function for members and route closures), optional `role` (`schema` for Alembic/migrations), optional `http_calls` (stashed client URLs for registry rematch), optional `queue_events`, optional `unresolved_calls` (interface/protocol with no scanned implementer), optional `unresolved_http`, optional `route_literals` (JS/TS API-looking path strings `{path, method?, evidence}` kept for registry rematch) |
| `table` | `table:<name>` | `name`; optional `external` (system catalogs / `storage.*` / `vault.*` / `auth.*`), `kind` (`external_relation` \| `unresolved_relation`), `dropped` (still referenced after `DROP TABLE`), `candidates` (dynamic/`unknown`), `rls`, `policies` |
| `view` | `view:<name>` | `name`, `kind` (`view` \| `unresolved_relation`), optional `materialized` |
| `column` | `column:<table>.<column>` | `table`, `name`, `data_type`, `nullable`, optional `why` |
| `db_trigger` | `db_trigger:<name>` | `name`, `table`, `timing`, `events`, `function`, `evidence` |
| `collection` | `collection:<name>` | `name` (Firestore collection) |
| `queue` | `queue:<kind>:<name>` | `kind` (`celery` \| `rq` \| `cloud_tasks` \| `pubsub` \| `firestore`), `name` |

On a merged registry graph, function / entrypoint / queue ids are prefixed
with the repo (`catalog/function:db.create_order`). Table, column, view,
collection, and `db_trigger` ids stay unprefixed when every contributing
graph declares the same `database`.

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
| `calls` | `function` | `function` | Intra-project call (handler → service → repository). Interface dispatch uses `inferred` and may list `candidates`. `supabase.rpc` calls `function:sql.<name>`. |
| `reads` | `function` / `entrypoint` / `db_trigger` | `column`, `table`, or `collection` | SQL, ORM, or Firestore reads this data. |
| `writes` | `function` / `entrypoint` / `db_trigger` | `column`, `table`, or `collection` | SQL, ORM, or Firestore writes this data. |
| `triggers` | `entrypoint` or `db_trigger` (or a collection writer) | `function` / `entrypoint` | HTTP route / ticker job / DB trigger / Firestore writer invokes a function. |
| `calls_http` | `function` | `entrypoint` | HTTP client URL matched a scanned route (same repo or via `services:` env/host/URL). |
| `enqueues` | `function` | `queue` | Celery/RQ/Cloud Tasks/Pub/Sub produce site. |
| `derives_from` | `view` | `table` or `view` | View (or materialized view) is defined from these base relations. |

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

Optional `via` records how an edge was derived: a write propagated through a
database trigger, or `route_literal` on a `calls_http` edge that was matched
from an API-looking string literal (see below) rather than a traced client.
Optional `situation` is a list of const/iota names observed at call sites
(for example `audit.AppointmentCanceled`) so `bizmap why` can say *when* a
column is written.

### Route literals (`calls_http` with `via: route_literal`)

Front-end clients are often built through layers static analysis cannot
follow (DI containers, registries, factories of factories). As a fallback,
every JS/TS string or template that looks like an API path — `'/orders/' +
id`, `` `${base}/orders/${id}` `` — is recorded on its enclosing function and
matched against scanned routes, in the same repo or across a joint scan:

- Navigation and non-API strings are ignored: route registration arguments,
  `navigate()` / `router.push()` / `redirect()`, `<Link to>` and other JSX
  attributes, `path:` / `href:` / `to:` object keys, comparisons and
  `startsWith()`, arrays of paths, imports, and asset files (`/logo.png`).
- Only when the call's **base is unknown**. If a traced client call for the
  same path has a concrete base (env var placeholder or absolute URL), the
  literal is ignored: that call is either matched to its service or stays
  `unresolved_http` (`target_not_scanned`). The discarded candidates are kept
  on the function as `route_literal_skipped` (`reason: base_known` with the
  base and service, or `reason: ambiguous`).
- A same-length template match is accepted. A *suffix* match (the client base
  carried `/api/v1`) needs 2+ literal segments or a method hint from the
  surrounding call (`.get(...)`, `fetch(url, { method: 'POST' })`,
  `{ url, method }`).
- The edge is created only when **exactly one** route matches, and never to
  a route registered in the same file. Ambiguous literals link to nothing.
- A bare `fetch('/x')` is page-relative and only links within its own repo.
- A function that only returns the path (a path helper) is never the origin;
  a call to it at an HTTP call site is the literal of the calling function.
- If the call's base resolves to a scanned service and exactly one route of
  that service matches, the edge is `inferred`.
- Otherwise edges are `heuristic` with `match: full|suffix`; the matching
  `unresolved_http` entry is dropped.

### SQL by reference (`via: sql_reference` / `sql_key`, Python)

SQL kept in a module constant or dict and handed to an executor the
detectors cannot resolve (dependency injection, a service object, a generic
runner) is attributed to the function that **names** it:
`QUERIES["top"]`, `QUERIES.get("top")`, `queries.QUERIES["top"]`,
`SALES_SQL`, `sql.SALES_SQL`. A string argument equal to the key of exactly
one SQL dict entry (`store.named("sales_by_day")`, keys with `_`/`.`/`-`/`:`
and 5+ characters, not inside logging calls) counts as `via: sql_key`.
These reads/writes are `inferred` and propagate to entrypoints like any other.
They are skipped when the function already executes that SQL, or reaches a
function that does through the call graph, so exact edges stay exact.

## Confidence

| Value | When |
| --- | --- |
| `exact` | Fully static SQL (string literals / const concatenation, including cross-package `pkg.Const`) parsed by sqlglot, or a route/job registration whose handler resolved via type. |
| `inferred` | Call-graph propagation (table-level), `SELECT *` (table-level, or columns with `--expand-star`), INSERT without a column list, interface/object dispatch to a structural implementer or constructor-assigned type, or a writer of table T also writing what a trigger on T writes. |
| `heuristic` | A weaker signal than a traced call: a JS/TS path literal matched to a single route while its client/base could not be followed (`via: route_literal`). |
| `unknown` | Dynamically built SQL (`"UPDATE t SET " + col` or `"UPDATE "+table+" SET"`), unparsed SQL, an unresolvable route path (`{otherpkg.PendingPath}/setup`, with `unresolved: true`), or an ambiguous handler (candidates listed, no arbitrary pick). FROM-less SELECT is not unknown: it emits no table edge. |

When the same edge is observed twice, the **highest** confidence is kept
(`exact` > `inferred` > `heuristic` > `unknown`).

### Services and cross-repo HTTP

When a client URL's base resolves to a `services:` entry (by env var
placeholder, host, or URL prefix) and the graph spans several repos, it only
matches routes of **that** service's repo (the entry's `repo:`, else its key).
If that service was not scanned, nothing matches and the call stays
`unresolved_http` with `reason: target_not_scanned` and `service: <name>`; it
never falls through to a same-path route in another repo.

## Schema replay

DDL files (prefer `schema.sql`, then `**/migrations/**`, then other `*.sql`) are
applied in order. Supported PostgreSQL constructs:

- `CREATE TABLE IF NOT EXISTS`
- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `DROP COLUMN`
- `ALTER TABLE ... ADD/DROP CONSTRAINT` (including `FOREIGN KEY`)
- `DO $$ ... $$` blocks (inner `ALTER`/`CREATE` is applied)
- `CREATE TRIGGER` + `CREATE FUNCTION` (quoted / schema-qualified pg_dump
  identifiers; plpgsql body scanned for `INSERT`/`UPDATE`/`DELETE`/`SELECT`
  after comments, string literals, DECLARE variables, and INTO targets are
  stripped). Function bodies are **re-analyzed after the full replay** so
  `CREATE FUNCTION` that appears before `CREATE TABLE` still records those
  tables. `EXECUTE format('UPDATE %I ...')` is treated as dynamic SQL.
- `CREATE VIEW` / `CREATE MATERIALIZED VIEW` (and Alembic `op.execute` of the
  same) as `view` nodes with `derives_from` edges
- `DROP TABLE` (the table is marked `dropped: true` when it is still referenced)
- `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`
- `CREATE POLICY ... ON table` (name, command, role, `USING` / `WITH CHECK`)
- Alembic Python revisions (`op.create_table`, `add_column`, `drop_*`, `execute`) applied in `down_revision` order; those functions are `role: schema` and listed by `why` as **defined/altered by**, not runtime writers

Table nodes are created for reconstructed DDL tables and for system catalogs
(`pg_roles`, `pg_catalog.*`, `information_schema.*`, `pg_*`, `storage.*`,
`vault.*`, `auth.*`) marked `external` (`kind: external_relation`).
Relations named in FROM/JOIN/INSERT/UPDATE that are not in DDL become
`unresolved_relation` nodes (views on read, tables on write) with edges, so
a route still shows `reads view X`. CTE / subquery / LATERAL aliases
(`l`, `r`, `l.x`, `files.file_id`), SQL keywords, and PL/pgSQL keywords /
DECLARE'd variables are never tables. FROM-less SELECT (`SELECT now()`)
touches no table. Dynamic table names (`"UPDATE "+t+" SET"`, `%I` holes)
always become `table:unknown` with confidence `unknown` and `candidates`
when determinable, and stay on the origin function only. HTTP/job
entrypoints that can reach that origin get `dynamic_table: true` and
`dynamic_table_unknown` (count), never a `reads`/`writes` edge to
`table:unknown`. Concat/helper holes (`{helper(o)}`) are not columns.

## Overrides (`bizmap.yaml`)

Uncertain bits can be completed manually. `bizmap.yaml` is loaded from
`--config path`, else the scan root, else the git toplevel. Top-level
`exclude` globs skip files (seed/demo trees) inside the scan root;
`bizmap scan --exclude GLOB` (repeatable) is merged with that list.
`expand_star: true` expands `SELECT *` the same way as the CLI flag.
`--profile` prints parse / resolve / sql / propagate timings to stderr.

```yaml
name: catalog
database: platform
services:
  catalog: http://catalog.internal
  billing:
    url: http://billing.internal
    env: BILLING_URL
queues:
  ingest: ingest
  jobs: on_jobs
  # entrypoint id or route — links a producer without an extracted http_url
  # jobs-queue: entrypoint:http:POST:/jobs/drain
repos:
  - catalog
  - worker
constants:
  PathPrefix: /api/v1
  shared.Base: /items
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
large graphs). A multi-repo / joint scan also draws `calls_http` edges
between services (function or entrypoint in repo A → entrypoint in repo B),
one subgraph per repo. `--mermaid-focus TABLE` shows functions, columns,
views, and entrypoints around one table or view. `--mermaid-limit N` caps
node count.

`bizmap scan` prints `HTTP unmatched by reason: …` then
`N unresolved HTTP calls (ENV, host, …)` after the graph write when client
URLs did not match a scanned route or `services:` entry. Each entry has a
`reason`: `target_not_scanned`, `base_unknown`, `path_dynamic`, or
`client_unknown`. The same list is stored as top-level `unresolved_http`
(and on each caller) so the count matches the graph. Declare the missing
env/host in `services:` to resolve them. `bizmap registry merge` re-runs
HTTP/queue matching from stored `http_calls`.
