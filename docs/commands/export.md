# `bizmap export` (coming soon)

!!! note "Not released yet"

    `bizmap export --format html` is planned for **0.5.0**. It is not in the
    current release, and this page will describe it once it ships.

The idea: a single self-contained HTML page built from `bizmap.graph.json`, to
browse entrypoints, functions, tables and columns without installing anything —
handy to attach to a PR or publish next to the code.

Until then, `bizmap scan --mermaid` writes a diagram that GitHub renders, and
[`bizmap mcp`](mcp.md) lets an agent query the graph.
