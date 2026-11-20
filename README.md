# BizMap

**Who reads and writes this column — across all your services — and what triggers it?**

BizMap is a static, deterministic data-lineage map for application code. Point it
at one or more repos and it answers, for every table and column:

- **who writes it** and **who reads it** (function, file, line)
- **what triggers that code**: HTTP route, scheduled job, queue consumer, DB trigger, Firestore event
- **which other service calls in**: front-end → API → internal service, matched across repos
- **why**, in the words already sitting in the code (the comment or docstring above the function)

No LLM, no tokens, no account, no server. It runs locally, and the same input always produces the same graph.

Typical questions it answers:

- *Can I drop or rename `orders.status`?* — every reader and writer, in every repo.
- *Which endpoints touch `users.email`?* — useful for LGPD/GDPR data mapping.
- *What happens to the database when the front-end calls `placeOrder()`?*

## A 30-second demo

[`examples/checkout`](examples/checkout) is three services sharing one Postgres:
**web** (TypeScript + axios) → **api** (Go + Echo + pgx) → **billing** (Python + FastAPI),
and billing calls the api back when a payment is confirmed.

```bash
pip install -e .
bizmap scan examples/checkout --mermaid
bizmap why orders.status
```

The diagram that `--mermaid` writes (entrypoint → table rollup plus cross-service
calls; `reads` edges omitted here for readability):

```mermaid
flowchart LR
  subgraph web
    placeOrder["placeOrder()"]
    pollOrder["pollOrder()"]
  end
  subgraph api
    POST_orders["POST /api/orders"]
    GET_order["GET /api/orders/:id"]
    POST_paid["POST /api/orders/:id/paid"]
    job_expire["job ExpireJob.Run"]
    CreateInvoice["Billing.CreateInvoice"]
    orders[("orders")]
  end
  subgraph billing
    POST_invoices["POST /invoices"]
    POST_webhook["POST /webhooks/payment"]
    invoices[("invoices")]
  end
  placeOrder -->|calls_http| POST_orders
  pollOrder -->|calls_http| GET_order
  CreateInvoice -->|calls_http| POST_invoices
  POST_webhook -->|calls_http| POST_paid
  POST_orders -->|writes| orders
  POST_paid -->|writes| orders
  job_expire -->|writes| orders
  POST_invoices -->|writes| invoices
  POST_webhook -->|writes| invoices
```

And `bizmap why orders.status` (trimmed):

```
column: orders.status (TEXT)

writers:
  [exact] api/main.Repo.ExpirePending  (repo.go:54) — ExpirePending releases orders that were never paid.
      triggered by api/scheduled job main.ExpireJob.Run — Run expires unpaid orders every minute so abandoned carts free their slot.
  [exact] api/main.Repo.InsertOrder  (repo.go:35) — InsertOrder stores a new pending order.
      triggered by api/HTTP POST /api/orders — CreateOrder records a checkout as pending and asks billing for an invoice.
      invoked via HTTP from web/src.checkout.placeOrder
  [exact] api/main.Repo.MarkPaid  (repo.go:48) — MarkPaid settles an order after billing confirms payment.
      triggered by api/HTTP POST /api/orders/:id/paid — MarkPaid is called by billing once the payment provider confirms the charge.
      invoked via HTTP from billing/app.payment_webhook

readers:
  [exact] api/main.Repo.GetOrder  (repo.go:42) — GetOrder loads one order by id.
      triggered by api/HTTP GET /api/orders/:id — GetOrder returns the order status for the confirmation page.
      invoked via HTTP from web/src.checkout.pollOrder
```

Three languages, four cross-service hops, every line backed by `file:line`.

## Quickstart

Python 3.11+:

```bash
pip install -e .                     # or: uv sync
bizmap scan path/to/repo --mermaid   # writes bizmap.graph.json (+ .mmd)
bizmap why orders.status             # writers, readers, triggers, callers
bizmap report path/to/repo           # how much of the code BizMap understood
```

Several services at once (one joint graph, cross-repo HTTP links matched):

```bash
bizmap scan ./web ./api ./billing --out merged.json
bizmap registry merge web.json api.json billing.json --out merged.json   # same result
```

A `bizmap.yaml` per repo (or one at the parent) is optional and only fills gaps:

```yaml
name: api
database: checkout            # repos with the same database share table ids
services:                     # which env var / host points at which service
  billing: { url: http://billing.internal, env: BILLING_URL }
exclude: [cmd/seed, demo/]
overrides:                    # anything static analysis cannot see
  columns:
    orders.status:
      why: "Lifecycle: pending → paid | expired"
```

## Coverage report

`bizmap report` tells you how much of a codebase BizMap understood: entrypoints that
reach a table, HTTP links matched vs. unresolved (by reason, with a hint for each),
how links were derived, and confidence.

```
entrypoints: 6  reaching a table/collection: 6 (100.0%)
http links: 4  (cross-repo 4)
unresolved http: 0
edges by confidence: exact 31, inferred 23
```

`--redact` replaces every name, route, table, URL, env key, and file with a salted
hash, so a report from a private codebase can be shared without sharing code:

```bash
bizmap report ./svc-a ./svc-b --redact --salt-file ~/.bizmap-salt --json
```

Only counts, edge types, entrypoint kinds and reasons stay readable. Without a
salt each run uses a random one (hashes differ between runs); `--salt-file`
creates a private salt once and reuses it so runs can be compared.

## What it understands

| | Routes / triggers | Data access | Outbound HTTP |
| --- | --- | --- | --- |
| **Go** | Echo (groups, const paths, wrappers), ticker/cron jobs | pgx, `database/sql`, sqlx | `net/http` (env/const/`Sprintf` URLs) |
| **Python** | FastAPI, Flask (routers, blueprints, factories), Celery, RQ, Cloud Tasks, Pub/Sub, Firebase Functions, `while True` workers | SQLAlchemy Core/ORM, raw SQL, SQL dicts/constants, Firestore, Supabase | httpx, requests, aiohttp |
| **JS / TS** | Express, Fastify, Hono (chains, mounts), Supabase Edge Functions | `pg`/`postgres`, Supabase | fetch, axios, ky, ofetch, got (instances, factories, `tsconfig` paths) |
| **SQL** | `CREATE TRIGGER` | schema replayed from `schema.sql` / migrations / Alembic, views, RLS policies | — |

When a client or executor cannot be traced through every layer, BizMap falls back to
evidence it can still see — an API path literal in a front-end function
(`via: route_literal`), or the SQL constant a handler names (`via: sql_reference`) —
and marks those links `inferred`. Details: [docs/how-it-works.md](docs/how-it-works.md).

## How it decides

- **Evidence over vibes.** Every edge stores `{file, line, symbol}`.
- **Confidence on every edge.** `exact` (static SQL / resolved handler), `inferred`
  (call-graph propagation, `SELECT *`, interface dispatch, fallbacks above), `unknown`
  (dynamic SQL like `"UPDATE " + table`).
- **Say when you do not know.** Dynamic SQL is `table:unknown`, ambiguous handlers list
  `candidates`, unmatched HTTP calls are kept as `unresolved_http` with a `reason`
  (`target_not_scanned`, `base_unknown`, `path_dynamic`, `client_unknown`).
- **Deterministic.** Byte-identical output for identical input, checked in CI across
  hash seeds; every bundled example has a golden snapshot.
- **Static means static.** SQL rewritten at runtime (`sql.replace(...)`,
  `.format()` with runtime values) is read as written in the source.
- **No AI in the core, ever.** Models are welcome as *clients* of the graph (MCP is on
  the roadmap), not as its authors.

## How it compares

- **Code search / call graphs** (Sourcegraph, CodeQL, tree-sitter code-graph MCP servers)
  know functions and calls, but not which **table and column** a function touches, nor
  which route in *another* repo an HTTP client hits.
- **Data-pipeline lineage** (dbt, OpenLineage, warehouse catalogs) starts at the
  warehouse or at runtime; it does not see the application code that writes the data.
- **Commercial code-level lineage** (Gable, Foundational) does this for data teams as a
  hosted platform. BizMap is the open-source, local, developer-first take on it.

## Roadmap

- **v0.3** — GitHub Action that comments on each PR with the routes, functions, tables
  and columns it impacts (and a Mermaid diagram).
- **v0.4** — MCP server so coding agents can ask `why orders.status` instead of grepping.
- Static graph page (GitHub Pages).

Full history: [CHANGELOG.md](CHANGELOG.md). Graph format:
[docs/graph-schema.md](docs/graph-schema.md). Detector rules:
[docs/detector-rules.md](docs/detector-rules.md).

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

`tests/test_snapshots.py` keeps a golden, line-per-link summary of every example in
`tests/snapshots/*.txt` and checks that each example scans to byte-identical JSON under
several `PYTHONHASHSEED` values. When a detector change is intended, refresh with
`BIZMAP_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py` and review the `+`/`-` diff
in the PR.

Fixtures are synthetic (`orders`, `users`, `invoices`, …); no proprietary code is
included. MIT licensed. Contributions that add detector rules for new frameworks are
especially welcome — please include a minimal example and let the snapshot diff show
what changed.
