# TEMP debug, removed before review
from pathlib import Path

import bizmap.detect.js_lang as jl
from bizmap.scan import scan_path
import sys; sys.path.insert(0, str(Path(__file__).parent))
from test_web_api_links_r45 import DELIVERY, _workspace


def test_debug(tmp_path: Path, monkeypatch) -> None:
    orig = jl._collect_wrappers
    out = []

    def spy(tree, consts, clients):
        w = orig(tree, consts, clients)
        out.append((sorted(clients.items()), {k: v for k, v in w.items()}))
        return w

    monkeypatch.setattr(jl, "_collect_wrappers", spy)
    _workspace(tmp_path, DELIVERY)
    graph = scan_path(tmp_path)
    calls = [(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http"]
    fns = [n.id for n in graph.nodes.values() if "delivery" in n.id]
    raise AssertionError(repr((out, graph.attrs.get("unresolved_http"), calls, fns)))
