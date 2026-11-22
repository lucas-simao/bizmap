# `bizmap who-writes`, `who-reads`, `trace`

Questions about one table, collection or column, answered from the graph in
the terminal: who writes it, who reads it, and where its data comes from and
goes to. They only read `bizmap.graph.json` (no scan, no network), so they
answer in a second or two even on a graph of ~70k nodes.

```bash
bizmap who-writes ENTITY [--path] [--format text|json|md] [--lang en|pt-BR] [--graph bizmap.graph.json]
bizmap who-reads  ENTITY [--path] [--format text|json|md] [--lang en|pt-BR] [--graph bizmap.graph.json]
bizmap trace      ENTITY [--depth 2] [--direction up|down|both] [--format …] [--lang …] [--graph …]
# all three: [--no-cache] [--cache-dir DIR]
```

The examples below use a small fictional system: `orders-api` (Go) saves
orders in Firestore and mirrors them into Postgres, `shop-web` (TypeScript)
reads the collection, and `billing-worker` (Python) consumes the
`order-created` topic and also updates `orders.status`.

## Naming the entity

| You type | It means |
| --- | --- |
| `orders` | anything called `orders` (asks you to choose if that is more than one thing) |
| `table:orders`, `collection:orders`, `view:orders` | only that kind |
| `orders-api/table:orders` | only that repo's node |
| `orders.status`, `column:orders.status` | one column |

**Ambiguous names are never resolved silently.** A Firestore collection and a
Postgres table both called `orders` are different things: the command lists
them and exits with code 2.

```text
$ bizmap who-writes orders
'orders' names 2 different things. Qualify it:

  collection · Firestore · orders-api, shop-web   →  collection:orders
  table · Postgres · billing-worker, orders-api   →  table:orders
```

The *same* table or collection seen by several repos (same kind and name, same
or unknown database) is one entity: the answer groups it by repo and says so.
To look at one repo only, qualify it (`orders-api/table:orders`).

## `who-writes` / `who-reads`

Every function that writes (reads) the entity, by repo, starting from what runs
it: an HTTP route, a queue consumer, a job, a trigger, or — when nothing starts
it — its outermost caller (a `main`, an exported function).

```text
$ bizmap who-writes table:orders
Who writes orders (table · Postgres · billing-worker, orders-api)

Same table in 2 repos: billing-worker, orders-api. To pick one, qualify it (billing-worker/table:orders).

billing-worker
  pubsub order-created → consumer.handle_order_created → repo.mark_billed  writes orders (status)  [exact]
      billing/repo.py:24

orders-api
  job mirror → Job.Run  writes orders (customer_id, id, total)  [exact]
      internal/mirror/job.go:30
  PATCH /orders/{id}/status → OrdersHandler.UpdateStatus → Repo.SetStatus  writes orders (status)  [exact]
      internal/orders/repo.go:33
  main → … → Handle → (via 1 function) → mirrorTx.WriteOrder  writes orders  [inferred]
      internal/pgstore/store.go:47

Could change this answer:
  2 write(s) with a table name built at runtime (billing-worker: 1, orders-api: 1) could not be resolved: app.touch [billing-worker], Repo.BulkFix [orders-api]
```

How to read a line: *entrypoint → handler → (via N functions) → the function
that writes*, the columns it writes, the confidence of the weakest link
(`exact` > `inferred` > `heuristic` > `unknown`), and the `file:line` of the
write. Intermediate functions are folded into "via N functions"; **`--path`**
shows all of them.

When nothing but start-up code reaches the writer — a `main`, a job — the
wiring right after it (`buildRouter`, `Register*`, `setup*`, `New*Server`,
`*Router`, `run`, the closures inside them) is folded into `…`, so the line
starts at the first real handler: `main → … → Handle → (via 1 function) →
mirrorTx.WriteOrder`. After an HTTP route, a consumer or a trigger nothing is
folded that way: the function the route calls is its handler, whatever its
name. `--path` still shows every step
(`main → buildRouter → RegisterEvent → RegisterEvent.func1 → Handle → Service.Sync → mirrorTx.WriteOrder`).

## `trace`

Where the entity's data goes (**downstream**) and where it comes from
(**upstream**), following:

- `feeds` edges — values copied from one table or collection into another
  (a Firestore → Postgres mirror, an `INSERT … SELECT`, a copy through a queue);
- **queues** — the code that writes the entity also publishes a message; the
  consumers in other repos, and what they write;
- **HTTP** — the code that writes the entity calls a route of another
  scanned service, and what that route writes; upstream, the services that call
  the route that writes it.

```text
$ bizmap trace collection:orders
Trace orders (collection · Firestore · orders-api, shop-web)

Downstream (where the data goes):
  feeds: orders (collection · Firestore · orders-api) → orders (table · Postgres · orders-api)  [heuristic]  customerId→customer_id, total→total via Job.Run
  queue order-created: POST /orders → Publisher.OrderCreated publishes
    consumed by billing-worker: pubsub order-created  [inferred]; then writes orders (table · Postgres · billing-worker)

Upstream (where it comes from):
  written by POST /orders → Service.Save [orders-api]  (orders (collection · Firestore · orders-api))
    called over HTTP by orders.createOrder [shop-web]

Could change this answer:
  1 HTTP call(s) from the functions involved matched no route
```

`--depth N` (default 2) is how many hops to follow; `--direction up|down|both`
(default `both`). A table with many writers lists the first 50 in a trace
(`bizmap who-writes` lists them all).

## What could change the answer

Every answer ends with what the graph could **not** resolve and could change
it, so a short list never reads as a complete one:

- reads / writes whose table or collection name is built at runtime, in the
  entity's repos — `table:unknown` / `collection:unknown` in the graph: SQL
  with a table name from a variable (`UPDATE {table_name} …`), and Firestore
  with the collection from a variable or parameter in Go
  (`Collection(coll)`, `CollectionGroup(name)`, `Doc(path)`) and in
  JavaScript / TypeScript (`collection(db, x)`, `doc(db, name, id)`,
  `db.collection(x)`). They are counted per repo with the functions that do
  them, and never turned into a collection of their own:

  ```text
  Could change this answer:
    2 write(s) with a collection name built at runtime (orders-api: 1, shop-web: 1) could not be resolved: Service.Archive [orders-api], orders.saveTo [shop-web]
  ```
- in `trace`: publishes to a topic only known at runtime, and HTTP calls from
  the functions involved that matched no route.

## The index cache

The first question on a graph writes an index cache — the same nodes and edges
with the indexes already built, in a format Python loads about twice as fast —
in your **user cache folder, never in the repo**:

| System | Folder |
| --- | --- |
| Linux | `$XDG_CACHE_HOME/bizmap/index` (default `~/.cache/bizmap/index`) |
| macOS | `~/Library/Caches/bizmap/index` |
| Windows | `%LOCALAPPDATA%\bizmap\index` |

One file per graph, named after the graph's absolute path, so two repos never
share one; the 16 most recent are kept. A later question uses it only while the
graph has the same path, size, modification time and content fingerprint (the
first and last MiB): a new scan is picked up on the next question. A cache from
another Python version is rebuilt.

- `--no-cache` (or `BIZMAP_NO_CACHE=1`): neither read nor write it.
- `--cache-dir DIR` (or `BIZMAP_CACHE_DIR=DIR`): put it elsewhere.
- bizmap 0.13.1 wrote `bizmap.graph.idx` next to the graph; it is no longer
  used (the commands point it out): delete it and keep it out of git.

`orjson`, when installed, also speeds up the first read.

## Output formats

- `--format text` (default) for the terminal.
- `--format md` — headings, tables and lists ready to paste in a pull request.
- `--format json` — the stable contract for tools (the MCP server, the GitHub
  Action), described below.
- `--lang pt-BR` writes the text and Markdown in Portuguese (default English).

Exit codes: `0` answered, `1` nothing matches the name, `2` the name is
ambiguous. With `--format json` the error is printed as JSON on stdout too.

### JSON (`bizmap.query/v1`)

Keys are sorted and lists ordered (by repo, confidence, name), so the same
graph and question always give the same bytes. A node anywhere in the output is
`{id, type, label, repo?, evidence?: "file:line", store?, db?, kind?, method?, path?}`.

`who-writes` / `who-reads`:

```json
{
  "schema": "bizmap.query/v1",
  "query": {"command": "who-writes", "entity": "table:orders"},
  "access": "writes",
  "entity": {"kind": "table", "name": "orders", "store": "Postgres",
             "repos": ["billing-worker", "orders-api"], "nodes": [{"id": "…"}]},
  "results": [
    {
      "function": {"id": "orders-api/function:internal/orders.Repo.SetStatus", "label": "Repo.SetStatus", "…": "…"},
      "repo": "orders-api",
      "entity_node": "orders-api/table:orders",
      "evidence": "internal/orders/repo.go:33",
      "columns": ["status"],
      "confidence": "exact",
      "entrypoints": [
        {"entrypoint": {"label": "PATCH /orders/{id}/status", "…": "…"},
         "handler": {"label": "OrdersHandler.UpdateStatus", "…": "…"},
         "wiring": 0, "via": 0, "path": [{"…": "…"}], "confidence": "exact"}
      ],
      "entrypoints_partial": false
    }
  ],
  "unresolved": [
    {"kind": "dynamic_table", "access": "writes", "repo": "orders-api", "count": 1, "functions": [{"…": "…"}]}
  ]
}
```

- `store`: `Firestore`, `Postgres`, `MySQL`, `SQLite`, `SQL Server` or `SQL`.
  `SQL` only means the engine is unknown: a node that is `SQL` takes the engine
  of the same table in another repo when exactly one is known, so one entity
  has one label (in text, md, JSON and the HTML). A list only when known
  engines really differ. The engine comes from the repo's SQL driver imports,
  its dependency manifests (`requirements*.txt`, `pyproject.toml`, `Pipfile`,
  `package.json`, `go.mod`) and literal DSNs (`postgresql://…`).
- `entrypoints`: empty list → nothing reaches the function; otherwise at most
  25, shortest path first. `entrypoints_partial` is `true` when there are more,
  or the walk stopped in a very dense call graph. `handler` is `null` when the
  entrypoint calls the writer directly; `via` counts the functions between the
  handler and the writer; `wiring` counts the start-up functions folded into
  `…` right after a `main` / job root (0 otherwise).
- `unresolved` (`who-*`): one item per repo and kind (`dynamic_table`,
  `dynamic_collection`), with `access`, `repo`, `count` and the `functions`;
  `trace` has the same items under `unresolved.dynamic_writes`.
- `confidence` on an entrypoint is the weakest link on the path *and* the
  access.

`trace`:

```json
{
  "schema": "bizmap.query/v1",
  "query": {"command": "trace", "entity": "collection:orders", "depth": 2, "direction": "both"},
  "entity": {"…": "…"},
  "downstream": [
    {"kind": "feeds", "depth": 1, "from": {}, "to": {}, "confidence": "heuristic",
     "columns": [{"from": "customerId", "to": "customer_id", "confidence": "heuristic"}],
     "via": [{}], "evidence": "internal/mirror/job.go:18"},
    {"kind": "queue", "depth": 1, "after": {}, "trigger": {}, "queue": {"name": "order-created"},
     "producers": [{}], "consumers": [{"consumer": {}, "repo": "billing-worker", "writes": [{}], "confidence": "inferred"}],
     "writes": [{}]},
    {"kind": "http", "depth": 1, "after": {}, "trigger": {}, "caller": {}, "route": {}, "confidence": "exact",
     "evidence": "src/x.ts:9", "writes": [{}]}
  ],
  "upstream": [
    {"kind": "writer", "depth": 1, "into": {}, "trigger": {}, "function": {},
     "queues": [{"queue": {}, "producers": [{}]}], "http_callers": [{}], "http_callers_total": 1},
    {"kind": "more_writers", "depth": 1, "into": {}, "count": 12}
  ],
  "unresolved": {"queues": [], "http": [{"function": {}, "method": "POST", "url": "…", "reason": "base_unknown"}],
                 "dynamic_writes": [{"kind": "dynamic_table", "repo": "orders-api", "count": 1}]}
}
```

`feeds` hops also appear upstream (with `from` the source). `queues` and
`http_callers` are present on a `writer` hop only when there is something to
show.
