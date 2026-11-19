"""`bizmap report` — coverage numbers, with and without redaction."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.report import build_report, format_report

ROOT = Path(__file__).resolve().parents[1]

GRAPH = {
    "version": "test",
    "repos": ["web", "api"],
    "nodes": [
        {"id": "api/entrypoint:http:GET:/orders", "type": "entrypoint", "kind": "http", "method": "GET", "path": "/orders"},
        {"id": "api/entrypoint:http:GET:/health", "type": "entrypoint", "kind": "http", "method": "GET", "path": "/health"},
        {"id": "api/entrypoint:scheduled_job:jobs.Expire", "type": "entrypoint", "kind": "scheduled_job", "symbol": "jobs.Expire", "dynamic_table": True},
        {"id": "web/function:src.orders.load", "type": "function", "unresolved_calls": ["svc.run"]},
        {"id": "table:orders", "type": "table", "name": "orders"},
    ],
    "edges": [
        {"id": "a", "type": "reads", "from": "api/entrypoint:http:GET:/orders", "to": "column:orders.status", "confidence": "inferred"},
        {"id": "b", "type": "calls_http", "from": "web/function:src.orders.load", "to": "api/entrypoint:http:GET:/orders", "confidence": "inferred", "via": "route_literal"},
        {"id": "c", "type": "reads", "from": "web/function:src.orders.load", "to": "table:orders", "confidence": "inferred", "via": "sql_reference"},
    ],
    "unresolved_http": [
        {"url": "{BILLING_URL}/invoices", "method": "POST", "keys": ["BILLING_URL"], "from": "api/function:x", "reason": "target_not_scanned"},
        {"url": "{base}/x/{y}", "method": "GET", "keys": [], "from": "api/function:y", "reason": "path_dynamic"},
    ],
}


def test_build_report_counts() -> None:
    rep = build_report(GRAPH)
    ep = rep["entrypoints"]
    assert ep["total"] == 3
    assert ep["reaching_data"] == 1
    assert ep["by_kind"]["http"] == {"total": 2, "reaching_data": 1}
    labels = {row["entrypoint"] for row in ep["without_data"]}
    assert labels == {"GET /health", "scheduled_job jobs.Expire"}
    assert rep["http"]["calls_http"] == 1
    assert rep["http"]["cross_repo"] == 1
    assert rep["http"]["by_via"] == {"route_literal": 1}
    assert rep["http"]["unresolved_by_reason"] == {"path_dynamic": 1, "target_not_scanned": 1}
    assert rep["http"]["unresolved_top_keys"] == [{"key": "BILLING_URL", "count": 1}]
    assert rep["data_links_by_via"] == {"sql_reference": 1}
    assert rep["unresolved_calls"] == 1
    text = format_report(rep)
    assert "reaching a table/collection: 1 (33.3%)" in text


def test_redacted_report_hides_names_and_is_salt_stable() -> None:
    a = build_report(GRAPH, redact=True, salt="s1")
    b = build_report(GRAPH, redact=True, salt="s1")
    c = build_report(GRAPH, redact=True, salt="s2")
    assert a == b
    assert a != c
    blob = json.dumps(a)
    for secret in ("/health", "jobs.Expire", "BILLING_URL", "web", "api", "orders"):
        assert secret not in blob, secret
    assert a["entrypoints"]["total"] == 3
    assert a["http"]["unresolved_by_reason"] == {"path_dynamic": 1, "target_not_scanned": 1}


def test_report_cli_scans_paths_in_memory(tmp_path: Path) -> None:
    runner = CliRunner()
    shop = str(ROOT / "examples" / "shop")
    result = runner.invoke(main, ["report", shop])
    assert result.exit_code == 0, result.output
    assert "entrypoints:" in result.output
    assert "POST /api/orders" not in result.output or "without data" in result.output
    redacted = runner.invoke(main, ["report", shop, "--redact", "--salt", "x", "--json"])
    assert redacted.exit_code == 0, redacted.output
    data = json.loads(redacted.output)
    assert data["redacted"] is True
    assert "/api/orders" not in redacted.output
    assert not (tmp_path / "bizmap.graph.json").exists()
