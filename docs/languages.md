# Languages and detectors

BizMap parses code with [tree-sitter](https://tree-sitter.github.io/) and SQL
with [sqlglot](https://github.com/tobymao/sqlglot). Nothing is executed and
nothing leaves your machine.

## What it recognises

| | Routes / triggers | Data access | Outbound HTTP |
| --- | --- | --- | --- |
| **Go** | Echo (groups, const paths, wrappers), ticker/cron jobs | pgx, `database/sql`, sqlx | `net/http` (env/const/`Sprintf` URLs) |
| **Python** | FastAPI, Flask (routers, blueprints, factories), Celery, RQ, Cloud Tasks, Pub/Sub, Firebase Functions, `while True` workers | SQLAlchemy Core/ORM, raw SQL, SQL dicts/constants, Firestore, Supabase | httpx, requests, aiohttp |
| **JS / TS** | Express, Fastify, Hono (chains, mounts), Supabase Edge Functions | `pg`/`postgres`, Supabase | fetch, axios, ky, ofetch, got (instances, factories, `tsconfig` paths) |
| **SQL** | `CREATE TRIGGER` | schema replayed from `schema.sql` / migrations / Alembic, views, RLS policies | — |

## Databases

| Database | What is read |
| --- | --- |
| **PostgreSQL** | `CREATE TABLE` / `ALTER TABLE` (including pg_dump output, quoted and schema-qualified names), migration folders replayed in order, views and materialized views, `CREATE TRIGGER` and the tables their PL/pgSQL functions write, row-level security policies. Queries through pgx, `database/sql`, sqlx, `pg`, `postgres`, SQLAlchemy, raw SQL strings. |
| **Supabase** | Postgres as above, plus `supabase.from('t')` / `.rpc('fn')` and Edge Functions as HTTP entrypoints. |
| **Firestore** | Collections and document refs written and read through the Firebase Admin SDK, transactions and batches; Firebase Functions triggers as entrypoints. |
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
- Firebase / Firestore event functions.

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
