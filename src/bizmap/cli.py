from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from bizmap import __version__, progress
from bizmap.discover import (
    DEFAULT_DEPTH,
    Discovery,
    discover,
    format_discovery,
    load_discover_settings,
)
from bizmap.graph import Graph
from bizmap.impact import compute_impact, render_markdown
from bizmap.mermaid import DEFAULT_LIMIT, render_mermaid
from bizmap.overrides import ConfigError
from bizmap.registry import graph_from_json, merge_graphs
from bizmap.report import build_report, format_report
from bizmap.scan import relink_merged_graph, scan_path, scan_paths
from bizmap.why import format_why


class _Group(click.Group):
    """Report bizmap.yaml mistakes as one clear line, not a traceback."""

    def invoke(self, ctx: click.Context):  # type: ignore[override]
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=_Group)
@click.version_option(__version__, prog_name="bizmap")
def main() -> None:
    """BizMap — deterministic business-rule and data-lineage mapper.

    No AI, no tokens, no accounts. SQL, routes, jobs, and comments only.
    """


TOP_N = 10


def _short(text: str, width: int = 100) -> str:
    """One line, at most ``width`` characters (minified code can be huge)."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


_ORIGIN_DIRS = {"tests": "tests", "test": "tests", "scripts": "scripts", "script": "scripts", "eval": "eval", "evals": "eval"}


def _origin_of(graph: Graph, node_id: str) -> str:
    """app / scripts / eval / tests, from the file of the calling function."""
    node = graph.nodes.get(node_id)
    ev = (node.attrs.get("evidence") or {}) if node is not None else {}
    path = str(ev.get("file") or "")
    parts = path.split("/")
    name = parts[-1] if parts else ""
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" or name.endswith("_test.go"):
        return "tests"
    for part in parts[:-1]:
        if part in _ORIGIN_DIRS:
            return _ORIGIN_DIRS[part]
    return "app"


def scan_summary(graph: Graph, *, verbose: bool = False) -> list[str]:
    """What `bizmap scan` prints after the graph: skipped paths, warnings and
    unresolved HTTP calls as counts plus the top 10 (all with ``verbose``)."""
    lines: list[str] = []
    skipped = graph.attrs.get("skipped") or {}
    if skipped:
        labels = {
            "generated_dirs": "generated dirs",
            "generated_files": "generated/minified files",
            "gitignored": ".gitignored paths",
            "nested_repos": "nested repos/worktrees",
            "go_tests": "Go test files",
            "go_test_dirs": "Go testdata dirs",
            "py_tests": "Python test files",
            "py_test_dirs": "Python test dirs",
        }
        bits = [f"{n} {labels.get(k, k)}" for k, n in sorted(skipped.items())]
        lines.append("skipped: " + ", ".join(bits) + "  (scan: include / gitignore / skip_generated in bizmap.yaml)")
        paths = graph.attrs.get("skipped_paths") or {}
        for key in sorted(paths):
            names = [str(p) for p in paths[key]]
            if not names:
                continue
            shown_names = names if verbose else names[:5]
            more = (skipped.get(key) or len(names)) - len(shown_names)
            tail = f" … +{more} more (--verbose / `skipped_paths` in the JSON)" if more > 0 and not verbose else ""
            if verbose and more > 0:
                tail = f" … +{more} more (list capped in the JSON)"
            lines.append(f"  {labels.get(key, key)}: " + ", ".join(shown_names) + tail)
    warnings = [str(w) for w in graph.attrs.get("warnings") or []]
    shown = warnings if verbose else warnings[:TOP_N]
    for warning in shown:
        lines.append("warning: " + (warning if verbose else _short(warning, 160)))
    if len(warnings) > len(shown):
        lines.append(f"… {len(warnings) - len(shown)} more warnings (use --verbose, or see `warnings` in the JSON)")
    for item in graph.attrs.get("inferred_services") or []:
        lines.append(
            f"linked {item['calls']} call(s) through {item['env']} to {item['repo']} "
            f"(every matched call through it goes there; confirm with services: {item['repo']}: {{env: {item['env']}}})"
        )
    unresolved = graph.attrs.get("unresolved_http") or []
    hints = graph.attrs.get("service_hints") or []
    if hints:
        lines.append("hint: these env keys point at a scanned repo; add to bizmap.yaml so their calls link:")
        lines.append("  services:")
        for h in hints:
            prefix = f", prefix: {h['prefix']}" if h.get("prefix") else ""
            lines.append(
                f"    {h['repo']}: {{ env: {h['env']}{prefix} }}   # {h['calls']} unmatched call(s), "
                f"{h['matching']} match routes in {h['repo']}"
            )
    external = graph.attrs.get("external_envs") or []
    if external:
        shown = ", ".join(f"{e['env']} ({e['calls']})" for e in external[:8])
        more = f" +{len(external) - 8} more" if len(external) > 8 else ""
        lines.append(f"probably external (no scanned route matches their calls): {shown}{more}")
    in_process = int(graph.attrs.get("in_process_http") or 0)
    if in_process:
        lines.append(
            f"in-process test calls: {in_process} (TestClient/ASGITransport; not calls between services, "
            "not counted as unresolved)"
        )
    if not unresolved:
        return lines
    reason_counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    origin_counts: dict[str, int] = {}
    for item in unresolved:
        reason = str(item.get("reason") or "target_not_scanned")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        target = f"{str(item.get('method') or '').upper()} {item.get('url') or ''}".strip()
        target_counts[target] = target_counts.get(target, 0) + 1
        origin = _origin_of(graph, str(item.get("from") or ""))
        origin_counts[origin] = origin_counts.get(origin, 0) + 1
    order = ("route_not_found", "target_not_scanned", "base_unknown", "path_dynamic", "client_unknown")
    bits = [f"{name}={reason_counts[name]}" for name in order if name in reason_counts]
    bits += [f"{n}={c}" for n, c in sorted(reason_counts.items()) if n not in order]
    lines.append(f"{len(unresolved)} unresolved HTTP calls: " + ", ".join(bits))
    via_counts: dict[str, int] = {}
    for item in unresolved:
        via = str(item.get("via") or "")
        url = str(item.get("url") or "")
        path_part = url.split("}", 1)[1] if url.startswith("{") and "}" in url else url
        if via and "{" in path_part:
            via_counts[via] = via_counts.get(via, 0) + 1
    for via, count in sorted(via_counts.items(), key=lambda kv: (-kv[1], kv[0]))[: (None if verbose else 5)]:
        lines.append(f"  {count} call(s) via {via} with a dynamic path")
    if len(origin_counts) > 1 or "app" not in origin_counts:
        origin_order = ("app", "scripts", "eval", "tests")
        obits = [f"{k} {origin_counts[k]}" for k in origin_order if k in origin_counts]
        obits += [f"{k} {v}" for k, v in sorted(origin_counts.items()) if k not in origin_order]
        lines.append("  by origin: " + ", ".join(obits))
    ranked = sorted(target_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked if verbose else ranked[:TOP_N]
    for target, count in top:
        times = f" ×{count}" if count > 1 else ""
        lines.append("  " + (target if verbose else _short(target)) + times)
    if len(ranked) > len(top):
        lines.append(
            f"  … {len(ranked) - len(top)} more distinct calls (use --verbose, or see `unresolved_http` in the JSON)"
        )
    return lines


@main.command("scan")
@click.argument("path", nargs=-1, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("bizmap.graph.json"),
    show_default=True,
    help="Write the lineage graph JSON here.",
)
@click.option(
    "--mermaid",
    "mermaid_flag",
    is_flag=True,
    help="Also write a Mermaid flowchart next to the JSON (entrypoint→table rollup; joint scans include cross-service calls_http).",
)
@click.option(
    "--mermaid-focus",
    "mermaid_focus",
    default=None,
    help="Restrict the Mermaid diagram to one table and its neighborhood.",
)
@click.option(
    "--mermaid-limit",
    "mermaid_limit",
    type=int,
    default=DEFAULT_LIMIT,
    show_default=True,
    help="Max nodes in the Mermaid diagram.",
)
@click.option(
    "--expand-star",
    "expand_star",
    is_flag=True,
    help="Expand SELECT * to every schema column (off by default; table-level otherwise).",
)
@click.option(
    "--rules",
    "rules_dirs",
    type=click.Path(exists=True, path_type=Path),
    multiple=True,
    help="Extra YAML detector-rule files or directories (repeatable).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to bizmap.yaml (default: scan root, then git toplevel).",
)
@click.option(
    "--exclude",
    "exclude_globs",
    multiple=True,
    help="Exclude glob (repeatable); merged with bizmap.yaml exclude.",
)
@click.option(
    "--profile",
    "profile_flag",
    is_flag=True,
    help="Print phase timings (parse, resolve, sql, propagate) to stderr.",
)
@click.option(
    "-v",
    "--verbose",
    "verbose",
    is_flag=True,
    help="List every unresolved HTTP call and warning (default: counts and the top 10).",
)
@click.option(
    "-q",
    "--quiet",
    "quiet",
    is_flag=True,
    help="No progress lines on stderr (they appear only for scans longer than a few seconds).",
)
@click.option(
    "--discover",
    "discover_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    is_flag=False,
    flag_value=".",
    help="Find the git repos under DIR (default: the current directory; clones and worktrees "
    "of the same repo count once) and scan them jointly.",
)
@click.option("--list", "list_flag", is_flag=True, help="With --discover: show what would be scanned and why, then stop.")
@click.option("--json", "json_flag", is_flag=True, help="With --discover --list: print the discovery as JSON.")
@click.option(
    "--depth",
    type=click.IntRange(min=1),
    default=None,
    help="With --discover: how many folder levels below DIR to look for repos (default 2).",
)
@click.option(
    "--include",
    "include_globs",
    multiple=True,
    help="With --discover: also treat these folders (relative to DIR) as projects, even without git (repeatable).",
)
@click.option(
    "--prefer",
    "prefer_paths",
    multiple=True,
    help="With --discover: copy to scan when a repo has several (path relative to DIR, repeatable).",
)
@click.option(
    "--include-tests",
    "include_tests",
    is_flag=True,
    help="Also scan tests (Python tests/, test_*.py, conftest.py; Go *_test.go, testdata/). "
    "Same as `scan: include_tests: true` in bizmap.yaml.",
)
def scan_cmd(
    path: Path,
    out_path: Path,
    mermaid_flag: bool,
    mermaid_focus: str | None,
    mermaid_limit: int,
    expand_star: bool,
    rules_dirs: tuple[Path, ...],
    config_path: Path | None,
    exclude_globs: tuple[str, ...],
    profile_flag: bool,
    verbose: bool,
    quiet: bool,
    discover_dir: Path | None,
    list_flag: bool,
    json_flag: bool,
    depth: int | None,
    include_globs: tuple[str, ...],
    prefer_paths: tuple[str, ...],
    include_tests: bool,
) -> None:
    """Scan one or more codebases and write bizmap.graph.json.

    With --discover DIR, find the projects under DIR instead of listing them."""
    discover_only = [
        name
        for name, used in (("--list", list_flag), ("--json", json_flag), ("--depth", depth is not None),
                           ("--include", bool(include_globs)), ("--prefer", bool(prefer_paths)))
        if used
    ]
    if discover_dir is None and discover_only:
        verb = "works" if len(discover_only) == 1 else "work"
        raise click.UsageError(f"{', '.join(discover_only)} only {verb} with --discover DIR")
    if discover_dir is not None:
        if path:
            raise click.UsageError("give either PATH... or --discover DIR, not both")
        if json_flag and not list_flag:
            raise click.UsageError("--json needs --list (the scan itself always writes JSON to --out)")
        found = _run_discover(
            discover_dir, config_path, depth, list(include_globs), list(exclude_globs), list(prefer_paths)
        )
        if list_flag:
            if json_flag:
                click.echo(json.dumps(found.to_dict(), indent=2, sort_keys=True))
            else:
                click.echo(format_discovery(found), nl=False)
            return
        for warning in found.warnings:
            click.echo(f"warning: {warning}", err=True)
        chosen = found.chosen
        if not chosen:
            raise click.ClickException(
                f"no projects with supported code found under {discover_dir} "
                "(see `bizmap scan --discover DIR --list`; --depth N looks deeper)"
            )
        click.echo(
            f"discovered {len(chosen)} project(s): "
            + ", ".join(f"{g.name}={g.chosen.rel}" for g in chosen if g.chosen)
        )
        exclude_globs = ()  # used for discovery; per-repo excludes come from each bizmap.yaml
        roots = [g.chosen.path for g in chosen if g.chosen]
        names = {g.chosen.path: g.name for g in chosen if g.chosen}
    else:
        if not path:
            raise click.UsageError("missing scan path (or use --discover DIR)")
        roots = [Path(p) for p in path]
        names = None
    if not quiet:
        progress.start()
        what = f"{len(roots)} repos" if len(roots) > 1 else str(roots[0])
        click.echo(f"bizmap {__version__}: scanning {what}…", err=True)
    from bizmap import scan as scan_module

    scan_module.INCLUDE_TESTS[0] = include_tests
    try:
        if discover_dir is not None:
            graph = scan_paths(
                roots,
                extra_rules=list(rules_dirs) or None,
                expand_star=True if expand_star else None,
                config=config_path,
                profile=profile_flag,
                base=discover_dir,
                names=names,
            )
        elif len(roots) == 1:
            graph = scan_path(
                roots[0],
                extra_rules=list(rules_dirs) or None,
                expand_star=True if expand_star else None,
                config=config_path,
                exclude=list(exclude_globs) or None,
                profile=profile_flag,
            )
        else:
            graph = scan_paths(
                roots,
                extra_rules=list(rules_dirs) or None,
                expand_star=True if expand_star else None,
                config=config_path,
                exclude=list(exclude_globs) or None,
                profile=profile_flag,
            )
    finally:
        scan_module.INCLUDE_TESTS[0] = False
    payload = graph.to_dict()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    progress.stop()
    click.echo(
        f"wrote {out_path}  ({len(payload['nodes'])} nodes, {len(payload['edges'])} edges)"
    )
    for line in scan_summary(graph, verbose=verbose):
        click.echo(line)
    written = [out_path]
    if mermaid_flag or mermaid_focus:
        mmd_path = out_path.with_suffix(".mmd")
        mmd_path.write_text(
            render_mermaid(graph, focus=mermaid_focus, limit=mermaid_limit),
            encoding="utf-8",
        )
        click.echo(f"wrote {mmd_path}")
        written.append(mmd_path)
    from bizmap.gitignore_hint import gitignore_hint

    hint = gitignore_hint(written)
    if hint:
        click.echo(hint, err=True)


def _run_discover(
    root: Path,
    config_path: Path | None,
    depth: int | None,
    include: list[str],
    exclude: list[str],
    prefer: list[str],
) -> Discovery:
    """Flags win over the `discover:` section of DIR's bizmap.yaml."""
    try:
        settings = load_discover_settings(root, config=config_path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    return discover(
        root,
        depth=depth or settings.get("depth") or DEFAULT_DEPTH,
        include=include or settings.get("include"),
        exclude=exclude or settings.get("exclude"),
        prefer=prefer or settings.get("prefer"),
    )


@main.command("why")
@click.argument("column")
@click.option(
    "--graph",
    "graph_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("bizmap.graph.json"),
    show_default=True,
    help="Graph JSON produced by `bizmap scan`.",
)
def why_cmd(column: str, graph_path: Path) -> None:
    """Print who writes/reads TABLE.COLUMN and what triggers those functions."""
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid graph JSON: {exc}") from exc
    graph = Graph.from_dict(data)
    try:
        text = format_why(graph, column)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(text, nl=False)
    if text.startswith("unknown column"):
        sys.exit(1)


def _query_options(fn):
    fn = click.option(
        "--cache-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
        help="Folder for the index cache (default: BIZMAP_CACHE_DIR, else the user's cache folder; never the repo).",
    )(fn)
    fn = click.option(
        "--no-cache", is_flag=True, help="Neither read nor write the index cache (also: BIZMAP_NO_CACHE=1).",
    )(fn)
    fn = click.option(
        "--lang", type=click.Choice(["en", "pt-BR"]), default="en", show_default=True, help="Language of the text / md output."
    )(fn)
    fn = click.option(
        "--format", "fmt", type=click.Choice(["text", "json", "md"]), default="text", show_default=True,
        help="text for the terminal, json for tools (schema bizmap.query/v1), md to paste in a PR.",
    )(fn)
    fn = click.option(
        "--graph", "graph_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
        default=Path("bizmap.graph.json"), show_default=True, help="Graph JSON produced by `bizmap scan`.",
    )(fn)
    return fn


def _run_query(
    graph_path: Path, fmt: str, lang: str, run, show_path: bool = False, no_cache: bool = False,
    cache_dir: Path | None = None,
) -> None:
    from bizmap.query import Index, QueryError, legacy_cache_path, render, render_error

    legacy = legacy_cache_path(graph_path)
    if legacy.is_file() and legacy != graph_path:
        click.echo(
            f"note: {legacy} was written by bizmap 0.13.1 and is no longer used "
            "(the index cache now lives outside the repo): delete it, and keep it out of git.",
            err=True,
        )
    try:
        ix = Index.load(graph_path, use_cache=not no_cache, cache_directory=cache_dir)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        result = run(ix)
    except QueryError as err:
        click.echo(render_error(err, fmt, lang), nl=False, err=fmt != "json")
        sys.exit(err.code)
    click.echo(render(result, fmt, lang, show_path=show_path), nl=False)


@main.command("who-writes")
@click.argument("entity")
@_query_options
@click.option("--path", "show_path", is_flag=True, help="Show every function between the entrypoint and the writer.")
def who_writes_cmd(
    entity: str, graph_path: Path, fmt: str, lang: str, show_path: bool, no_cache: bool, cache_dir: Path | None
) -> None:
    """Who writes ENTITY (a table, collection, view or TABLE.COLUMN), from which entrypoints.

    ENTITY: `orders`, `table:orders`, `collection:orders`, `orders-api/table:orders`,
    `orders.status`. A name that matches different things (a collection and a table)
    lists them and exits with code 2; no match exits with 1."""
    from bizmap.query import who

    _run_query(graph_path, fmt, lang, lambda ix: who(ix, entity, "writes"), show_path, no_cache, cache_dir)


@main.command("who-reads")
@click.argument("entity")
@_query_options
@click.option("--path", "show_path", is_flag=True, help="Show every function between the entrypoint and the reader.")
def who_reads_cmd(
    entity: str, graph_path: Path, fmt: str, lang: str, show_path: bool, no_cache: bool, cache_dir: Path | None
) -> None:
    """Who reads ENTITY (a table, collection, view or TABLE.COLUMN), from which entrypoints.

    Same ENTITY forms and exit codes as `who-writes`."""
    from bizmap.query import who

    _run_query(graph_path, fmt, lang, lambda ix: who(ix, entity, "reads"), show_path, no_cache, cache_dir)


@main.command("trace")
@click.argument("entity")
@_query_options
@click.option("--depth", type=click.IntRange(min=1, max=10), default=2, show_default=True, help="Hops to follow.")
@click.option(
    "--direction", type=click.Choice(["up", "down", "both"]), default="both", show_default=True,
    help="up: where the data comes from; down: where it goes.",
)
def trace_cmd(
    entity: str, graph_path: Path, fmt: str, lang: str, depth: int, direction: str, no_cache: bool,
    cache_dir: Path | None,
) -> None:
    """Where ENTITY's data comes from and where it goes: feeds between tables /
    collections, queues (publishers → consumers) and HTTP calls between services."""
    from bizmap.query import trace

    _run_query(graph_path, fmt, lang, lambda ix: trace(ix, entity, depth=depth, direction=direction),
               no_cache=no_cache, cache_dir=cache_dir)


@main.command("report")
@click.argument("path", nargs=-1, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--graph",
    "graph_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("bizmap.graph.json"),
    show_default=True,
    help="Graph JSON to report on (ignored when PATH roots are given).",
)
@click.option("--redact", is_flag=True, help="Replace every name/URL/key with a salted hash.")
@click.option(
    "--salt",
    default=None,
    envvar="BIZMAP_REDACT_SALT",
    help="Salt for --redact hashes (env BIZMAP_REDACT_SALT). Same salt = comparable runs.",
)
@click.option(
    "--salt-file",
    "salt_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Read the --redact salt from this file, creating a random one on first use. "
    "Keeps redacted runs comparable without putting the salt on the command line.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
@click.option("--limit", type=int, default=20, show_default=True, help="Max rows per list.")
@click.option(
    "--mermaid",
    "mermaid_views",
    is_flag=True,
    help="Append the architecture and each queue / trigger flow as Mermaid diagrams (Markdown, for GitHub).",
)
@click.option(
    "--cross-service",
    "cross_service",
    is_flag=True,
    help="Count entrypoints that reach data only through other scanned services (calls_http) as reaching data.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="bizmap.yaml for PATH scans.",
)
def report_cmd(
    path: tuple[Path, ...],
    graph_path: Path,
    redact: bool,
    salt: str | None,
    salt_file: Path | None,
    as_json: bool,
    limit: int,
    config_path: Path | None,
    cross_service: bool,
    mermaid_views: bool,
) -> None:
    """Coverage report: entrypoints reaching data, HTTP links, unresolved reasons.

    Pass PATH roots to scan in memory (nothing written), or read --graph.
    With --redact the output contains counts and hashes only, so it can be
    shared from a private codebase.
    """
    if path:
        graph = (
            scan_paths(list(path), config=config_path)
            if len(path) > 1
            else scan_path(path[0], config=config_path)
        )
        data = graph.to_dict()
    else:
        if not graph_path.exists():
            raise click.UsageError(f"{graph_path} not found; run `bizmap scan` or pass PATH roots")
        try:
            data = json.loads(graph_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"invalid graph JSON: {exc}") from exc
    if mermaid_views and (redact or as_json):
        raise click.UsageError("--mermaid prints names: it cannot be combined with --redact or --json")
    if redact and not salt and salt_file is not None:
        import secrets

        if salt_file.exists():
            salt = salt_file.read_text(encoding="utf-8").strip()
        if not salt:
            import os

            salt = secrets.token_hex(16)
            salt_file.parent.mkdir(parents=True, exist_ok=True)
            # private from the first byte: 0600, never world/group readable
            fd = os.open(salt_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(salt + "\n")
            os.chmod(salt_file, 0o600)
            click.echo(
                f"note: wrote a new redaction salt to {salt_file}; keep it private and reuse it",
                err=True,
            )
    if redact and not salt:
        import secrets

        salt = secrets.token_hex(16)
        click.echo(
            "note: random salt for this run, so hashes differ between runs; "
            "use --salt-file PATH (or --salt / BIZMAP_REDACT_SALT) to compare runs",
            err=True,
        )
    rep = build_report(data, redact=redact, salt=salt or "", cross_service=cross_service)
    if as_json:
        click.echo(json.dumps(rep, indent=2, sort_keys=True))
    else:
        click.echo(format_report(rep, limit=limit), nl=False)
        if mermaid_views:
            from bizmap.views import context_from_data, mermaid_markdown

            click.echo("\n" + mermaid_markdown(context_from_data(data)))


@main.command("impact")
@click.option(
    "--graph",
    "graph_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("bizmap.graph.json"),
    show_default=True,
    help="Graph JSON of the changed code (scan of the PR head).",
)
@click.option(
    "--diff",
    "diff_path",
    type=click.Path(dir_okay=False, allow_dash=True, path_type=Path),
    default="-",
    show_default=True,
    help="Unified diff (`git diff base...head`); `-` reads stdin.",
)
@click.option(
    "--prefix",
    default=".",
    show_default=True,
    help="Scanned directory relative to the git root (the path given to `bizmap scan`).",
)
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Checkout the diff paths are relative to (read for changed .sql files).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the impact as JSON.")
@click.option("--limit", type=int, default=40, show_default=True, help="Max nodes in the diagram.")
@click.option("--max-rows", type=int, default=50, show_default=True, help="Max table rows (functions with impact).")
@click.option(
    "--max-chars",
    type=int,
    default=60000,
    show_default=True,
    help="Hard cap on the Markdown size (GitHub comments stop at 65,536).",
)
@click.option("--details-url", default=None, help="Link shown when rows are cut (e.g. the workflow run).")
@click.option(
    "--blob-url",
    default=None,
    help="Link `file:line` to this tree (https://github.com/OWNER/REPO/blob/SHA).",
)
@click.option("--mermaid/--no-mermaid", default=True, show_default=True, help="Include the Mermaid diagram.")
@click.option(
    "--shared-services",
    type=click.IntRange(min=2),
    default=3,
    show_default=True,
    help="High risk when the PR writes a table read or written by this many services.",
)
@click.option(
    "--full-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Also write the uncapped report here (the Action's run summary), from the same impact.",
)
@click.option(
    "--full-max-bytes",
    type=click.IntRange(min=1000),
    default=1_000_000,
    show_default=True,
    help="UTF-8 size cap of --full-out (GitHub step summaries stop at 1 MiB).",
)
def impact_cmd(
    graph_path: Path,
    diff_path: Path,
    prefix: str,
    repo_root: Path,
    as_json: bool,
    limit: int,
    max_rows: int,
    max_chars: int,
    details_url: str | None,
    blob_url: str | None,
    mermaid: bool,
    shared_services: int,
    full_out: Path | None,
    full_max_bytes: int,
) -> None:
    """Map a diff onto the graph: changed functions → entrypoints and data.

    Prints Markdown with a Mermaid diagram (for a PR comment), or JSON.
    """
    try:
        graph = graph_from_json(json.loads(graph_path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid graph JSON: {exc}") from exc
    diff_text = sys.stdin.read() if str(diff_path) == "-" else diff_path.read_text(encoding="utf-8")
    result = compute_impact(graph, diff_text, prefix=prefix, repo_root=repo_root)
    if result.hint:
        click.echo(f"warning: {result.hint}", err=True)
    for warning in result.warnings:
        click.echo(f"warning: {warning}", err=True)
    if full_out is not None:
        full_out.write_text(
            render_markdown(
                graph,
                result,
                limit=limit,
                max_rows=100_000,
                max_chars=full_max_bytes,
                max_bytes=full_max_bytes,
                details_url=details_url,
                blob_url=blob_url,
                mermaid=mermaid,
                shared_services=shared_services,
            ),
            encoding="utf-8",
        )
    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(
            render_markdown(
                graph,
                result,
                limit=limit,
                max_rows=max_rows,
                max_chars=max_chars,
                details_url=details_url,
                blob_url=blob_url,
                mermaid=mermaid,
                shared_services=shared_services,
            ),
            nl=False,
        )


@main.command("mcp")
@click.option(
    "--graph",
    "graph_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("bizmap.graph.json"),
    show_default=True,
    help="Graph JSON produced by `bizmap scan` (reloaded when it changes). "
    "Use an absolute path in editor configs.",
)
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Checkout read by the `impact` tool for changed .sql files (off by default).",
)
def mcp_cmd(graph_path: Path, repo_root: Path | None) -> None:
    """Serve the graph to MCP clients over stdio (read-only, offline).

    Tools: search, call_graph, function_data, table_usage, impact, why_column.
    """
    from bizmap.mcp_server import GraphStore, serve

    resolved = graph_path.expanduser().resolve()
    if not resolved.is_file():
        raise click.ClickException(
            f"graph not found: {resolved}\n"
            f"Create it with `bizmap scan <repo> --out {resolved}`, then point --graph at it "
            "(MCP clients start servers from their own directory, so use an absolute path)."
        )
    try:
        store = GraphStore(resolved, repo_root=repo_root)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"{resolved} is not a BizMap graph (invalid JSON: {exc}). Re-run `bizmap scan`."
        ) from exc
    except ValueError as exc:
        raise click.ClickException(f"{exc}. Re-run `bizmap scan`.") from exc
    except OSError as exc:
        raise click.ClickException(f"cannot read graph {resolved}: {exc}") from exc
    click.echo(
        f"bizmap mcp {__version__}: serving {resolved} "
        f"({len(store.graph.nodes)} nodes, {len(store.graph.edges)} edges) on stdio",
        err=True,
    )
    serve(store)


@main.command("export")
@click.option(
    "--graph",
    "graph_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("bizmap.graph.json"),
    show_default=True,
    help="Graph JSON produced by `bizmap scan`.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["html", "mermaid", "llm"]),
    default="html",
    show_default=True,
    help="html: one self-contained, offline page to read the system (services, domains, routes, tables). "
    "mermaid: Markdown with the architecture and each queue / trigger flow as Mermaid diagrams. llm: not available yet.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output file (default: bizmap.html for --format html, bizmap-architecture.md for --format mermaid).",
)
@click.option(
    "--lang",
    type=click.Choice(["en", "pt-BR"]),
    default="en",
    show_default=True,
    help="Language of the page.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="bizmap.yaml with `export.services` (split a monorepo into services). "
    "Default: ./bizmap.yaml when present.",
)
@click.option(
    "--openapi",
    "openapi",
    multiple=True,
    metavar="[SERVICE=]FILE",
    help="OpenAPI/Swagger file (swag's docs/swagger.json, FastAPI's openapi.json) with the "
    "request and response JSON of each route; repeatable. Default: found next to each repo "
    "(<repo>/docs/swagger.json, <repo>/openapi.json…) under the current directory.",
)
def export_cmd(
    graph_path: Path, fmt: str, out_path: Path | None, lang: str, config_path: Path | None,
    openapi: tuple[str, ...],
) -> None:
    """Export the graph to share it: `--format html` writes one offline page
    (services first; click to expand, search, filter by table)."""
    from bizmap.export import ExportError, export_graph

    if config_path is None and Path("bizmap.yaml").is_file():
        config_path = Path("bizmap.yaml")
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"graph not found: {graph_path}. Create it with `bizmap scan <repo> --out {graph_path}`."
        ) from exc
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{graph_path}: invalid graph JSON: {exc}") from exc
    try:
        notes: list[str] = []
        text = export_graph(
            data, fmt, lang=lang, config=config_path, openapi=list(openapi), root=Path("."), notes=notes
        )
        for note in notes:
            click.echo(note, err=True)
    except ExportError as exc:
        raise click.ClickException(str(exc)) from exc
    out = out_path or Path("bizmap-architecture.md" if fmt == "mermaid" else "bizmap.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    size = out.stat().st_size
    if fmt == "mermaid":
        click.echo(f"wrote {out}  ({size / 1024:.1f} KB) — Mermaid diagrams GitHub renders")
        return
    click.echo(
        f"wrote {out}  ({size / 1_048_576:.1f} MB, {len(data['nodes'])} nodes, "
        f"{len(data['edges'])} edges) — open it in a browser, no server needed"
    )


@main.group("registry")
def registry_grp() -> None:
    """Merge and query cross-repo lineage graphs."""


@registry_grp.command("merge")
@click.argument("graphs", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("bizmap.graph.json"),
    show_default=True,
    help="Write the merged graph JSON here.",
)
def registry_merge_cmd(graphs: tuple[Path, ...], out_path: Path) -> None:
    """Merge graph JSON files into one registry graph with repo-qualified ids."""
    if not graphs:
        raise click.UsageError("missing graph JSON paths")
    loaded = []
    for path in graphs:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"{path}: invalid graph JSON: {exc}") from exc
        loaded.append(graph_from_json(data))
    merged = merge_graphs(loaded)
    services: dict = dict(merged.attrs.get("services") or {})
    for graph in loaded:
        for k, v in (graph.attrs.get("services") or {}).items():
            services.setdefault(k, v)
    merged.attrs["services"] = services
    relink_merged_graph(merged, services)
    payload = merged.to_dict()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    click.echo(
        f"wrote {out_path}  ({len(payload['nodes'])} nodes, {len(payload['edges'])} edges)"
    )
