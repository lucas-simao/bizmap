# `bizmap export`

*Requires bizmap 0.5.0+.*

!!! note "Not released yet"

    `bizmap export` is merged on `main` and ships with **0.5.0**. The current
    release on PyPI does not have it yet.

```bash
bizmap scan . --out bizmap.graph.json            # or a --discover / registry-merged graph
bizmap export --graph bizmap.graph.json --format html --out bizmap.html
bizmap export --format html --lang pt-BR         # page in Brazilian Portuguese
```

`--format html` writes **one self-contained file**: the graph and every library
are inside it. It opens with a double click, works offline, and can be attached
to an e-mail or a ticket. Nothing is fetched from a CDN or a server.

`--format llm` (a compact text overview for an AI assistant) arrives in 0.5.1.

## What the page shows

- **Services first.** The page opens on one box per service with the
  `calls_http` links between them; each arrow carries the number of links.
- **Click a service** to expand its routes and jobs, its functions and the
  tables it reads or writes; **click its box again** to collapse it. Calls
  through functions that are not drawn appear as dashed arrows.
- **Search** (route, function, table, column) expands the right service,
  centres the node and highlights it. A column opens its table with the column
  marked.
- **Filter by table** (the box next to search, the ⛃ button on a search result
  or the table panel) shows only the routes, jobs and functions that read or
  write that table, across services: the same multi-hop traversal as the MCP
  `table_usage` tool (functions with a `reads`/`writes` edge to the table or a
  column, and every entrypoint reaching them through `calls`, `calls_http`,
  `triggers` or `enqueues`). Functions of other services that enter that
  chain through `calls_http` (a front end has functions, not routes) are
  listed as **called from other services**.
- **"Many depend on it"**: a service called by 3 or more other services, or a
  table read or written by 3 or more services. The 5 most used are marked ⚠
  in red on the graph and listed in the side panel.
- The side panel shows the selected node: evidence (`file:line`), the `why`
  comment, tables and columns read/written, callers and callees.

## Services

A service is a **repository** by default: the `repo/` prefix of ids in a
merged or joint graph, else the graph's `repo`. Tables of a shared `database:`
belong to no service, so several services can point at the same table.

A monorepo can be split into several services in `bizmap.yaml` (read from
`--config`, else `./bizmap.yaml`). Only `bizmap export` reads this key:

```yaml
export:
  services:
    orders: [services/orders/, libs/orders/]
    billing: services/billing/
    admin: "api:cmd/admin/"     # in a multi-repo graph: only in repo `api`
```

A node whose evidence file starts with one of the prefixes goes to that
service (longest prefix wins); everything else stays in its repo.

## Large graphs

The page never draws the whole graph. The data is embedded as compact JSON
(integer indexes, nodes grouped by service) and each subgraph is built when it
is opened. An expanded service draws at most 400 routes and 300 functions
(functions that touch data, route handlers and HTTP callers first); the rest
are listed behind a "+N more" box and any of them can be drawn from search or
that list. Labels hide when zoomed out.

Measured on a synthetic graph of 54,900 nodes / 87,033 edges (30 services) in
headless Chrome on a laptop: file 4 MB, generated in about 0.5 s, page ready in
about 0.5 s, a service expanded in 0.4–0.7 s, a table filter in about 0.2 s.
`pytest -m perf` regenerates that graph and checks the file size and
generation time.

## What the page is made of

| Part | What | License |
| --- | --- | --- |
| [Cytoscape.js](https://js.cytoscape.org/) 3.34.1 | graph drawing, zoom and pan (inlined, minified) | MIT (notice kept at the top of the inlined file) |
| `export/reach.js`, `export/app.js` | BizMap's own plain JavaScript: no framework, no build step | MIT (BizMap) |
| `export/template.html` | the page and its CSS | MIT (BizMap) |

`bizmap export` fills the template with these files and the graph data
(JSON in a `<script type="application/json">` element). No Python dependency
is added and nothing is downloaded when the page is generated or opened.

A graph with a single service opens already expanded.
