"""`bizmap export`: turn a `bizmap.graph.json` into something to share.

Formats:

- ``html`` — one self-contained interactive page (offline, no server).
- ``llm`` — planned (compact text overview for an AI assistant),
  built on the same :mod:`bizmap.export.overview`.

The graph is only read; its format is not changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bizmap.export.services import ExportConfigError, assign_services, load_export_config

FORMATS = ("html", "llm")
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
) -> str:
    """`openapi`: spec files, `path` or `service=path`; with none given, specs
    are looked up under `root` (`<root>/<service>/docs/swagger.json`…)."""
    if fmt in PLANNED:
        raise ExportError(f"--format {fmt} is not available yet (planned for {PLANNED[fmt]}); use --format html")
    if fmt != "html":
        raise ExportError(f"unknown format {fmt!r} (known: {', '.join(FORMATS)})")
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        raise ExportError("not a BizMap graph (expected an object with `nodes` and `edges` lists)")
    try:
        cfg = load_export_config(config)
        svc_of = assign_services(data, cfg["services"])
    except ExportConfigError as exc:
        raise ExportError(str(exc)) from exc
    from bizmap.export.html import render_html
    from bizmap.export.openapi import SpecError, find_specs, load_spec

    wanted: list[tuple[str | None, Path]] = []
    base = config.parent if config is not None else Path(".")
    for item in openapi or []:
        svc, sep, path = item.partition("=")
        wanted.append((svc, Path(path)) if sep else (None, Path(item)))
    for svc, path in (cfg.get("openapi") or {}).items():
        wanted.append((svc or None, base / path))
    if not wanted and root is not None:
        names = sorted({s for s in svc_of.values() if s})
        wanted = [(svc or None, path) for svc, path in sorted(find_specs(root, names).items())]
    specs = []
    for svc, path in wanted:
        try:
            specs.append((svc, load_spec(path)))
        except (OSError, SpecError) as exc:
            raise ExportError(str(exc)) from exc
    return render_html(
        data, svc_of, lang=lang, title=title, domain_rules=cfg["domains"], domains_by=cfg["domains_by"],
        specs=specs,
    )


__all__ = ["FORMATS", "ExportError", "export_graph"]
