"""The HTML start screen, export side (no browser): summary strip, findings,
matrix cells and the graph shipped to the page's query engine.
Fictional three-repo system: tests/fixtures/home_repro.py."""

from __future__ import annotations

import json
import re

from bizmap.export import export_graph
from bizmap.export.book import build_book
from bizmap.export.home import engine_data, is_hidden
from bizmap.export.services import assign_services
from bizmap.query import Index
from fixtures.home_repro import API, BILL, WEB, build


def _home(data: dict) -> tuple[dict, dict, list[str]]:
    index, chunks = build_book(data, assign_services(data), lang="pt-BR")
    return index["home"], chunks, [s["name"] for s in index["services"]]


def test_summary_strip_counts_the_repro() -> None:
    home, _chunks, services = _home(build())
    s = home["summary"]
    assert services == [BILL, API, WEB]  # the shared topic is no service of its own
    assert (s["repos"], s["services"], s["data"], s["eps"], s["flows"]) == (3, 3, 2, 4, 1)
    assert s["stores"] == {"Firestore": 1, "Postgres": 1}
    # the FRAUD_API call with no route + 3 reads / writes with a runtime name
    assert s["unres"] == 4 and s["unresk"] == {"collection": 1, "http": 1, "table": 2}


def test_unresolved_is_counted_even_when_zero() -> None:
    data = build()
    data["unresolved_http"] = []
    data["edges"] = [e for e in data["edges"] if not e["to"].endswith(":unknown")]
    home, _c, _s = _home(data)
    assert home["summary"]["unres"] == 0  # present (the page shows "0 unresolved"), not left out


def test_cards_unexpected_writer_multi_dynamic_orphans() -> None:
    home, chunks, services = _home(build())
    sv = {n: i for i, n in enumerate(services)}
    c = home["cards"]
    # billing-worker writes orders.status; orders-api has the migration
    assert c["unexpected"] == [[sv[BILL], "table:orders", ["status"], sv[API], "ddl", "orders.status"]]
    assert c["multi"] == [["table:orders", 2, sorted([sv[BILL], sv[API]])]]
    assert {s: n for s, n, _h in c["dynamic"]} == {sv[WEB]: 1, sv[BILL]: 1, sv[API]: 1}
    # repo.touch and Repo.CountByStatus; the test that writes orders.status is hidden by default
    assert {s: (n, h) for s, n, h in c["orphans"]} == {sv[BILL]: (1, 0), sv[API]: (1, 1)}
    orphans = {o[0]: o[4] for o in chunks["fx"]["orphans"]}
    assert orphans == {"repo.touch": 0, "Repo.CountByStatus": 0, "TestSetStatus": 1}
    assert c["mirrors"] == [["Firestore", "Postgres", 1]]


def test_matrix_cells_per_service() -> None:
    home, _c, services = _home(build())
    sv = {n: i for i, n in enumerate(services)}
    ents = {(e[1], e[2]): e for e in home["ents"]}
    coll, table = ents[("collection", "orders")], ents[("table", "orders")]
    assert coll[0] == "collection:orders" and table[0] == "table:orders"
    cells = lambda e: {s: rw for s, rw, _r, _hrw, _hr in e[7]}  # noqa: E731
    assert cells(coll) == {sv[API]: 3, sv[WEB]: 1}
    assert cells(table) == {sv[API]: 3, sv[BILL]: 2}
    hidden = {s: hrw for s, _rw, _r, hrw, _hr in table[7]}
    assert hidden[sv[API]] == 2  # TestSetStatus, shown with "tests and helpers"
    assert table[8] == sv[API] and table[9] == "ddl"
    # shop-web writes a collection whose name is a parameter: flagged on its row
    assert home["dyn"][str(sv[WEB])] == {"collection": 2}


def test_engine_data_rebuilds_the_same_index() -> None:
    data = build()
    ix = Index.from_dict(data)
    g = engine_data(ix, data)
    assert g["ids"][: len(ix.nodes)] == list(ix.nodes)
    assert len(g["e"]["a"]) == sum(1 for e in data["edges"])  # every edge type here is one the engine reads
    k = g["ids"].index(f"{API}/function:internal/orders.Repo.storeStatus")
    assert g["nm"][k] == "Repo.storeStatus" and g["F"][g["ef"][k]] == "internal/orders/repo.go"
    feeds = [j for j, t in enumerate(g["e"]["t"]) if g["ET"][t] == "feeds"]
    assert len(feeds) == 1 and str(feeds[0]) in g["fe"]


def test_hidden_files() -> None:
    for f in ("internal/orders/repo_test.go", "tests/test_x.py", "billing/repo_test.py", "src/a.test.ts", "scripts/seed.py",
              "tools/gen/main.go", "conftest.py", "src/__mocks__/api.ts"):
        assert is_hidden(f), f
    for f in ("internal/orders/repo.go", "cmd/mirror/main.go", "src/pages/Orders.tsx", "billing/main.py", "src/testing_utils_real.go"):
        assert not is_hidden(f), f


def test_old_graph_without_new_attributes_exports() -> None:
    """A graph from an older version: no repos list, no engines, no evidence,
    single repo, edges to nodes it does not list."""
    data = {
        "nodes": [
            {"id": "function:pkg.Save", "type": "function"},
            {"id": "table:orders", "type": "table"},
            {"id": "column:orders.status", "type": "column", "table": "orders", "name": "status"},
        ],
        "edges": [
            {"id": "w", "type": "writes", "from": "function:pkg.Save", "to": "column:orders.status"},
            {"id": "c", "type": "calls", "from": "function:pkg.Main", "to": "function:pkg.Save"},
        ],
    }
    html = export_graph(data, "html")
    index = json.loads(re.search(r'<script id="bm-index" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert index["home"]["summary"]["data"] == 1
    assert index["home"]["summary"]["unres"] == 0


def test_markup_in_names_stays_inside_json() -> None:
    data = build()
    for n in data["nodes"]:
        if n["id"] == f"{API}/function:internal/orders.Repo.storeStatus":
            n["name"] = "</script><img src=x onerror=alert(1)>"
    html = export_graph(data, "html")
    assert "</script><img" not in html
