"""Every traced HTTP call ends somewhere: linked, or `unresolved_http` with a reason.

Guards against links disappearing silently between versions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bizmap.scan import scan_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["shop", "clinic", "pyplatform", "checkout"])
def test_every_traced_http_call_is_accounted_for(name: str) -> None:
    graph = scan_path(ROOT / "examples" / name)
    linked = {e.from_id for e in graph.edges.values() if e.type == "calls_http"}
    missing = []
    for node in graph.nodes.values():
        for item in node.attrs.get("http_calls") or []:
            url = item.get("url")
            notes = {n.get("url") for n in node.attrs.get("unresolved_http") or [] if isinstance(n, dict)}
            if node.id not in linked and url not in notes:
                missing.append((node.id, item.get("method"), url))
    assert missing == [], missing
