# FAQ and limitations

## Does it use AI? Does my code leave the machine?

No and no. BizMap is a static analyser: tree-sitter for code, sqlglot for SQL,
rules in YAML. It runs locally (or in your CI), needs no account or network,
and the same input always gives the same graph. Models are welcome as
*clients* of the graph ([MCP](mcp.md)), never as its authors.

## How is this different from code search or a call graph?

Code search and call-graph tools (Sourcegraph, CodeQL, tree-sitter MCP
servers) know functions and calls, but not which **table and column** a
function touches, nor which route in *another* repository an HTTP client
hits. Data-pipeline lineage (dbt, OpenLineage, warehouse catalogs) starts at
the warehouse or at runtime and does not see the application code that
writes the data. BizMap sits in between: application code → database, across
services.

## How much can I trust an answer?

Every edge carries evidence (`file:line`) and a confidence:

- **`exact`** — static SQL in that function, or a resolved handler;
- **`inferred`** — derived: propagated through the call graph, `SELECT *`,
  interface dispatch, a database trigger, a fallback such as a route literal;
- **`unknown`** — dynamic SQL such as `"UPDATE " + table`.

Run [`bizmap report`](commands/report.md) on a new codebase first: it tells you
how many entrypoints reach data and why the rest do not.

## Limitations

- **Static means static.** SQL rewritten at runtime (`sql.replace(...)`,
  `.format()` with runtime values, query builders assembled in loops) is read
  as written in the source, or kept as `table:unknown`.
- **Dynamic dispatch** that cannot be resolved from types or constructors
  stays an unresolved call (listed with `candidates` when there are several).
- **URLs built at runtime** from configuration BizMap cannot see need a
  [`services:`](configuration.md#services) entry; otherwise they are kept as
  `unresolved_http` with a reason.
- **Frameworks**: only those in [Languages and detectors](languages.md) are
  understood out of the box. Others can often be added with
  [YAML rules](detector-rules.md).
- **Databases**: schema replay and triggers target PostgreSQL syntax.
- **JS/TS imports** follow relative paths, `index` barrels, `export … from`,
  `tsconfig` `paths` and Vite `resolve.alias`; Webpack aliases are not read.
- **Only what is scanned.** A call into a service you did not scan is reported
  as `target_not_scanned`, not guessed.

## Why is my route or table missing?

1. Run `bizmap scan -v` and read the summary: skipped paths, unresolved HTTP
   calls with their reason, warnings.
2. Run `bizmap report`: entrypoints that reach no table are listed.
3. Generated code, tests, build caches and `.gitignore`d paths are skipped by
   default — bring them back with [`scan: include`](configuration.md#exclude-and-scan).
4. Still wrong? Open an issue with a minimal, fictional example; `bizmap
   report --redact` produces a shareable summary of a private codebase.

## Several services, one database?

Give them the same [`database:`](configuration.md#name-and-database) and scan
them together — `bizmap scan ./a ./b`, a [workspace](configuration.md#repos),
or [`--discover`](discover.md) on a folder of clones. `bizmap why` then lists
writers and readers from every service.

## Is the graph format stable?

It is versioned (`version` in the JSON) and documented in
[Graph format](graph-schema.md). BizMap is pre-1.0: fields are added between
minor versions; breaking changes are called out in the
[changelog](https://github.com/lucas-simao/bizmap/blob/main/CHANGELOG.md).

## Is there an HTML view?

`bizmap export --format html` is coming in 0.5.0 —
see [export](export.md) (merged on `main`, ships in 0.5.0).
