# GitHub Action

On every pull request, the Action scans the PR head, maps the diff onto the
graph and posts **one** comment — updated on each push — with the routes,
functions, tables and columns the change reaches, and a Mermaid diagram.

![The BizMap comment on a pull request](assets/demo/pr-comment-small.png){ .shot width="760" }

## Setup

`.github/workflows/bizmap-impact.yml`:

```yaml
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
      - uses: lucas-simao/bizmap@v0.17.2
        with:
          path: .            # or the workspace dir holding bizmap.yaml with `repos:`
```

Pin a release tag (or a commit SHA, for supply-chain hygiene:
`lucas-simao/bizmap@<sha> # v0.17.2`). The Action installs BizMap from that
same ref, so the CLI and the Action always match.

## See it running

The two demo repos keep pull requests open as live examples, each scanned
together with the other repo:

| PR | The change |
| --- | --- |
| [api #1 Pricing: gold discount 12%, new silver tier, cap 20%](https://github.com/lucas-simao/bizmap-demo-api/pull/1) | A business rule in `pricing.go`: the routes that reach it and the order columns it writes. |
| [api #2 Migration: add orders.shipping_cents, rename shipments.tracking_code](https://github.com/lucas-simao/bizmap-demo-api/pull/2) | A migration only: the columns added or renamed and the code that uses them. |
| [web #1 Returns screen: request a return on the shared order timeline](https://github.com/lucas-simao/bizmap-demo-web/pull/1) | A new storefront screen writing the Firestore collection both repos share. |
| [web #2 Order page: show the shipment tracking code](https://github.com/lucas-simao/bizmap-demo-web/pull/2) | A front-end change whose data lives in the other repo (*crosses services: web → api*). |

??? example "A change reached from another service (earlier version of the demo)"

    ![The BizMap comment on a change reached from another service](assets/demo/pr-comment.png){ .shot }

??? example "A migration that drops and renames columns still in use (earlier version of the demo)"

    ![The BizMap comment on a migration that drops and renames columns still in use](assets/demo/pr-comment-schema.png){ .shot }

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `path` | `.` | Directory to scan, relative to the repository root. A `bizmap.yaml` with `repos:` there scans a monorepo jointly. |
| `config` | — | Path to `bizmap.yaml`, when it is not in `path`. |
| `comment` | `"true"` | Post (or update) the PR comment. `"false"` only writes the job summary. |
| `comment-author` | `github-actions[bot]` | Login that owns the comment to update. Comments by anyone else are never edited. |
| `max-rows` | `"50"` | Max functions in the comment (the job summary lists all). |
| `fail-on-error` | `"false"` | Fail the job when BizMap itself fails. The check is advisory by default. |
| `python-version` | `"3.12"` | Python used to run BizMap. |
| `github-token` | `${{ github.token }}` | Token for the comment (needs `pull-requests: write`). |

## Outputs

| Output | Meaning |
| --- | --- |
| `markdown` | Path of the Markdown written to the comment. |
| `status` | `ok`, `skipped` or `failed`. |

## Behaviour

- **One comment per PR.** It carries a hidden marker and is edited on every
  push. If the comments cannot be listed, nothing is posted (no duplicates).
- **Size-safe.** At most `max-rows` functions, ranked by how many routes and
  tables they reach; the comment stays under GitHub's size limit and the
  workflow run summary has the full list.
- **Forks** get a read-only token, so they only get the job summary.
- **Failures are visible but advisory.** If the scan fails, the comment says so
  and links the log; the job fails only with `fail-on-error: "true"`.
- **Only the PR's own change.** The diff starts at the merge base, so commits
  that landed on the base branch meanwhile are not reported (also with
  `ref: <head sha>` checkouts).
- **Schema changes.** Changed `.sql` files (schema, migrations) are read: a
  column dropped or renamed while code still uses it is flagged ⚠️ with the
  routes that use it.

## Running it locally

The comment is exactly what [`bizmap impact`](commands/impact.md) prints:

```bash
bizmap scan . --out /tmp/head.json
git diff --unified=0 origin/main... | bizmap impact --graph /tmp/head.json
```
