# BizMap graph schema (v0.9.1)

`bizmap scan` writes `bizmap.graph.json`. The graph is fully deterministic: every
node and edge is produced from source text (SQL, tree-sitter ASTs, comments, or
an explicit `bizmap.yaml` override). There is no LLM in the loop. List
attributes (`unresolved`, `unresolved_http`, `unresolved_calls`, `candidates`,
…) are sorted so identical input is byte-identical across `PYTHONHASHSEED`
values except `generated_at`.

## Top-level object

| Field | Type | Meaning |
| --- | --- | --- |
| `version` | string | Graph format version (`"0.9.1"`). |
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
| `skipped` | object? | How many paths the scan left out, per reason: `generated_dirs`, `generated_files`, `gitignored`. |
| `skipped_paths` | object? | The same paths, per reason, sorted (at most 200 per reason; `skipped` has the full count). In a merged graph each path is prefixed with `repo:`. |
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
| `function` | `function:<qualname>` | `name`, `package`, optional `import_path`, `language`, `why`, `evidence`, optional `end_line` (last line of the definition) and `start_line` (first line, when it differs from `evidence.line`; for an inline route handler `evidence.line` is where the registration call starts — in a chain like `new Hono().post(a).post(b)` or `router.route('/x').get(a)`, the chain's first line — and `start_line`/`end_line` are the handler function's own lines), optional `parent` (enclosing factory/function for members and route closures), optional `role` (`schema` for Alembic/migrations), optional `http_calls` (stashed client URLs for registry rematch), optional `queue_events`, optional `unresolved_calls` (interface/protocol with no scanned implementer), optional `unresolved_http`, optional `route_literals` (JS/TS API-looking path strings `{path, method?, evidence}` kept for registry rematch), optional `params` (the parameters in order: `[{name, type?, optional?}]`, `[]` when there are none) and `returns` (the return type's text) — see below |
| `table` | `table:<name>` | `name`; optional `db` (database the code reaches it through, see [Data flow](#data-flow-between-tables-feeds)), `db_candidates`, `same_as`, `external` (system catalogs / `storage.*` / `vault.*` / `auth.*`), `kind` (`external_relation` \| `unresolved_relation`), `dropped` (still referenced after `DROP TABLE`), `candidates` (dynamic/`unknown`), `rls`, `policies` |
| `view` | `view:<name>` | `name`, `kind` (`view` \| `unresolved_relation`), optional `materialized` |
| `column` | `column:<table>.<column>` | `table`, `name`, `data_type`, `nullable`, optional `why` |
| `db_trigger` | `db_trigger:<name>` | `name`, `table`, `timing`, `events`, `function`, `evidence`; optional `columns` (the `UPDATE OF col, …` list, present only when the trigger has that clause) |
| `collection` | `collection:<name>` | `name` (Firestore collection: the **last** collection of its path, so `customers/{id}/notes` is `collection:notes`, as in Python); optional `path` (the full path with `{}` for ids, from JS/TS) / `paths`, `db` (`firestore:<project\|default>`), `same_as` |
| `queue` | `queue:<kind>:<name>` | `kind` (`celery` \| `rq` \| `cloud_tasks` \| `pubsub` \| `firestore` \| `topic`), `name` |

On a merged registry graph, function / entrypoint / queue ids are prefixed
with the repo (`catalog/function:db.create_order`). Table, column, view,
collection, and `db_trigger` ids stay unprefixed when every contributing
graph declares the same `database`.

Go `qualname` is always the directory path relative to the scan root plus the
type and method (`cmd/server.main`, `controllers/orders.Handler.Create`,
`internal/services/appointments.Service.Run`). Functions also store the full
Go import path.

`params` / `returns` are the function's own signature text, read from the code
(Go, Python, TypeScript; plain JavaScript gives the names only). A type is the
code's text with whitespace collapsed, at most 120 characters (cut with `…`);
`type` is absent for a parameter without annotation and `returns` for a
function without a return type. Go leaves the receiver out (it is in the name)
and writes a variadic as `...string`; Python leaves `self` / `cls` and the bare
`*` / `/` out and keeps `*args` / `**kwargs`; defaults are not kept; a
destructured TS/JS parameter is named by its pattern (`{ page, size }`). Both
fields are optional: older graphs stay valid.

```json
{"id": "function:src.api.orders.ordersClient.get", "type": "function",
 "params": [{"name": "id", "type": "string"}, {"name": "opts", "type": "{ expand?: boolean }", "optional": true}],
 "returns": "Promise<Order>"}
```

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
| `writes` | `function` / `entrypoint` / `db_trigger` | `column`, `table`, or `collection` | SQL, ORM, or Firestore writes this data. A JS/TS Firestore write has `fields` (the keys of the object written). |
| `triggers` | `entrypoint` or `db_trigger` (or a collection writer, or a `queue`) | `function` / `entrypoint` | HTTP route / ticker job / DB trigger / Firestore writer invokes a function. `queue:pubsub:<topic> -> entrypoint` with `via: push_subscription`: a Pub/Sub push subscription delivers the topic to that route (see [Firestore in JS/TS](#firestore-in-jsts-and-pubsub-push)). |
| `calls_http` | `function` | `entrypoint` | HTTP client URL matched a scanned route (same repo or via `services:` env/host/URL). |
| `enqueues` | `function` | `queue` | Celery/RQ/Cloud Tasks/Pub/Sub produce site. JS/TS `pubsub.topic(t).publishMessage({ json })` / `publishJSON(obj)` has `fields` (the message keys). |
| `derives_from` | `view` | `table` or `view` | View (or materialized view) is defined from these base relations. |
| `feeds` | `table` / `view` | `table` or `collection` | Values read from one table are written to the other (often in another database). See [Data flow](#data-flow-between-tables-feeds). |

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
database trigger (`db_trigger:NAME`), or `route_literal` on a `calls_http` edge that was matched
from an API-looking string literal (see below) rather than a traced client.
A read/write copied from the function that runs the SQL up to its callers and
entrypoints carries `derived: "call"`; edges without `derived` are the
function's own access. Direct writes carry `ops`, the SQL operations they
come from (`["INSERT"]`, `["UPDATE"]`, `["DELETE"]`; an upsert gives both
INSERT and UPDATE). A DB trigger fires only on a direct write whose `ops`
match its events (and, for `UPDATE OF col`, that writes one of those
columns); its effect is a table-level `writes` with
`via: db_trigger:NAME` on the firing function, and also `derived: "call"` on
its callers and entrypoints.
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

## Data flow between tables (`feeds`)

`feeds` says a function **copies values** from one table into another, for
example a service that reads `pedidos` in the orders database and upserts
`resumo_pedido` in the summary database. It is a text pass over the function
bodies the detectors already located; it never changes their `reads` /
`writes` edges, and a graph with no such copy is unchanged (apart from `db`).

```json
{
  "id": "feeds:table:pedidos->table:resumo_pedido",
  "type": "feeds",
  "from": "table:pedidos",
  "to": "table:resumo_pedido",
  "confidence": "exact",
  "evidence": { "file": "sync/orders.go", "line": 21, "symbol": "sync.SyncOrders" },
  "via": [
    { "path": ["function:sync.SyncOrders"], "confidence": "exact",
      "evidence": { "file": "sync/orders.go", "line": 21, "symbol": "sync.SyncOrders" } }
  ],
  "via_total": 1,
  "column_map": [
    { "from": "pedidos.valor_centavos", "to": "resumo_pedido.total",
      "fn": "function:sync.SyncOrders", "confidence": "exact",
      "evidence": { "file": "sync/orders.go", "line": 21, "symbol": "sync.SyncOrders" } }
  ]
}
```

A `feeds` edge is only created when a value is **seen** going from the read
to the write:

| How | `confidence` |
| --- | --- |
| In one function: the rows of `SELECT id, valor FROM pedidos` (Go `rows.Scan(&a, &o.Total)`, `QueryRow(...).Scan`; JS/TS `const { rows } = await a.query(...)`, `for (const p of rows)`, `(await a.query(...)).rows[0]`; Python `cur.execute(...)` then `fetchone()` / `fetchall()` / `for row in cur`, `row["valor"]`, `row.valor`, `row[1]`) reach the arguments of an `INSERT` / `UPDATE` / upsert, a Firestore `set` / `update` / `add`, or an ORM write the detectors saw on that line (`upsert({ update: { total: p.valor } })`). Struct / object literals, assignments and lists (`append`, `push`) are followed. `INSERT … SELECT` and `UPDATE … FROM` map in one statement (unqualified columns with a single table in `FROM` belong to it), also in a SQL function body (`CREATE FUNCTION`), even when the detectors recorded only the write. | `exact` |
| Through calls in the same service: a reader passes the value to a writer (`s.writeSummary(ctx, o)`), or a writer uses what a reader returns (`orders, _ := repo.ListPaid(ctx)`), up to 2 calls deep; Celery / RQ `task.delay(...)` to the task. | `heuristic` (edge; a column passed through unchanged stays `exact`) |
| From a Firestore document: `fs.collection("orders").document(id).get().to_dict()`, `snap.data()`, `s.to_dict()` for `s in ….stream()`, `getDoc(doc(db, 'orders', id))`, a trigger's `event.data.after.data()`. Its fields are the source "columns" (`orders.customerId`, case kept, no schema needed); `snap.id`, `event.params.<id>` and the id passed to `.document(id)` are `orders.__id__`. | as above |
| Through a queue: the producer publishes a message built from the source (`queue.publish('pedido-pago', { orderId: o.id })`), the consumer of the same topic (`subscribe` / `consume` / `process` / BullMQ `Worker`) writes the message's fields. Also across repos in a joint scan or `registry merge`. `emit` / `on` events only link inside one repo. | `heuristic` |

- `via`: up to 5 paths (functions, and the queue for a queue hop), strongest
  first; `via_total` counts every path.
- `column_map`: `{from, to, fn, confidence, evidence}` per mapped column.
  `exact` when the value goes through unchanged (in one function, or passed
  through calls as it is); `heuristic` through a queue or a transformation. `transform: true` when an expression or function
  is in between (`sum(valor)`, `date_trunc('month', criado_em)`,
  `Math.round(p.valor * 100)`, `decimal.NewFromFloat(o.Valor)`); `path` for
  a value that crossed calls or a queue; `field` for the message field of a
  queue hop. `match: "name"` (heuristic) is used only when a row was copied
  whole and no column path was seen: columns with the same name on both
  sides (not `id` / `created_at` / `updated_at`).
- `__id__` is a Firestore document's id. Like any identifier copied into an
  identifier column, it is a reference: a flow made only of ids is not a
  `feeds`; next to other fields it is mapped (`orders.__id__ ->
  order_summary.order_id`).
- Never invented: a column that is not in the source (or destination)
  table's schema is never mapped (a Firestore collection has no schema: a
  field read from its document counts) — a plpgsql parameter or `DECLARE`
  variable in `INSERT … SELECT found, store, s.name FROM stores s` is not
  `stores.found`; a column only in `WHERE` is not mapped; a function that
  reads A and writes B with no value in common (a permission check, a
  lookup) creates nothing; a copy into the same table, a flow made only of
  identifiers into identifier columns (`users.id -> orders.user_id`), or only
  into columns declared as foreign keys to the source, is a reference, not a
  `feeds`.
- Queue producers and consumers keep what they publish / write per message
  field on the function node (`dataflow_out` / `dataflow_in`), so a
  `registry merge` can link them across repos. When a topic has no queue
  node yet, a `queue:topic:<name>` node (`kind: topic`) is added.

### Database identity (`db`)

Table ids do not change: `db` is an attribute. It is set on a table / view
when the code says which connection reaches it, and left out otherwise (the
table is in the repo's database, as before):

- a connection built from a DSN env var or config field —
  `sql.Open("pgx", os.Getenv("ORDERS_DATABASE_URL"))`,
  `pgxpool.New(ctx, cfg.OrdersDatabaseURL)`,
  `new Pool({ connectionString: process.env.SUMMARY_DATABASE_URL })`,
  `create_engine(settings.ORDERS_DATABASE_URL)`, `psycopg2.connect(os.environ["X"])`
  — is named after the variable (`ORDERS_DATABASE_URL` → `orders_db`; a bare
  `DATABASE_URL` names nothing), or by `databases:` in `bizmap.yaml`. Only a
  variable that looks like a DSN (`…_URL`, `…_URI`, `…_DSN`, `…_CONNECTION`)
  names a database: `DB_SCHEMA`, `DB_HOST`, `DB_USER`, `DB_PASSWORD` do not.
  `DB_NAME` (or `PGDATABASE`, `POSTGRES_DB`) given to the connector with a
  literal default (`os.getenv("DB_NAME", "orders")`) uses that default;
- each SQL edge's call (`src.QueryContext`, `b.query`, `cur.execute`, through
  `cursor()` / `BeginTx` / `connect()` / `with … as` aliases) picks the
  connection;
- a repo with exactly one named connection puts on it the tables its code
  reads or writes (a table only in the schema gets no `db`);
- a table reached through two connections gets `db_candidates` instead;
- Firestore clients (`firestore.NewClient`, `getFirestore()`,
  `firestore.client()`) put collections on `firestore:<project>` (a literal
  project id) or `firestore:default`.

In a merged graph, the same table name with the same `db` in two repos gets
`same_as` (the other node ids) on each node; nodes are not merged.

## Firestore in JS/TS and Pub/Sub push

A text pass over the JS/TS files that import Firestore (`firebase/firestore`,
`firebase-admin`, `@google-cloud/firestore`) or Cloud Functions, attributed to
the function the JS detector found around each call:

- web modular SDK: `getDoc` / `getDocs` / `onSnapshot` / `getCountFromServer`
  read, `setDoc` / `updateDoc` / `addDoc` / `deleteDoc` write, on
  `collection(db, …)`, `doc(db, …)`, `query(…)`, `collectionGroup(db, …)`, or a
  reference kept in a variable (`doc(ordersRef, id)`); `batch.set(ref, …)`,
  `tx.get(ref)`;
- admin / server SDK chains: `db.collection('x').doc(id).get()` /
  `.where(…).get()` read, `.set / .update / .delete / .create / .add` write;
- the collection from a literal, an exported constant (`ORDERS_COLLECTION`),
  an enum or object member (`Collections.Customers`), or a template path
  (`` `customers/${id}/notes` `` → `collection:notes`, `path:
  customers/{}/notes`). A name passed to a helper's parameter
  (`listAll('customers')` → `collection(db, path)`) puts the access on the
  caller, with `via` the helper. Callers and their entrypoints get the
  access with `derived: call`, as with SQL.
- Cloud Functions triggers (`onDocumentWritten('orders/{orderId}', …)`,
  `functions.firestore.document('orders/{id}').onWrite(…)`) become
  `entrypoint:firestore:<collection>:<event>` (as in Python), triggering a
  handler function that reads the collection; its `event.data.after.data()`
  is the document.
- `pubsub.topic('t').publishMessage({ json })` → `enqueues queue:pubsub:t`.

A **push subscription** (Pub/Sub delivering a topic to an HTTP route) lives in
deploy config, not in code. bizmap reads it from `push_subscriptions:` in
`bizmap.yaml` (see [configuration](configuration.md#push_subscriptions)) and
from `gcloud pubsub subscriptions create … --topic=T --push-endpoint=URL` in
shell / YAML / Makefile files of the repo, and adds `triggers
queue:pubsub:<topic> -> entrypoint` (`via: push_subscription`). The route's
handler then receives the message (its first parameter; methods on it,
`json.loads(base64.b64decode(…))` and Pydantic `model_validate(…)` keep it
the message), so in a joint scan or `registry merge` the trigger → topic →
route → write path becomes a `feeds` (`heuristic`). Message fields match the
written attribute by name ignoring case and `_` (`customerId` /
`customer_id`) or a Pydantic `Field(alias="…")`.

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
