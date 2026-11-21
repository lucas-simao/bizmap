# Changelog

## Unreleased

### Added

- **Firestore in Go**: `Collection("x").Doc(id).Get / Set / Update / Delete /
  Create`, `Documents(ctx).GetAll()`, `Where(…)`, `tx.Get/Set(ref)` as
  `reads` / `writes collection:x`; `DataTo(&d)` maps the struct's
  `firestore:"…"` tags, so `feeds collection:orders -> table:orders_mirror`
  lists `customerId → customer_id`, `__id__ → id`.
- **Pub/Sub publish in Go**: direct (`client.Topic(t)` / v2
  `client.Publisher(t)`) and through a wrapper constructor
  (`NewPublisher(ctx, project, []string{cfg.OrderTopic})`) used via an
  injected interface. Each constructor call is an instance followed through
  assignments, struct fields, map keys (`pubs[k] = p` from a topic map) and
  call arguments, so every caller publishes only to its instance's topics;
  only calls with the wrapper's `Publish` signature count (a domain
  `store.Publish(ctx, id)` adds nothing), and an unknown instance gives
  `unresolved_queues` with `candidates` instead of edges. Topic from a
  literal, a package constant or a config field (`getEnv("X","def")`,
  `os.Getenv` — with the default the same variable has in the config
  package —, `env:"X"` tags), with `env` on the queue node and the message
  `fields` from the `json:"…"` tags. The dataflow follows the message to a
  push route in another repo (`feeds collection:orders ->
  table:order_summary`, or `orders_mirror -> order_summary` when Go re-reads
  its table, also through `Build(row)` / `Send(ctx, pub, payload)` in other
  packages).
- Go: publishers built in `if p, err := NewPublisher(…); … else { x = p }` (also
  into a topic map) keep their topics; `candidates` only list topics of the
  constructors that build the receiver's type.
- Go SQL: a function returning `"INSERT INTO t (" + …` passed to
  `pgx.Batch.Queue` / `Exec` is `writes table:t` (table level), a literal write
  in `batch.Queue(…)` too, and a call on a closure parameter typed with an
  interface gets `calls` to its one implementation. A Firestore → Postgres
  mirror of that shape gives a table-level `feeds`.
- Go Firestore: files of a package that imports Firestore elsewhere, references
  passed to local helpers (read or write by what the helper does), and
  `refs = append(refs, …)`.
- Go Firestore: references built in a loop (`refs[i] = …Doc(id)` then
  `GetAll(ctx, refs)` / `batch.Update(refs[i], …)`) and a collection held in
  a struct field set in its constructor. A document read by one repository
  and written to SQL by another gives `feeds` through the calls.
- `env:` in `bizmap.yaml` gives values to env vars that name a queue or topic;
  without one the function lists the publish in `unresolved_queues`.

### Changed

- `queue:pubsub:<topic>` ids are never repo-qualified, so a producer and a
  push subscription in different repos meet on one node.
- Python queues: `publish` / `publish_message` count only on a
  `PublisherClient()` receiver and `enqueue` only on a `Queue(…)` (the edge
  gets `task`). The topic is resolved inside `topic_path(project, t)`, from
  parameter defaults and `os.environ.get("X", "d")`.

### Fixed

- No queue node named after an expression (`queue:pubsub:{live[:10]}`) or
  without a name (`queue:pubsub:queue`), and nothing for `.enqueue` /
  `.publish` on parameters, repositories or plain methods. Python topic
  lookups stay in the function (or class / module) they belong to.
- Joint scans no longer fail with "dictionary changed size during iteration"
  when the queue relink adds a node.

## 0.10.0 — 2026-11-21

### Added

- **Firestore in JS/TS.** Reads and writes with the web modular SDK
  (`getDoc(s)`, `onSnapshot`, `setDoc` / `updateDoc` / `addDoc` /
  `deleteDoc` on `collection(db, …)` / `doc(db, …)` / `query(…)` or a stored
  reference) and admin chains (`db.collection('x').doc(id).get()` …
  `.set()`), with the collection from a literal, a constant, an enum member,
  a template path, a multi-segment path (`collection(db, 'drawings', id,
  'versions')`, `doc(db, 'settings', 'pricing')`) or the literal a helper's
  caller passes; the file's own (non-exported) constants come first. Writes list their
  `fields`; callers and their entrypoints inherit the access. Subcollections
  are the last collection of the path (`collection:notes`, `path:
  customers/{}/notes`), as in Python. Collections get `db:
  firestore:<projectId|default>` (also from `initializeApp({ projectId })`).
- Cloud Functions Firestore triggers in TS (`onDocumentWritten('orders/{id}',
  …)`, v1 `.document(…).onWrite`) as `entrypoint:firestore:<collection>:<event>`,
  and `pubsub.topic(t).publishMessage({ json })` as `enqueues
  queue:pubsub:<t>` with the message `fields`.
- **Firestore as a `feeds` source**: a document read (`to_dict()`, `data()`,
  `stream()`, a trigger's `event.data.after.data()`) and its fields written to
  Postgres (`orders.customerId -> order_summary.customer_id`), in one
  function, through calls (across modules), and collection → collection. The
  document id is `orders.__id__` (a reference). Fields need no schema.
- **Pub/Sub push between repos**: `push_subscriptions:` in `bizmap.yaml` (and
  `gcloud pubsub subscriptions create --topic --push-endpoint` in shell / YAML
  files, commented or continued with `\`; `$VAR/path`, `${VAR}/path`,
  `$(…)/path` and full URLs match the route by path) link a topic to an HTTP route; the route's handler gets the message,
  so trigger → topic → route → `pg_insert` becomes a `feeds` in a joint scan /
  `registry merge` (fields matched by name ignoring case / `_`, or a Pydantic
  alias).

### Fixed

- SQLAlchemy Core: `pg_insert` / `sa_insert` (dialect or aliased `insert`)
  is `insert`; `order_summary.insert()` / `.update()` / `.delete()` on a known
  `Table` is a write to it; a table held in `t = order_summary` or
  `self._t = order_summary` (set in the class) resolves; `conn.execute(stmt…)`
  on a statement built above is that statement. `.values({...})`, a dict
  variable, `dict(...)` and `on_conflict_do_update(set_={...})` give their
  columns. None of these add `table:unknown` any more, and a plain
  `some_dict.values()` is no longer taken for a SQLAlchemy write.
- FastAPI / Flask: a router picked by an expression (`feeds = served if cond
  else unserved`) registers its routes, included wherever a candidate is.
- `feeds` never start or end on `table:unknown`.

### Changed

- A column passed through calls unchanged is `exact` in `column_map` (the
  `feeds` edge stays `heuristic` for call paths).

## 0.9.1 — 2026-11-21

### Fixed

- A config wrapper that spreads the caller's config into a new object
  (`api.request({ ...config, validateStatus: () => true })`, arrow or
  `async function`) passes the caller's `baseURL` / `url` / `method` through:
  `reportsGet('/reports/flow')` with `baseURL: V2` →
  `{VITE_API_URL}/api/v2/reports/flow`, never the v1 route. An object that
  sets its own `url` / `baseURL` / `method` is not treated as a pass-through.
- The wrapper's own `api.request(config)` / `api.request({ ...config })` call
  has method `{dynamic}` (it comes from the config), not `POST`.

## 0.9.0 — 2026-11-21

### Added

- **gin routes** (`rules/gin.yaml`): `GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS`,
  `Handle(method, path, h)` and `Any` (method `ANY`); `Group` chains, groups
  with middleware (`r.Group("/admin", auth())`), chained
  `admin.Group("/reports").GET(...)` and groups passed to a function in
  another package (`routes.Register(v1)` with `rg *gin.RouterGroup`). The
  handler is the last argument (middleware may come before it); methods and
  closures work as with echo.
- The scan summary groups helper calls with a dynamic path:
  `N call(s) via forecast.Client.get with a dynamic path`.
- **Data flow between databases.** A new `feeds` edge says a function copies
  values from one table into another, often in another database
  (`pedidos -> resumo_pedido`), with a `column_map`
  (`pedidos.valor_centavos -> resumo_pedido.total`, the function, `exact` /
  `heuristic`, `file:line`). Only created when a value is seen going from
  the read to the write: in one function (Go `Scan` targets and struct
  literals, JS/TS `rows` / `rows[0]` / `for…of`, Python cursors and row
  access, `INSERT … SELECT`, `UPDATE … FROM`, upserts, Firestore
  `set` / `update` / `add`, ORM writes), through calls in the same service,
  and through a queue (producer → topic → consumer, also across repos in a
  joint scan or `registry merge`). Columns that are not in the table's
  schema (plpgsql variables, parameters) are never mapped. Expressions in between give
  `transform: true`; a lookup or a permission check gives nothing; ids
  copied only into id / foreign-key columns are references, not `feeds`.
- **`db` on tables**: the database the code reaches a table through, from
  the connection's DSN env var or config field (`ORDERS_DATABASE_URL` →
  `orders_db`; `DB_SCHEMA` / `DB_HOST` name nothing) or `databases:` in
  `bizmap.yaml`, only on tables the code reads or writes; Firestore collections get
  `firestore:<project|default>`. Table ids do not change; in a merged graph
  the same table with the same `db` in two repos gets `same_as`.
- `bizmap why` shows where a column **comes from** and **goes to** through
  `feeds` (up to 3 steps); `bizmap report` has a "data flow between
  databases" section; `bizmap impact` lists the **downstream** tables and
  columns fed from a changed sync function or a changed write of a source
  column (JSON `downstream`, a Markdown section).
- HTML export: a table's page has a **Data flow** section — diagram,
  column correspondence both ways, and the diagram as Mermaid text.
- **Function signatures.** Function nodes carry `params` (in order:
  `[{name, type?, optional?}]`, `[]` when there are none) and `returns` (the
  return type's text) for Go, Python and TypeScript (plain JavaScript: names
  only). Optional fields: older graphs stay valid.
- The HTML export shows the signature (`get(id: string, opts?: Options):
  Promise<Order>`) under a function's name in its panel and page, as the
  tooltip of its row in the call tree, and for the handler at the top of a
  route. Without one in the graph: `name(…)`.
- `pytest -m browser`: the exported HTML opened in Chromium (offline), every
  kind of page visited; fails on a page error, a console error, a network
  request or the error panel. Its own CI job.
- **PR comment (GitHub Action), triggers:** the Triggers table shows the whole
  clause (`AFTER UPDATE OF status`), and "Fired by this change" answers
  **yes**, **no** (with the reason: no matching event in the writes, or the
  `UPDATE OF` columns are not set) or **unknown** (the trigger's function was
  not scanned or has no known effect).
- **GitHub Action `html` input:** also writes the offline graph page
  (`bizmap export --format html`) to a path, with an `html` output; on `push`
  events it only scans and writes the page, to publish on GitHub Pages
  (example workflow in docs/action.md). Without the input nothing changes.
- **Graph example on the docs site:** https://lucas-simao.github.io/bizmap/graph/
  (`examples/checkout`), rebuilt with the docs on every push to `main`; the
  PR `docs-site` artifact includes `graph/index.html`.
- Graph: `db_trigger` nodes have an optional `columns` list (the
  `UPDATE OF col, …` columns), only when the trigger has that clause; every
  other node is unchanged.

### Changed

- The PR comment renders much faster on large diffs (~17× for the whole
  command on a 32k-function diff); table cells sample the alphabetically first
  names.
- **HTTP labels are clean in every language**: the query string / fragment is
  dropped before matching (`{FORECAST_API_URL}/daily`, not `…/daily?lat=…`),
  and any placeholder that is not a plain name (`{a.b}`) becomes `{dynamic}` —
  no label holds `(`, `)`, `=>`, `...` or spaces. A method that is not a
  literal (`http.NewRequest(method, …)`) is `{dynamic}`.
- **One method per call site**: a path literal with no method hint links only
  to `GET` (or `ANY`) routes, never to every method of the path. A route
  parameter only matches a literal segment that looks like an id (has a digit,
  no hyphen) and only with at least two equal literal segments
  (`/time-clocks/history` no longer matches `/projects/:projectId/history`).
- A factory's internal helper (`read(url) => api.get(url)`) is followed with
  every base the factory was instantiated with, like a direct member call.
- A per-request `baseURL` set in a helper (`http.request({ baseURL: V2, url })`)
  is the call's base: `{VITE_API_URL}/api/v2/reports/flow`, never the v1 route
  with the same tail.
- The same holds when the helper hands its config to a config wrapper of
  another module (`reportsGet = (url) => requestWithStatus({ method: 'GET',
  baseURL: V2, url })` with `requestWithStatus = (config) => api.request(config)`).
- **A path literal counts as a request only when something sends it**: a
  client method, `fetch`, a config with `url` and a method, or an argument of
  a call whose name says it sends one (`apiGet(uid, '/x')`, `fetchJson`,
  `doRequest`). A URL builder that only returns a URL
  (`projectLink = (d, id) => `${d}/projects/${id}``), a route template
  (`PROJECT_ROUTE_TEMPLATE`), a menu constant (`MENU_ACCESS = { projects:
  '/projects' }`) or a hyperlink is navigation. A path helper / module const
  that nobody is seen sending no longer links by itself (`origin:
  path_helper` / `module_const`).
- Navigation is not a request: `redirectUri`/`callbackUrl`/`returnTo`-style
  variables (assigned or returned) and JS/TS test files and mocks
  (`__tests__/`, `__mocks__/`, `mocks/`, `*.test.*`, `*.spec.*`, e2e) produce
  no `calls_http`.
- Note: Go `net/http` `HandleFunc` is not detected as a route, so there is
  no duplicate route to merge there.

## 0.8.0 — 2026-11-21

### Added

- Front-end pages list **the API routes they use** ("API routes web uses"),
  by API and domain: method, path, summary, and the screens (or client files)
  that call each one; likely links marked. The service page shows the count
  per API. A route's screens are the ones that reach the function calling
  that route, not every screen of its client file.
- Screens page text has a singular ("1 screen calls the API…").
- A comment that only restates the route ("List is GET /widgets.") is no
  longer used as a summary.
- **Front ends link to the API through their client modules.** The chain page
  → hook → client method → route now reaches the graph for the shapes React /
  Vite fronts use:
  - methods of a module-level object (`export const widgetsHttpClient = {
    list: (p) => api.get(...), async create() {} }`) are functions of their
    own (`widgetsHttpClient.list`); their HTTP calls no longer sit on the
    file's `.module` node;
  - a member call on an imported object (`client.list(p)`, with
    `import { widgetsHttpClient as client }`, through barrels) or on an
    instance a factory made (`deliveryHttpClient.getSite(id)` →
    `createDeliveryHttpClient.getSite`) links to the method;
  - a function passed by reference (`queryFn: listWidgets`,
    `mutationFn: client.create`, `useEffect(load)`, `onClick={X.m}`) counts
    as a call by whoever passes it (`inferred`).
- A client made by a factory keeps its argument's env:
  `createClientApi(import.meta.env.X ?? '')`, a base built in a local and
  passed as `axios.create({ baseURL })`, and an instance passed inline to
  another factory. Those calls are no longer `base_unknown`. A factory made
  twice with two clients (the app's and a portal's) calls through both bases;
  consts follow `as string | undefined` and `?? ''` to the env.
- Fictional repro `tests/test_web_api_links.py` (two Go/echo APIs and a
  React/Vite front).

### Fixed

- Relative imports from a dotted file name (`widgets.queries.ts`,
  `*.service.ts`) resolved from the wrong folder, so its imports linked to
  nothing.
- A path in a component (`.tsx`, `.jsx`, `.vue`, `.svelte`) with no request
  hint (no client method, no `fetch`, no `url` + method) no longer becomes a
  heuristic `calls_http`: it is navigation or a label.

## 0.7.0 — 2026-11-20

### Added

- **`bizmap export --format html` reads like API docs.** A domain lists its
  endpoints (method, path, summary); opening one shows the description,
  parameters, the **JSON request body** and each **response with its JSON**
  (nested objects open on click), the screens that use it by name, its data in
  one sentence ("Writes orders. Reads products."), and **See in code** for the
  call tree. Domains follow the API's tags.
- Docs come from an **OpenAPI / Swagger file** (swag's `docs/swagger.json`,
  FastAPI's `openapi.json`; Swagger 2.0 / OpenAPI 3, JSON or YAML), found next
  to each repo or given with `--openapi [SERVICE=]FILE` / `export.openapi` in
  `bizmap.yaml`; else from **swag comments** in the graph (`@Summary`, `@Tags`,
  `@Param`, `@Success`, `@Failure`); else from the handler's comment.
- Service pages are shorter: domains with endpoint counts and data; screens,
  HTTP calls, tables and code fold under one "more" section.
- Fictional example `examples/export/api/openapi.json`.
- Request and response JSON shown like Swagger UI: an **Example** tab (a real,
  colored JSON with sample values: enum's first value, `example`, dates,
  numbers) and a **Fields** tab (field, type, required, description, nested
  fields indented).
- Specs found next to the code: also every spec in a repo's `openapi/`,
  `docs/`, `api/`, `spec/`, `specs/` folder (one per area), in the folder the
  graph's `repo_roots` names; a bad one found that way is skipped with a note,
  and the export lists the specs it used. `--openapi` for a service the graph
  does not have is an error.
- A spec without `description` takes the handler's comment/docstring; query
  parameters show their enum and default; routes without docs join the tag
  named like a segment of their path after any prefix; the side panel no
  longer repeats the raw swag comment.
- Graph format version `0.7.0` (no field changed; it follows the release).
  GitHub Action examples pin `@v0.7.0`.

### Fixed

- Front-end ↔ API noise: `mocks/` (MSW), `test-utils/`, `testing/`,
  `stories/`, `cypress/`, `playwright/` folders and `*.cy.*` / `*.mock.*`
  files are left out like tests; a service calling its own routes from code
  none of its routes reaches (a generated SDK kept in the API repo) is too.
- A screen that reaches a route only through a likely (heuristic) link is
  marked likely in "Consumed by".
- Service links (system page, side panel, map) leave out tests, mocks and
  generated code, so every count matches; "1 screen" / "1 file"; a front-end
  without UI files no longer reads "0 of 1 client files linked"; "Called by 1
  function", "1 HTTP call … is not counted".

### Changed

- `bizmap export --format html`, front-end ↔ API links:
  - **Screens are UI files** (pages, components) that reach an API call
    through the call graph (page → hook → API client → route), not the client
    files themselves; each lists the routes it uses and the client functions it
    goes through. The client layer has its own section (**API client code**),
    for every service, with the screens that use each file.
  - **Tests, mocks, fixtures, `.d.ts` and generated code** are left out of
    screens, client code, a route's consumers and the counts (the page says
    how many calls it left out).
  - A route's **consumers** list the screens first, then the calling functions.
  - **Likely** links (heuristic `calls_http`, matched from a path literal by
    its end) are dashed.
  - **Calls with no route** for every service (APIs and workers too), with what
    each reason means.
  - Module-level code reads as `pkg.file (module)`.
  - Table of contents grouped by kind (APIs, front-ends, workers, libraries),
    each service once; the routes table no longer overflows (fixed columns,
    long handler names cut with `…`).

## 0.6.0 — 2026-11-20

### Changed

- **`bizmap export --format html` reads the system like a book.** Overview
  first, detail on click, never the whole graph: system (one card per service:
  API, front-end, worker, library) → service (**domains**: routes grouped by
  the first useful path segment, large groups split into sections, small ones
  in `other`) → domain (routes by path, handler, tables read/written, callers)
  → route (**call tree**, opened node by node, with the tables and columns each
  function reads or writes) → table (columns, who reads/writes it, routes that
  reach it). Detail panel: `file:line`, `why`, path parameters (and `params` /
  `returns` when the graph has them), calls, callers, data access, HTTP calls
  and who consumes a route.
- **Front-ends**: screens (files that call the API, grouped by folder) with the
  routes each uses, and the **calls with no route** from `unresolved_http`, by
  reason. Each API route lists the screens and services that call it.
- `bizmap.yaml`: `export.domains` (`{service: {domain: [path prefixes |
  "folder:…"]}}`) and `export.domains_by: path | folder`.
- Faster on large graphs: one small index read at open, one JSON block per
  domain / front-end / table batch / folder read on first use, no derived
  edges shipped. Synthetic 107k nodes / 628k edges: 31 MB, ready in ~0.4 s,
  every level under 100 ms in headless Chrome.
- Every service has a page with something to read: besides domains and
  screens, the tables it reads and writes itself (the count matches the list)
  and its **code by folder** (functions, HTTP calls, tables), so libraries and
  clients without routes are browsable. A JS/TS service that calls APIs and
  has no routes is a front-end (a Vite app with only `.ts` files); screens
  without a UI folder are grouped by their own folder (`src/api`).
- Domains skip a first path segment shared by at least 70% of the routes.
- The service graph stays under **Map**; the graph expand/filter view is gone
  (a table's page lists who reads and writes it).
- Fictional example: `examples/export/shop.graph.json`
  (`scripts/export_demo_graph.py`).
- `--format llm` now says it is not available yet (it did not ship in 0.5.1).
- Graph format version `0.6.0` (no field changed; it follows the release).
  GitHub Action examples pin `@v0.6.0`.

## 0.5.2 — 2026-11-20

### Fixed

- **Python tests are skipped by default**, like Go's `*_test.go`: `tests/` and
  `test/` directories, `test_*.py`, `*_test.py`, `conftest.py` (counted as
  "Python test dirs/files" under `skipped:`). `--include-tests` or
  `scan: include_tests: true` scans them (and Go tests) again.
- **A test helper no longer changes production results**: an HTTP wrapper is
  only used where the call can reach it (defined in the same module,
  imported from its module, `self.x()` in its class, `mod.x()` through the
  module; a private wrapper name such as `client._post` still matches a
  method of that name). A `post(app, path, body)` helper in `tests/` used to
  capture every `.post(...)` of the repo, `httpx.post` included, turning
  `POST {HOOKS_URL}/hooks/orders/{order_id}` into `POST {base}/{path}`.
  Wrappers whose first parameter is the app or client (`_post(client, id)`)
  take the path from the next one, and a path passed by keyword is used.
- **In-process test calls are not calls between services**: calls through
  `TestClient(...)`, `httpx.AsyncClient(transport=ASGITransport(app=…))` /
  `WSGITransport` / `Client(app=…)`, functions returning one, and parameters
  receiving one, produce no `calls_http` and are not unresolved; the scan
  prints `in-process test calls: N` (`in_process_http` in the graph).
- **Clean URL labels**: a dynamic part of a Python URL keeps its name
  (`{oid}`, `{settings.API_URL}`) and anything else becomes `{dynamic}` — no
  more `{X.format(oid)}`, `{Report(\n rows=…}` or `{1 << 20}`.
  `CONST.format(oid)` with a URL template is resolved
  (`"/orders/{}/confirm"` → `/orders/{oid}/confirm`).
- **Unresolved HTTP by origin**: the scan summary splits unresolved calls
  into app / scripts / eval / tests.

## 0.5.1 — 2026-11-20

### Fixed

- **Python phase is linear on large repos** (it was quadratic: a large
  FastAPI monorepo ran for 20+ minutes). The HTTP and SQL-dict helper
  collectors ran once per file over every function of the repo: they run once
  per scan. Looking up a class's methods for each `obj.field()` call, the
  candidates of each `from X import Y`, and the routers/blueprints an import
  refers to no longer scan every function or module (name indexes and a
  module-suffix index). During propagation the schema's table names are
  computed once per schema, the callers of an origin function once per
  origin, and each access's target once per access. Synthetic monorepo
  (`scripts/bench_py.py`, 300 tables, 12 imports per module): 120 modules
  40.1 s → 8.8 s on the same CI runner, 150 → 300 modules 2.06×; the graph is
  byte-identical (checked against 0.5.0 at 30, 40 and 120 modules, and a
  golden summary in the test suite).
- **One bad SQL string no longer stops the scan**: a statement sqlglot
  cannot take apart (e.g. `"UPDATE orders ..."` in a test, which crashed in
  `_table_name`) yields no accesses; the scan summary reports how many were
  skipped and the first one.
- **`services:` hints**: only suggested when the env key's calls match routes
  in that repo; with no match they are listed as `probably external` (e.g.
  `SUPABASE_URL`). When the calls only match with a route prefix, the hint
  includes it (`api: { env: VITE_API_URL, prefix: /api/v1 }`).
- `pytest -m perf` has a Python scaling check (150 vs 300 modules).

## 0.5.0 — 2026-11-20

### Added

- **`bizmap export --format html`**: one self-contained, offline HTML page
  (Cytoscape.js, MIT, inlined; no CDN, no server). Opens on the services and
  their `calls_http` links (with link counts); click a service to expand its
  routes, functions and tables; search routes, functions, tables and columns;
  filter by table across services (the `table_usage` traversal, plus the
  functions of other services that call in over `calls_http`); services or
  tables used by 3+ services flagged, top 5 highlighted. `--lang pt-BR`.
  Subgraphs are built on demand, so ~55k-node graphs stay usable.
  `bizmap.yaml` `export.services` splits a monorepo into services.
  `--format llm` is reserved for 0.5.1. See `docs/export.md`.
  The table filter banner counts the services that read or write the table
  apart from those that only call in over HTTP ("5 services read or write
  it. Called from 3 more services over HTTP.").

### Changed

- Graph format version `0.5.0` (no field changed; it follows the release).
- Docs: `bizmap export` documented on the site (Commands → export), no longer
  "coming soon". GitHub Action examples pin `@v0.5.0` (`@v0.4.2` was never
  tagged). The sdist no longer ships `docs/assets` and `docs/img`.

## 0.4.3 — 2026-11-20

### Fixed

- **Reach across services**: `bizmap report` follows resolved `calls_http`
  from everything an entrypoint reaches, across services, without cycles and
  up to 5 hops. An entrypoint that reaches data only through other scanned
  services is counted in `reaching_data_via_http` (with `http_hops`), and
  with `--cross-service` in "reaching a table" too. "Only calls other
  services over HTTP" rows say where the chain stops: no resolved target,
  the services it calls reach no data either, or the hop limit.
- **`bizmap why` shows the whole HTTP chain**: `invoked via HTTP from
  gateway/… ← via HTTP from edge/… ← …`.
- **Front-end calls link to the API scanned with them**:
  - an env key no `services:` entry maps (`VITE_API_URL`) is linked to a repo
    when every call through it that already matched goes to that repo; its
    other calls (`/me`, `` `/carts/${id}` ``) are then matched against that
    repo only (`via: env_inferred`, listed in the scan summary and as
    `inferred_services` in the graph);
  - `services:` entries take `prefix:` (`api: {env: VITE_API_URL, prefix:
    /api/v1}`), and inside the repo a declared service points at, a unique
    route whose tail matches the call also links (the env value carries
    `/api/v1`);
  - env keys whose calls still do not link get a ready-to-paste
    `services:` hint in the scan output (`service_hints` in the graph);
  - an HTTP wrapper imported from another module
    (`import { getJSON } from '../lib/http'`) is followed at the caller.
  - README: the `services:` short form, `repo:`, `prefix:`, and that the key
    is the scanned repo's name.
- **JS/TS phase is linear and ~45% faster**: a per-file function index
  replaced a scan of every function seen so far (quadratic in the number of
  files); the collectors share one walk per file for the node types they
  need; exports are read from statements only. Synthetic React/TS repo
  (`scripts/bench_js.py`, in `pytest -m perf`): 4k files 18.8 s → 10.4 s on
  the same CI runner, 4k/1k = 4.0×.
- **Progress**: counters inside each long pass (`go: parsing 1200/3721`,
  `go: route prefixes …`, `go: field bindings …`, `go: analysing …`,
  `js/ts: parsing …`, `js/ts: analysing …`, `graph: propagating …`), a
  `[repo 3/14]` prefix in joint scans, and an immediate `bizmap X: scanning …`
  line.
- **`bizmap scan --discover`** without a value scans the current directory.
- **JS/TS calls resolved by name are `exact`** (a function in scope, or
  imported by name / through barrels), like Go's; members, factories and
  callbacks stay `inferred`.
- `pytest -m perf`: the thresholds are relative within one run and loose in
  absolute terms; the docstrings say a failure under heavy machine load is
  worth a re-run first.

## 0.4.2 — 2026-11-20

### Fixed

- **Go interfaces are matched by signature, not just by method names**: an
  implementer needs the same number of parameters and results and the same
  parameter type names (`OrderStatus(echo.Context) error` no longer
  implements `OrderStatus(context.Context, string) (string, error)`). The
  false call from a service to the same-named controller method (a cycle) is
  gone, and the route correctly stops at an unresolved call.
- **Handler factories keep the data their arguments bring**:
  `e.GET("/events", streamEvents(hub, visibleTo(pool)))` routes to
  `streamEvents` and also triggers `visibleTo` (inferred,
  `via: handler_factory`), so the route reaches what `visibleTo` reads.
- **Go phase**: the nodes each detector pass looks for (calls, assignments,
  literals…) are collected once per function instead of re-walking the
  syntax tree in every pass, and the garbage collector's thresholds are
  raised (parsed files frozen) during the phase. `scripts/bench_go.py
  --heavy --all-files` (bigger files, many standard-library calls, tests and
  generated code analysed) is part of `pytest -m perf`.
- README: the `scan:` defaults (what is skipped, including Go tests, mocks
  and generated code, and how to bring it back).

## 0.4.1 — 2026-11-20

### Fixed

- **Go phase is linear and much faster on large repos** (round 38). Calls to
  packages that were not scanned (stdlib, third parties) no longer scan every
  import path, `pkg.Const` lookups no longer scan every constant, package
  constants are grouped once, an HTTP client whose base URL is unknown no
  longer re-analyses its file on every helper call (800 methods: ~25 s →
  0.3 s), imports are read from the top-level declarations only. The time
  per file now stays flat as the repo grows (4k/1k files = 4.6× on a
  synthetic repo with tests and generated code; 8k vs 0.4.0: ~85 s → 53 s
  analysing every file, 20 s with the new defaults below). Absolute cost
  depends on the code: ~1 ms per file on the small files of
  `scripts/bench_go.py`, ~5 ms on a heavier synthetic repo, ~11 ms on real
  code. A slow Go phase prints `go: N/total files analysed`.
- **Go tests, fixtures, mocks and generated code are skipped by default**
  (`skip_generated`): `*_test.go`, `testdata/`, `mocks/`, `*.pb.go`,
  `*.pb.gw.go`, `*_mock.go`, `mock_*.go` and files headed
  `// Code generated … DO NOT EDIT.` — except sqlc output and any generated
  file that runs `.Exec(` / `.Query…(`, which is data-access code and is
  kept. Counted as `go_tests`, `go_test_dirs`, `generated_files`;
  `scan: include: ["*_test.go"]` brings them back.
- **sqlc queries are SQL**: a statement may start with `--` or `/* */`
  comments (sqlc's `-- name: GetOrder :one`); they used to give no links.
- **DB triggers fire only where they really do**: only a function's *own*
  write fires a trigger (not the copies propagated to its callers), the SQL
  operation must match the trigger's events (`INSERT` / `UPDATE` / `DELETE`;
  SQLAlchemy `insert()`, `update()`, `delete()`, `session.add/delete` too),
  and `UPDATE OF col` needs a write to one of those columns. The effect is
  table-level (`via: db_trigger:NAME`) on the firing function, its callers
  and entrypoints; triggers whose function writes another table with
  triggers are followed (up to 4 levels).
- **Data links say where they come from**: copies of a read/write up the call
  graph carry `derived: "call"`, direct writes carry `ops` (the SQL
  operations). `bizmap report` shows `data links: direct (exact …, inferred
  …), copied to callers/entrypoints N, via DB triggers N`.
- **Go call resolution**: a call by exact name in the same package, or
  `pkg.Func()`, is `exact` (was `inferred`); a method passed as a value to a
  scanned function (`c.observe(ctx, c.svc.SubmitMarks)`) is an inferred
  call; `e.GET(path, listCarts(newCartRepo(db)))` routes to the factory
  `listCarts`, not to its argument; a decorator that implements the interface
  it wraps no longer resolves `s.repo.X()` to itself (reported as
  `no_scanned_implementer` when nothing else implements it); a call on a type
  from a package of the same project that was not scanned is reported as
  unresolved with reason `type_not_scanned`.
- **Entrypoints show the unresolved calls of everything they reach**, not
  only of the handler, and `bizmap report` splits "without data" by reason:
  handler not resolved, stops at an unresolved call, only calls other
  services over HTTP, no data access found — and counts those that reach
  data in another scanned service through HTTP. Unresolved-call totals count
  functions only (entrypoints hold copies).
- **`out/` and `coverage/` are only skipped as build output next to a project
  manifest** (package.json, go.mod, pyproject.toml, setup.py, requirements.txt,
  tsconfig.json…). Inside source code (`internal/coverage/`, `adapters/out/`)
  they are scanned like any other package; before, they were skipped at any
  depth in every language, silently. Unambiguous build folders (`.yarn`,
  `.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.cache`, `storybook-static`…) are
  still skipped by name.
- **Skipped paths are named**: `bizmap scan` lists up to 5 per reason under the
  `skipped:` line (all with `--verbose`), and the graph has `skipped_paths`
  (per reason, sorted, capped at 200; `skipped` keeps the counts).
- **Progress while a slow phase runs**: once a scan passes 3 seconds, each phase
  is announced when it starts (`shop: js/ts: 19 files…`), and a `still in …`
  line follows every 5 seconds of silence. The final `done in …` line groups
  times by phase name only (`files 0.2s`, not the last repo's file counts).
- **bizmap.yaml `scan:` and `exclude:` are validated**: `scan: 5`, `exclude: 5`,
  `skip_generated: "no"`, unknown `scan:` keys and invalid YAML now stop with one
  line naming the file and the key, instead of a traceback or a silently wrong
  value. A top-level `exclude:` and `scan.exclude:` are now combined (before, the
  second was ignored when the first was set).
- **`--discover`**:
  - folders with code but no git are listed under `left out:` as
    `no git; add with --include PATH`; included ones show as `folder · no git`;
  - new criterion `folder_name` (after `default_branch`, before
    `latest_commit`): the copy in the folder named like the repository wins
    over a clone with a newer commit;
  - without git, the commit time is read from the commit object
    (`commit-file`) before falling back to the ref file's mtime; a tie that
    could not be broken without git says so in the reason;
  - a `--prefer` that matches no folder is a warning (in `--list` and on
    stderr), `nothing to scan` replaces `0 project(s) to scan:`, and the usage
    error reads `--list only works with --discover DIR`.
- **Pathological files scan in seconds**: in unusually deep syntax trees (a
  10,000-term `+` constant, a 3,000-call method chain, 800–1,600 chained
  `else if`), climbing to a node's parent cost a walk from the root, so a
  single file could take from 30 s to several minutes. Such trees now get a
  parent index (built only for files deeper than 200 levels), JS/TS scope
  bindings and enclosing-function names are memoized: 1,600 `else if` with
  calls 300 s → 4 s, 800 in a route handler 29 s → 1 s, 3,000 `.q()` calls in
  Python 13 s → 1.4 s, a 10,000-term Python constant 5.6 s → 2.5 s. The graph
  is unchanged.
- README: install with `pipx install bizmap` / `pip install bizmap` (the
  editable install is under Development), and links point at GitHub so they
  work on the PyPI page.
- docs/discover.md: submodules and nested repositories are scanned as part of
  their parent, never as projects of their own.

## 0.4.0 — 2026-11-20

### Fixed

- **Long `+` chains no longer crash the scan** (`RecursionError` from about 990
  terms): string concatenation is resolved iteratively in the Go, JS/TS and
  Python detectors (constants, `var`, parenthesized chains, SQL built in a
  function body). Climbing from each literal to the top of such a chain is
  memoized too (tree-sitter's `node.parent` walks from the root, which made a
  5,000-term chain take minutes); it now scans in seconds.
- **Generated code is skipped by default**: `.yarn/`, `out/`, `coverage/`,
  `.next/`, `.nuxt/`, `.svelte-kit/`, `.turbo/`, `.cache/` and similar build
  dirs (besides `dist/`, `build/`, `node_modules/`, `vendor/`), `*.min.js`,
  `*.bundle.js`, `*.chunk.js`, JS files ending in a `sourceMappingURL`, and
  minified JS (very long lines). `.gitignore` files are honoured (nested ones
  and those above a scanned subdirectory too, `!` re-includes). The graph
  records how much was skipped (`skipped`). Bring things back with
  `scan: {include: [".yarn", "public/app.js"], skip_generated: false, gitignore: false}`
  in bizmap.yaml.
- **`bizmap scan` output stays short**: unresolved HTTP calls are a count per
  reason plus the 10 most frequent calls (each shortened to one line), and
  warnings are capped at 10; `--verbose` lists everything (the JSON always has
  the full lists). Skipped generated/ignored paths are reported as counts.
- **Progress for slow scans**: past 3 seconds, `bizmap scan` prints each phase
  (file listing, Go, Python, JS/TS, graph, cross-service merge) with its time to
  stderr, and a per-phase total at the end; `--quiet` turns it off. The graph
  and stdout are unchanged.
- **A detector crash on one file no longer aborts the scan**: the files that
  make it fail are found, skipped and reported in the graph's `warnings`; the
  rest of the repo is analysed as usual.

### Added

- **`bizmap scan --discover DIR`**: finds the git repositories under a work
  folder (`--depth`, default 2; never inside dependency/build dirs or another
  repo), groups the copies of one repository (worktrees sharing a git dir,
  clones whose `origin` URLs match once normalized: https/ssh, user, host case,
  `.git`, trailing slash) and scans one copy of each, jointly. The copy is chosen
  deterministically: `--prefer`, then main checkout over worktree, then the
  default branch (`origin/HEAD`, else main/master), then the most recent commit
  (`git log`, or the ref file's mtime when git is not installed), then path.
  Repos with no supported code are skipped. `--list` (and `--list --json`) shows
  every group, the chosen copy, the criterion that decided and the other copies,
  without scanning. `--include`, `--exclude`, `--prefer` and `--depth` can also
  live in a `discover:` section of the folder's bizmap.yaml. See
  [docs/discover.md](docs/discover.md).
- **`bizmap mcp`**: a read-only MCP server over stdio for coding agents. It reads
  the graph JSON (reloaded when it changes), never uses the network or a model,
  and answers deterministically. Tools: `search`, `call_graph`, `function_data`,
  `table_usage`, `impact` (same logic as `bizmap impact`) and `why_column`. Every
  item carries `evidence` (`file:line`) and, when it comes from an edge, a
  `confidence`; lists are capped (`limit`, default 20, max 100) with totals and
  `truncated`, results stay under 20,000 characters, and order is documented.
  No new dependency: the JSON-RPC/MCP layer is built in (no `bizmap[mcp]` extra
  is needed). Setup for Claude Code, Cursor and Claude Desktop: `docs/mcp.md`.
  Hardened: the stdio loop survives any input (lone surrogates, invalid UTF-8,
  non-UTF-8 stdout; a failing message gets `-32603`), a file that is not a graph
  is rejected with a clear message, arguments are validated against each tool's
  schema, `impact` never reads `.sql` files outside `--repo-root` (and warns when
  it has none), `METHOD /path` names the route rather than its inline handler,
  results are trimmed item by item close to the size cap (least important list
  first, nested lists capped too), and the text content is compact JSON.
- **`bizmap why` / `why_column`**: a table name lists the readers and writers of
  its columns, and a column that exists in several repos is an error listing the
  repo-qualified ids (one of which can be passed) instead of a silent pick.

## 0.3.10 — 2026-11-20

### Fixed

- **Hono `.route('/prefix', subApp)` inside a chain** no longer counts as an
  Express `.route('/x')` chain (regressed in 0.3.9): a handler after the mount
  (`new Hono().route('/inv', sub).post('/pay', h)`) keeps its own path
  (`POST /pay`, not `POST /inv`). Only a one-argument `.route(path)` opens an
  Express method chain.

### Known limitations

- Routes of a Hono sub-app mounted inside a chain (`new Hono().route('/inv',
  sub)…`) don't get the mount prefix; a mount as its own statement does.

## 0.3.9 — 2026-11-20

### Fixed

- **Express `router.route('/x').get(a).post(b)`** registers one entrypoint per
  method, with the path from `.route()` and each handler's own span and SQL.
- `docs/graph-schema.md`: for an inline route handler, `evidence.line` is where
  the registration call (or chain) starts; `start_line`/`end_line` are the
  handler's own lines.

## 0.3.8 — 2026-11-20

### Known limitations

- Firestore collection access from JS/TS is not detected (Python and the
  Firestore triggers are).

### Fixed

- **`impact` on chained routes** (`new Hono().use(...).post(a).post(b)`): each
  inline handler's node spans its own function (from its `.post(` to its `})`),
  not the chain's first lines, so a changed line maps to the right route (the
  graph's edges were already right).

## 0.3.7 — 2026-11-20

### Fixed

- **Inline Express handlers that touch the same column**: each handler gets its
  own edges. SQL inside an inline handler is now attributed to it when the
  handler is detected (every site), instead of moving the module's single
  merged edge afterwards, which left the second handler without it.

## 0.3.6 — 2026-11-20

First version published to PyPI by `.github/workflows/release.yml` (Trusted
Publishing, on the `v0.3.6` tag). 0.3.3–0.3.5 can be published with
Actions → Release → Run workflow and the tag name.

### Known limitations

- Webpack `resolve.alias` is not read; Vite `find` regexes other than a simple
  prefix (`/^@\//`) are ignored.
- `const lib = require('./x'); lib.a()` is not followed (it can't be told apart
  from a client instance like `api.get()`); destructured `require` is.
- Same-named local helpers in two sibling anonymous callbacks share one id
  (only one node is kept; no false edge).
- `.sql` schema changes are read from the checkout (`--repo-root`); statements
  built at runtime are read as written.

### Fixed

- **Inline Express handlers own their SQL**: `app.post('/x', async (req, res) =>
  { await pool.query(...) })` writes from the handler (and the route), no
  longer from `<module>.module`.
- **Route handlers imported as default or through a namespace**
  (`import listHandler from`, `h.cancelHandler` with `import * as h`) resolve to
  the scanned function: the route gets its tables and `why`, and no empty
  `<module>.default` / `<module>.h.cancelHandler` nodes are left.
- **Schema changes in the comment** name the operation — added, altered,
  **dropped**, **renamed → new**, changed — and flag (⚠️, listed first) a
  dropped or renamed column that code still uses. Every column of a multi-column
  `ALTER TABLE … ADD …, ADD …` is listed; a new table is one row
  ("new table `shipments` (4 columns)"); a line whose change is only
  punctuation/spacing (a comma added for the next column) is ignored.
- A column definition added inside an existing `CREATE TABLE` is "added" (no
  removed line names it), not "changed"; a changed function that runs SQL itself
  is never folded into a helper's row, even with the same reach; an inline
  handler keeps only its column-level edges (as a named handler does).
- **`ALTER TABLE … RENAME COLUMN a TO b`** is applied to the reconstructed
  schema.
- The header says how many changed functions were folded into another row; a
  `.sql`-only diff no longer prints the "not under a scanned root" hint; a
  changed `.sql` file missing under `--repo-root` is reported instead of the
  schema section silently disappearing.
- **`why` license filter** only drops real license text (`Copyright`, `SPDX`,
  "licensed under", "all rights reserved", `(c) 2024`, `/*!`); a comment that
  merely starts with "License …" is kept.

### Added

- **Schema changes in the PR comment**: changed lines of `.sql` files inside
  `CREATE TABLE` / `ALTER TABLE` that name a known column are listed with the
  entrypoints and functions that read or write it ("not used yet" for a new
  column). `bizmap impact --repo-root` (default `.`) is where those files are
  read; the Action passes the checkout.
- **Pass-through handlers folded**: a changed function that only calls another
  changed function with the same impact (an inline route handler calling
  `cancelOrder`) is shown as "also changed" on the callee's row; JSON keeps
  both.
- Other changed files outside any function are named in the comment, and the
  header reads "writes 2 tables · reads 1 table".

- **`why` for JS/TS**: a JSDoc block (`/** ... */`, tag lines like `@param`
  dropped) or `//` lines right above a function declaration, a `const` arrow or
  function expression, a class method or an object member become its `why`, as
  in Python and Go. A blank line between comment and code breaks the link; a
  comment trailing code on its line (`const limit = 5; // max retries`), tool
  directives (`eslint-…`, `prettier-ignore`, `@ts-…`, coverage ignores,
  `TODO`/`FIXME`/`HACK`) and license headers (`Copyright`, `SPDX`, `/*!`) are
  not used. A named Express handler's doc is also the route's `why`, and a
  TypeScript overload set uses the doc above its first signature.

## 0.3.5 — 2026-11-20

### Fixed

- **Function expressions are named by their binding**: `const topNamed =
  function topN() {}` is `<module>.topNamed` and `topNamed()` links to it; the
  inner name `topN` (only visible inside itself) no longer links from other
  functions, and two `const a1 = function dupn(){}` / `const a2 = function
  dupn(){}` are two nodes.
- **Generators** (`function* gen()`, `const g = function* () {}`) are function
  nodes.
- **Vite aliases**: `find` as a simple prefix regex (`/^@\//`) and plain-string
  values (`'@': '/src'`, root-relative, or `'./src'`) are read.
- **tsconfig base configs** (`tsconfig.base.json` extended by apps) apply only
  to the configs that `extend` them, not to every app under their directory.

## 0.3.4 — 2026-11-20

### Fixed

- **Local helpers in anonymous callbacks / object-literal methods**: `const
  report = (m) => ...` declared inside `schema.refine((p, ctx) => {...})` or a
  `{ async start(c) {...} }` method is the callee of `report(...)` there
  (0.3.3's shadowing check stopped at the `const` and lost these `calls`).
- **Ambiguous `export *`**: a name two `export * from` sources both provide is
  not exported by TypeScript, so no `calls` edge is made to either.
- **Path aliases**: tsconfig `baseUrl`/`paths` resolve relative to the config
  that declares them (also through `extends`), each alias applies only under
  its config's directory (two apps can each have `@/*`), and simple Vite
  `resolve.alias` entries (`'@': path.resolve(__dirname, './src')`,
  `fileURLToPath(new URL('./src', import.meta.url))`) are read, including the
  array form `alias: [{ find: '@', replacement: path.resolve(__dirname, 'src') }]`.
  With several targets (`"~/*": ["src/*", "generated/*"]`) the first one that
  exists is used, like TypeScript.
- **Named re-exports resolve through barrels**: `export { a } from './x'`,
  `export { a as b } from`, directory barrels, and barrels mixing named
  re-exports with `export *`.
- **`const f = function () {...}`** is a function node like a const arrow.

### Known limitations

- Webpack `resolve.alias` is not read (tsconfig `paths` and Vite aliases are);
  Vite `find` regexes other than a simple prefix (`/^@\//`) are ignored.
- Two sibling anonymous callbacks that each declare a local `const tick = () =>`
  share the id `<module>.tick`: only one node is kept, so calls to the other
  get no edge (no false edge).

## 0.3.3 — 2026-11-20

No tag was pushed for 0.3.0–0.3.2; this is the first tagged release of the
`bizmap impact` command and the PR-comment Action (see 0.3.2 for those).

### Fixed

- **Shadowed names**: a parameter (plain, destructured, arrow), `let`/`var`/
  `const` or `catch (x)` binding hides a module-level or imported function of
  the same name: no false `calls`.
- **Imported call targets** resolve through `index` barrels (`export { a } from`,
  `export * from`), `export default <identifier>`, namespace imports
  (`import * as lib`; `lib.top()`) and CommonJS (`const { a } = require(...)`
  with `module.exports = { a }` / `exports.a = a`).
- **Go closure numbering** follows Go's own per-level scheme (`func2`,
  `func2.1`): a literal nested in another closure no longer shifts the next
  route's id.
- **Performance**: the span post-passes and `impact` index edges once; the
  route rollup memoizes per strongly connected component. Synthetic Go service
  (2000 chained functions, 600 closure routes): 3.4 s → 0.7 s, growing about
  linearly (`scripts/bench_scale.py`, run in CI).
- **JS/TS calls to module-level and imported functions** produce `calls`
  (callee resolved by scope: nested helper → module function → import, only
  when the target is a scanned function).
- **Method members** (`return { stock(sku) {...} }`) are called by their
  factory, like arrow members.
- **Literal route precedence**: a client calling `/shops/points` links only to
  `GET /shops/points`, not also to `GET /shops/{shop_id}`.
- **Go inline route closures** roll their table reads/writes up onto the route,
  like named handlers, so `report`/`why` see them.

## 0.3.2 — 2026-11-20

<!-- 0.3.1 -->
_0.3.1 has no entry of its own: it was bumped on `main` (the first `bizmap impact`
+ Action) but never tagged, and its changes are listed under 0.3.2._

0.3.1 was bumped on `main` but never tagged; its additions ship here.

### Upgrading — ids that changed

- **Nested JS/TS functions** are qualified by every named function around them:
  `function createA(http) { function build() {} }` → `<module>.createA.build`
  (was `<module>.build`, which collided across factories).
- **Factory members**: functions in an object returned by a named function
  (`return { one: (id) => ... }`, `=> ({ ... })`, `stock() {...}`) are
  `<factory>.<key>`; their `calls_http`, `unresolved_http`, reads and writes
  leave the factory, which `calls` each member.
- **Go inline route closures** (`e.GET("/x", func(c echo.Context) error {...})`)
  are `<enclosing>.funcN` (Go's numbering), triggered by the route; the SQL and
  calls inside move off the enclosing function.

### Added

- **`bizmap impact`** (`--graph`, `--diff`, `--prefix`, `--json`): maps a unified
  diff onto the graph — changed functions → entrypoints that reach them and the
  tables/columns they (or their callees, across `calls_http`) read or write —
  as Markdown with a Mermaid diagram, or JSON.
- **GitHub Action** (`action.yml`, `uses: lucas-simao/bizmap@<tag>`): scans the
  PR, runs `impact` on the PR's diff (base..HEAD for the default merge-commit
  checkout, merge-base..HEAD otherwise), writes the step summary and keeps one
  PR comment up to date.
- **Graph**: function nodes carry `end_line`; the graph records `repo_roots`
  (repo name → directory, relative to the git root inside a git work tree,
  else to the scan path) so diff paths map to functions in joint scans.
- **`fail-on-error`** Action input (default `"false"`: advisory check).
- **Graph**: `start_line` on function nodes whose definition starts below the
  evidence line; `repo_roots_base` (`git` or `scan`).

### Fixed

- **`end_line` for every function node**: inline JS/TS handlers, Go func
  literals, `<module>.module` pseudo-functions and SQL functions from
  migrations have spans, so a diff inside them maps.
- **Diff parsing**: a replacement is described by its added lines only (no
  longer marks the line above); a pure deletion marks the lines around the gap
  (`{c, c+1}` when the new side is empty); hunk bodies are read by header
  counts, so a removed `-- comment` is not a file header.
- **Calls to local helpers**: `one: (id) => build(id)` / `addUser: () =>
  loadUser()` with helpers declared in an enclosing function produce `calls`.
- **`registry merge` keeps `repo_roots`**: roots are relative to the git root,
  so per-repo scans + merge map a diff like the joint scan (merge == joint is
  byte-equal again inside git). `impact` hints when changed code files are
  unknown to the graph.
- **PR comment size**: rows ranked by reach and capped (`--max-rows`), functions
  with no route/table summarized, hard cap `--max-chars 60000` that always keeps
  the marker and header, `--details-url`.
- **Action**: fork PRs write only the step summary; only `comment-author`'s
  marker comment is updated; a failed comment listing skips posting (no
  duplicates); base commit fetched with a clear error; `ref: <head>` checkouts
  diff from the merge base; non-PR events are skipped; non-ASCII paths
  (`core.quotepath=false` + unquoting); install/scan failures update the
  comment with a log link.

## 0.3.0 — 2026-11-19

### Upgrading — ids and confidence that changed

Graphs from 0.2.x and 0.3.0 are not directly diffable for these nodes:

- **Python module names follow package roots**: the highest `__init__.py`
  package, else a declared source root (`src/` next to `pyproject.toml`), else
  the path; collisions across services fall back to the path. Function ids
  inside packages change (`reports.layers.asset.bind_id`, was
  `services.reports.src.reports.layers.asset.bind_id`).
- **Nested Python functions** include the enclosing function
  (`app.create_app.list_items`).
- **TS path helpers stored in an object** (`export const paths = { summary:
  (id) => … }`) are attributed to `<module>.paths`, not `<module>.module`.
- **New confidence `heuristic`** (between `inferred` and `unknown`):
  route-literal suffix links on untraced clients are now `heuristic`, no
  longer `inferred`.
- **Graph `version`** is `"0.3.0"`.

### Fixed

- **`baseURL` inside a config object sent through a local wrapper**
  (`request({ method, url, baseURL: ALT })` → `http.request(config)`): a base
  key in the same object literal as `url` is that request's base. Unresolved →
  whole-path match only (no suffix link to the default API); an env mapped in
  `services:` → `inferred` link to that service.

- **Go base wrapped in `strings.TrimRight` (regression from the struct-base
  change)**: `strings.TrimRight/TrimLeft/TrimSuffix/TrimPrefix/TrimSpace/Trim/
  ToLower` are transparent for bases (like `encodeURIComponent` in TS), and
  `url.JoinPath` / `path.Join` compose; a struct base replaces `{base}` only
  when it is concrete (env placeholder, absolute URL, static text), never an
  opaque expression. `examples/checkout` now builds its billing client this way
  (`NewBilling(os.Getenv("BILLING_URL"))` + `TrimRight`), so the link guard
  covers it.

- **Go client base from config structs and constructor parameters**: a
  struct base field (`baseURL`) assigned `cfg.ReportAPIURL`, where the config
  struct field has an `env:"X"` / `envconfig:"X"` tag (or is populated with
  `os.Getenv("X")` in some literal), resolves to `{X}`; a base assigned from a
  constructor parameter takes the value every call site passes when they
  agree. `{X}` then selects the service in `services:` (`inferred`), instead
  of a generic `{base}`. Direct calls in methods use the struct's resolved
  base too, and Get/Post/Head/PostForm on an `*http.Client` value
  (`c.http.Get(url)`, `http.DefaultClient.Get`) are detected.

- **Python package re-exports**: names re-exported by a package's
  `__init__.py` (`from .encode import f`, `from .resize import *`, chained
  barrels) resolve to the defining module, for `from pkg import f` and
  `import pkg; pkg.f()`. Found on the way: in an `__init__.py`, a relative
  import (`from .encode import …`) was resolved from the package's *parent*
  (`encode` instead of `media_utils.encode`); `.` now means the package itself.

- **Per-call `baseURL` overrides the instance base**: `http.post(url, body,
  { baseURL: NEXT })`, `http.get(url, { baseURL })` and
  `http.request({ baseURL, url })` use the call's own base (axios/ky config
  position only; `baseURL`/`baseUrl`/`prefixUrl`). An unresolvable override is
  an unknown explicit base, so the URL must match whole — the
  `/api/next/items/:id/summary` false link to `/api/v1/shop/...` is gone.

- **Silent drop of calls into unscanned submodules of scanned packages**:
  with an `__init__.py` chain, `pkg.store.views.counts.shop_counts` was
  skipped because `pkg` / `pkg.store` are scanned (their `__init__.py`). Now
  only the longest scanned prefix decides: a package means an unscanned
  submodule → `target_not_scanned`; a regular module means our own class /
  constant → left alone.

- **False link from object-member path helpers**: a helper stored as an
  object property (`export const paths = { summary: (id) => … }`) had no
  symbol of its own, so its literal stayed on `<module>.module` without the
  caller's base and was suffix-matched (`${API_BASE_URL}/api/next${paths.summary(id)}`
  linked to `/api/v1/shop/items/:id/summary`). Member helpers now get
  their own symbol (`paths.summary`), `obj.key(...)` calls on local objects
  and named imports are their call sites, and a literal left on a
  helper/const with no caller found is matched whole only.

- **axios/ky instances through barrels and inline**: imports are resolved to
  the module that actually defines the name (`index` files,
  `export { a as b } from`, `export * from`) before client bases propagate, so
  `import { shopApi } from '../http'` re-exported from `http/index.ts` keeps
  its `baseURL`; `createShopClient(axios.create({ baseURL }))` binds the inline
  instance. The factory may return an object literal or a variable, and may be
  called inside a hook/function. Go clients whose base arrives as a
  constructor parameter / struct field and are used through a service field
  are covered by tests (already resolved).

- **Go Echo groups nested inside a function that receives the group**
  (regression exposed by whole-path matching): `s := g.Group("/settings")`
  where `g` is a `*echo.Group` parameter now keeps the caller's prefix
  (`/api/v1/shop/settings/...`), at any depth and across packages; local
  groups are recomputed whenever a parameter prefix arrives from a caller.
  `examples/checkout` registers its order routes this way, and a test names
  every expected cross-service link that goes missing.
- **Go routes with an empty path** (`orders.POST("", h)`, the group root)
  were dropped; found by the new checkout link guard.

- **Base passed as a parameter → `inferred`, in real shapes**: caller
  arguments now resolve through env aliases (`const env = import.meta.env;
  env.VITE_API_URL`), env destructuring (`const { VITE_API_URL } =
  import.meta.env`), config objects (`config.apiUrl`) and consts imported from
  other modules; and the binding also applies to route literals (untraced
  clients), so when every caller passes the same env mapped in `services:`
  the link is `inferred` instead of `heuristic`.

- **Route literals use only the final URL**:
  - a literal covered by a traced call of the same function is matched with
    that call's full path; a URL with an explicit base (placeholder or
    absolute, even unknown) must match whole — no suffix match
    (`${API_BASE_URL}/api/next/items/:id/summary` no longer links to
    `/api/v1/shop/items/:id/summary`);
  - a helper call is the request path only when it is the whole argument
    (`http.get(itemPath(id))`); inside a larger URL (`${base(id)}/photos`) the
    composed URL is the literal, never the intermediate `base(id)`;
  - a literal held in a module-level const moves to the functions that pass
    it directly to a call; when it is only part of larger URLs those URLs are
    the literals; edges never originate from `<module>.module` for a
    referenced const (kept with `origin: module_const` only if unreferenced).

- **Namespace packages under `src/` (regression from the package-roots
  change)**: a source root now wins over a partial `__init__.py` chain, and
  any `src/` directory counts as a source root even without a `pyproject`. With
  `src/pipeline/` lacking `__init__.py` and `src/pipeline/render/` having
  one, the module is `pipeline.render._utils` (it was
  `render._utils`), so `from pipeline.render._utils import …` resolves.
  **Node ids change**: Python modules under `src/` lose the `src.` prefix.
- **Import suffix fallback**: an imported module that is not a scanned module
  name is remapped to the unique scanned module sharing a 2+ segment suffix
  (`backend.app.repo` ↔ `app.repo` when scanning `backend/`), for
  `import`, `from … import name` and `from pkg import module`.

- **`sink_not_scanned` no longer downgrades literal SQL**: the edge
  confidence describes the SQL (a literal stays `exact`); the unscanned
  executor is carried by the `sink_not_scanned: true` flag.
- **Nested Python functions** get the enclosing function in their id
  (`panels.sales.compute` vs `panels.stock.compute`), so same-named inner
  functions no longer share a node. Handlers defined inside an app factory
  change id accordingly (`app.create_app.list_items`, mirroring Python's
  `__qualname__`); the parent is always included so ids do not depend on
  whether a same-named function exists elsewhere.

- **TS helper composition**: transparent segment helpers
  (`seg = (s) => encodeURIComponent(s)`) inside other helpers
  (`` `/v1/items/${seg(id)}` ``); helpers imported through barrels
  (`index.ts`, `export * from`, `export { a as b } from`); and a base passed
  as a function parameter (`fetch(`${baseUrl}/v1/x`)`) is composed from the
  call sites when every caller passes the same env/URL, so the traced matcher
  links it (`inferred`, honoring `services:`) instead of the literal fallback.
- **Known HTTP client with an opaque path was dropped silently**:
  `http.get(buildUrl(x))` where the path cannot be evaluated is now recorded
  (`{base}{buildUrl(x)}` → `unresolved_http`, `path_dynamic`).

- **`unresolved_calls` noise**: entries whose target also has a resolved
  `calls` edge from the same function are dropped (dedupe by origin/target);
  packages declared as dependencies anywhere in the scan (`pyproject`
  `[project]`/Poetry, `requirements*.txt`, `setup.cfg`) are third-party and
  never reported as `target_not_scanned`; a dotted path through a scanned
  module is never reported.
- **`sink_not_scanned` on real code**: SQL in a local variable
  (`sql = "..."; await conn.fetch(sql)`) and dotted sinks (`db.conn.fetch`)
  are resolved; the dynamic `table:unknown` site the generic scan recorded for
  that call is replaced by the resolved tables.

- **Python `src/` layouts and monorepos**: module names now follow package
  roots — the highest `__init__.py` package, else a declared source root
  (`src/` next to `pyproject.toml`/`setup.*`, setuptools `packages.find.where`,
  Poetry `packages[].from`) — so `from reports.layers import asset;
  asset.bind_id()` resolves when scanning from the repo root
  (`services/reports/src/reports/...`). Names that would collide across
  services fall back to the path-based name. Function ids inside packages
  change accordingly (`reports.layers.asset.bind_id`).

- **Route-literal links lost after the origin fix**: a path helper's literal
  is no longer dropped. It moves to every call site of the helper (resolved
  through imports, any file; edge `via_helper: <helper>`), with the method of
  the surrounding HTTP call; if no caller is found it stays on the helper with
  `origin: path_helper`. Helpers whose template starts with any base
  placeholder (`${base}/v1/...`) are evaluated at call sites too. An env base
  that is not declared in `services:` may still link a literal to a single
  route in the caller's own repo (self-call, `heuristic`); other repos stay
  out. New test: every traced HTTP call in the examples ends linked or in
  `unresolved_http`.

- **TS base helpers and namespace imports**: helpers whose template starts
  with an env/base placeholder (`base = () => `${API_URL}/api/v1/shop``)
  are evaluated, so `` `${base()}${site(id)}/orders` `` composes the full URL;
  `import * as P from '@/lib/paths'` + `P.site(id)` resolves; helpers built
  from imported helpers (`siteStock = (id) => `${base()}/sites/${id}/stock``)
  resolve after imports. Helpers are never `calls_http` origins.

- **Route-literal origin**: a function that only returns a path
  (`const site = (id) => `/sites/${id}``) is never the origin of
  `calls_http`; a call to such a helper at an HTTP call site
  (`getSite = (id) => api.get(site(id))`) is the literal, so the edge leaves
  `getSite`.
- **Route-literal confidence**: when the call's base resolves to a scanned
  service (`services:`) and exactly one route *of that service* matches, the
  literal edge is `inferred`; `heuristic` stays for unknown bases.

- **Route-literal uniqueness across services**: a literal that was unique
  inside its own repo kept its `heuristic` edge after merge even when another
  scanned service had the same path. Literal edges are now recomputed on the
  merged graph and exist only when the path has exactly one owner among all
  scanned services (otherwise `route_literal_skipped: ambiguous`).
- **`--salt-file` permissions**: the salt file is created `0600`.

- **TS path helpers on real code**: helpers that wrap the parameter
  (`` `/sites/${encodeURIComponent(id)}` ``) were rejected as impure, so no
  link appeared. `encodeURIComponent`, `encodeURI`, `String`, `Number`,
  `.toString()`, `.trim()`, `.toLowerCase()` are now transparent — in helpers
  and directly in client URLs, including concatenations
  (`'/items/' + encodeURIComponent(id)`, which used to drop the segment).
  Object helpers (`routes.team(id)`, imported or local) and `@/` alias
  imports are covered by a test with a real `tsconfig.json`.

- **SQLAlchemy builders on models outside the scan left no trace**
  (regression from the phantom-reads fix above): `update(Invoice)...` /
  `select(Invoice)` where `Invoice` is imported from an unscanned package now
  record `writes`/`reads` to `table:unknown` (confidence `unknown`) instead of
  nothing. Builders on scanned models keep their exact edges with no unknown
  extra. Raw SQL on a table without DDL keeps its `unresolved_relation` edge
  (now covered by a test).

- **`registry merge` ≠ joint scan (1 edge)**: `bizmap scan web` (one repo)
  did not see the `services:` / `database:` declared in the workspace
  `bizmap.yaml` that lists `repos: [web, api, billing]`, so per-repo graphs
  lacked services and `{BILLING_URL}/invoices` stayed unresolved after merge.
  A repo now inherits `services:` and `database:` from the nearest ancestor
  `bizmap.yaml` whose `repos:` lists it (its own entries win). A CLI test
  runs both paths on every multi-repo example and compares the full JSON.

- **`report --redact` leaked schema names**: `via` values such as
  `db_trigger:<name>` were printed in clear. Every name from code or schema is
  now hashed (`db_trigger:h:…`); only counts, edge types, entrypoint kinds,
  reasons, and the fixed derivation names (`route_literal`, `sql_reference`, …)
  stay readable. New `--salt-file PATH` creates a private random salt on first
  use and reuses it, so redacted runs are comparable; without a salt each run
  uses a random one (documented in the CLI note).

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

- **Dataclass rosters**: a receiver taken from a roster — a module-level
  list/tuple/dict of dataclass instances, possibly imported from another
  module (`for s in SECTIONS`, comprehensions, `roster.SECTIONS[0]`,
  `.values()`, `enumerate`/`sorted`) — is field dispatch even when a scanned
  class has a method with the same name (`query`). It links only to the
  functions assigned to that class's field. (Fixed on the way: the field
  dispatch pass resolved imports with the last module's import map.)

### Known issues

- SQL rewritten at runtime (`sql.replace("old", "new")`, `str.format` with
  runtime values) is analyzed as written in the source; static analysis does
  not follow the rewrite.

- The older object-dispatch resolver links `obj.query()` to every function
  stored in a `query` field even when `obj` is annotated with an unrelated
  class (tracked by a strict `xfail` test).

- **Python configuration rosters**: functions stored in nested dicts / lists of
  dicts are dispatched through local aliases (`q = cfg["query"]; q(conn)`),
  `cfg.get("query")(…)` and `ROSTER[k]["query"](…)`; a `lambda` value
  contributes the scanned functions it calls. A report route whose `identity`
  query lives in such a roster now reaches `properties`.

- **Calls into unscanned first-party code** (direct, `functools.partial`,
  executors such as `run_in_executor` / `submit` / `to_thread`): the caller
  gets the qualified name in `unresolved_calls` and
  `unresolved_call_reasons: {name: target_not_scanned}`. SQL strings or SQL
  constants handed to that call are linked to the caller's tables as
  `inferred` with `sink_not_scanned: true` (never `exact`, never dropped).
  Stdlib and common third-party packages are not reported. `bizmap report`
  breaks unresolved calls down by reason.

- **Python module-qualified calls**: `from reports import sales` +
  `sales.bind_store(...)` now resolves to `reports.sales.bind_store`
  (`exact`), like `import reports.sales as m` + `m.bind_store(...)`.
- **Functions passed as arguments**: a function reference passed to another
  function (`_respond(request, exports.export_items)`, `run_with(self.build)`,
  `functools.partial(...)` included) is called by the function that passes it
  (`calls`, `inferred`, `via: passed_as_arg`) when the receiver calls or
  forwards that parameter — or when the receiver is not scanned. Route
  registration and logging helpers are ignored.

- **Python executors are calls**: a function handed to
  `loop.run_in_executor(pool, fn, …)`, `pool.submit(fn, …)`,
  `asyncio.to_thread(fn, …)`, `anyio … run_sync`, `run_in_threadpool`,
  FastAPI/Starlette `BackgroundTasks.add_task(fn, …)`, multiprocessing
  `apply_async`/`starmap`/`imap`, `executor.map`, `call_soon`/`call_later`,
  `Thread(target=fn)` / `Process(target=fn)` / `Timer(t, fn)` (also through
  `functools.partial`) is now called by the code that hands it over
  (`inferred`), so the endpoint reaches the tables queried inside it.

- **Python field dispatch**: functions assigned to a field of a dataclass /
  NamedTuple / pydantic model (keyword or positional, at module level or in a
  function) or to a string key of a dict literal are called by
  `obj.field(...)`, `getattr(obj, "field")(...)`, and `obj["key"](...)`
  (`inferred`). `PANELS = [PanelDef("sales", build=_build_sales)]` +
  `for p in PANELS: p.build(conn)` now reaches the builders' tables. Field
  names that are also method names of a scanned class, and `self`/`cls`
  receivers, are left alone (ambiguous).

- **TS path helpers**: pure helpers that return a path template or
  concatenation (`const site = (id) => `/sites/${id}``,
  `function seg(id) { return '/teams/' + id }`), in the same file or
  imported, are evaluated inside client URLs with the caller's argument names:
  `` `/shop${site(siteId)}/orders` `` → `/shop/sites/:siteId/orders`.
  A call segment that cannot be evaluated makes the unresolved reason
  `path_dynamic` instead of `base_unknown`.

- **Python function tables**: a call through a dict/list of functions
  (`BUILDERS[kind](conn)`, `BUILDERS.get(kind)(...)`, `b = BUILDERS[k]; b()`,
  `for f in BUILDERS.values(): f()`, imported or `module.BUILDERS`,
  `functools.partial` members) calls every member (`inferred`), so dashboard
  endpoints that dispatch to `_build_x()` builders reach their tables.
- **Reach definition in `bizmap report`**: printed in the text output and as
  `entrypoints.definition` in JSON — any-confidence `reads`/`writes` from the
  entrypoint (propagated through calls and DB triggers, not across
  `calls_http`) to a named table/column/view/collection; tables missing from
  the DDL count, `table:unknown` does not and is reported separately as
  `reaching_only_unknown`.

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
