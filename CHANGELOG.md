# Changelog

## 0.2.0 — 2026-11-19

Python stacks, Firestore/queues, PL/pgSQL + RLS, and a cross-repo registry.
Behavior is generic; tests and fixtures are synthetic only (`examples/shop`,
`examples/clinic`, `examples/pyplatform`, tmp fixtures). Clinic SQL that
binds a session GUC uses `app.tenant_id` (not a domain-specific setting).

### Added

- **SQLAlchemy Core/ORM + Alembic**: `__tablename__` / `mapped_column` /
  `Table(...)` become schema nodes. `select`/`insert`/`update`/`delete`,
  `session.add`, `.values(...)`, and `text()`/`execute()` become reads and
  writes. Alembic `upgrade()` revisions follow `down_revision` and are
  **schema** (`role: schema`), not runtime writers. `bizmap why` lists them
  under **defined/altered by**.
- **Firebase / Firestore**: `https_fn`, `firestore_fn` document triggers,
  scheduler, Cloud Tasks, and Pub/Sub entrypoints. `collection()` /
  `document()` chains become `collection:` nodes with reads/writes.
  Writers of a collection `triggers` matching `on_document_*` handlers.
- **Python constant resolution** for routes: module and imported consts,
  `+` concatenation, f-strings, `APIRouter(prefix=)`, and
  `include_router(..., prefix=)`. Unresolvable pieces stay as `{name}`
  placeholders with `unresolved: true` / `confidence: unknown` — never a
  truncated path.
- **Queues and workers**: Celery `@task` / `.delay()`, RQ `enqueue`,
  Cloud Tasks `create_task` (HTTP URL → `calls_http`), Pub/Sub `publish`,
  and `while True` / `run_forever` workers. Produce sites emit `enqueues`
  edges to `queue:` nodes.
- **PL/pgSQL, POLICY, RPC**: function bodies are scanned for SQL;
  `CREATE POLICY` / `ENABLE ROW LEVEL SECURITY` attach to tables;
  `supabase.rpc("fn")` (Python and JS) `calls` `function:sql.fn`.
- **Cross-repo HTTP + registry**: `httpx`/`requests`/`fetch` URLs match
  scanned routes via `bizmap.yaml` `services:`. `bizmap scan a b` and
  `bizmap registry merge a.json b.json` merge graphs. Function /
  entrypoint / queue ids are repo-qualified; tables and collections
  **share** ids when `database:` matches, so `why` on a merged graph
  shows writers and readers across repos.
- **Catalog tables as external**: `pg_catalog.*`, `information_schema.*`,
  and `pg_*` names are table nodes marked `external`.
- **JavaScript detector** (Express routes, `fetch()`, `supabase.rpc`).
  `.ts`/`.tsx` are parsed with the JavaScript grammar (best-effort).
- Synthetic **`examples/pyplatform`**: FastAPI catalog (SQLAlchemy +
  Alembic + RLS), Firebase worker, storefront HTTP client, Express web
  proxy. Generic names only (`orders`, `users`, `tenants`,
  `notifications`, `documents`, `jobs`).
- YAML rules: `flask.yaml`, `firebase.yaml`, `celery.yaml`, `httpx.yaml`,
  `supabase.yaml`. `express.yaml` is **active** (was a stub).

### Changed

- Graph format **0.2.0**: node types `collection` and `queue`; edge types
  `calls_http` and `enqueues`; top-level `repo`, `database`, `repos`,
  `services`.
- `bizmap scan` accepts **one or more** roots. `bizmap.yaml` gains
  `name`, `database`, `services`, and `repos`.
- SQLAlchemy YAML rule is no longer “partial”; ORM/Core usage is handled
  by the Python engine alongside `text()`/`execute()`.

## 0.1.3 — 2026-11-19

Interface dispatch, dynamic SQL expansion, and alias-scope fixes after a
private evaluation of v0.1.2. Behavior is generic; tests and fixtures are
synthetic only (`examples/shop`, `examples/clinic`, tmp fixtures).

### Fixed

- **Go interface dispatch**: calls on interface-typed fields, parameters, and
  constructor-injected values (`s.Bell.Changed(e)`) follow structural
  implementers in the scan. One implementer becomes an `inferred` call edge;
  several become edges to all, `inferred`, with `candidates`.
- **Dynamic SQL**: `for _, q := range []string{"UPDATE a ...", "DELETE FROM b ..."}`
  expands into one SQL site per element. Helpers that take a `query`/`q`
  string (`named(query, args)`, `exec(ctx, q)`) bind literals/consts at each
  call site and attribute tables to the **caller**.
- **LATERAL / subquery aliases**: `LEFT JOIN LATERAL (SELECT ...) l ON true`
  and `(SELECT ...) r` never become table or column nodes (`l`, `r`, `l.x`),
  including on the sqlglot fallback path. When DDL is present, only
  schema-known tables (plus catalogs) are table nodes.

### Added

- `--exclude GLOB` (repeatable) on `bizmap scan`, merged with `bizmap.yaml`.
- `--profile` prints parse / resolve / sql / propagate timings to stderr.
  Directory walks skip `vendor`/`node_modules`/…, `go.mod` lookup is cached,
  and AST walks are iterative over named nodes so ~150 Go files stay well
  under 10s.

## 0.1.2 — 2026-11-19

Precision fixes after a private evaluation of v0.1.1 on a real Echo + pgx
codebase. Behavior is generic; tests and fixtures are synthetic only
(`examples/shop`, `examples/clinic`, tmp fixtures).

### Fixed

- **FROM-less SELECT** (`SELECT now()`, `SELECT set_config(...)`) is not a
  dynamic table. Functions are never tables. `table:unknown` and other
  dynamic-table nodes stay on the origin function and are not copied up
  the call graph or onto HTTP/job entrypoints.
- **Qualified names** always use the directory path relative to the scan
  root (`cmd/server.main`, `internal/services/appointments.Service.Run`).
  A package no longer has two spellings (clause vs dir prefix).
- **HTTP routes** match method names exactly (`GET`, `POST`, `PUT`,
  `PATCH`, `DELETE`, `OPTIONS`, `HEAD`, `Any`, `Match`) and require a
  string-like path. Lowercase helpers such as `h.options(c, ...)` are not
  routes.
- **Const route paths** resolve across scanned packages (import path, then
  unique suffix). Unscanned consts keep the `{pkg.Name}` placeholder and
  set `unresolved: true` on the entrypoint.
- **Function-field dispatch**: `s.ring(...)` follows the single function
  assigned in a composite literal or constructor (`Service{ring: notify.Send}`
  / `New(repo, notify.Send)`). Ambiguous assignments stay unresolved.
- **Subquery / LATERAL / CTE alias outputs** (`l`, `r`, `l.*`,
  `files.file_id`) never become table or column nodes. Only DDL-known or
  clearly-real tables/columns do.
- **Inferred calls** are created only when the callee is unique within the
  resolved package/type set. Unknown receivers do not pick a globally
  unique method or propagate its table edges.
- **Wrapped handlers** (`wrap(h.Version)`, `mw(handler)`) unwrap to the
  inner func/method. Factories that take data (`StaffPhoto(cfg)`) still
  resolve to the factory.
- **`bizmap.yaml` lookup**: `--config path`, then the scan root, then the
  git toplevel. Excludes apply to the scanned tree.

### Performance

Per-function `_local_types` (and per-file types) are cached so call
resolution stays close to v0.1 speed.

## 0.1.1 — 2026-11-19

Go resolution accuracy fixes after a private evaluation of v0.1 against a
large Echo + pgx codebase. Behavior is generic; tests and fixtures are
synthetic only (`examples/shop`, `examples/clinic`).

### Fixed

- **Handler resolution** uses a `(import-path, type, method)` key. Locals
  assigned as `x := pkg.Type{}`, `x := &pkg.Type{}`, `x := pkg.NewType(...)`,
  and struct fields are type-inferred. Name collisions no longer pick
  `matches[0]`; ambiguous handlers are `unknown` with `candidates`.
- **Packages keyed by import path** (module path from `go.mod` + directory),
  not the `package` clause. Call resolution uses each file's import map,
  including aliases. Same-named packages such as `repositories/foo` and
  `services/foo` stay distinct.
- **False call edges**: the unique-method-name fallback is gone for import
  aliases of unscanned packages and unknown receivers
  (`middleware.Current(c)`, `s.Now()` stay unresolved).
- **Edge explosion**: `SELECT *` and column-less INSERT stay **table-level**
  by default. Column edges are emitted only where SQL names columns.
  Call-graph propagation is table-level. `--expand-star` restores SELECT *
  column expansion at the SQL origin only.
- **Jobs**: any `go` statement (function, method, package func,
  `go (&pkg.Svc{}).Run(ctx)`, `go func(){...}()`) whose body or transitive
  callees use `time.NewTicker` / `time.Tick` / `time.After` in a loop or a
  cron library.
- **Cross-package const route paths**: `pkg.Const` is resolved through the
  import map (including Echo `Group` prefixes). Unresolvable pieces become
  placeholders such as `{otherpkg.PendingPath}/setup` with confidence
  `unknown` — never a truncated path like `/setup`.
- **Phantom tables**: CTE / subquery / LATERAL aliases and SQL keywords
  (`SET` from `"UPDATE "+table+" SET"`) are not table nodes. Dynamic table
  names are `table:unknown` with candidate tables. `pg_roles` and other
  catalogs are marked `external`.
- **Factory handlers**: `g.GET("/x", profile.StaffPhoto(cfg))` resolves to
  the factory (and the SQL in the returned closure).
- **Entrypoint noise**: `exclude` globs in `bizmap.yaml` (e.g. `cmd/seed`,
  `demo/`). `func main` is not treated as a writer entrypoint unless it is a
  detected job/route.

### Added

- `examples/clinic` — synthetic Go app with colliding `Create`/`List`/`Get`
  methods across two controllers and same-named `orders`/`users` packages.
- `--mermaid-focus TABLE`, `--mermaid-limit N`. Default `--mermaid` is an
  entrypoint→table rollup.
- Optional `situation` on write edges from const/iota arguments
  (`audit.AppointmentCanceled`) so `bizmap why` can show when a column is
  written.

## 0.1.0 — 2026-11-19

Initial deterministic data-lineage CLI (Go Echo/pgx + Python FastAPI).
