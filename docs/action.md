# BizMap impact — GitHub Action

On every pull request, the Action scans the repository, maps the PR's diff onto
the lineage graph, and posts **one** comment (updated on each push) that says
which routes, jobs, tables and columns the change reaches, and how risky that is.

It is deterministic static analysis: no AI, no tokens, no account. The code
never leaves the runner.

## Quick start

```yaml
# .github/workflows/bizmap-impact.yml
name: BizMap impact
on: pull_request            # not pull_request_target: never run PR code with a write token
permissions:
  contents: read
  pull-requests: write
concurrency:                # two quick pushes must not race to create two comments
  group: bizmap-impact-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  bizmap:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4   # the base commit is fetched if missing; fetch-depth: 0 also works
      - uses: lucas-simao/bizmap@v0.4.2   # pin a tag or a commit SHA
        with:
          path: .            # or the workspace dir holding bizmap.yaml with `repos:`
```

A stricter setup that fails the check on high-risk PRs and leaves the diagram
out:

```yaml
      - uses: lucas-simao/bizmap@v0.4.2
        id: bizmap
        with:
          path: services
          risk-threshold: high      # the comment is still posted first
          mermaid: "false"
          shared-services: "2"
      - run: echo "BizMap risk: ${{ steps.bizmap.outputs.risk }}"
        if: always()
```

## The comment

````markdown
### BizMap impact · 🔴 High risk

**2** entrypoints (2 routes) · writes **3** tables · reads **1** table · **5** columns — from **3** changed functions

**Why high:**
- writes `orders`, used by 3 services (api, billing, worker)
- writes `orders`, which has trigger `orders_audit`
- changes the handler of `api: POST /api/orders`, `api: POST /api/orders/:id/cancel`
- crosses services (api → billing, web → api)

▸ Diagram (open) · Entrypoints · Data · Schema changes · Triggers ·
  Cross-service calls · Changed functions · Unresolved & notes
````

- **Header**: a risk badge and one line with how many entrypoints (by kind:
  routes, jobs, workers…), tables written/read and columns the PR reaches.
- **Why …**: the reasons behind the badge, most severe first (see below).
- **Diagram** (Mermaid, rendered by GitHub): entrypoint → changed function →
  table. Bounded: at most 4 entrypoints per function (the rest become one
  `+N more` node), columns collapsed into tables, one box per service in a
  joint scan, triggers hanging off the table they fire on, at most 40 nodes.
- **Collapsible sections** (`<details>`): entrypoints (with *handler changed*
  when the PR edits the route's own handler), data (columns written and read,
  shared tables and triggers flagged), schema changes from `.sql` files
  (opened when a column still in use is dropped or renamed), triggers,
  calls between services (and how each was matched), every changed function,
  and what static analysis could not settle (unmatched HTTP calls, ambiguous
  handlers, dynamic table names, `heuristic`/`unknown` links, files outside any
  function).
- **`file:line` links** point at the PR's head commit (or at the merge commit
  that was scanned, when the base branch also changed those files, so line
  numbers always match).
- When nothing reaches a route, job or table the comment is two lines:
  `🟢 No data impact`.

The comment always stays under GitHub's size limit: rows are capped by
`max-rows`, then the diagram is dropped; the run summary has the full report.

### Risk levels

| Level | When (any of) |
|---|---|
| 🔴 high | a `.sql` change drops or renames a column that code still reads or writes · the PR writes a table that `shared-services` (default 3) or more services read or write |
| 🟠 medium | it writes a table that has a DB trigger · it changes the handler of an HTTP route · the reach crosses services over HTTP · it writes a table reached from an entrypoint |
| 🟡 low | it writes data no entrypoint reaches · it only reads data · it reaches entrypoints but no table · other schema changes (a new column, a new table) |
| 🟢 none | nothing above |

"Services" are the repos of a joint scan (`bizmap.yaml` with `repos:`) or of a
merged registry graph, so the shared-table rule needs a multi-repo graph.
BizMap does not know which routes are public; any HTTP route whose handler
changed counts.

## Inputs

| Input | Default | |
|---|---|---|
| `path` | `.` | Directory to scan, relative to the repository root (a `bizmap.yaml` with `repos:` scans a monorepo jointly). |
| `config` | | Optional path to `bizmap.yaml`. |
| `comment` | `true` | Post (or update) the PR comment. `false` only writes the step summary. |
| `comment-author` | `github-actions[bot]` | Login that owns the comment to update (the token's user). Comments by anyone else are never edited. |
| `max-rows` | `50` | Max rows per table in the comment (the step summary lists all). |
| `mermaid` | `true` | Include the Mermaid diagram. |
| `shared-services` | `3` | High risk when the PR writes a table this many services read or write (integer ≥ 2). |
| `risk-threshold` | | `low`, `medium` or `high`: fail the job when the risk is at least this level. Empty never fails. |
| `fail-on-error` | `false` | Fail the job when BizMap itself fails (the comment always says it failed). |
| `python-version` | `3.12` | Python used to run BizMap. |
| `github-token` | `${{ github.token }}` | Token for the comment (needs `pull-requests: write`). |

## Outputs

| Output | |
|---|---|
| `markdown` | Path of the Markdown written to the PR comment. |
| `status` | `ok`, `skipped` or `failed`. |
| `risk` | `none`, `low`, `medium` or `high` (empty when no report was produced). |

## Behaviour worth knowing

- One comment per PR, found by the hidden `<!-- bizmap-impact -->` marker and
  `comment-author`, and edited in place on every push. If the comments cannot
  be listed, nothing is posted (no duplicates).
- PRs from forks get a read-only token: the impact is only in the step summary.
- The check is advisory: a BizMap failure is reported in the comment and as a
  warning, and fails the job only with `fail-on-error: "true"`.
  `risk-threshold` fails it on purpose, after the comment is posted.
- With `ref: <head sha>` checkouts the diff starts at the merge base, so
  base-branch drift is not reported as the PR's change.

## Running it locally

The comment is the Markdown printed by `bizmap impact`:

```bash
bizmap scan . --out /tmp/g.json
git diff --unified=0 origin/main... | bizmap impact --graph /tmp/g.json \
  --blob-url https://github.com/OWNER/REPO/blob/$(git rev-parse HEAD)
```

`--no-mermaid`, `--shared-services N`, `--max-rows N` and `--max-chars N`
match the Action inputs; `--json` prints the raw impact instead.
