"""`bizmap export`: turn a `bizmap.graph.json` into something to share.

Formats:

- ``html`` — one self-contained interactive page (offline, no server).
- ``mermaid`` — Markdown with the architecture and every event flow (topics,
  Firestore triggers, scheduled jobs) as ``flowchart LR`` blocks, for GitHub
  (:mod:`bizmap.views`).
- ``llm`` — planned (compact text overview for an AI assistant),
  built on the same :mod:`bizmap.export.overview`.

The graph is only read; its format is not changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bizmap.export.services import ExportConfigError, assign_services, load_export_config

FORMATS = ("html", "mermaid", "llm")
PLANNED = {"llm": "a later release"}


class ExportError(ValueError):
    """The export cannot be produced (bad input or a format not available yet)."""


def export_graph(
    data: dict[str, Any],
    fmt: str = "html",
    *,
    lang: str = "en",
    config: Path | None = None,
    title: str | None = None,
    openapi: list[str] | None = None,
    root: Path | None = None,
    notes: list[str] | None = None,
) -> str:
    """`openapi`: spec files, `path` or `service=path`; with none given, specs
    are looked up under `root` (`<root>/<service>/docs/swagger.json`…) and a
    bad one found that way is skipped. Messages for the user go to `notes`."""
    if fmt in PLANNED:
        raise ExportError(f"--format {fmt} is not available yet (planned for {PLANNED[fmt]}); use --format html")
    if fmt not in FORMATS:
        raise ExportError(f"unknown format {fmt!r} (known: {', '.join(FORMATS)})")
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        raise ExportError("not a BizMap graph (expected an object with `nodes` and `edges` lists)")
    try:
        cfg = load_export_config(config)
        svc_of = assign_services(data, cfg["services"])
    except ExportConfigError as exc:
        raise ExportError(str(exc)) from exc
    if fmt == "mermaid":
        from bizmap.views import context_from_data, mermaid_markdown

        return mermaid_markdown(context_from_data(data, svc_of))
    from bizmap.export.html import render_html
    from bizmap.export.openapi import SpecError, find_specs, load_spec

    services = sorted({s for s in svc_of.values() if s})
    explicit: list[tuple[str | None, Path]] = []
    base = config.parent if config is not None else Path(".")
    for item in openapi or []:
        svc, sep, path = item.partition("=")
        explicit.append((svc, Path(path)) if sep else (None, Path(item)))
    for svc, path in (cfg.get("openapi") or {}).items():
        explicit.append((svc or None, base / path))
    for svc, path in explicit:
        if svc and svc not in services:
            raise ExportError(f"--openapi {svc}={path}: no service {svc!r} in the graph (services: {', '.join(services)})")
    specs: list[tuple[str | None, dict[str, Any]]] = []
    used: list[str] = []
    for svc, path in explicit:  # given by the user: a bad file is an error
        try:
            specs.append((svc, load_spec(path)))
        except (OSError, SpecError) as exc:
            raise ExportError(str(exc)) from exc
        used.append(f"{svc + ': ' if svc else ''}{path}")
    if not explicit and root is not None:  # found next to the code: a bad file is skipped
        repo_dirs = data.get("repo_roots") if isinstance(data.get("repo_roots"), dict) else None
        for svc, path in find_specs(root, services, repo_dirs):
            try:
                specs.append((svc or None, load_spec(path)))
            except (OSError, SpecError) as exc:
                _note(notes, f"API docs: skipping {path}: {exc}")
                continue
            used.append(f"{svc + ': ' if svc else ''}{path}")
    if used:
        _note(notes, f"API docs: {len(used)} spec{'s' if len(used) != 1 else ''} ({', '.join(used)})")
    return render_html(
        data, svc_of, lang=lang, title=title, domain_rules=cfg["domains"], domains_by=cfg["domains_by"],
        specs=specs,
    )


def _note(notes: list[str] | None, text: str) -> None:
    if notes is not None:
        notes.append(text)


__all__ = ["FORMATS", "ExportError", "export_graph"]
