"""`bizmap mcp`: read-only MCP server over the graph (examples/checkout)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bizmap.mcp_server import GraphStore, handle, serve
from bizmap.scan import scan_path

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = ROOT / "examples" / "checkout"
PREFIX = "examples/checkout"


@pytest.fixture(scope="module")
def graph_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mcp") / "bizmap.graph.json"
    path.write_text(json.dumps(scan_path(CHECKOUT).to_dict()), encoding="utf-8")
    return path


@pytest.fixture()
def store(graph_file: Path) -> GraphStore:
    return GraphStore(graph_file)


def call(store: GraphStore, name: str, **args):
    resp = handle(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert resp is not None and "result" in resp, resp
    return resp["result"]


def ok(store: GraphStore, name: str, **args) -> dict:
    result = call(store, name, **args)
    assert result["isError"] is False, result
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


def test_initialize_and_tool_list(store: GraphStore) -> None:
    init = handle(store, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert init["result"]["serverInfo"]["name"] == "bizmap"
    old = handle(store, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert old["result"]["protocolVersion"] == "2025-06-18"
    assert handle(store, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle(store, {"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    tools = handle(store, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["search", "call_graph", "function_data", "table_usage", "impact", "why_column"]
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["openWorldHint"] is False
        assert tool["inputSchema"]["type"] == "object" and tool["inputSchema"]["required"]


def test_protocol_errors(store: GraphStore) -> None:
    assert handle(store, {"jsonrpc": "2.0", "id": 9, "method": "nope"})["error"]["code"] == -32601
    bad = handle(store, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "rm -rf"}})
    assert bad["error"]["code"] == -32602
    assert handle(store, {"id": 1})["error"]["code"] == -32600


def test_search(store: GraphStore) -> None:
    out = ok(store, "search", query="CreateInvoice")
    assert out["results"][0]["id"].endswith("function:main.Billing.CreateInvoice"), out
    assert out["results"][0]["file"] == "billing.go"
    routes = ok(store, "search", query="/orders", kind="route")
    assert routes["results"] and all(r["type"] == "entrypoint" for r in routes["results"])
    tables = ok(store, "search", query="invoices", kind="table")
    assert [r["label"] for r in tables["results"]] == ["invoices"]
    assert ok(store, "search", query="zzz-nothing")["results"] == []


def test_call_graph(store: GraphStore) -> None:
    out = ok(store, "call_graph", function="CreateInvoice")
    callers = {c["label"] for c in out["callers"]}
    assert any("createOrder" in c or "CreateOrder" in c for c in callers), callers
    assert any(e.get("path") == "/api/orders" for e in out["entrypoints"]), out["entrypoints"]
    # across services: the client function calls the billing route over HTTP
    assert any(c["edge"] == "calls_http" and c.get("path") == "/invoices" for c in out["callees"]), out["callees"]
    deep = ok(store, "call_graph", function="CreateInvoice", direction="callees", depth=3)
    assert "callers" not in deep and deep["callees_total"] >= len(out["callees"])


def test_function_data(store: GraphStore) -> None:
    direct = ok(store, "function_data", function="CreateInvoice")
    assert direct["writes"] == [] or all(r["table"] != "invoices" for r in direct["writes"])
    deep = ok(store, "function_data", function="CreateInvoice", transitive=True)
    inv = next(r for r in deep["writes"] if r["table"] == "invoices")
    assert "amount_cents" in inv["columns"] and inv["confidence"] in {"exact", "inferred", "heuristic", "unknown"}
    route = ok(store, "function_data", function="POST /api/orders", transitive=True)
    assert any(r["table"] == "orders" for r in route["writes"]), route


def test_table_usage(store: GraphStore) -> None:
    out = ok(store, "table_usage", table="invoices")
    assert out["target"]["label"] == "invoices" and out["functions"], out
    assert any(e.get("path") == "/invoices" for e in out["entrypoints"]), out["entrypoints"]
    col = ok(store, "table_usage", table="orders.status")
    assert col["target"]["label"] == "orders.status" and col["functions"]


def test_impact_tool(store: GraphStore) -> None:
    diff = (
        f"diff --git a/{PREFIX}/api/billing.go b/{PREFIX}/api/billing.go\n"
        f"--- a/{PREFIX}/api/billing.go\n+++ b/{PREFIX}/api/billing.go\n@@ -27,0 +27,1 @@\n+x\n"
    )
    out = ok(store, "impact", diff=diff, markdown=True)
    assert [f["function"].rsplit(".", 1)[-1] for f in out["functions"]] == ["CreateInvoice"]
    assert out["markdown"].startswith("<!-- bizmap-impact -->")


def test_why_column(store: GraphStore) -> None:
    out = ok(store, "why_column", column="orders.status")
    assert out["text"].startswith("column: orders.status"), out["text"]


def test_tool_errors_are_results(store: GraphStore) -> None:
    for name, args in (
        ("search", {}),
        ("call_graph", {"function": "doesNotExist"}),
        ("call_graph", {"function": "CreateInvoice", "depth": 99}),
        ("table_usage", {"table": "nope"}),
        ("impact", {"diff": ""}),
    ):
        result = call(store, name, **args)
        assert result["isError"] is True and result["content"][0]["type"] == "text", (name, result)


def test_deterministic(store: GraphStore) -> None:
    a = call(store, "table_usage", table="orders")
    b = call(store, "table_usage", table="orders")
    assert a == b


def test_reloads_when_the_graph_changes(tmp_path: Path, graph_file: Path) -> None:
    path = tmp_path / "g.json"
    path.write_text(graph_file.read_text(encoding="utf-8"), encoding="utf-8")
    store = GraphStore(path)
    assert ok(store, "search", query="CreateInvoice")["results"]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["nodes"] = [n for n in data["nodes"] if "CreateInvoice" not in n["id"]]
    data["edges"] = [e for e in data["edges"] if "CreateInvoice" not in e["from"] + e["to"]]
    path.write_text(json.dumps(data) + " ", encoding="utf-8")  # size changes
    assert ok(store, "search", query="CreateInvoice")["results"] == []


def test_serve_stdio_loop(store: GraphStore) -> None:
    import io

    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + "not json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "invoices"}}}) + "\n"
    )
    stdout = io.StringIO()
    serve(store, stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700


def test_cli_subprocess(graph_file: Path) -> None:
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "why_column", "arguments": {"column": "orders.status"}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-c", "from bizmap.cli import main; main()", "mcp", "--graph", str(graph_file)],
        input="".join(json.dumps(m) + "\n" for m in msgs),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[2]["result"]["isError"] is False
    assert "serving" in proc.stderr
