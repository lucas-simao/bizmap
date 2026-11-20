from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main
from bizmap.graph import Graph, column_id, function_id, table_id
from bizmap.scan import scan_path


def _edge(graph: Graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def test_scan_shop_example_graph(example_root: Path) -> None:
    graph = scan_path(example_root)

    assert "table:orders" in graph.nodes
    assert column_id("orders", "status") in graph.nodes
    assert "db_trigger:trg_orders_audit" in graph.nodes
    assert "db_trigger:trg_order_items_inventory" in graph.nodes

    # Echo routes with group prefix
    assert "entrypoint:http:POST:/api/orders" in graph.nodes
    assert "entrypoint:http:GET:/api/orders/:id" in graph.nodes
    assert "entrypoint:http:POST:/api/users" in graph.nodes

    # Ticker job
    job_nodes = [n for n in graph.nodes.values() if n.type == "entrypoint" and n.attrs.get("kind") == "scheduled_job"]
    assert job_nodes, "expected a scheduled_job entrypoint from go expire.Run + NewTicker"
    job_id = job_nodes[0].id

    insert = function_id("repository.Repo.InsertOrder")
    assert insert in graph.nodes

    write_status = _edge(graph, "writes", insert, column_id("orders", "status"))
    assert write_status is not None, graph.to_dict()["edges"]
    assert write_status.confidence.value == "exact"

    # handler -> service -> repo call chain
    handler = function_id("handlers.Handler.CreateOrder")
    service = function_id("service.Service.CreateOrder")
    assert _edge(graph, "calls", handler, service) is not None
    assert _edge(graph, "calls", service, insert) is not None

    # inferred write on handler / HTTP entrypoint is table-level (no column explosion)
    ep = "entrypoint:http:POST:/api/orders"
    assert _edge(graph, "writes", handler, table_id("orders")) is not None
    inferred_ep = _edge(graph, "writes", ep, table_id("orders"))
    assert inferred_ep is not None
    assert inferred_ep.confidence.value == "inferred"

    assert _edge(graph, "triggers", ep, handler) is not None

    # starting the goroutine from main is not a synchronous writer
    assert _edge(graph, "writes", function_id("cmd/shop.main"), column_id("orders", "status")) is None

    # job writes orders.status
    expire_fn = function_id("repository.Repo.ExpirePendingOrders")
    assert _edge(graph, "writes", expire_fn, column_id("orders", "status")) is not None
    assert _edge(graph, "writes", job_id, table_id("orders")) is not None

    # const concatenation SELECT
    list_users = function_id("repository.Repo.ListActiveUsers")
    assert _edge(graph, "reads", list_users, column_id("users", "email")) is not None

    # dynamic SQL marked unknown
    dyn = function_id("repository.Repo.UpdateUserField")
    dyn_edges = [
        e
        for e in graph.edges.values()
        if e.from_id == dyn and e.type == "writes" and "users" in e.to_id
    ]
    assert dyn_edges
    assert any(e.confidence.value == "unknown" for e in dyn_edges)

    # trigger writes audit_log; CreateOrder inferred via trigger
    log_fn = function_id("sql.log_order_change")
    assert any(
        e.from_id == log_fn and e.type == "writes" and "audit_log" in e.to_id
        for e in graph.edges.values()
    )
    assert any(
        e.from_id == insert and e.type == "writes" and "audit_log" in e.to_id
        and e.confidence.value == "inferred"
        for e in graph.edges.values()
    )

    # Python FastAPI worker reads inventory
    worker = function_id("python.worker.low_stock")
    assert worker in graph.nodes
    assert _edge(graph, "reads", worker, column_id("inventory", "sku")) is not None
    assert "entrypoint:http:GET:/reports/low-stock" in graph.nodes

    # override why
    qty = graph.get(column_id("inventory", "qty"))
    assert qty is not None
    assert "On-hand units" in (qty.attrs.get("why") or "")

    # comments become why
    create = graph.get(handler)
    assert create is not None
    assert "paid checkout" in (create.attrs.get("why") or "")


def test_cli_scan_and_why(example_root: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    out = tmp_path / "bizmap.graph.json"
    result = runner.invoke(
        main,
        ["scan", str(example_root), "--out", str(out), "--mermaid"],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert out.with_suffix(".mmd").is_file()
    mmd = out.with_suffix(".mmd").read_text(encoding="utf-8")
    assert "flowchart LR" in mmd
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["version"] == "0.4.1"
    assert data["nodes"]
    assert data["schema"]["tables"]["orders"]

    why = runner.invoke(main, ["why", "orders.status", "--graph", str(out)])
    assert why.exit_code == 0, why.output
    text = why.output
    assert "column: orders.status" in text
    assert "writers:" in text
    assert "InsertOrder" in text
    assert "POST /api/orders" in text
    assert "Expire" in text or "scheduled job" in text
    assert "readers:" in text

    missing = runner.invoke(main, ["why", "nope.missing", "--graph", str(out)])
    assert missing.exit_code == 1
