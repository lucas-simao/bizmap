from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from bizmap import __version__
from bizmap.graph import Graph
from bizmap.mermaid import DEFAULT_LIMIT, render_mermaid
from bizmap.registry import graph_from_json, merge_graphs
from bizmap.scan import scan_path, scan_paths
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
    help="Also write a Mermaid flowchart next to the JSON (entrypoint→table rollup).",
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
    payload = merged.to_dict()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    click.echo(
        f"wrote {out_path}  ({len(payload['nodes'])} nodes, {len(payload['edges'])} edges)"
    )
