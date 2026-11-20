"""The PR comment posted by the GitHub Action (`bizmap impact` Markdown).

The graphs here are built by hand, so these snapshots only move when the
*render* changes, never when a detector does. Refresh with::

    BIZMAP_UPDATE_SNAPSHOTS=1 pytest tests/test_action_comment.py

and review the diff of ``tests/snapshots/comment_*.md`` in the PR.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from bizmap import __version__
from bizmap.graph import Evidence, Graph
from bizmap.impact import (
    MARKER,
    FunctionImpact,
    Impact,
    SchemaChange,
    compute_impact,
    render_markdown,
    render_mermaid,
)

SNAP_DIR = Path(__file__).resolve().parent / "snapshots"
BLOB = "https://github.com/acme/shop/blob/0123456789abcdef0123456789abcdef01234567"
RUN = "https://github.com/acme/shop/actions/runs/42"


def _fn(g: Graph, repo: str, qual: str, file: str, start: int, end: int, **attrs) -> str:
    nid = f"{repo}/function:{qual}" if repo else f"function:{qual}"
    g.add_node(
        nid, "function", name=qual, repo=repo or None, start_line=start, end_line=end,
        evidence={"file": file, "line": start, "symbol": qual}, **attrs,
    )
    return nid


def _ep(g: Graph, repo: str, kind: str, key: str, file: str, line: int, **attrs) -> str:
    nid = f"{repo}/entrypoint:{kind}:{key}" if repo else f"entrypoint:{kind}:{key}"
    g.add_node(nid, "entrypoint", kind=kind, repo=repo or None, evidence={"file": file, "line": line}, **attrs)
    return nid


def _col(g: Graph, table: str, col: str) -> str:
    g.add_node(f"table:{table}", "table", name=table)
    g.add_node(f"column:{table}.{col}", "column", table=table, name=col)
    return f"column:{table}.{col}"


def _ev(file: str, line: int) -> Evidence:
    return Evidence(file=file, line=line, symbol="")


def checkout_graph() -> Graph:
    """web (TS) → api (Go) → billing (Python), plus a worker job; one database."""
    g = Graph()
    g.repo = "checkout"
    g.attrs["repo_roots"] = {"web": "web", "api": "api", "billing": "billing", "worker": "worker"}
    g.attrs["repo_roots_base"] = "git"
    submit = _fn(g, "web", "src.checkout.submitOrder", "src/checkout.ts", 10, 22)
    create_ep = _ep(g, "api", "http", "POST:/api/orders", "cmd/server.go", 31, method="POST", path="/api/orders")
    cancel_ep = _ep(g, "api", "http", "POST:/api/orders/:id/cancel", "cmd/server.go", 32, method="POST", path="/api/orders/:id/cancel")
    create = _fn(g, "api", "handlers.Orders.Create", "handlers/orders.go", 20, 48,
                 unresolved_http=[{"method": "POST", "url": "${FRAUD_URL}/score", "reason": "target_not_scanned"}])
    cancel = _fn(g, "api", "handlers.Orders.Cancel", "handlers/orders.go", 50, 70)
    insert = _fn(g, "api", "repository.Repo.InsertOrder", "repository/orders.go", 10, 30)
    update = _fn(g, "api", "repository.Repo.SetStatus", "repository/orders.go", 32, 44)
    charge_ep = _ep(g, "billing", "http", "POST:/charges", "app.py", 12, method="POST", path="/charges")
    charge = _fn(g, "billing", "app.create_charge", "app.py", 30, 52)
    job = _ep(g, "worker", "scheduled_job", "expire_orders", "jobs.py", 5, symbol="expire_orders")
    expire = _fn(g, "worker", "jobs.expire_orders", "jobs.py", 8, 20)
    g.add_node("db_trigger:orders_audit", "db_trigger", name="orders_audit", table="orders", timing="AFTER",
               events=["INSERT", "UPDATE"], function="log_order", evidence={"file": "db/schema.sql", "line": 40})

    g.add_edge("calls_http", submit, create_ep, evidence=_ev("src/checkout.ts", 15))
    g.add_edge("triggers", create_ep, create)
    g.add_edge("triggers", cancel_ep, cancel)
    g.add_edge("calls", create, insert)
    g.add_edge("calls", cancel, update)
    for c in ("status", "total_cents", "customer_id"):
        g.add_edge("writes", insert, _col(g, "orders", c), evidence=_ev("repository/orders.go", 14), ops=["INSERT"])
    g.add_edge("writes", insert, "table:audit_log", evidence=_ev("repository/orders.go", 14), via="db_trigger:orders_audit")
    g.add_node("table:audit_log", "table", name="audit_log")
    g.add_edge("writes", update, _col(g, "orders", "status"), evidence=_ev("repository/orders.go", 35), ops=["UPDATE"])
    g.add_edge("reads", update, _col(g, "orders", "id"), evidence=_ev("repository/orders.go", 35))
    g.add_edge("calls_http", create, charge_ep, confidence="heuristic", via="route_literal", evidence=_ev("handlers/orders.go", 40))
    g.add_edge("triggers", charge_ep, charge)
    g.add_edge("writes", charge, _col(g, "invoices", "amount_cents"), evidence=_ev("app.py", 40))
    g.add_edge("reads", charge, _col(g, "orders", "total_cents"), evidence=_ev("app.py", 35))
    g.add_edge("triggers", job, expire)
    g.add_edge("writes", expire, _col(g, "orders", "status"), evidence=_ev("jobs.py", 12))
    return g


def _diff(*changes: tuple[str, int]) -> str:
    out = []
    for path, line in changes:
        out.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{line},1 +{line},1 @@\n-old\n+new\n")
    return "".join(out)


def _snapshot(name: str, text: str) -> None:
    text = text.replace(f"[BizMap](https://github.com/lucas-simao/bizmap/blob/main/docs/action.md) {__version__}",
                        "[BizMap](https://github.com/lucas-simao/bizmap/blob/main/docs/action.md) X.Y.Z")
    path = SNAP_DIR / f"comment_{name}.md"
    if os.environ.get("BIZMAP_UPDATE_SNAPSHOTS") == "1" or not path.exists():
        path.write_text(text, encoding="utf-8")
        if os.environ.get("BIZMAP_UPDATE_SNAPSHOTS") != "1":
            pytest.fail(f"snapshot {path.name} was missing and has been written; re-run")
    assert text == path.read_text(encoding="utf-8"), f"render changed; refresh {path.name} (see module docstring)"


def checkout_impact(g: Graph) -> Impact:
    diff = _diff(("api/handlers/orders.go", 25), ("api/handlers/orders.go", 60), ("api/repository/orders.go", 15),
                 ("api/README.md", 1))
    return compute_impact(g, diff)


# ── snapshots ────────────────────────────────────────────────────────────────


def test_snapshot_multi_service_high_risk() -> None:
    g = checkout_graph()
    md = render_markdown(g, checkout_impact(g), blob_url=BLOB, details_url=RUN)
    _snapshot("multi_service", md)


def test_snapshot_no_data_impact() -> None:
    g = Graph()
    g.attrs["repo_roots"] = {"shop": "."}
    _fn(g, "", "src.util.slugify", "src/util.ts", 1, 9)
    imp = compute_impact(g, _diff(("src/util.ts", 3), ("docs/notes.md", 2)))
    md = render_markdown(g, imp, blob_url=BLOB, details_url=RUN)
    _snapshot("no_data_impact", md)
    assert len(md.splitlines()) <= 10, md


def test_snapshot_nothing_mapped() -> None:
    g = checkout_graph()
    md = render_markdown(g, compute_impact(g, _diff(("README.md", 1))), details_url=RUN)
    _snapshot("nothing_mapped", md)


def test_snapshot_schema_drop_still_used() -> None:
    g = checkout_graph()
    imp = Impact(
        functions=[],
        unmapped_files=["db/migrations/0042_drop_total.sql"],
        schema_changes=[
            SchemaChange(column="column:orders.total_cents", file="db/migrations/0042_drop_total.sql", line=3,
                         entrypoints=["billing/entrypoint:http:POST:/charges"], functions=["billing/function:app.create_charge"],
                         op="dropped", table="orders", name="total_cents"),
            SchemaChange(column="column:orders.note", file="db/migrations/0042_drop_total.sql", line=4,
                         op="added", table="orders", name="note"),
        ],
    )
    md = render_markdown(g, imp, blob_url=BLOB)
    _snapshot("schema_drop", md)


def test_snapshot_single_repo_medium_without_mermaid() -> None:
    g = Graph()
    g.repo = "shop"
    g.attrs["repo_roots"] = {"shop": "."}
    ep = _ep(g, "", "http", "GET:/orders/:id", "src/server.ts", 12, method="GET", path="/orders/:id")
    paid_ep = _ep(g, "", "http", "POST:/orders/:id/paid", "src/server.ts", 14, method="POST", path="/orders/:id/paid")
    get = _fn(g, "", "src.orders.getOrder", "src/orders.ts", 18, 24)
    paid = _fn(g, "", "src.orders.markPaid", "src/orders.ts", 27, 29)
    g.add_edge("triggers", ep, get)
    g.add_edge("triggers", paid_ep, paid)
    g.add_edge("reads", get, _col(g, "orders", "id"))
    g.add_edge("reads", get, _col(g, "orders", "status"))
    g.add_edge("writes", paid, "table:orders", confidence="unknown")
    imp = compute_impact(g, _diff(("src/orders.ts", 20), ("src/orders.ts", 28)))
    md = render_markdown(g, imp, blob_url=BLOB, mermaid=False)
    _snapshot("single_repo", md)
    assert "```mermaid" not in md


# ── behaviour ────────────────────────────────────────────────────────────────


def _risk(md: str) -> str:
    m = re.search(r"<!-- bizmap-risk: (\w+) -->", md)
    assert m, md
    return m.group(1)


def test_header_marker_and_risk_line() -> None:
    g = checkout_graph()
    md = render_markdown(g, checkout_impact(g))
    lines = md.splitlines()
    assert lines[0] == MARKER and lines[1] == "<!-- bizmap-risk: high -->"
    assert lines[2] == "### BizMap impact · 🔴 High risk"
    # one-line summary right under the header
    assert lines[4].startswith("**2** entrypoints (2 routes) · writes **3** tables · reads **1** table · **5** columns — from **3** changed functions")


def test_shared_table_threshold() -> None:
    g = checkout_graph()
    imp = checkout_impact(g)
    high = render_markdown(g, imp)
    assert "used by 3 services (api, billing, worker)" in high
    medium = render_markdown(g, imp, shared_services=4)
    assert _risk(medium) == "medium" and "used by 3 services" not in medium
    assert "writes `orders`, which has trigger `orders_audit`" in medium
    assert "writes `audit_log`, which has" not in medium  # the trigger sits on orders, not on what it writes
    assert "changes the handler of `api: POST /api/orders`, `api: POST /api/orders/:id/cancel`" in medium


def test_links_point_at_the_commit() -> None:
    g = checkout_graph()
    md = render_markdown(g, checkout_impact(g), blob_url=BLOB + "/")
    assert f"[api/handlers/orders.go:25]({BLOB}/api/handlers/orders.go#L25-L48)" in md
    assert f"[api/cmd/server.go:31]({BLOB}/api/cmd/server.go#L31)" in md
    assert "commit [`0123456`](https://github.com/acme/shop/commit/0123456789abcdef0123456789abcdef01234567)" in md
    plain = render_markdown(g, checkout_impact(g))
    assert "`api/handlers/orders.go:25`" in plain and "](https://" not in plain.split("<sub>[BizMap]")[0]


def test_sections_are_collapsible_and_diagram_open() -> None:
    g = checkout_graph()
    md = render_markdown(g, checkout_impact(g))
    for title in ("Entrypoints", "Data", "Triggers", "Cross-service calls", "Changed functions", "Unresolved"):
        assert f"<summary><b>{title}" in md, title
    assert "<details open>\n<summary><b>Diagram</b>" in md
    assert md.count("<details") == md.count("</details>")
    assert "**api** → **billing**" in md and "heuristic · route literal" in md
    assert "`api: Orders.Create` calls `POST ${FRAUD_URL}/score` — target not scanned" in md


def test_mermaid_is_bounded_and_grouped_by_service() -> None:
    g = checkout_graph()
    imp = checkout_impact(g)
    mm = render_mermaid(g, imp, triggers={"table:orders": ["db_trigger:orders_audit"]})
    assert mm.startswith("flowchart LR\n")
    assert '  subgraph s1["api"]' in mm
    assert re.search(r'n\d+\{\{"⚡ orders_audit"\}\}:::trigger', mm)
    # many entrypoints on one function: capped, the rest folded into "+N more"
    big = Graph()
    f = _fn(big, "", "pkg.hot", "a.py", 1, 5)
    for i in range(12):
        e = _ep(big, "", "http", f"GET:/r{i}", "a.py", 1, method="GET", path=f"/r{i}")
        big.add_edge("triggers", e, f)
    big.add_edge("writes", f, _col(big, "t", "c"))
    mm = render_mermaid(big, compute_impact(big, _diff(("a.py", 2))))
    assert mm.count("(([") == 0 and mm.count('(["GET') == 4 and '(["+8 more"]):::more' in mm
    # node cap across many functions
    many = Graph()
    fns = []
    for i in range(30):
        fid = _fn(many, "", f"pkg.f{i}", "a.py", i * 10 + 1, i * 10 + 9)
        many.add_edge("writes", fid, _col(many, f"t{i}", "c"))
        fns.append(FunctionImpact(function=fid, file="a.py", lines=[i * 10 + 2], writes=[f"column:t{i}.c"]))
    mm = render_mermaid(many, Impact(functions=fns, unmapped_files=[]), limit=20)
    declared = [ln for ln in mm.splitlines() if re.match(r"  n\d+[\[(]", ln)]
    assert len(declared) <= 20 and "more changed function(s) not drawn" in mm


def test_mermaid_labels_are_escaped() -> None:
    g = Graph()
    f = _fn(g, "", 'pkg.say"hi"<b>', "a.py", 1, 3)
    g.add_edge("writes", f, _col(g, "t", "c"))
    mm = render_mermaid(g, compute_impact(g, _diff(("a.py", 2))))
    assert '"' not in mm.split('["', 1)[1].split('"]', 1)[0]
    assert "#quot;" in mm and "#lt;b#gt;" in mm


def test_no_impact_is_short() -> None:
    g = Graph()
    g.attrs["repo_roots"] = {"x": "."}
    _fn(g, "", "pkg.helper", "a.py", 1, 5)
    md = render_markdown(g, compute_impact(g, _diff(("a.py", 2))))
    assert _risk(md) == "none" and "🟢 No data impact" in md and "<details" not in md
    assert "**1** changed function, none reaching a route, job or table." in md


def test_size_cap_keeps_new_sections_bounded() -> None:
    g = checkout_graph()
    imp = checkout_impact(g)
    for cap in (60000, 4000, 1500, 600):
        md = render_markdown(g, imp, max_chars=cap, details_url=RUN)
        assert len(md) <= cap, (cap, len(md))
        assert md.startswith(MARKER) and "### BizMap impact" in md


def test_dynamic_table_is_a_note_not_data() -> None:
    g = Graph()
    g.attrs["repo_roots"] = {"x": "."}
    f = _fn(g, "", "pkg.purge", "a.py", 1, 9, unresolved_calls=["str.replace", "str | None.rstrip", "Store.save"])
    g.add_node("table:unknown", "table", name="unknown", candidates=["logs", "events"])
    g.add_edge("writes", f, "table:unknown", confidence="unknown")
    md = render_markdown(g, compute_impact(g, _diff(("a.py", 2))))
    assert _risk(md) == "none" and "**`unknown`**" not in md and "<details" not in md
    assert "`pkg.purge` builds a table name at runtime (candidates: events, logs)" in md
    assert "`Store.save`" in md and "str.replace" not in md and "rstrip" not in md
