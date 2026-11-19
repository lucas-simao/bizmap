from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from bizmap import __version__
from bizmap.graph import Graph
from bizmap.mermaid import render_mermaid
from bizmap.scan import scan_path
from bizmap.why import format_why


@click.group()
@click.version_option(__version__, prog_name="bizmap")
def main() -> None:
    """BizMap — deterministic business-rule and data-lineage mapper.

    No AI, no tokens, no accounts. SQL, routes, jobs, and comments only.
    """


@main.command("scan")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
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
    help="Also write a Mermaid flowchart next to the JSON (same stem, .mmd).",
)
@click.option(
    "--rules",
    "rules_dirs",
    type=click.Path(exists=True, path_type=Path),
    multiple=True,
    help="Extra YAML detector-rule files or directories (repeatable).",
)
def scan_cmd(path: Path, out_path: Path, mermaid_flag: bool, rules_dirs: tuple[Path, ...]) -> None:
    """Scan a codebase and write bizmap.graph.json."""
    graph = scan_path(path, extra_rules=list(rules_dirs) or None)
    payload = graph.to_dict()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    click.echo(
        f"wrote {out_path}  ({len(payload['nodes'])} nodes, {len(payload['edges'])} edges)"
    )
    if mermaid_flag:
        mmd_path = out_path.with_suffix(".mmd")
        mmd_path.write_text(render_mermaid(graph), encoding="utf-8")
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
