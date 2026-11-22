---
hide:
  - navigation
---

# BizMap

**Who reads and writes this column — across all your services — and what triggers it?**

BizMap reads your code and writes a map from every **entrypoint** (HTTP route,
scheduled job, queue consumer, database trigger) through the **functions** it
calls to the **tables and columns** they read and write, across repositories.
It is static and deterministic: no LLM, no tokens, no account, no server.

```bash
pipx install bizmap
bizmap scan . --mermaid               # writes bizmap.graph.json (+ .mmd)
bizmap who-writes orders.status       # entrypoint → via N functions → the writer, file:line
bizmap trace orders                   # where the data comes from and where it goes
bizmap export --format html           # one offline page that opens on "who writes / reads X?"
```

`who-writes` / `who-reads` / `trace` answer from the graph in about a second,
in text, Markdown or JSON ([query commands](commands/query.md)). The HTML page
asks the same question first — a big search, findings, a services × tables
matrix and a map around what you pick ([start screen](commands/export-home.md)).

![The HTML start screen answering who writes orders.status](img/home-answer.png){ .shot }

`bizmap why orders.status` still prints everything around one column at once:

![bizmap scan and bizmap why on bizmap-demo](assets/demo/hero.gif){ .shot }

<div class="grid cards" markdown>

-   :material-rocket-launch: **[Getting started](getting-started.md)**

    Install, scan the demo repository, read your first `why`.

-   :material-cog: **[Configuration](configuration.md)**

    `bizmap.yaml`: service names, shared databases, excludes, overrides.

-   :material-console: **[Commands](commands/index.md)**

    `scan`, `who-writes` / `who-reads` / `trace`, `export`, `why`, `report`, `impact`, `registry`, `mcp`.

-   :material-source-pull: **[GitHub Action](github-action.md)**

    One comment per pull request with the routes and columns it touches.

-   :material-robot: **[MCP server](mcp.md)**

    Let Claude Code, Cursor or any MCP client query the graph.

-   :material-code-tags: **[Languages and detectors](languages.md)**

    Go, Python, JavaScript/TypeScript and SQL: what is recognised.

</div>

## What it answers

- *Can I drop or rename `orders.status`?* Every reader and writer, in every repo.
- *Which endpoints touch `customers.email`?* Useful for LGPD/GDPR data mapping.
- *What happens to the database when the front-end calls `placeOrder()`?*
- *What does this pull request touch?* As a PR comment, on every push.

Every answer points at `file:line` and says how sure it is (`exact`,
`inferred` or `unknown`). When BizMap cannot see something — dynamic SQL, a
URL built at runtime, a service that was not scanned — it says so instead of
guessing. See [FAQ and limitations](faq.md).

## Try it on the demo

[bizmap-demo](https://github.com/lucas-simao/bizmap-demo) is a small
fictional shop with two services sharing one Postgres database: **shop**
(TypeScript + Express) and **payments** (Python + FastAPI), a database trigger,
and the GitHub Action running on its pull requests.

```bash
git clone https://github.com/lucas-simao/bizmap-demo && cd bizmap-demo
bizmap scan . --mermaid
bizmap who-writes orders.status
bizmap export --format html --out bizmap.html
```

The walk-through is in [Getting started](getting-started.md).
