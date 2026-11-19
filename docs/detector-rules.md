# YAML detector rules

BizMap ships language engines (Go + Python in v0.1) that interpret **declarative
YAML rules**. Adding a framework should mean adding a YAML file, not a parser.

Bundled rules live in `src/bizmap/rules/`. Pass extra files or directories with
`bizmap scan --rules path`.

## Rule shape

```yaml
id: echo.v4.routes          # unique
language: go                # go | python | javascript (js/ts engine is v0.2)
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
  go_call:                  # `go x.Run(ctx)`
    method: Run
  requires_inside_callee:
    calls: { method: NewTicker, receiver: time }
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
    kind: http              # http | scheduled_job
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
3. `+` concatenation of the above
4. local `q := constA + constB` then `Query(ctx, q)`

Anything involving a non-literal identifier is marked `unknown`.

## Bundled rules (v0.1)

| File | Status | What it detects |
| --- | --- | --- |
| `echo.yaml` | active | Echo v4 `group.POST("/path", handler)` |
| `pgx.yaml` | active | pgx v5 `Exec` / `Query` / `QueryRow` SQL |
| `go_ticker.yaml` | active | `go x.Run(ctx)` jobs that use `time.NewTicker` |
| `fastapi.yaml` | active | FastAPI `@app.get("/path")` |
| `sqlalchemy.yaml` | active (partial) | `text()` / `execute()` SQL strings; no ORM models yet |
| `express.yaml` | stub | Documents the v0.2 JS shape |

v0.1 still walks a simple intra-project call graph in code (not YAML) so
handler → service → repository reads/writes propagate to HTTP/job entrypoints.
