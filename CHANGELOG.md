# Changelog

## Unreleased

### Fixed

- **Cross-service false positives (route literals and traced clients)**: a
  call whose base resolves to a `services:` entry now matches only that
  service's routes; if the service was not scanned it stays
  `target_not_scanned` (`service:` on the note) instead of linking to a
  same-path route in another repo. The route-literal fallback only runs when
  the base is unknown, its edges are the new `heuristic` confidence with
  `match: full|suffix`, and skipped literals are recorded in
  `route_literal_skipped` with the discarded candidates. `services:` entries
  accept `repo:` when the service key differs from the repo name.
  **Behavior change:** in multi-repo graphs a service whose key (or `repo:`)
  is not a scanned repo is treated as not scanned; `bizmap scan` prints a
  warning naming it.

- **Python phantom unknown reads**: `execute(text("..."), params)` no longer
  adds a `reads <table> [unknown]` next to the exact edges (the `text()` call
  was also read as unresolved SQL), and SQLAlchemy builders
  (`update(T).where(...).values(...)`, `insert(...)`, `delete(...)`) no
  longer produce `table:unknown` because their source text contains
  `UPDATE`/`INSERT`/`DELETE`.

### Added

- **`examples/checkout`**: web (TypeScript + axios) → api (Go + Echo + pgx,
  ticker job) → billing (Python + FastAPI) → api, one database. Used by the
  README demo, covered by a chain test and a golden snapshot; CI prints its
  Mermaid diagram, `why orders.status`, and report.
- **README rewrite**: problem-first pitch, 3-service demo with real output,
  stack matrix, comparison, coverage report. Detector-by-detector detail
  moved to `docs/how-it-works.md`.

- **`bizmap report`** (`--json`, `--redact`, `--salt`): coverage of a scan —
  entrypoints reaching a table/collection (by kind, with the list that does
  not), `calls_http` by derivation and cross-repo, `unresolved_http` by reason
  with a hint each and the top base/env keys, data links by `via`, edges by
  confidence. Accepts scan roots (in memory, nothing written) or `--graph`.
  `--redact` hashes every name/URL/key with a salt so reports from private
  codebases can be shared.

- **Front-end → API by route literal** (`calls_http`, `inferred`,
  `via: route_literal`): API-looking strings and templates in JS/TS
  (`'/orders/' + id`, `` `${base}/orders/${id}` ``) link to the single
  matching route in the same repo or a joint scan, even when the axios/ky/fetch
  client was built through DI, registries, or factories the detectors cannot
  follow. Navigation (`navigate`, `<Link to>`, `path:` keys), comparisons,
  assets, imports, and route registrations are ignored; suffix matches need
  2+ literal segments or a method hint; ambiguous literals link nothing.
  The matching `unresolved_http` entry is removed. Works after
  `registry merge` (literals are stored on function nodes as
  `route_literals`).
- **Python SQL by reference** (`reads`/`writes`, `inferred`,
  `via: sql_reference` or `sql_key`): a function that names a SQL constant or
  SQL dict entry (`QUERIES["top"]`, `.get("top")`, `queries.QUERIES[...]`,
  `SALES_SQL`, `sql.SALES_SQL`) — or passes a key that exists in exactly one
  SQL dict (`store.named("sales_by_day")`) — reads/writes those tables even
  when an injected or unresolved executor runs the query. Endpoints that used
  to show 0 tables now reach them. Skipped when the function already executes
  the SQL or reaches its executor through the call graph.

- **Golden snapshots** (`tests/snapshots/*.txt`, `bizmap.snapshot.summarize`):
  one sorted line per node, edge (with confidence), and unresolved HTTP call
  for every bundled example. Any link gained or lost shows up as a readable
  diff in the PR. Refresh with `BIZMAP_UPDATE_SNAPSHOTS=1`.
- **Determinism across hash seeds for every example** (not only a synthetic
  fixture): `shop`, `clinic`, and the joint `pyplatform` scan must produce
  byte-identical JSON under three `PYTHONHASHSEED` values.

## 0.2.9 — 2026-11-19

Route-registration HTTP false positives, stable `unresolved_calls` labels,
TypeScript `tsconfig` path + factory-arg client URLs, Go `database/sql` /
sqlx argument picking, and joint-scan mermaid `calls_http` after a private
evaluation of v0.2.8. Behavior is generic; tests and fixtures are synthetic
only (`shop`, `orders`, `items`, `billing`, `invoices`, `sites`).

### Fixed

- **Route registration is not outbound HTTP**: a function that builds an
  `APIRouter` and registers `@router.get` / `@router.post` is not a
  `calls_http` caller of those same routes. Route decorators and app/router
  registrations never emit client HTTP (v0.2.7 gave 0 edges; v0.2.8 did not).
- **Stable `unresolved_calls`**: when several classes share a method name
  (`A.model_dump` vs `B.model_dump`) the label is the sorted candidate set,
  not an arbitrary `set` iteration. Identical trees scan to byte-identical
  JSON except `generated_at`.
- **TS `tsconfig` / `jsconfig` paths**: `compilerOptions.paths` (with
  `baseUrl` and `extends`) resolve imports such as `@/*` → `src/*`. Without
  a config, `@/` still maps to `src/` (inferred).
- **TS factory call-site bases**: `createApi(API_URL)` with
  `baseURL: \`${baseUrl}/api/v1/shop\`` substitutes the caller's argument
  (`import.meta.env.X` / `process.env.X` → `{X}`), so the composed URL is
  `{VITE_API_URL}/api/v1/shop/...` rather than `{baseUrl}/...`.
- **TS typed HTTP params**: parameters typed `AxiosInstance`,
  `Pick<AxiosInstance, …>`, `Partial<AxiosInstance>`, `typeof someInstance`,
  or `KyInstance` are HTTP clients and inherit the call-site instance base
  (`makeOrdersClient(shopApi)`).
- **Go `database/sql` / sqlx**: SQL is taken from the argument that is a
  string/SQL literal or const, so `db.Exec(sql)`, `Query`/`QueryRow`,
  `ExecContext`/`QueryContext`/`QueryRowContext(ctx, sql)`, and sqlx
  `Get`/`Select`/`NamedExec` match alongside pgx `Exec(ctx, sql)` without
  duplicates.

### Changed

- **Mermaid joint scans**: `--mermaid` on a multi-repo scan draws
  `calls_http` between services (function or entrypoint in repo A →
  entrypoint in repo B), one subgraph per repo, so
  `web.checkout → api POST /orders → billing POST /invoices → invoices`
  is visible.
- Graph format **0.2.9**.

## 0.2.8 — 2026-11-19

TypeScript factory-returned axios/ky instances and local path wrappers,
Python SQL dicts derived in another module, unscanned transport HTTP,
`constants:` in `bizmap.yaml`, and `reason` on every `unresolved_http`
entry after a private evaluation of v0.2.7. Behavior is generic; tests
and fixtures are synthetic only (`orders`, `users`, `tenants`,
`documents`, `jobs`, `items`).

### Fixed

- **TS factory-returned clients**: `export function createClient(token?) { return axios.create({ baseURL: \`${import.meta.env.VITE_API_URL}/api/v1\` }) }` and `const makeHttp = () => { const inst = axios.create({...}); inst.interceptors.request.use(...); return inst }` then `export const api = createClient()`. Direct calls `api.get('/orders')` in another module, `api.get<Order[]>('/orders')`, `(await api.get(...)).data`, and the instance passed into `makeOrders(api)` / `new OrdersApi(api)` compose `{VITE_API_URL}/api/v1/orders`. Never `:client` / `:http` placeholders.
- **TS local path wrappers**: `const get = <T>(url: string) => api.get<T>(BASE + url)` and `async function request(method, url, body) { return api.request({ method, url: \`${BASE}${url}\`, data: body }) }`. Callers `get('/orders/' + id)` / `request('POST', '/orders', b)` emit `calls_http` with the literal call-site path (dynamic parts → `:param`), not `base:url`.
- **Python SQL dicts in another module**: `from . import queries; Q = {k: _transform(v) for k, v in queries.RAW.items()}` evaluates identity, `dedent`/`strip`, `.replace('old_schema.', 'new_schema.')`, and `.format(schema=CONST)` with constant args, then resolves tables (including renames). Unknown transforms keep the raw SQL tables with confidence `inferred` and a note.
- **Unscanned transports**: `x.request('POST', path)` / `x.post(path)` on a transport whose class is not scanned emit `unresolved_http` on the caller with method + composed path (never silent).
- **`constants:` in bizmap.yaml**: `constants: { PathPrefix: /api/v1 }` keyed by name or `pkg.Name` resolves Go/TS/Python consts that alias unscanned packages.
- **`unresolved_http.reason`**: every unmatched call has one of `target_not_scanned` (URL fully composed but no matching route), `base_unknown` (path known, base not resolvable), `path_dynamic` (path not composable), `client_unknown`. CLI prints per-reason counts.

### Changed

- Graph format **0.2.8**.

## 0.2.7 — 2026-11-19

TypeScript axios/ky instance parameters and class clients, Hono long chains
and statement-style sub-apps, Python outbound wrappers and SQL-dict aliases,
Go const aliases, and hash-stable graph JSON after a private evaluation of
v0.2.6. Behavior is generic; tests and fixtures are synthetic only
(`orders`, `users`, `tenants`, `documents`, `jobs`, `items`).

### Fixed

- **TS instance clients across modules**: an axios/ky instance passed as a
  function parameter into an object-literal client
  (`makeOrders = (client) => ({ list: () => client.get('/orders') })`) or a
  class (`constructor(private client: AxiosInstance)` + `new OrdersApi(api)`)
  keeps the caller's `baseURL`, including
  `` `${import.meta.env.VITE_API_URL}/api/v1` ``. Dynamic segments
  (`/orders/${id}`) become `/orders/:id`. `calls_http` matches a Go route
  `/api/v1/orders/:id` in another scanned repo when `services:` maps
  `VITE_API_URL` or the remaining path has 2+ literal segments.
- **Hono long chains**: chained `.get/.post/...` with more than 12 links
  still receive the mount prefix (iterative walk, no shallow parent cap).
- **Hono statement-style sub-apps**: `const s = new Hono(); s.get('/x', h)`
  mounted via `.route('/', s)` or `.route('/sub', s)` inside an exported
  chain that is itself mounted with a prefix records the composed path
  (`/api/x`, `/api/sub/x`).
- **Hono/node `serve`**: `serve({ fetch: app.fetch, port })` and
  `export default { fetch: app.fetch }` are not a false `POST /` entrypoint.
- **Python outbound wrappers**: `def _post(self, path, body): return
  self._transport.request('POST', self.base_url + path, ...)` is an outbound
  helper. Callers `self._post('/internal/orders:sync', ...)` emit
  `calls_http` with `{base}` + the literal path (constructor/config/env
  base, matched across repos per the v0.2.6 rule). When the path is not
  composable the caller keeps `unresolved_http`.
- **Python SQL dict aliases**: `from . import queries; run(queries.Q['k'])`,
  `import pkg.queries as q; q.Q[k]`, and import-time transforms
  (`Q = {k: textwrap.dedent(v) for k, v in RAW.items()}`, `v.strip()`)
  resolve to reads/writes. A route calling `rpc(name)` that does
  `run_query(Q[name])` with a literal name attributes those tables to the
  route.
- **Go const aliases**: `const PathPrefix = shared.PathPrefix` and
  `var Base = shared.Base + "/v1"` resolve when both packages are scanned.
- **Determinism**: every list attribute on nodes/edges (and top-level
  `unresolved_http` / `warnings`) is sorted so identical input yields
  byte-identical `bizmap.json` across `PYTHONHASHSEED` values (except
  `generated_at`).

### Changed

- Graph format **0.2.7**.

## 0.2.6 — 2026-11-19

Chained-export Hono mounts, nested Python app factories, injected Deps
callables, axios/ky object-literal factories, dynamic Go URL segments,
constructor-base HTTP matching, Python dict.get precision, and assigned
Firestore document refs after a private evaluation of v0.2.5. Behavior is
generic; tests and fixtures are synthetic only (`orders`, `users`,
`tenants`, `documents`, `jobs`, `items`).

### Fixed

- **Chained-export Hono mounts**: `export const r = new Hono().use(mw).get('/a', h).post('/b/:id', h)`
  mounted via `app.route('/v1/p', r)` (and nested `.route('/api', b)`)
  records the full prefix `/api/v1/p/a`. Statement-style mounts already
  worked; chained-export style now uses the same cross-file mount path.
- **Nested FastAPI/Flask factories**: `create_service() -> new_app(**kw) -> FastAPI(**kw)`
  (across modules, depth up to 5) keeps `@app.get` / `@app.route` on the
  caller's variable.
- **Deps callables**: a dataclass/NamedTuple of callables constructed with
  keyword args in another module (`Deps(fetch=_fetch, save=_save)`) and
  passed into `create_app(deps)` resolves `deps.fetch(...)` in nested
  handlers, `app.state.deps`, and typed parameters so SQL reaches tables.
  Unresolvable typed dispatch emits `unresolved_calls`.
- **TS object-literal client factories**: `export const makeApi = (client) => ({ list: () => client.get('/x') })`
  called as `makeApi(api)` where `api = axios.create({baseURL: ENV})`
  composes `{ENV}/x` `calls_http`. If the instance is not composable the
  call is kept as `unresolved_http` (never dropped).
- **Go dynamic URL segments**: `"/items/" + url.PathEscape(id) + "/approve"`
  and `fmt.Sprintf("/items/%s/approve", id)` normalize the hole to a param
  placeholder and match `/items/:id/approve` or `{id}` routes.
- **Constructor/config HTTP bases**: a client whose base comes from a
  constructor parameter or config field (`self.base_url`, Go `baseURL`) is
  not a relative fetch. Treat it as `{base}+path` and match across repos
  when the remaining path has 2+ literal segments (inferred) or
  `services:` maps it. Only bare `fetch('/x')` stays same-repo.
- **Python dict.get**: `payload.get('k')` / `d.get(...)` on dicts, params,
  and Mapping are not HTTP. Only `requests` / `httpx` / `aiohttp` /
  `urllib` clients and their sessions/instances emit `calls_http`.
- **Assigned Firestore refs**: `ref = db.collection('c').document(x)` then
  `ref.update({'at': datetime.now()})` writes `c` and does not invent a
  `table:unknown` read from the non-DB call inside the dict.

### Changed

- Graph format **0.2.6**.

## 0.2.5 — 2026-11-19

Factory/imported FastAPI+Flask routes, HTTP-client precision, SQL concat
holes, cross-file router mounts, and outbound HTTP composition after a
private evaluation of v0.2.4. Behavior is generic; tests and fixtures are
synthetic only (`orders`, `users`, `tenants`, `documents`, `jobs`, `items`).

### Fixed

- **FastAPI/Flask factory routes**: `app = create_service()` (factory in
  another module that returns `FastAPI()` / `APIRouter()` / `Flask()`) and
  an app/router imported from another module keep `@app.get` / `@app.route`
  / `@app.api_route`. When the factory return type is not scannable, a
  decorator `@<name>.get/post/put/patch/delete/api_route/route(...)` with a
  literal path is accepted if `<name>` is a module-level variable (never a
  call chain such as `@patch("x.y")`).
- **Non-HTTP `.get` / `.delete`**: `config.headers.delete`, `headers.get`,
  `params.delete`, `searchParams.get`, and `Map` / `Set` / `Headers` /
  `URLSearchParams` methods are not client HTTP. Only known clients
  (`fetch`, `axios` + instances, `ky`, `ofetch`, `got`, `superagent`) or
  wrappers emit `calls_http`.
- **SQL concat holes**: `"... WHERE " + helper(o)` / `{helper(o)}` never
  becomes column `t.o`. Columns are not derived from SQL holes or concat
  placeholders.
- **Cross-file Hono/Express mounts**: `.route('/v1/items', importedChild)`
  and `app.use('/v1/items', importedRouter)` apply the mount prefix across
  files (default and named exports, chained and nested child routers).
- **Outbound HTTP composition**: Go helpers that take `path`/`method` from
  the caller (`getJSON(ctx, path, out)` building `c.baseURL + path` or
  `fmt.Sprintf("%s%s", c.baseURL, path)`) emit `calls_http` from the
  **caller** with base (env/const/config field) + the literal path
  argument. TS `axios`/`ky` instances exported from another module, built
  by a factory, or passed as a parameter carry `baseURL` across imports.
- **Relative client URLs**: `fetch('/projects')` matches routes in the
  same repo (or a repo mapped via `services:`), never an arbitrary repo.
- **Injected callables**: dataclass/attrs `Callable` fields set by keyword
  at construction sites in other modules (union of all sites, inferred);
  `svc = Svc(run_query); svc.go()` on a module-level instance resolves to
  the class method. Unresolvable typed dispatch emits `unresolved_calls`.
- **Silent failures**: Go cross-package calls via import path with no
  `go.mod` fall back to matching the import-path suffix against a scanned
  directory and print a warning. Non-DB calls inside a Firestore
  `.update({...})` / `.set({...})` dict (for example `datetime.now()`) do
  not add `reads table:unknown`. `table:unknown` stays on the origin
  function; entrypoints expose it only as `dynamic_table` /
  `dynamic_table_unknown` (flag/count), never as a reach edge.

### Changed

- Graph format **0.2.5**. Entrypoint nodes may carry `dynamic_table` and
  `dynamic_table_unknown`. Top-level `warnings` holds Go `go.mod` fallback
  notes (also printed by `bizmap scan`).

## 0.2.4 — 2026-11-19

Chained TypeScript routers, Deno/Edge serve call-graphs, outbound HTTP compose,
and cross-repo rematch after a private evaluation of v0.2.3. Behavior is generic;
tests and fixtures are synthetic only (`orders`, `users`, `tenants`,
`documents`, `jobs`, `items`).

### Fixed

- **Chained Hono-style routers**: `export const app = new Hono().use('*', mw).get('/a', h).post('/b', h)`
  (with or without type args such as `new Hono<{Bindings: B}>()`) again
  records each `.get/.post/.put/.patch/.delete/.all`. `_is_router_factory`
  walks the member-call chain to `new Hono()` / `express()` / `Router()` /
  `fastify()`. `.route('/x', sub)` and `.basePath('/v1')` apply prefixes.
  `client.get` / `axios.get` / `Map.get` stay outbound HTTP, not routes.
- **Deno/Edge `serve`**: `serve(async (req) => …)`, `Deno.serve(handler)`,
  and HOF wrappers `serve(withAuth(async (req) => …))` emit call edges from
  the handler body into imported helpers, so `supabase.from` / SQL in those
  modules reaches tables on the Edge entrypoint.
- **Go outbound HTTP**: `http.NewRequest` / `NewRequestWithContext(method, url, body)`,
  `client.Do`, and `http.Get` / `http.Post` resolve urls from const / env /
  `fmt.Sprintf` / string concat into `calls_http` (or `unresolved_http`).
- **TS HTTP compose**: `axios.create` / `ky.create` / `ofetch.create`
  `{baseURL}` then `api.get('/x')` becomes `base+'/x'`. Wrapper functions
  `request(path)` that prepend a base const/env compose the same way.
- **Python injected callables**: `deps.run_query(Q)`, `self.run_query`, and
  dataclass `Callable` fields follow to the unique function so SQL reaches
  tables. Dynamic Firestore `collection(name)` is `collection:unknown`.
  `@patch("x.x.logger")` is not an HTTP entrypoint — only decorators on a
  known FastAPI/Flask app, APIRouter, or Blueprint.
- **Registry merge rematch**: `bizmap registry merge` re-runs HTTP and queue
  matching from each function's `http_calls` / `unresolved_http`, so merging
  per-repo graphs yields the same cross-repo links as a joint scan.
- **Multi-path names**: `bizmap scan a b c --config shared.yaml` keeps each
  repo's own name (per-repo `bizmap.yaml` or path basename), not the shared
  config `name:`.
- **Persisted unresolved HTTP**: unmatched outbound calls are stored on the
  caller and as top-level `unresolved_http` in the graph JSON, so stderr
  counts match the graph.

### Changed

- Graph format **0.2.4**. Top-level `unresolved_http` (and optional `queues`)
  round-trip in `bizmap.graph.json`. Function nodes keep `http_calls` for
  rematch.

## 0.2.3 — 2026-11-19

Precision and hang fixes after a private evaluation of v0.2.1/v0.2.2.
Behavior is generic; tests and fixtures are synthetic only (`orders`,
`users`, `tenants`, `documents`, `jobs`, `items`).

### Fixed

- **Go Group prefix hang**: `_path_prefixes` is scoped per function (capped
  iterations) so `g := e.Group("/a")` in one func and `g := e.Group("/b")`
  in another (including `_test.go`) cannot loop forever. Groups passed as
  parameters or returned keep their prefix when resolvable; otherwise the
  prefix is marked unresolved instead of being dropped.
- **INSERT ON CONFLICT EXCLUDED**: literal
  `INSERT INTO t (...) VALUES ... ON CONFLICT (...) DO UPDATE SET c = EXCLUDED.c`
  is not a dynamic table. `has_dynamic_table_hole` (from v0.2.2) still fires
  for genuine interpolated table names (`"UPDATE "+t+" SET"`,
  `EXECUTE format('%I')`). `SELECT ... FOR UPDATE OF alias` does not create
  phantom columns.
- **TS/JS routes**: on top of v0.2.2's tree-sitter-typescript engine,
  `client.get`, `api.post`, `axios.get`, `fetch`, and `Map.get` / `.delete`
  are not Express routes. Routes require a known app/router receiver and a
  string-literal (or resolvable const) path. Client calls become outbound
  `calls_http`.
- **HTTP matching**: an unresolved client path never wildcards onto every
  `{param}` route. Match only when method and a meaningful literal prefix
  agree. `PATH = A if cond else B` emits both candidates; `self.base_url + path`
  resolves `base_url` from env/const.
- **Handler/table resolution**: TS inline-arrow (and same-module) handlers
  attribute `supabase.from('t')` access to the route. Firestore dynamic
  collection names become `collection:unknown`. Go interface / Python
  Protocol calls with no scanned implementer leave `unresolved_calls`.
  Module-level SQL dicts via an injected executor still resolve.
- **Packaging**: non-editable `pip install .` no longer hits a hatch
  duplicate `bizmap/rules/__init__.py`. CI runs a non-editable install and
  `bizmap --version`.

### Changed

- Graph format **0.2.3**. Function/entrypoint nodes may carry
  `unresolved_calls` and unmatched client calls keep an `unresolved_http`
  marker instead of wildcard edges.

## 0.2.2 — 2026-11-19

Forward refs, external relations, object dispatch, and TypeScript servers
after a private evaluation of v0.2.1. Behavior is generic; tests and fixtures
are synthetic only (`orders`, `users`, `tenants`, `documents`, `jobs`).

### Fixed

- **Forward-referenced SQL functions**: plpgsql bodies are re-analyzed after
  the full schema replay, so `CREATE FUNCTION` that appears before
  `CREATE TABLE` (pg_dump order) still records writes/reads of those tables.
- **Dynamic table names**: `"UPDATE "+t+" SET ..."` (Go/Python) and
  `EXECUTE format('UPDATE %I ...')` again emit `writes -> table:unknown`
  with confidence `unknown` and `candidates` when DDL is present — they
  no longer disappear.
- **Missing relations**: FROM/JOIN/INSERT/UPDATE names that are not in the
  reconstructed schema become `view:` / `table:` nodes with
  `kind: unresolved_relation` (and edges), so a route still shows
  `reads view X`. `DROP TABLE` leaves `dropped: true` when the name is
  still referenced. Schema-qualified built-ins (`storage.*`, `vault.*`,
  `auth.*`, `pg_*`, `information_schema.*`) are `external` with
  `kind: external_relation`.
- **Object dispatch**: `Drainer(outbox)` then `self.outbox.settle(...)`,
  and `def run(store: Store): store.save(...)`, follow the class method
  (by annotation, constructor assignment, or a single implementer).
  Confidence `inferred`. Python and Go.
- **Cloud Tasks / Pub/Sub**: a one-hop helper
  (`create_task(task={'http_request': {'url': _get_url()}})`) is traced
  to the helper's return value. `queues: {queue_name: <entrypoint or route>}`
  links a producer even when no `http_url` was extracted.
- **HTTP origin keys**: unresolved client URLs report the env var
  (`os.environ["X_URL"]`, `os.getenv`, `settings.x_url`), a constant, or
  a hostname — not a local variable such as `base`. `services:` env
  matching uses that origin (`base = os.environ["X_URL"]; post(base+"/p")`
  links to `/p` in another repo via `env: X_URL`).

### Added

- **TypeScript server detection** via `tree-sitter-typescript`: Express /
  Fastify / Hono routes (`app.get`, `router.post`), Supabase JS
  `.from('t').select/insert/update/delete/upsert` and `.rpc('fn')`,
  `pg`/`postgres` `query()` SQL, and Supabase Edge Functions
  (`Deno.serve` / `serve` in `supabase/functions/<name>/index.ts` →
  HTTP `/functions/v1/<name>`).

### Changed

- Graph format **0.2.2**: table/view `kind` may be `unresolved_relation`
  or `external_relation`; tables may be `dropped: true`.

## 0.2.1 — 2026-11-19

Precision fixes for SQL functions, views, and service links after a private
evaluation of v0.2.0. Behavior is generic; tests and fixtures are synthetic
only (`orders`, `users`, `tenants`, `documents`, `jobs`).

### Fixed

- **Quoted / schema-qualified DDL** (pg_dump style): `CREATE FUNCTION`,
  `CREATE TRIGGER`, `CREATE TABLE`, `CREATE VIEW`, and `EXECUTE FUNCTION`
  accept `"public"."fn"` and `"trg"` identifiers; quotes are stripped.
- **PL/pgSQL junk table nodes**: comments and string literals are masked
  before body analysis; DECLARE'd variables, INTO targets, and PL/pgSQL
  keywords (`FOUND`, `PERFORM`, `ANY`, `IF`, `RETURN`, `RAISE`, …) are
  never table nodes. With DDL present, unknown names become an
  `unresolved` attribute on the SQL function, not a `table:` node.
- **SQL dicts and injected runners**: module-level dict/constant SQL
  (`QUERIES = {"a": "SELECT … FROM orders"}` then `run_query(QUERIES[key])`)
  is expanded. A function that receives a callable (`run_query`) and calls
  it with SQL attributes tables to that function. Entrypoints link to
  functions passed as arguments, dataclass fields, or aliases.
- **Views**: `CREATE VIEW` / `CREATE MATERIALIZED VIEW` (and Alembic
  `op.execute` of the same) are `view` nodes with `derives_from` edges to
  base tables. `bizmap why` on a base table shows through readers of the
  view.
- **Firestore document refs**: `ref = db.collection(X).document(id)` then
  `ref.set/update/delete/get()`, `doc_ref = col.document()`,
  `transaction.update(ref, …)`, and `batch.set(ref, …)` resolve to the
  collection.
- **Queue consumer link**: Cloud Tasks / Pub/Sub enqueue edges connect to
  the consumer entrypoint by matching the task URL path suffix (including
  when the base comes from env) to an HTTP/Firebase route, or via a
  `queues:` mapping in `bizmap.yaml`.
- **Cross-repo HTTP env bases**: `services:` entries match by env var name
  (`os.environ["BILLING_URL"]`, `settings.billing_url`) or hostname.
  `bizmap scan` prints `N unresolved HTTP calls` (with the env/host keys
  seen) so missing `services:` declarations are obvious.

### Changed

- Graph format **0.2.1**: node type `view`; edge type `derives_from`.

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
