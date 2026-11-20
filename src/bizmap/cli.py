from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from bizmap import __version__
from bizmap.graph import Graph
from bizmap.impact import compute_impact, render_markdown
from bizmap.mermaid import DEFAULT_LIMIT, render_mermaid
from bizmap.registry import graph_from_json, merge_graphs
from bizmap.report import build_report, format_report
from bizmap.scan import relink_merged_graph, scan_path, scan_paths
from bizmap.why import format_why


@click.group()
@click.version_option(__version__, prog_name="bizmap")
def main() -> None:
    """BizMap — deterministic business-rule and data-lineage mapper.

    No AI, no tokens, no accounts. SQL, routes, jobs, and comments only.
    """


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
) -> None:
    """Scan one or more codebases and write bizmap.graph.json."""
    if not path:
        raise click.UsageError("missing scan path")
    roots = [Path(p) for p in path]
    if len(roots) == 1:
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
    payload = graph.to_dict()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    click.echo(
        f"wrote {out_path}  ({len(payload['nodes'])} nodes, {len(payload['edges'])} edges)"
    )
    for warning in graph.attrs.get("warnings") or []:
        click.echo(warning)
    unresolved = graph.attrs.get("unresolved_http") or []
    if unresolved:
        keys: list[str] = []
        seen: set[str] = set()
        reason_counts: dict[str, int] = {}
        for item in unresolved:
            reason = str(item.get("reason") or "target_not_scanned")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            for key in item.get("keys") or []:
                if key not in seen:
                    seen.add(key)
                    keys.append(str(key))
        order = ("target_not_scanned", "base_unknown", "path_dynamic", "client_unknown")
        reason_bits = [f"{name}={reason_counts[name]}" for name in order if name in reason_counts]
        for name, count in sorted(reason_counts.items()):
            if name not in order:
                reason_bits.append(f"{name}={count}")
        if reason_bits:
            click.echo("HTTP unmatched by reason: " + ", ".join(reason_bits))
        suffix = f" ({', '.join(keys)})" if keys else ""
        click.echo(f"{len(unresolved)} unresolved HTTP calls{suffix}")
    if mermaid_flag or mermaid_focus:
        mmd_path = out_path.with_suffix(".mmd")
        mmd_path.write_text(
            render_mermaid(graph, focus=mermaid_focus, limit=mermaid_limit),
            encoding="utf-8",
        )
        click.echo(f"wrote {mmd_path}")


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
    rep = build_report(data, redact=redact, salt=salt or "")
    if as_json:
        click.echo(json.dumps(rep, indent=2, sort_keys=True))
    else:
        click.echo(format_report(rep, limit=limit), nl=False)


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
    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(
            render_markdown(
                graph, result, limit=limit, max_rows=max_rows, max_chars=max_chars, details_url=details_url
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
    except OSError as exc:
        raise click.ClickException(f"cannot read graph {resolved}: {exc}") from exc
    click.echo(
        f"bizmap mcp {__version__}: serving {resolved} "
        f"({len(store.graph.nodes)} nodes, {len(store.graph.edges)} edges) on stdio",
        err=True,
    )
    serve(store)


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
