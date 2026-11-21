"""Function nodes carry their parameters and return type (all fictional)."""

from __future__ import annotations

import json
from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_path


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _fn(graph, qual: str) -> dict:
    node = graph.nodes[function_id(qual)]
    return {k: node.attrs[k] for k in ("params", "returns") if k in node.attrs}


def test_typescript_and_javascript_signatures(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/api/orders.ts",
        """
import { api } from './api';

export const ordersClient = {
  get: (id: string, opts?: { expand?: boolean }): Promise<Order> => api.get(`/orders/${id}`).then((r) => r.data),
  async list({ page, size }: Paging): Promise<Order[]> {
    return (await api.get('/orders', { params: { page, size } })).data;
  },
};

export function formatTotal(o: Order, currency = 'BRL'): string { return ''; }

export const noArgs = () => 1;

export function weird(x: "<img src=x onerror=alert(1)>"): `<script>` { return '<script>' as never; }
""",
    )
    _write(tmp_path, "src/api/api.ts", "export const api = { get: (u: string, c?: object) => fetch(u) };\n")
    _write(tmp_path, "src/util.js", "export function pick(a, { b, c }, ...rest) { return a; }\n")
    graph = scan_path(tmp_path)
    assert _fn(graph, "src.api.orders.ordersClient.get") == {
        "params": [{"name": "id", "type": "string"}, {"name": "opts", "type": "{ expand?: boolean }", "optional": True}],
        "returns": "Promise<Order>",
    }
    assert _fn(graph, "src.api.orders.ordersClient.list") == {
        "params": [{"name": "{ page, size }", "type": "Paging"}],
        "returns": "Promise<Order[]>",
    }
    assert _fn(graph, "src.api.orders.formatTotal") == {
        "params": [{"name": "o", "type": "Order"}, {"name": "currency"}],
        "returns": "string",
    }
    assert _fn(graph, "src.api.orders.noArgs") == {"params": []}  # no `returns: ""`
    weird = _fn(graph, "src.api.orders.weird")
    assert weird["params"][0]["type"] == '"<img src=x onerror=alert(1)>"' and weird["returns"] == "`<script>`"
    assert _fn(graph, "src.util.pick") == {
        "params": [{"name": "a"}, {"name": "{ b, c }"}, {"name": "...rest"}],
    }


def test_go_signatures(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module example.com/shop\n\ngo 1.22\n")
    _write(
        tmp_path,
        "orders/orders.go",
        """
package orders

import (
    "context"

    "github.com/labstack/echo/v4"
)

type Order struct{}
type OrderHandler struct{}

func (h *OrderHandler) Get(c echo.Context) error { return nil }

func FindOrder(ctx context.Context, id string) (*Order, error) { return nil, nil }

func Tags(names ...string) []string { return names }

func Pair(a, b int) {}
""",
    )
    graph = scan_path(tmp_path)
    by_name = {n.attrs.get("name"): n.attrs for n in graph.nodes.values() if n.type == "function"}

    def sig(suffix: str) -> dict:
        attrs = next(a for name, a in by_name.items() if str(name).endswith(suffix))
        return {k: attrs[k] for k in ("params", "returns") if k in attrs}

    assert sig("OrderHandler.Get") == {"params": [{"name": "c", "type": "echo.Context"}], "returns": "error"}
    assert sig("FindOrder") == {
        "params": [{"name": "ctx", "type": "context.Context"}, {"name": "id", "type": "string"}],
        "returns": "(*Order, error)",
    }
    assert sig("Tags") == {"params": [{"name": "names", "type": "...string"}], "returns": "[]string"}
    assert sig("Pair") == {"params": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}]}


def test_python_signatures(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "shop/orders.py",
        """
from decimal import Decimal


def get_db():
    return None


async def create_order(payload: OrderIn, *, db: Session = Depends(get_db)) -> OrderOut:
    return payload


class Cart:
    def total(self, items: list[Item], **opts) -> Decimal:
        return Decimal(0)

    @classmethod
    def empty(cls, *args, limit=3):
        return cls()
""",
    )
    graph = scan_path(tmp_path)
    by_name = {n.attrs.get("name"): n.attrs for n in graph.nodes.values() if n.type == "function"}

    def sig(suffix: str) -> dict:
        attrs = next(a for name, a in by_name.items() if str(name).endswith(suffix))
        return {k: attrs[k] for k in ("params", "returns") if k in attrs}

    assert sig("create_order") == {
        "params": [{"name": "payload", "type": "OrderIn"}, {"name": "db", "type": "Session"}],
        "returns": "OrderOut",
    }
    assert sig("Cart.total") == {
        "params": [{"name": "items", "type": "list[Item]"}, {"name": "**opts"}],
        "returns": "Decimal",
    }
    assert sig("Cart.empty") == {"params": [{"name": "*args"}, {"name": "limit"}]}
    assert sig("get_db") == {"params": []}


def test_params_keep_their_order_in_the_json(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.ts", "export function f(zeta: string, alpha: number): void {}\n")
    data = json.loads(json.dumps(scan_path(tmp_path).to_dict()))
    node = next(n for n in data["nodes"] if n["id"] == function_id("src.a.f"))
    assert [p["name"] for p in node["params"]] == ["zeta", "alpha"] and node["returns"] == "void"
