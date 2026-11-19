from __future__ import annotations

from bizmap.detect.rules import load_rules


def test_bundled_rules_include_echo_pgx_and_stubs() -> None:
    rules = {r.id: r for r in load_rules()}
    assert "echo.v4.routes" in rules
    assert "pgx.v5.sql" in rules
    assert "go.ticker.job" in rules
    assert "fastapi.routes" in rules
    assert "sqlalchemy.sql" in rules
    assert "express.routes" in rules
    assert not rules["express.routes"].is_stub
    assert "firebase.functions" in rules
    assert "flask.routes" in rules
    assert "python.queues" in rules
    assert not rules["echo.v4.routes"].is_stub
    assert rules["echo.v4.routes"].language == "go"
    assert rules["fastapi.routes"].language == "python"
    assert rules["express.routes"].language == "javascript"
