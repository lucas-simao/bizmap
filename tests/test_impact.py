"""`bizmap impact`: a diff mapped onto the graph (the PR-comment Action)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.impact import MARKER, compute_impact, parse_unified_diff, render_markdown
from bizmap.scan import scan_path

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = ROOT / "examples" / "checkout"
PREFIX = "examples/checkout"


def _diff(path: str, line: int, old: str = "old", new: str = "new") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -{line},1 +{line},1 @@\n-{old}\n+{new}\n"
    )


def test_parse_unified_diff_lines() -> None:
    text = (
        "diff --git a/x.go b/x.go\n--- a/x.go\n+++ b/x.go\n"
        "@@ -10,4 +10,5 @@ func A() {\n"
        " ctx\n"
        "+added\n"
        " ctx\n"
        "-gone\n"
        " ctx\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-a\n-b\n"
        "diff --git a/new.ts b/new.ts\n--- /dev/null\n+++ b/new.ts\n"
        "@@ -0,0 +1,2 @@\n+a\n+b\n"
    )
    got = parse_unified_diff(text)
    # +added is line 11; the deletion sits between new lines 12 and 13
    assert got == {"x.go": {11, 12, 13}, "new.ts": {1, 2}}


def test_functions_carry_end_lines_and_repo_roots() -> None:
    graph = scan_path(CHECKOUT)
    assert set(graph.attrs["repo_roots"].values()) == {"web", "api", "billing"}
    langs = {}
    for node in graph.nodes.values():
        if node.type != "function":
            continue
        lang = node.attrs.get("language")
        end = node.attrs.get("end_line")
        start = (node.attrs.get("evidence") or {}).get("line")
        if isinstance(end, int):
            assert end >= start, node.id
            langs[lang] = langs.get(lang, 0) + 1
    assert {"go", "python", "javascript"} <= set(langs), langs
    data = graph.to_dict()
    assert data["repo_roots"] == dict(sorted(graph.attrs["repo_roots"].items()))


def test_go_client_change_reaches_route_and_billing_table() -> None:
    graph = scan_path(CHECKOUT)
    impact = compute_impact(graph, _diff(f"{PREFIX}/api/billing.go", 27), prefix=PREFIX)
    names = [f.function for f in impact.functions]
    assert len(names) == 1 and names[0].endswith("CreateInvoice"), names
    fi = impact.functions[0]
    assert any(e.endswith("entrypoint:http:POST:/api/orders") for e in fi.entrypoints), fi.entrypoints
    assert any("invoices" in t for t in fi.writes), fi.writes
    assert impact.unmapped_files == []


def test_python_and_ts_changes() -> None:
    graph = scan_path(CHECKOUT)
    diff = _diff(f"{PREFIX}/billing/app.py", 35) + _diff(f"{PREFIX}/web/src/checkout.ts", 12)
    impact = compute_impact(graph, diff, prefix=PREFIX)
    by_name = {f.function.rsplit(".", 1)[-1]: f for f in impact.functions}
    assert set(by_name) == {"payment_webhook", "placeOrder"}, list(by_name)
    hook = by_name["payment_webhook"]
    assert any(e.endswith("entrypoint:http:POST:/webhooks/payment") for e in hook.entrypoints)
    assert any("invoices" in t for t in hook.writes), hook.writes
    # the webhook calls the api, whose handler writes orders
    assert any("orders" in t for t in hook.writes), hook.writes
    place = by_name["placeOrder"]
    assert any("orders" in t for t in place.writes), place.writes


def test_lines_outside_functions_and_unknown_files() -> None:
    graph = scan_path(CHECKOUT)
    diff = _diff(f"{PREFIX}/api/billing.go", 4) + _diff("docs/readme.md", 1)
    impact = compute_impact(graph, diff, prefix=PREFIX)
    assert impact.functions == []
    assert impact.unmapped_files == ["docs/readme.md"]
    md = render_markdown(graph, impact)
    assert md.startswith(MARKER) and "No mapped function" in md


def test_markdown_and_cli(tmp_path: Path) -> None:
    graph = scan_path(CHECKOUT)
    gpath = tmp_path / "g.json"
    gpath.write_text(json.dumps(graph.to_dict()), encoding="utf-8")
    dpath = tmp_path / "d.patch"
    dpath.write_text(_diff(f"{PREFIX}/api/billing.go", 27), encoding="utf-8")
    res = CliRunner().invoke(main, ["impact", "--graph", str(gpath), "--diff", str(dpath), "--prefix", PREFIX])
    assert res.exit_code == 0, res.output
    assert res.output.startswith(MARKER)
    assert "```mermaid" in res.output and "CreateInvoice" in res.output
    assert "POST /api/orders" in res.output
    assert "invoices (amount_cents, id, order_id, status)" in res.output
    res = CliRunner().invoke(
        main, ["impact", "--graph", str(gpath), "--diff", "-", "--prefix", PREFIX, "--json"],
        input=dpath.read_text(encoding="utf-8"),
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert len(data["functions"]) == 1 and data["writes"]
