# YAML detector rules

BizMap ships language engines (Go, Python, and JavaScript/TypeScript)
that interpret **declarative YAML rules**. Adding a framework
should mean adding a YAML file, not a parser.

Bundled rules live in `src/bizmap/rules/`. Pass extra files or directories with
`bizmap scan --rules path`.

## Rule shape

```yaml
id: echo.v4.routes          # unique
language: go                # go | python | javascript | typescript
framework: echo
description: human text
status: active              # active | stub  (stubs are documented but not run)
when:
  imports:
    contains: github.com/labstack/echo
    optional: false
detect:
  calls:                    # Go / Python call expressions
    method:
      in: [GET, POST, PUT, PATCH, DELETE]
      # or: regex: "^(Exec|Query|QueryRow)$"
    arguments:
      path: { index: 0, capture: string }
      handler: { index: 1, capture: ident_or_selector }
      sql: { index: 1, capture: string_or_ident }
  go_statement: true        # any `go` statement (v0.1.1)
  go_call:                  # still accepted; engine no longer requires method==Run
    method: Run
  requires_inside_callee:
    calls: { method: { in: [NewTicker, Tick, After] }, receiver: time }
    cron_imports: { contains: [github.com/robfig/cron, github.com/go-co-op/gocron] }
    cron_methods: { in: [AddFunc, AddJob, Schedule, Start] }
  decorators:               # Python @app.get("/path")
    method: { in: [get, post, put, patch, delete] }
    arguments:
      path: { index: 0, capture: string }
options:
  receiver_path_prefixes:   # Echo Group("/api") path join
    group_method: Group
    path_arg: 0
produce:
  entrypoint:
    kind: http              # http | scheduled_job | firestore | celery_task | worker | ...
    method: "{method}"
    path: "{path}"
    handler: "{handler}"
  sql:
    dialect: postgres
    source: "{sql}"
    caller: "{enclosing_function}"
```

String arguments are resolved from:

1. interpreted / raw string literals
2. same-package `const` (and `var`) string values
3. **cross-package** `pkg.Const` via the file's import map (including aliases)
4. `+` concatenation of the above (unresolved pieces become `{pkg.Const}` placeholders)
5. local `q := constA + constB` then `Query(ctx, q)`
6. Echo `Group` prefixes, including const prefixes

Anything involving a non-literal identifier is marked `unknown`. Truncated
paths such as `/setup` from `otherpkg.PendingPath+"/setup"` are not emitted.

## Python resolution (v0.2)

- Function display names are the module path relative to the scan root
  (`catalog.db.create_order`, `python.worker.low_stock`).
- Route strings resolve from literals, same-module assignments, imported
  names (`from .paths import PREFIX`), `+` concatenation, and f-strings.
  Unresolvable pieces become `{module.NAME}` placeholders with
  `unresolved: true` — never a truncated path such as `/hidden`.
- FastAPI `APIRouter(prefix=)` and `app.include_router(router, prefix=)`
  join onto handler paths. Flask `@app.route` / `Blueprint(url_prefix=)`
  use the same string resolver. Factories that return `FastAPI()` /
  `APIRouter()` / `Flask()` (and apps imported from another module) keep
  their `@app.get` / `@app.route` / `@app.api_route`. Otherwise a
  module-level `@name.get("/path")` is accepted; `@patch("x.y")` is not.
- SQLAlchemy models (`__tablename__`, `mapped_column`, `Table(...)`) and
  Alembic `op.create_table` / `add_column` / `drop_*` feed the schema
  replayer. Alembic `upgrade` functions are tagged `role: schema`.
- Firestore `collection(name).document(...).set/get/...` walks the chain;
  the last `collection()` name is the `collection:` node. Nested
  subcollections are not joined into a single path.
- Queue produce sites (`delay`, `enqueue`, `create_task`, `publish`) emit
  `enqueues`. Cloud Tasks HTTP URLs also become `calls_http` when they
  match a scanned route. A one-hop helper
  (`create_task(task={'http_request': {'url': _get_url()}})` where
  `_get_url` returns an f-string) is traced to that return value.
  `queues: {queue_name: <entrypoint id or route>}` links a producer even
  without an extracted `http_url`.
- Constructor / annotation dispatch: `Drainer(outbox)` then
  `self.outbox.settle(...)`, or `def run(store: Store): store.save(...)`,
  follows the class method (`inferred`). A single Protocol/ABC
  implementer is used when the annotation is abstract. Dataclass/attrs
  `Callable` fields union every construction site; `svc = Svc(fn); svc.go()`
  on a module-level instance resolves to the class method. Unresolvable
  typed dispatch is `unresolved_calls`. Non-DB calls inside Firestore
  `.update({...})` / `.set({...})` dicts are not SQL.
- Client URLs keep the env/host origin (`os.environ["X_URL"]`,
  `os.getenv`, `settings.x_url`, a constant, or a hostname) through local
  assignments (`base = os.environ["X_URL"]; post(base+"/p")`).
  `services:` env matching uses that origin, not the local variable name.
- `supabase.rpc("name")` becomes a `calls` edge to `function:sql.name`
  when that SQL function exists.

## Go resolution (v0.1.3)

- Function **display names** are the directory relative to the scan root,
  always (`internal/services/appointments.Service.Run`). Packages are still
  keyed by **import path** for resolution. With no `go.mod`, a unique
  scanned-directory suffix of the import path is used and a warning is
  printed.
- Calls and Echo handlers resolve through each file's import aliases.
  Outbound helpers that take `path`/`method` from the caller
  (`getJSON(ctx, path, out)` with `c.baseURL + path`) emit `calls_http`
  from the call site.
- Method values (`h.Create`) use a `(import-path, Type, Method)` key after
  inferring `h` from constructors, composite literals, parameters, and struct
  fields. There is **no** `matches[0]` fallback on name collisions.
- Import-alias or unknown-receiver calls (`middleware.Current(c)`, `s.Now()`)
  stay unresolved when the package is unscanned or the type is unknown.
  Inferred call edges require a unique callee inside that package/type set
  and do not propagate table edges otherwise.
- Injected **func fields** (`s.ring(...)` with `Service{ring: notify.Send}` or
  a constructor argument) resolve to the single assigned function.
- **Object / interface dispatch**: a call on a field, parameter, or
  constructor-injected value (`s.Bell.Changed(e)`, `d.outbox.Settle(ctx)`)
  follows the concrete type assigned in a constructor or composite literal,
  or every scanned type that has the interface's method set. One implementer
  → `inferred` call edge; several → edges to all, `inferred`, with
  `candidates`.
- **Dynamic SQL**: `for _, q := range []string{"UPDATE a ...", ...}` expands
  to one SQL site per element. Helpers with a `query`/`q`/`sql` string param
  bind literals/consts at the call site and attribute tables to the caller.
- A handler argument that is a **call** unwraps to an inner func/method
  (`wrap(h.Version)`, `mw(handler)`). Factories that take data
  (`profile.StaffPhoto(cfg)`) still resolve to the factory.
- Echo methods match **exactly** (`GET`, not `options`). The path argument
  must be string-like (literal, const, or concatenation).
- `func main` is not a writer entrypoint unless it is a detected job or route.
  `bizmap.yaml` is found via `--config`, the scan root, or the git toplevel.
  `--exclude GLOB` (repeatable) merges with YAML `exclude`. `--profile`
  prints parse / resolve / sql / propagate timings to stderr.

## JavaScript / TypeScript resolution (v0.2.6)

- `.js` / `.mjs` / `.cjs` use tree-sitter-javascript; `.ts` / `.tsx` use
  tree-sitter-typescript (`language_typescript` / `language_tsx`).
- Express / Fastify / Hono: routes require a known app/router receiver
  (`express()`, `Router()`, `fastify()`, `new Hono()`, chained
  `new Hono().use().get()`, chained-export
  `export const r = new Hono().use(mw).get('/a', h)`, or typed as such)
  and a string-literal or
  resolvable const path. `.basePath('/v1')` and `.route('/x', sub)` /
  `app.use('/v1/items', importedRouter)` apply prefixes across files
  (default/named exports, nested mounts, chained-export children).
  Known HTTP clients (`fetch`,
  `axios` + instances, `ky`, `ofetch`, `got`, `superagent`) emit outbound
  `calls_http`; `Map` / `Set` / `Headers` / `URLSearchParams` /
  `searchParams` / `config.headers` do not. `axios.create` /
  `ky.create` / `ofetch.create` `{baseURL}` (exported, factory-built, or
  passed as a parameter into an object-literal factory) compose with
  later `.get('/x')`; uncomposed parameter clients stay
  `unresolved_http`. Relative
  `fetch('/x')` matches only the same repo; constructor/config `{base}`
  URLs may match across repos when the path has 2+ literal segments or a
  `services:` mapping.
  Inline-arrow handlers attribute `supabase.from('t')` access to the route.
- Supabase JS: `supabase.from('t').select|insert|update|delete|upsert`
  emits table reads/writes; `.rpc('fn')` calls `function:sql.fn` when
  that SQL function exists.
- `pg` / `postgres` `client.query('SELECT …')` is analyzed as SQL.
- Supabase Edge Functions: `Deno.serve` / `serve(handler)` (including
  `serve(withAuth(async (req) => …))`) in
  `supabase/functions/<name>/index.ts` become HTTP
  `entrypoint:http:POST:/functions/v1/<name>`, with call edges from the
  handler body into imported helpers.

## Bundled rules (v0.2.6)

| File | Status | What it detects |
| --- | --- | --- |
| `echo.yaml` | active | Echo v4 `group.POST("/path", handler)` (literals, consts, factories, wrappers; `Any`/`Match`) |
| `pgx.yaml` | active | pgx v5 `Exec` / `Query` / `QueryRow` SQL |
| `go_ticker.yaml` | active | Any `go` statement whose target uses ticker/cron patterns |
| `fastapi.yaml` | active | FastAPI `@app.get` / `APIRouter` / `include_router` |
| `flask.yaml` | active | Flask `@app.route` / `Blueprint(url_prefix=)` |
| `sqlalchemy.yaml` | active | `text()` / `execute()` SQL; ORM/Core usage is in the Python engine |
| `firebase.yaml` | active | Firebase Functions entrypoints + Firestore `collection()` |
| `celery.yaml` | active | Celery / RQ / Cloud Tasks / Pub/Sub produce + consume |
| `httpx.yaml` | active | httpx / requests client calls (`calls_http`) |
| `supabase.yaml` | active | `supabase.rpc("fn")` |
| `express.yaml` | active | Express / Fastify / Hono routes, `fetch()`, Supabase `.from` / `.rpc`, `pg`/`postgres` `query()` (JS/TS via tree-sitter-typescript). Edge Functions in `supabase/functions/<name>/index.ts` become HTTP `/functions/v1/<name>`. |

The engine still walks a simple intra-project call graph in code (not YAML) so
handler → service → repository reads/writes propagate to HTTP/job entrypoints
as table-level `inferred` edges. HTTP clients and queue producers add
`calls_http` / `enqueues` the same way.
