# Commands

| Command | What it does |
| --- | --- |
| [`bizmap scan`](scan.md) | Reads one or more repositories and writes the graph (`bizmap.graph.json`), optionally a Mermaid diagram. |
| [`bizmap scan --discover`](../discover.md) | Finds the git repositories in a work folder, keeps one copy of each, scans them together. |
| [`bizmap why`](why.md) | Every writer and reader of one column, what triggers them and which service calls in. |
| [`bizmap report`](report.md) | How much of the code BizMap understood, with hints for what it could not link. |
| [`bizmap impact`](impact.md) | Maps a diff onto the graph: the routes and columns a change reaches (the PR comment). |
| [`bizmap registry merge`](registry.md) | Joins graphs scanned separately into one, re-linking calls between them. |
| [`bizmap mcp`](mcp.md) | Serves the graph to coding agents over MCP (read-only, offline). |
| [`bizmap export`](../export.md) | :material-clock-outline: *Coming in 0.5.0 (not released yet)*: one offline HTML page to browse the graph. |

Every command that reads a graph takes `--graph PATH` (default
`bizmap.graph.json` in the current folder). `bizmap --version` prints the
version; `bizmap COMMAND --help` lists every option.
