# How BizMap works

Detector-by-detector detail. Graph format: [graph-schema.md](graph-schema.md). YAML rules: [detector-rules.md](detector-rules.md).

## Pipeline

1. **Schema replayer** — applies `schema.sql` / migration folders in order
   (`CREATE TABLE IF NOT EXISTS`, quoted/schema-qualified pg_dump identifiers,
   `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, drop/add constraints, `DO $$`
   blocks) and extracts `CREATE TRIGGER` plus the tables/columns their plpgsql
   functions write. Function bodies are re-scanned after the full replay so
   pg_dump `CREATE FUNCTION` before `CREATE TABLE` still records those
   tables. `EXECUTE format('UPDATE %I ...')` is dynamic `table:unknown`.
   Comments, string literals, DECLARE'd variables, INTO
   targets, and PL/pgSQL keywords are never table nodes. `CREATE VIEW` /
   `CREATE MATERIALIZED VIEW` become `view` nodes with `derives_from` edges.
   Also `CREATE POLICY`, `ENABLE ROW LEVEL SECURITY`, `DROP TABLE`
   (`dropped: true` when still referenced), and Alembic `upgrade()`
   chains (schema role, not runtime writers) including `op.execute` of views.
2. **Go detector** — Echo v4 routes (`group.POST(path, handler)` with group
   prefixes, cross-package consts (including `const X = shared.X` aliases),
   factory handlers, and wrappers such as
   `wrap(h.Version)`). Method names must be exactly `GET`/`POST`/…/`Any`/`Match`
   with a string-like path. SQL is taken from the argument that looks like
   SQL, so pgx `tx.Exec(ctx, sql)`, `database/sql` `db.Exec(sql, args…)`,
   `Query`/`QueryRow`, `ExecContext`/`QueryContext`/`QueryRowContext`, and
   sqlx `Get`/`Select`/`NamedExec` all match without duplicates. Helpers
   that take a `query`/`q` string attribute tables to the caller. Outbound `net/http`
   `NewRequest` / `NewRequestWithContext`, `client.Do`, and `http.Get`/`Post`
   (const, env, `fmt.Sprintf`, concat, `url.PathEscape`) become `calls_http`.
   Dynamic segments (`"/items/" + url.PathEscape(id) + "/approve"`) match
   `/items/:id/approve` routes. Helpers that take
   `path`/`method` from the caller (`getJSON(ctx, path, out)` concatenating
   `c.baseURL + path`) compose at the call site. A struct `baseURL` field is
   `{base}+path`, not a relative fetch. Any `go` statement
   whose target uses `time.NewTicker` / `time.Tick` / `time.After` loops or a
   cron library. Packages are keyed by import path; display names are the
   directory relative to the scan root. With no `go.mod`, import paths fall
   back to a unique scanned-directory suffix and print a warning.
3. **Python detector** — FastAPI/Flask routes (module/imported consts, `+`,
   f-strings, `APIRouter` / `include_router` prefixes, factory helpers such as
   `app = create_service()` including nested helpers
   (`create_service() -> new_app(**kw) -> FastAPI(**kw)`) and apps imported
   from another module). Unresolvable
   path pieces stay `{name}` placeholders (`unresolved`, never truncated).
   SQLAlchemy Core/ORM usage and `text()`/`execute()` SQL, plus SQL strings
   held in module dicts (`QUERIES["a"]`) and callables injected as arguments,
   `deps.run_query` / `self.run_query`, dataclass/NamedTuple `Callable`
   fields constructed with kwargs in another module and passed into
   `create_app(deps)` (closure, `app.state`, or a typed parameter), and
   module-level instances (`svc = Svc(run_query); svc.go()`). Unresolvable
   typed dispatch is `unresolved_calls`. HTTP routes require a known
   FastAPI/Flask app, APIRouter, or Blueprint, or a module-level
   `@name.get("/path")` (so `@patch("x.x.logger")` is not a route).
   Constructor/annotation dispatch
   (`Drainer(outbox)` → `self.outbox.settle`, `store: Store` → `store.save`).
   Firebase Functions + Firestore collections, including
   document refs (`ref.set` after `collection().document()`, including an
   assigned `ref` variable), transactions, and batches. Non-DB calls inside
   a Firestore write dict (`datetime.now()`) are not `table:unknown`.
   Celery / RQ / Cloud Tasks / Pub/Sub (enqueue linked to the
   consumer by URL suffix, a one-hop URL helper, or `queues:`) and
   `while True` workers. `supabase.rpc("fn")` links to SQL functions.
   Client URLs keep the env/host origin (`os.environ["X_URL"]`), not a
   local variable name. `payload.get` / Mapping `.get` are not HTTP;
   only `requests` / `httpx` / `aiohttp` / `urllib` clients and sessions.
   A constructor/config `base_url` is `{base}+path`, not a relative fetch.
   Outbound helpers such as `_post(path)` that concatenate `self.base_url +
   path` (or an httpx/requests session on `self`) compose at the caller;
   uncomposed calls keep `unresolved_http`. SQL dicts reached through a
   module alias (`queries.Q['k']`, `import pkg.queries as q`) and
   import-time `dedent`/`strip` transforms resolve; `rpc("list")` wrappers
   that do `run_query(Q[name])` attribute those tables to the route.
   SQL dicts derived in another module
   (`Q = {k: _transform(v) for k, v in queries.RAW.items()}`) evaluate
   identity, `dedent`/`strip`, `.replace` / `.format` with constant args
   (and apply schema renames); unknown transforms keep the raw SQL tables
   with confidence `inferred` and a note. `x.request('POST', path)` /
   `x.post(path)` on a transport whose class is not scanned emit
   `unresolved_http` (never silent).
4. **JavaScript / TypeScript detector** — Express / Fastify / Hono routes
   on a known app/router, including chained factories
   (`new Hono().use('*', mw).get('/a', h)`), including chained-export
   routers mounted across files
   (`export const r = new Hono().use(mw).get('/a', h)` then
   `app.route('/v1/p', r)` / nested `.route('/api', b)`), `.basePath('/v1')`,
   and `.route('/x', sub)` / `app.use('/v1/items', importedRouter)` with the
   mount prefix applied across files. Known HTTP clients
   (`fetch`, `axios` + instances, `ky`, `ofetch`, `got`, `superagent`)
   emit outbound `calls_http`; `Map` / `Set` / `Headers` / `URLSearchParams`
   / `searchParams` / `config.headers` do not. `axios.create({baseURL})` /
   `ky` / `ofetch` instances (exported, factory-built, or passed as a
   parameter into an object-literal factory
   `makeApi = (client) => ({ list: () => client.get('/x') })`) and
   `request(path)` wrappers compose the outbound URL; an instance passed
   into an object-literal factory or a class constructor
   (`new OrdersApi(api)` / `this.client.get`) keeps the caller's
   `baseURL` (including `import.meta.env`); if a parameter client cannot
   be composed it is `unresolved_http`, never dropped.
   Instances **returned by a factory** (`export function createClient() { return axios.create({baseURL}) }`,
   `const makeHttp = () => { const inst = axios.create({...}); inst.interceptors.request.use(...); return inst }`
   then `export const api = createClient()`) compose the same way for
   `api.get('/orders')`, `api.get<Order[]>('/orders')`, and
   `(await api.get(...)).data` — never `:client` / `:http`.
   Local wrappers `const get = <T>(url) => api.get<T>(BASE + url)` and
   `request(method, url, body)` that call `api.request({ method, url: \`${BASE}${url}\` })`
   emit the **call-site** path (`get('/orders/' + id)` → `/orders/:id`),
   not `base:url`. `tsconfig.json` / `jsconfig.json` `compilerOptions.paths`
   (plus `baseUrl` and `extends`) resolve `@/*` → `src/*`; without a config
   `@/` still maps to `src/`. A factory `createApi(baseUrl)` used as
   `createApi(API_URL)` substitutes the call-site argument
   (`import.meta.env.X` → `{X}`). Parameters typed `AxiosInstance`,
   `Pick`/`Partial` of it, `typeof instance`, or `KyInstance` inherit that
   instance's base.
   Chained Hono routes (any length) and statement-style sub-apps
   (`const s = new Hono(); s.get('/x'); app.route('/', s)` inside a
   chained export) keep the composed mount prefix.
   `serve({ fetch: app.fetch })` is not a `POST /` entrypoint.
   Relative `fetch('/projects')` matches only the same repo; a constructor
   or config base is `{base}+path` and may match across repos when the path
   has 2+ literal segments or `services:` maps it. Inline handlers
   attribute `supabase.from('t')` to
   the route. `.ts`/`.tsx` use tree-sitter-typescript. `pg`/`postgres`
   `query()` SQL and Supabase Edge Functions
   (`serve` / `Deno.serve` in `supabase/functions/<name>/index.ts` →
   HTTP `/functions/v1/<name>`, with call edges into imported helpers).
5. **Call graph** — handler → service → repository using typed locals,
   constructors, import aliases, injected func fields
   (`Service{ring: notify.Send}`), **object/interface dispatch**
   (`s.Bell.Changed(e)`, `self.outbox.settle(...)` → structural
   implementers / constructor-assigned types, `inferred`, with
   `candidates` when several). Unscanned/unknown receivers stay unresolved.
   Reads/writes propagate to HTTP and job entrypoints as table-level
   `inferred` edges; `table:unknown` stays attached to the function that
   does the dynamic write and is exposed on entrypoints only as
   `dynamic_table` / `dynamic_table_unknown` (flag/count), never as a
   reach edge. HTTP clients emit
   `calls_http` when the URL matches a scanned route (same repo, or via
   `services:` base URLs, env var names, or hostnames). Unmatched client
   calls print `N unresolved HTTP calls` with the env/host keys seen and
   per-reason counts (`target_not_scanned`, `base_unknown`, `path_dynamic`,
   `client_unknown`). Each `unresolved_http` entry has a `reason`.
   Queue produce sites emit `enqueues` and `triggers` the matched consumer.
6. **SQL** — named columns stay column-level; `SELECT *` and column-less
   INSERT are table-level. FROM-less SELECT (`now()`, `set_config`) touches
   no table. CTE/subquery/LATERAL aliases (`l`, `r`, `l.x`) are never tables.
   Dynamic table names (`"UPDATE "+t+" SET"`) stay `table:unknown` on the
   origin function. Concat/helper holes (`{helper(o)}`) are not columns.
   Relations named in FROM/JOIN/INSERT/UPDATE that are not in DDL become
   `unresolved_relation` nodes (not dropped). `pg_catalog.*`,
   `information_schema.*`, `pg_*`, `storage.*`, `vault.*`, and `auth.*`
   are marked `external`.
7. **Registry** — `bizmap scan a b` or `bizmap registry merge a.json b.json`
   qualifies function/entrypoint/queue ids by repo and re-matches HTTP/queue
   links from stored `http_calls`. Tables and collections share ids when
   `database:` is the same, so `why orders.status` on the merged graph lists
   writers and readers from every service. Multi-path scans with a shared
   `--config` keep each repo's own name. Unmatched client calls persist as
   `unresolved_http` in the graph (stderr counts match).
8. **Overrides** — `bizmap.yaml` (`--config`, scan root, or git toplevel)
   fills in `why`, extra edges, `exclude` globs, `expand_star`, plus
   `name`, `database`, `services`, `queues`, `repos`, and `constants`
   (unscanned package const aliases such as `PathPrefix: /api/v1` or
   `pkg.Name`). `--exclude GLOB`
   merges with YAML. `--profile` times each scan phase.

### Performance

v0.2 keeps the per-function type cache, skips `vendor`/`node_modules` during
file discovery, caches `go.mod` lookup, and walks named AST nodes iteratively.
`--profile` reports parse / resolve / sql / propagate. Synthetic fixtures
finish in tens of milliseconds; scans of ~150 Go files stay well under 10s.

## Fallbacks when a layer cannot be traced

- **Route literals (JS/TS)** — an API-looking string in a function
  (`` `/orders/${id}` ``) links to the single backend route it matches
  (`calls_http`, `inferred`, `via: route_literal`). Rules and exclusions:
  [graph-schema.md](graph-schema.md#route-literals-calls_http-with-via-route_literal).
- **SQL by reference (Python)** — a function that names a SQL constant or SQL
  dict entry reads/writes those tables even when an injected executor runs it
  (`inferred`, `via: sql_reference` / `sql_key`). See
  [graph-schema.md](graph-schema.md#sql-by-reference-via-sql_reference--sql_key-python).
