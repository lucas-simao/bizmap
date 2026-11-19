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


SECRETS = (
    "secretrepo",
    "secret_table",
    "secret_audit",
    "secret_trigger_name",
    "secret_fn",
    "secret-route",
    "secret_handler",
    "secret_job",
    "SECRET_SVC_URL",
    "secret_app.py",
)


def _secret_repo(tmp_path: Path) -> Path:
    root = tmp_path / "secretrepo"
    root.mkdir()
    (root / "bizmap.yaml").write_text("name: secretrepo\n", encoding="utf-8")
    (root / "schema.sql").write_text(
        """
CREATE TABLE secret_table (id text, v text);
CREATE TABLE secret_audit (id text);
CREATE FUNCTION secret_fn() RETURNS trigger AS $$
BEGIN
  INSERT INTO secret_audit (id) VALUES (NEW.id);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER secret_trigger_name AFTER INSERT ON secret_table
  FOR EACH ROW EXECUTE FUNCTION secret_fn();
""",
        encoding="utf-8",
    )
    (root / "secret_app.py").write_text(
        '''
import os

import httpx
from fastapi import FastAPI
from sqlalchemy import text

app = FastAPI()


@app.post("/secret-route")
def secret_handler(conn):
    conn.execute(text("INSERT INTO secret_table (id, v) VALUES ('a', 'b')"))
    httpx.post(os.environ["SECRET_SVC_URL"] + "/secret-route/sync")
''',
        encoding="utf-8",
    )
    return root


def test_redact_hides_every_code_and_schema_identifier(tmp_path: Path) -> None:
    root = _secret_repo(tmp_path)
    runner = CliRunner()
    plain = runner.invoke(main, ["report", str(root)])
    assert plain.exit_code == 0, plain.output
    # the fixture really exercises triggers, routes and unresolved env keys
    assert "secret_trigger_name" in plain.output, plain.output
    assert "secret-route" in plain.output, plain.output
    for args in (["--redact", "--salt", "s"], ["--redact", "--salt", "s", "--json"]):
        out = runner.invoke(main, ["report", str(root), *args])
        assert out.exit_code == 0, out.output
        leaked = [name for name in SECRETS if name.lower() in out.output.lower()]
        assert leaked == [], (leaked, out.output)


def test_salt_file_makes_redacted_runs_comparable(tmp_path: Path) -> None:
    root = _secret_repo(tmp_path)
    salt_file = tmp_path / "private" / "salt"
    runner = CliRunner()
    first = runner.invoke(main, ["report", str(root), "--redact", "--json", "--salt-file", str(salt_file)])
    assert first.exit_code == 0, first.output
    assert salt_file.exists() and len(salt_file.read_text().strip()) >= 32
    second = runner.invoke(main, ["report", str(root), "--redact", "--json", "--salt-file", str(salt_file)])
    assert second.exit_code == 0, second.output

    def _json(text: str) -> dict:
        return json.loads(text[text.index("{") :])

    assert _json(first.output) == _json(second.output)
