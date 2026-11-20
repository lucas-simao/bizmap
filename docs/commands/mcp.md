# `bizmap mcp`

Serves a graph to Claude Code, Cursor, Claude Desktop or any MCP client over
stdio: read-only, offline, no extra dependency.

```bash
bizmap scan . --out bizmap.graph.json
claude mcp add bizmap -- bizmap mcp --graph "$PWD/bizmap.graph.json"
```

| Option | Meaning |
| --- | --- |
| `--graph PATH` | Graph to serve, reloaded when it changes. Use an absolute path in editor configs. |
| `--repo-root DIR` | Checkout read by the `impact` tool for changed `.sql` files (off by default). |

Tools, client setup and the full input/output contract: [MCP server](../mcp.md).
