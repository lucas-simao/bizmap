# BizMap MCP server

> Requires **bizmap 0.4.0 or newer** (`pip install -U bizmap`).

`bizmap mcp` lets a coding agent (Claude Code, Cursor, Claude Desktop, or any
MCP client) ask the lineage graph questions — *what writes `orders.status`?*,
*which routes reach `markPaid`?*, *what does this diff touch?* — instead of
grepping.

- **Read-only and offline.** It reads the graph JSON written by `bizmap scan`.
  It never writes files, opens the network or runs a model.
- **Deterministic.** The same graph and arguments give the same answer: every
  list has a fixed order (below).
- **No extra dependency.** The MCP layer (JSON-RPC 2.0 over stdio) is built in;
  `pip install bizmap` (0.4.0+) is all you need.
- **Fresh.** The graph is reloaded when its file changes, so re-running
  `bizmap scan` updates a running session.

Supported protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`.

## 1. Build the graph

```bash
pip install bizmap
cd ~/code/shop                       # your repo (or a workspace with bizmap.yaml)
bizmap scan . --out bizmap.graph.json
```

Check that the server starts (it waits for a client on stdin; press Ctrl-C):

```bash
bizmap mcp --graph "$PWD/bizmap.graph.json"
# bizmap mcp 0.x: serving /home/you/code/shop/bizmap.graph.json (812 nodes, 1904 edges) on stdio
```

MCP clients start servers from their own working directory, so configs below use
**absolute paths**. If the client can't find `bizmap`, use the full path to the
executable (`which bizmap`, e.g. `/home/you/.local/bin/bizmap` with pipx).

## 2. Connect a client

### Claude Code

```bash
claude mcp add bizmap -- bizmap mcp --graph /home/you/code/shop/bizmap.graph.json
```

### Cursor

Project-level `.cursor/mcp.json` (or `~/.cursor/mcp.json` for every project).
Cursor expands `${workspaceFolder}`:

```json
{
  "mcpServers": {
    "bizmap": {
      "command": "bizmap",
      "args": ["mcp", "--graph", "${workspaceFolder}/bizmap.graph.json"]
    }
  }
}
```

### Claude Desktop

Settings → Developer → Edit Config opens `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`).
Paths must be absolute; on Windows escape backslashes (`"C:\\code\\shop\\bizmap.graph.json"`).

```json
{
  "mcpServers": {
    "bizmap": {
      "command": "/home/you/.local/bin/bizmap",
      "args": ["mcp", "--graph", "/home/you/code/shop/bizmap.graph.json"]
    }
  }
}
```

Quit and reopen Claude Desktop after editing. If the server doesn't appear, its
stderr (including the `serving …` line or the error) is in
`mcp-server-bizmap.log` under `~/Library/Logs/Claude` (Windows: `%APPDATA%\Claude\logs`).

### Options

| Flag | Default | |
|---|---|---|
| `--graph PATH` | `bizmap.graph.json` | graph from `bizmap scan`; use an absolute path |
| `--repo-root DIR` | off | lets `impact` read changed `.sql` files under this checkout (schema changes) |

## 3. Tools

All tools are annotated read-only, idempotent and closed-world.

> `who-writes`, `who-reads` and `trace` are **command-line only for now** —
> they are not MCP tools yet. The closest tool is
> [`table_usage`](#table_usage) (every reader and writer of a table or
> column); `call_graph` and `function_data` cover the rest of a trace.

**Naming things.** Wherever a tool takes `function`, pass an id returned by
`search` (`function:orders.markPaid`, `api/entrypoint:http:POST:/orders`), a
function name or its last part (`markPaid`), or a route as `METHOD /path`
(`POST /orders/:id/paid`). Tables are `orders`; columns are `orders.status`.
A name that matches several nodes is an error listing the candidate ids; for
`METHOD /path` the route wins over its inline handler function.

**Common output contract.**

- A node item is `{id, type, label, repo?, evidence?, end_line?, why?, method?, path?}`.
  `evidence` is `file:line` where it is defined (relative to its repo root);
  `why` is its doc comment.
- Every item derived from an edge also has `confidence`:
  `exact` > `inferred` > `heuristic` > `unknown`. For routes reaching a function it is the
  confidence of the strongest path (its weakest edge).
- Every top-level list has at most `limit` items (default 20, max 100), with
  `<list>_total` next to it; nested lists are capped too (`columns` per table at
  `limit`; per-function lists in `impact` at 10) with their own `<list>_total`.
  `truncated: true` whenever anything was cut.
- The whole result stays under 20,000 characters: if it would not, items are dropped
  from the end of the least important list first (e.g. in `impact`: unmapped files,
  then reads, writes, schema changes, markdown, entrypoints, and `functions` last),
  only as many as needed; a cut text (`why_column`) says how many lines were left out.
- Results are JSON: `structuredContent`, and the same JSON (compact) as text in
  `content[0].text`.
- Arguments are checked against each tool's schema (types, allowed values, ranges,
  no unknown keys): `"transitive": "false"` is an error, not `true`.
- Problems (unknown name, ambiguous name, bad argument) come back as a tool result with
  `isError: true` and a message saying what to do; protocol errors use JSON-RPC codes.

### `search`

| Input | |
|---|---|
| `query` (required) | text: a name, part of a path, a doc-comment word |
| `kind` | `any` (default), `function`, `route`/`entrypoint`, `table`, `column` |
| `limit` | 1–100, default 20 |

Output: `results[]` node items with `match` = `exact` \| `prefix` \| `substring` \| `doc`.
Order: match quality, then id.

### `call_graph`

| Input | |
|---|---|
| `function` (required) | function or route |
| `direction` | `callers`, `callees` or `both` (default) |
| `depth` | 1–5, default 1 |
| `limit` | 1–100, default 20 |

Output: `node`; `callers[]` / `callees[]` items with `edge` (`calls`, `calls_http`,
`triggers`, `enqueues`), `confidence`, `depth`, `call_evidence` (`file:line` of the call)
and `via` when known; with callers, `entrypoints[]` (routes/jobs reaching the function,
at any depth). Order: depth, edge type, id. `calls_http` edges cross services.

### `function_data`

| Input | |
|---|---|
| `function` (required) | function or route |
| `transitive` | also through everything it calls (default `false`) |
| `limit` | 1–100, default 20 (tables per list) |

Output: `writes[]` and `reads[]`, one row per table:
`{table, confidence, evidence, columns: [{column, confidence, evidence}]}`.
Order: table, then column.

### `table_usage`

| Input | |
|---|---|
| `table` (required) | `orders`, or `orders.status` for one column |
| `limit` | 1–100, default 20 |

Output: `target`; `functions[]` node items with `confidence`, `writes[]` and `reads[]`
(`{column, confidence, evidence}`, `*` = the whole table); `entrypoints[]` that reach
those functions. Order: id; columns by name.

### `impact`

| Input | |
|---|---|
| `diff` (required) | unified diff, paths relative to the git root (`git diff --unified=0 main...`) |
| `prefix` | scanned directory relative to the git root, for graphs scanned outside git |
| `markdown` | also return the PR-comment Markdown (default `false`) |
| `limit` | 1–100, default 20 |

Output:

- `functions[]`: node items with `changed_lines`, `entrypoints[]` (node items),
  `writes[]` and `reads[]` (data node **ids**), each capped at 10 with `<list>_total`.
- `entrypoints[]`: node items reached by any changed function.
- `writes[]`, `reads[]`, `unmapped_files[]`: ids / paths (strings).
- `schema_changes[]`: `{op, table, name, detail, column, file, line, entrypoints,
  functions}` for changed `.sql` lines — only when the server runs with `--repo-root`
  (paths outside it are never read).
- `hint`, `warnings`: present only when there is something to say (e.g. `.sql` files
  changed but the server has no `--repo-root`).

Same logic as `bizmap impact`.

### `why_column`

| Input | |
|---|---|
| `column` (required) | `table.column`, a table name (its columns' readers/writers), or an id from `search` |

Output: `text`, the `bizmap why` report (writers, readers, what triggers them). In a
multi-repo graph a name that exists in several repos is an error listing the ids
(`shop/column:orders.status`, …): pass one of them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `graph not found: …` | run `bizmap scan`, then use the absolute path it wrote |
| `graph not found: …/${workspaceFolder}/…` | the client doesn't expand `${workspaceFolder}` (only some do, e.g. Cursor): write the absolute path |
| `No such command 'mcp'` | your bizmap is older than 0.4.0: upgrade with `pip install -U bizmap` |
| client shows no tools | check the client's server log for the `serving …` line or an error |
| `command not found: bizmap` in the client log | use the full path from `which bizmap` |
| answers look stale | re-run `bizmap scan` (the server reloads the file automatically) |
| `'x' matches N nodes` | pass one of the listed ids |
