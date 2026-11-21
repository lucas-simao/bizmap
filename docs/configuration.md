# Configuration: `bizmap.yaml`

`bizmap.yaml` is **optional**. Without it BizMap names the repository after its
folder and finds everything it can on its own. The file only fills gaps that
static analysis cannot close: which environment variable points at which
service, which repositories share a database, what to leave out, and facts
that are not in the code.

## Where it is read from

For each scanned repository, in order:

1. `--config path/to/bizmap.yaml` when given;
2. `bizmap.yaml` (or `bizmap.yml`) in the scanned folder;
3. `bizmap.yaml` at the git top level, so scanning a sub-folder keeps its excludes.

A **workspace** file in a parent folder that lists the repository under
`repos:` is also read: its `services:` and `database:` apply to every
repository it lists, so `bizmap scan web` alone and a joint scan see the same
service map.

## Complete example

```yaml
# bizmap.yaml (all keys optional)
name: api                     # repository name in the graph (default: folder name)
database: checkout            # repos with the same database share table ids
databases:                    # name the connections a service opens (the `db` of its tables)
  orders_db: { env: ORDERS_DATABASE_URL }
  summary_db: SUMMARY_DATABASE_URL                     # short form
push_subscriptions:           # Pub/Sub topic delivered to a route of this repo
  order-announce: POST /pubsub/orders

repos:                        # makes this folder a workspace: scan these jointly
  - web
  - api
  - billing

services:                     # which URL / env var / host points at which repo
  billing: http://billing.internal                     # short form: URL alone
  api: { env: VITE_API_URL, prefix: /api/v1 }          # env value already ends in /api/v1
  backend: { env: BACKEND_URL, repo: core-api }        # key ≠ repo name: name the repo
  search:
    url: https://search.internal
    env: [SEARCH_URL, SEARCH_BASE]                     # one env var or a list
    hostname: search.internal

queues:                       # queue / topic name → consumer it triggers
  ingest: ingest
  jobs: on_jobs

constants:                    # values defined in code BizMap does not scan
  PathPrefix: /api/v1
  shared.Base: /items

exclude:                      # globs, relative to the repository
  - cmd/seed
  - demo/

scan:                         # what is left out by default, and how to bring it back
  include: ["*_test.go", .yarn]
  gitignore: true             # false: also scan .gitignored paths
  skip_generated: true        # false: also scan build caches, bundles, generated Go
  include_tests: false        # true (or --include-tests): also scan Python and Go tests
  expand_star: false          # true: SELECT * reads every schema column

discover:                     # only read by `bizmap scan --discover DIR`
  depth: 3
  exclude: [archive]
  prefer: [clients/shop-main]

overrides:                    # facts static analysis cannot see
  columns:
    orders.status:
      why: "Lifecycle: pending → paid | expired"
      writers:
        - symbol: repository.Repo.ExpireOrders
          confidence: inferred
  add_edges:
    - type: writes
      from: function:jobs.Nightly.Run
      to: column:orders.archived_at
      confidence: inferred
```

## Keys

### `name` and `database`

- **`name`** — the repository's name in the graph. Ids of functions, routes and
  jobs are qualified with it in joint scans (`api/main.Repo.InsertOrder`).
  With `--discover`, a repository without `name:` is named after its `origin`
  remote, else its folder.
- **`database`** — repositories that declare the same value share table and
  collection ids, so `bizmap why orders.status` lists writers and readers from
  every service that touches that database. Without it, each repository's
  tables stay separate.

### `databases`

Names the databases a service connects to, by the env var (or config field)
its connection is built from. Tables the code reaches through that
connection get `db: <name>` in the graph, and `bizmap why` / `report` /
the HTML show `orders_db.pedidos`. Without it the name comes from the
variable when it looks like a DSN (`ORDERS_DATABASE_URL` → `orders_db`;
`DB_SCHEMA` or `DB_HOST` name nothing unless listed here). See
[Data flow](graph-schema.md#data-flow-between-tables-feeds).

```yaml
databases:
  orders_db: { env: ORDERS_DATABASE_URL }
  summary_db: { env: [SUMMARY_DATABASE_URL, SUMMARY_DSN] }
  legacy: PG_LEGACY                      # short form
```

### `repos`

Turns the folder into a **workspace**: `bizmap scan .` (and the GitHub Action
with `path: .`) scans the listed folders jointly, as `bizmap scan web api
billing` would, and links HTTP calls between them. Paths are relative to the
workspace file. Each listed repository keeps its own `bizmap.yaml`.

### `services`

Maps the URLs that HTTP clients call to the **scanned repository** that serves
them. A key (or its `repo:`) is the name of a scanned repository; with
`--discover`, the name `--list` shows.

=== "Short form"

    ```yaml
    services:
      billing: http://billing.internal
    ```

    The URL alone. Calls to that host match routes in the `billing` repo.
    Environment variables named after the key (`BILLING_URL`, `BILLING`,
    `billing_url`) are recognised without listing them.

=== "Long form"

    ```yaml
    services:
      api:
        url: http://api.internal      # also: base, host
        env: [VITE_API_URL, API_URL]  # one name or a list
        hostname: api.internal
        prefix: /api/v1               # the env value already ends in /api/v1
        repo: core-api                # the repo that serves it, when ≠ key
    ```

| Key | Meaning |
| --- | --- |
| `url` / `base` / `host` | Base URL (or host) the clients call. |
| `env` | Environment variable(s) that hold the base URL (`process.env.X`, `import.meta.env.X`, `os.environ["X"]`, `os.Getenv("X")`). |
| `hostname` | Extra host name that points at the same service. |
| `prefix` | Path the base already includes. A call to `{API_URL}/orders` then matches the route `/api/v1/orders`. |
| `repo` | Name of the scanned repository that serves these calls, when it differs from the key. |

`services:` works in each repository's `bizmap.yaml` and in the workspace or
`--discover` root. When every call that matched through an env key goes to one
repository, the key's other calls are linked to it too (`via: env_inferred`).
Calls that still match nothing are kept as `unresolved_http` with a reason, and
`bizmap scan` prints a ready-to-paste `services:` hint for them.

### `queues`

Links a producer to its consumer when the queue or topic name alone does not:
the value is the consumer's function name, or an entrypoint id / route
(`entrypoint:http:POST:/jobs/drain`).

### `push_subscriptions`

A Pub/Sub push subscription delivers a topic to an HTTP route of this repo
(`gcloud pubsub subscriptions create … --push-endpoint=…`). Declaring it links
the topic to the route (`triggers queue:pubsub:<topic> -> entrypoint`), so a
producer in another repo reaches the route's writes (and `feeds`). `gcloud …
--topic=… --push-endpoint=…` commands in the repo's shell / YAML files are read
too. A subscription whose route is not in the repo is reported in `warnings`.

```yaml
push_subscriptions:
  order-announce: POST /pubsub/orders          # topic: METHOD /path (POST when omitted)
  invoice-paid: { path: https://billing.run.app/pubsub/paid, method: POST }
```

### `env`

Values of environment variables that name a queue or topic and have no
default in code (`os.environ.get("SHIPPED_TOPIC_NAME", "")`,
`os.Getenv("ORDER_TOPIC")`). With a value the publish gets its
`queue:<kind>:<name>` node (with `env: <VAR>`); without one it is listed in
the function's `unresolved_queues` instead of a node.

```yaml
env:
  SHIPPED_TOPIC_NAME: order-shipped
```

### `constants`

Values of constants defined in code that is not scanned (a shared module, a
generated package). By bare name, `pkg.Name`, or import-path suffix.

### `exclude` and `scan`

`exclude:` globs skip files inside the repository; `bizmap scan --exclude GLOB`
adds to them.

Never scanned unless listed in `scan: include`: `node_modules`, `vendor`,
`dist`, `build`, `.venv`. Left out by default (`skip_generated: true`,
`gitignore: true`):

- build caches (`.next`, `.yarn`, `.turbo`…, and `out/` / `coverage/` next to a
  project manifest), minified or bundled JS;
- `.gitignore`d paths;
- Python tests: `tests/` and `test/` directories, `test_*.py`, `*_test.py`,
  `conftest.py`;
- in Go: `*_test.go`, `testdata/`, `mocks/`, `*.pb.go`, `*_mock.go` and files
  headed `// Code generated … DO NOT EDIT.` — except generated data-access
  code (sqlc, or anything that runs `.Exec(` / `.Query…(`), which is always
  scanned.

`bizmap scan` names what it skipped. `scan: include:` brings paths back;
`skip_generated: false` and `gitignore: false` turn the defaults off;
`include_tests: true` (or `bizmap scan --include-tests`) scans Python and Go
tests. Calls made through an in-process test client (`TestClient`,
`httpx.AsyncClient(transport=ASGITransport(app=…))`) are counted as
`in-process test calls`, never as calls between services.
`expand_star: true` (or `--expand-star`) makes `SELECT *` read every column of
the table instead of the table as a whole.

### `discover`

Read only by `bizmap scan --discover DIR`, from the `bizmap.yaml` in `DIR`:
`depth`, `include`, `exclude`, `prefer`. Flags replace these lists. See
[scan --discover](discover.md#options-and-bizmapyaml).

### `overrides`

Anything BizMap cannot see in the code:

- `overrides.columns.<table.column>.why` — the meaning of a column, shown by
  `bizmap why` and the MCP `why_column` tool;
- `writers` / `readers` — extra edges from a function (`symbol`, optional
  `confidence`, `evidence`, `why`);
- `overrides.add_edges` — any edge (`type`, `from`, `to`, `confidence`,
  `evidence`).

Override edges default to `inferred` confidence.

## Errors

A key with the wrong type (`scan: include: 3`, an unknown `scan:` or
`discover:` key) stops the scan with a one-line message naming the file and the
key, instead of being ignored.
