# YAML detector rules

BizMap ships language engines (Go, Python, and a JavaScript/TypeScript
pass in v0.2) that interpret **declarative YAML rules**. Adding a framework
should mean adding a YAML file, not a parser.

Bundled rules live in `src/bizmap/rules/`. Pass extra files or directories with
`bizmap scan --rules path`.

## Rule shape

```yaml
id: echo.v4.routes          # unique
language: go                # go | python | javascript
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
  use the same string resolver.
- SQLAlchemy models (`__tablename__`, `mapped_column`, `Table(...)`) and
  Alembic `op.create_table` / `add_column` / `drop_*` feed the schema
  replayer. Alembic `upgrade` functions are tagged `role: schema`.
- Firestore `collection(name).document(...).set/get/...` walks the chain;
  the last `collection()` name is the `collection:` node. Nested
  subcollections are not joined into a single path.
- Queue produce sites (`delay`, `enqueue`, `create_task`, `publish`) emit
  `enqueues`. Cloud Tasks HTTP URLs also become `calls_http` when they
  match a scanned route.
- `supabase.rpc("name")` becomes a `calls` edge to `function:sql.name`
  when that SQL function exists.

## Go resolution (v0.1.3)

- Function **display names** are the directory relative to the scan root,
  always (`internal/services/appointments.Service.Run`). Packages are still
  keyed by **import path** for resolution.
- Calls and Echo handlers resolve through each file's import aliases.
- Method values (`h.Create`) use a `(import-path, Type, Method)` key after
  inferring `h` from constructors, composite literals, parameters, and struct
  fields. There is **no** `matches[0]` fallback on name collisions.
- Import-alias or unknown-receiver calls (`middleware.Current(c)`, `s.Now()`)
  stay unresolved when the package is unscanned or the type is unknown.
  Inferred call edges require a unique callee inside that package/type set
  and do not propagate table edges otherwise.
- Injected **func fields** (`s.ring(...)` with `Service{ring: notify.Send}` or
  a constructor argument) resolve to the single assigned function.
- **Interface dispatch**: a call on an interface-typed field, parameter, or
  constructor-injected value (`s.Bell.Changed(e)`) follows every scanned type
  that has the interface's method set. One implementer → `inferred` call edge;
  several → edges to all, `inferred`, with `candidates`.
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

## Bundled rules (v0.2.1)

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
| `express.yaml` | active | Express routes, `fetch()`, `supabase.rpc` (JS/TS) |

The engine still walks a simple intra-project call graph in code (not YAML) so
handler → service → repository reads/writes propagate to HTTP/job entrypoints
as table-level `inferred` edges. HTTP clients and queue producers add
`calls_http` / `enqueues` the same way.
