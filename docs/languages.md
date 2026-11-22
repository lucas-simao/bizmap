# Languages and detectors

BizMap parses code with [tree-sitter](https://tree-sitter.github.io/) and SQL
with [sqlglot](https://github.com/tobymao/sqlglot). Nothing is executed and
nothing leaves your machine.

## What it recognises

| | Routes / triggers | Data access | Outbound HTTP |
| --- | --- | --- | --- |
| **Go** | Echo and gin (groups, const paths, wrappers), routes mounted through a helper (`e.POST(Prefix+job.Name(), …)`, expanded per call site), Pub/Sub publishers (also wrappers and topics kept in a struct field), ticker/cron jobs | pgx, `database/sql`, sqlx, Firestore (`cloud.google.com/go/firestore`) | `net/http` (env/const/`Sprintf` URLs) |
| **Python** | FastAPI, Flask (routers, blueprints, factories), Celery, RQ, Cloud Tasks, Pub/Sub, Firebase Functions, `while True` workers | SQLAlchemy Core/ORM, raw SQL, SQL dicts/constants, Firestore, Supabase | httpx, requests, aiohttp |
| **JS / TS** | Express, Fastify, Hono (chains, mounts), Supabase Edge Functions, Firebase Functions (`onCall`, `onRequest`, `onSchedule`, `onMessagePublished`, `onTaskDispatched`, `beforeUser*`, Firestore triggers, and the v1 forms) keyed by their deployed name (`orders-markPaid`) | `pg`/`postgres`, Supabase, Firestore (Admin and Web SDK) | fetch, axios, ky, ofetch, got (instances, factories, `tsconfig` paths); `httpsCallable(fns, 'orders-markPaid')` linked to the callable function |
| **SQL** | `CREATE TRIGGER` | schema replayed from `schema.sql` / migrations / Alembic, views, RLS policies | — |

## Databases

| Database | What is read |
| --- | --- |
| **PostgreSQL** | `CREATE TABLE` / `ALTER TABLE` (including pg_dump output, quoted and schema-qualified names), migration folders replayed in order, views and materialized views, `CREATE TRIGGER` and the tables their PL/pgSQL functions write, row-level security policies. Queries through pgx, `database/sql`, sqlx, `pg`, `postgres`, SQLAlchemy, raw SQL strings. |
| **Supabase** | Postgres as above, plus `supabase.from('t')` / `.rpc('fn')` and Edge Functions as HTTP entrypoints. |
| **Firestore** | Collections and document refs written and read through the Firebase Admin SDK (JS/TS, Python) and the Go client, transactions and batches; built document paths (`` db.doc(`${ORDERS}/${id}`) `` with a same-file constant, `db.doc('notes/' + id)`, a direct `db.doc(path).set()`); Firebase Functions triggers as entrypoints, fired only by the matching write (`add` / `set` / `create` → `onDocumentCreated`, `update` / `set` → `onDocumentUpdated`, `delete` → `onDocumentDeleted`). A name known only at runtime goes to `collection:unknown` and is counted in every answer. |
| **Other SQL databases** | Static SQL strings are parsed (sqlglot reads most dialects), but schema replay and triggers are written for Postgres syntax. |

## Entrypoints

An **entrypoint** is anything that starts code from outside:

- `http` — a route (`POST /orders/:id/paid`), with the framework's prefixes,
  routers and mounts applied;
- `job` — a scheduled job (Go ticker / cron, Celery beat, worker loops);
- `queue` — a queue or topic consumer (Celery, RQ, Cloud Tasks, Pub/Sub),
  linked to the code that enqueues;
- `db_trigger` — a `CREATE TRIGGER`, so a plain `UPDATE orders` also reaches
  the tables the trigger writes;
- Firebase Functions (JS/TS): `firebase_call` (`onCall`, reached from
  `httpsCallable` in a client), `firebase_http` (`onRequest`), `scheduled_job`
  (`onSchedule`, v1 `pubsub.schedule().onRun`, with its `schedule`), `pubsub`
  (`onMessagePublished`, v1 `topic().onPublish`, triggered by the topic),
  `auth` (`beforeUser*`, v1 `auth.user().onCreate`) and Firestore event
  functions (`onDocumentCreated` / `Updated` / `Deleted` / `Written`, also
  namespaced as `firestore.onDocumentCreated`). Each carries its
  `deployed_name`, followed from the functions entry file through
  `exports.g = require('./g')`, `export { … } from` and `export *`.

## When a layer cannot be traced

BizMap falls back to evidence it can still see, and marks those links
`inferred`:

- an API path literal in a front-end function links to the one backend route
  it matches (`via: route_literal`);
- a handler that names a SQL constant or SQL dict entry reads/writes those
  tables even when an injected executor runs it (`via: sql_reference`).

What cannot be resolved at all is kept, with a reason: `table:unknown` for
dynamic SQL, `candidates` for ambiguous handlers, `unresolved_http` for calls
with no matching route.

## Going deeper

- [How it works](how-it-works.md) — detector by detector, every pattern.
- [YAML detector rules](detector-rules.md) — add a framework without changing
  the code, and the rules that ship with BizMap.
- A framework you use is missing? Open an issue with a minimal example; the
  snapshot tests show exactly what a new rule changes.
