# `bizmap impact`

Maps a diff onto the graph: each changed function → the entrypoints that reach
it (across services, in a joint scan) and the tables and columns it, or
anything it calls, reads or writes. Changed `.sql` files are read for schema
changes (columns added, altered, dropped or renamed while code still uses
them). Prints Markdown with a Mermaid diagram — the
[PR comment](../github-action.md) — or JSON.

```bash
bizmap scan . --out /tmp/head.json
git diff --unified=0 origin/main... | bizmap impact --graph /tmp/head.json
```

Scan the **head** of the change (the code after the diff), then pipe the diff
in. Lines outside any function (imports, docs) are listed as unmapped and
produce no impact.

## Options

| Option | Meaning |
| --- | --- |
| `--graph PATH` | Graph of the changed code (the PR head). Default `bizmap.graph.json`. |
| `--diff PATH` | Unified diff (`git diff base...head`); `-` (default) reads stdin. |
| `--prefix DIR` | The directory given to `bizmap scan`, relative to the git root (default `.`). |
| `--repo-root DIR` | Checkout the diff paths are relative to; changed `.sql` files are read from it (default `.`). |
| `--json` | The impact as JSON. |
| `--limit N` | Max nodes in the diagram (default 40). |
| `--max-rows N` | Max table rows, ranked by how many routes and tables each function reaches (default 50). |
| `--max-chars N` | Hard cap on the Markdown size (default 60000; GitHub comments stop at 65,536). |
| `--details-url URL` | Link shown when rows are cut (the workflow run, for example). |

## Example

In [bizmap-demo](https://github.com/lucas-simao/bizmap-demo), PR #4 changes the
handler behind `POST /orders/:id/paid`. The shop route is called by the
payments service, so the comment lists `payments: POST /webhooks/provider`
among the entrypoints, and the database trigger's table among the writes:

```text
| Changed function | Entrypoints | Writes | Reads |
|---|---|---|---|
| `shop: src.orders.markPaid` | `payments: POST /webhooks/provider`<br>`shop: POST /orders/:id/paid` | `shop: order_status_history`<br>`shop: orders (created_at, status)` | `shop: orders (id)` |
```

The same result is available to coding agents as the MCP
[`impact`](../mcp.md#impact) tool.
