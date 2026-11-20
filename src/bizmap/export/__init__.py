"""`bizmap export`: turn a `bizmap.graph.json` into something to share.

Formats:

- ``html`` — one self-contained interactive page (offline, no server).
- ``llm`` — planned for 0.5.1 (compact text overview for an AI assistant),
  built on the same :mod:`bizmap.export.overview`.

The graph is only read; its format is not changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bizmap.export.services import ExportConfigError, assign_services, load_service_split

FORMATS = ("html", "llm")
PLANNED = {"llm": "0.5.1"}


class ExportError(ValueError):
    """The export cannot be produced (bad input or a format not available yet)."""


def export_graph(
    data: dict[str, Any],
    fmt: str = "html",
    *,
    lang: str = "en",
    config: Path | None = None,
    title: str | None = None,
) -> str:
    if fmt in PLANNED:
        raise ExportError(f"--format {fmt} arrives in bizmap {PLANNED[fmt]}; use --format html for now")
    if fmt != "html":
        raise ExportError(f"unknown format {fmt!r} (known: {', '.join(FORMATS)})")
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        raise ExportError("not a BizMap graph (expected an object with `nodes` and `edges` lists)")
    try:
        svc_of = assign_services(data, load_service_split(config))
    except ExportConfigError as exc:
        raise ExportError(str(exc)) from exc
    from bizmap.export.html import render_html

    return render_html(data, svc_of, lang=lang, title=title)


__all__ = ["FORMATS", "ExportError", "export_graph"]
