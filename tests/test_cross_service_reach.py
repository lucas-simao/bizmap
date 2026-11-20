"""Round 40: reach across services. edge → gateway → orders (UPDATE orders):
the report credits edge and gateway (`reaching_data_via_http`, hops), and
`why` shows the whole HTTP chain."""

from __future__ import annotations

from pathlib import Path

from bizmap.report import build_report, format_report
from bizmap.scan import scan_path
from bizmap.why import format_why

ROOT_YAML = """repos: [edge, gateway, orders, lonely]
database: shop
services:
  gateway: { url: http://gateway.internal, env: GATEWAY_URL }
  orders: { url: http://orders.internal, env: ORDERS_URL }
  lonely: { url: http://lonely.internal, env: LONELY_URL }
"""

EDGE = '''import os

import httpx
from fastapi import FastAPI

app = FastAPI()
GATEWAY_URL = os.environ["GATEWAY_URL"]
LONELY_URL = os.environ["LONELY_URL"]


@app.post("/v2/orders/{order_id}/ship")
def ship(order_id: str):
    """Ship an order (public API)."""
    httpx.post(f"{GATEWAY_URL}/api/orders/{order_id}/ship")
    return {"ok": True}


@app.get("/v2/ping")
def ping():
    httpx.get(f"{LONELY_URL}/health")
    return {"ok": True}
'''

GATEWAY = '''import os

import httpx
from fastapi import FastAPI

app = FastAPI()
ORDERS_URL = os.environ["ORDERS_URL"]


@app.post("/api/orders/{order_id}/ship")
def ship(order_id: str):
    httpx.post(f"{ORDERS_URL}/internal/orders/{order_id}/ship")
    return {"ok": True}
'''

ORDERS = '''import os

from fastapi import FastAPI
from sqlalchemy import create_engine, text

app = FastAPI()
engine = create_engine(os.environ["DATABASE_URL"])


@app.post("/internal/orders/{order_id}/ship")
def ship(order_id: str):
    with engine.begin() as conn:
        conn.execute(text("UPDATE orders SET status = 'shipped' WHERE id = :id"), {"id": order_id})
    return {"ok": True}
'''

LONELY = '''from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}
'''


def _w(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _workspace(root: Path) -> None:
    _w(root, "bizmap.yaml", ROOT_YAML)
    _w(root, "edge/app.py", EDGE)
    _w(root, "gateway/app.py", GATEWAY)
    _w(root, "orders/app.py", ORDERS)
    _w(root, "orders/schema.sql", "CREATE TABLE orders (id TEXT PRIMARY KEY, status TEXT);\n")
    _w(root, "lonely/app.py", LONELY)
    for name in ("edge", "gateway", "orders", "lonely"):
        _w(root, f"{name}/bizmap.yaml", f"name: {name}\ndatabase: shop\n")


def _rows(rep: dict) -> dict[str, dict]:
    return {row["entrypoint"]: row for row in rep["entrypoints"]["without_data"]}


def test_report_follows_calls_http_across_services(tmp_path: Path) -> None:
    _workspace(tmp_path)
    data = scan_path(tmp_path).to_dict()
    assert len([e for e in data["edges"] if e["type"] == "calls_http"]) >= 3, data.get("unresolved_http")
    rep = build_report(data)
    rows = _rows(rep)
    edge = next(v for k, v in rows.items() if "/v2/orders" in k)
    gateway = next(v for k, v in rows.items() if "/api/orders" in k)
    ping = next(v for k, v in rows.items() if "/v2/ping" in k)
    assert (edge["reaches_data_via_http"], edge["http_hops"]) == (True, 2)
    assert (gateway["reaches_data_via_http"], gateway["http_hops"]) == (True, 1)
    assert ping["reaches_data_via_http"] is False and ping["http_stop"] == "target_without_data"
    assert rep["entrypoints"]["reaching_data_via_http"] == 2
    text = format_report(rep)
    assert "+ 2 reaching data through other scanned services over HTTP" in text
    assert "data via HTTP, 2 hop(s)" in text

    crossed = build_report(data, cross_service=True)
    assert crossed["entrypoints"]["reaching_data"] == rep["entrypoints"]["reaching_data"] + 2
    assert not any("/v2/orders" in r["entrypoint"] for r in crossed["entrypoints"]["without_data"])


def test_why_shows_the_http_chain(tmp_path: Path) -> None:
    _workspace(tmp_path)
    graph = scan_path(tmp_path)
    text = format_why(graph, "orders.status")
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if "invoked via HTTP from" in line)
    assert "gateway" in lines[first]
    assert "← via HTTP from" in lines[first + 1] and "edge" in lines[first + 1], text


def test_cycles_do_not_loop() -> None:
    data = {
        "nodes": [
            {"id": "a/entrypoint:http:GET:/a", "type": "entrypoint", "kind": "http"},
            {"id": "b/entrypoint:http:GET:/b", "type": "entrypoint", "kind": "http"},
            {"id": "a/function:fa", "type": "function"},
            {"id": "b/function:fb", "type": "function"},
        ],
        "edges": [
            {"type": "triggers", "from": "a/entrypoint:http:GET:/a", "to": "a/function:fa"},
            {"type": "triggers", "from": "b/entrypoint:http:GET:/b", "to": "b/function:fb"},
            {"type": "calls_http", "from": "a/function:fa", "to": "b/entrypoint:http:GET:/b"},
            {"type": "calls_http", "from": "b/function:fb", "to": "a/entrypoint:http:GET:/a"},
        ],
    }
    rep = build_report(data)
    assert rep["entrypoints"]["reaching_data_via_http"] == 0
    stops = {r["http_stop"] for r in rep["entrypoints"]["without_data"]}
    assert stops == {"target_without_data"}
