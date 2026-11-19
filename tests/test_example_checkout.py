"""README demo: web (TS) → api (Go) → billing (Python) → api, one database."""

from __future__ import annotations

from pathlib import Path

from bizmap.scan import scan_path
from bizmap.why import format_why

CHECKOUT = Path(__file__).resolve().parents[1] / "examples" / "checkout"


def _http_links(graph) -> list[tuple[str, str]]:
    return sorted(
        (e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http"
    )


EXPECTED_LINKS = [
    ("placeOrder", "entrypoint:http:POST:/api/orders"),
    ("pollOrder", "entrypoint:http:GET:/api/orders/:id"),
    ("CreateInvoice", "entrypoint:http:POST:/invoices"),
    ("payment_webhook", "entrypoint:http:POST:/api/orders/:id/paid"),
]


def test_checkout_links_never_disappear() -> None:
    """Regression guard: names every expected cross-service link that is missing
    (e.g. a Go route that lost its group prefix, or stricter matching)."""
    graph = scan_path(CHECKOUT)
    missing = [
        f"{frm} -> {to}" for frm, to in EXPECTED_LINKS if not _linked(graph, frm, to)
    ]
    routes = sorted(
        f"{n.attrs.get('method')} {n.attrs.get('path')}"
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    )
    assert missing == [], f"missing links: {missing}; scanned routes: {routes}"


def _linked(graph, frm_part: str, to_suffix: str) -> bool:
    return any(
        frm_part in frm and to.endswith(to_suffix) for frm, to in _http_links(graph)
    )


def test_checkout_chain_links_across_three_languages() -> None:
    graph = scan_path(CHECKOUT)
    links = _http_links(graph)
    # web → api
    assert _linked(graph, "placeOrder", "entrypoint:http:POST:/api/orders"), links
    assert _linked(graph, "pollOrder", "entrypoint:http:GET:/api/orders/:id"), links
    # api → billing
    assert _linked(graph, "CreateInvoice", "entrypoint:http:POST:/invoices"), links
    # billing → api
    assert _linked(graph, "payment_webhook", "entrypoint:http:POST:/api/orders/:id/paid"), links
    assert not graph.attrs.get("unresolved_http"), graph.attrs.get("unresolved_http")


def test_checkout_why_orders_status() -> None:
    graph = scan_path(CHECKOUT)
    text = format_why(graph, "orders.status")
    for writer in ("InsertOrder", "MarkPaid", "ExpirePending"):
        assert writer in text, text
    assert "POST /api/orders/:id/paid" in text, text
    assert "GetOrder" in text, text
