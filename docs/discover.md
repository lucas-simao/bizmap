# Discovering projects: `bizmap scan --discover`

A work folder usually holds more than one copy of the same repository: the
main clone, a few `git worktree`s for branches in progress, an old clone kept
around "just in case". Scanning all of them would count every route and table
two or three times. `--discover` finds the repositories under a folder, keeps
**one copy of each**, and scans those copies together (one joint graph, with
cross-repo HTTP links matched, exactly like `bizmap scan ./a ./b ./c`).

```bash
bizmap scan --discover ~/work --list         # what would be scanned, and why
bizmap scan --discover ~/work --list --json  # the same, as JSON
bizmap scan --discover ~/work --out work.json
```

Nothing is fetched, checked out or changed: BizMap only reads the files that
are on disk.

## What counts as a project

- A folder with a `.git` **directory** is a main checkout; a folder with a
  `.git` **file** (`gitdir: …`) is a worktree.
- BizMap looks `--depth` levels below the folder (default **2**:
  `~/work/shop` and `~/work/clients/shop`). Use `--depth 3` for deeper layouts.
- It never looks inside a repository it found, so nested repos, submodules and
  vendored copies are scanned as part of their parent repository and are never
  projects of their own. (A submodule checked out *next to* other repos, as its
  own folder, is a repository like any other.) Nor does it look inside dependency and build
  folders (`node_modules`, `vendor`, `.venv`, `dist`, `build`, `.next`, …) or
  hidden folders.
- If the folder itself is a repository, that repository is the only project.
- A repository with no Go, Python, JS/TS or SQL files (docs, infra, design
  assets) is listed as `skipped (no supported source code)`.
- Folders that are not repositories are ignored unless you name them with
  `--include` (relative to the folder; globs work). Those that hold code are
  listed under `left out:` as `no git; add with --include PATH`, so a missing
  project is never a mystery; once included they show as `folder · no git`.

## Which copies are the same repository

Two folders are copies of one repository when

- they share a git directory (a checkout and its worktrees), or
- their `origin` remotes are the same URL once normalized:
  `https://github.com/acme/shop.git`, `https://user@GitHub.com/Acme/Shop/`,
  `git@github.com:acme/shop` and `ssh://git@github.com:22/acme/shop.git` are all
  `github.com/acme/shop`. A local path origin is compared as an absolute path.

Different repositories that happen to share a folder name stay separate
(`acme/api` and `other/api` become `api` and `api-2`).

## Which copy is scanned

The rules are applied in order; the first one that tells the copies apart
decides, and `--list` shows which one it was:

| criterion        | the chosen copy is…                                                                 |
|------------------|-------------------------------------------------------------------------------------|
| `only_copy`      | the only copy found                                                                 |
| `preferred`      | named with `--prefer PATH` (or `discover.prefer` in bizmap.yaml)                    |
| `main_checkout`  | a main checkout rather than a worktree                                              |
| `default_branch` | on the default branch: `origin/HEAD` when the clone knows it, else `main`/`master`  |
| `folder_name`    | in the folder named like the repository (`shop-api/` for `…/shop-api.git`)          |
| `latest_commit`  | the one with the most recent commit                                                 |
| `path_order`     | first by path (everything else was equal)                                           |

The commit time comes from `git log -1 --format=%ct` when git is installed
(`latest_commit (git)`). Without git, BizMap reads the committer time from the
commit object itself when it is stored loose (`commit-file`), and otherwise
falls back to the modification time of the branch's ref file (`ref-mtime`, an
approximation). When refs are packed and the times cannot be told apart, the
tie goes to `path_order` and the reason says so. Detached checkouts compete
like any other; they are never on the default branch.

A `--prefer` that matches none of the folders found prints a warning (in
`--list` and on stderr during the scan) instead of being ignored silently.

The result is deterministic: the same folder gives the same choice and the same
`--list` output every time.

## `--list`

```
/home/me/work  (depth 2)

notes: skipped (no supported source code)
    notes                                    main · main · t=1763600000 (git) · no code
orders: orders  [folder_name: folder named like the repository (orders)]
  * orders                                   main · main · t=1763640000 (git)
    old/orders-2023                          main · main · t=1700000000 (git)
shop: shop  [main_checkout: main checkout (others are worktrees)]
  * shop                                     main · main · t=1763600000 (git)
    shop-feature                             worktree · feature · t=1763610000 (git)

left out:
  archive  (excluded)
  design-studio  (no git; add with --include design-studio)

2 project(s) to scan: orders, shop
```

Each group shows the scanned copy (`*`), the criterion and reason, and every
other copy with its kind, branch and commit time. `--json` prints the same data
(`groups[].chosen`, `criterion`, `reason`, `members[]`, `skipped[]`).

## Options and bizmap.yaml

| flag              | bizmap.yaml (`discover:` section) | meaning                                                   |
|-------------------|-----------------------------------|-----------------------------------------------------------|
| `--depth N`       | `depth: 3`                        | levels below the folder to search (default 2)             |
| `--include GLOB`  | `include: [scripts]`              | treat these non-git folders as projects too               |
| `--exclude GLOB`  | `exclude: [archive, "old/*"]`     | do not look in these folders                              |
| `--prefer PATH`   | `prefer: [shop-feature]`          | scan this copy when a repository has several              |

```yaml
# ~/work/bizmap.yaml
discover:
  depth: 3
  exclude: [archive]
  prefer: [clients/shop-main]
services:                       # shared by every discovered repo
  billing: { url: http://billing.internal, env: BILLING_URL }
```

Flags win over the file (a flag replaces the whole list, it does not add to
it). All paths are relative to the discovered folder.

With `--discover`, `--exclude` applies to **discovery**; excludes inside each
repository come from that repository's own `bizmap.yaml` (`exclude:`), as in a
normal scan. Each repository keeps its own `bizmap.yaml` (name, database,
overrides); a repository without a `name:` is named after its `origin`
(`github.com/acme/shop` → `shop`), else its folder. `services:` from the
folder's bizmap.yaml are shared, as with `repos:`. As in any joint scan,
`repo_roots` in the graph are relative to each repository's own git root, so
`bizmap impact` on a diff from one of those repositories works unchanged.
