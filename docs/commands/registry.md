# `bizmap registry merge`

Joins graphs that were scanned separately — in different CI jobs, on
different machines — into one graph, as if they had been scanned together.

```bash
bizmap scan ./web --out web.json
bizmap scan ./api --out api.json
bizmap scan ./billing --out billing.json
bizmap registry merge web.json api.json billing.json --out merged.json
```

Function, entrypoint and queue ids are qualified with each repository's name;
tables and collections share ids when the repositories declare the same
[`database:`](../configuration.md#name-and-database). HTTP and queue links
between them are matched again from the calls stored in each graph, using
the `services:` each graph was scanned with.

Inside git work trees the result equals a joint scan
(`bizmap scan ./web ./api ./billing`). Outside git, see `repo_roots` in the
[graph format](../graph-schema.md#top-level-object).

| Option | Meaning |
| --- | --- |
| `--out PATH` | Where to write the merged graph (default `bizmap.graph.json`). |
