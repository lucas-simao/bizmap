"""`bizmap who-writes` / `who-reads` / `trace` on the fictional three-repo graph
(tests/fixtures/query_repro.py): orders-api (Go), shop-web (TS), billing-worker (Python)."""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import pytest

from bizmap.query import SCHEMA, Index, QueryError, render, render_error, trace, who
from fixtures.query_repro import API, BILL, WEB, build


@pytest.fixture
def ix() -> Index:
    return Index.from_dict(build())


def _fns(result: dict) -> set[tuple[str, str]]:
    return {(r["repo"], r["function"]["label"]) for r in result["results"]}


# ── who-writes / who-reads ──


def test_who_writes_the_table_lists_every_repo_including_the_forgotten_worker(ix: Index) -> None:
    r = who(ix, "table:orders", "writes")
    assert r["entity"]["kind"] == "table" and r["entity"]["store"] == "Postgres"
    assert r["entity"]["repos"] == [BILL, API]
    assert _fns(r) == {
        (BILL, "repo.mark_billed"),
        (API, "Job.Run"),
        (API, "Repo.SetStatus"),
        (API, "mirrorTx.WriteOrder"),
    }
    patch = next(x for x in r["results"] if x["function"]["label"] == "Repo.SetStatus")
    (entry,) = patch["entrypoints"]
    assert entry["entrypoint"]["label"] == "PATCH /orders/{id}/status"
    assert entry["handler"]["label"] == "OrdersHandler.UpdateStatus"
    assert (patch["columns"], patch["evidence"], patch["confidence"]) == (["status"], "internal/orders/repo.go:33", "exact")
    worker = next(x for x in r["results"] if x["repo"] == BILL)
    assert [e["entrypoint"]["label"] for e in worker["entrypoints"]] == ["pubsub order-created"]  # once, not again as the queue


def test_text_shows_entry_handler_writer_and_the_same_table_in_two_repos(ix: Index) -> None:
    text = render(who(ix, "table:orders", "writes"))
    assert "PATCH /orders/{id}/status → OrdersHandler.UpdateStatus → Repo.SetStatus  writes orders (status)  [exact]" in text
    assert "pubsub order-created → consumer.handle_order_created → repo.mark_billed  writes orders (status)" in text
    assert "Same table in 2 repos: billing-worker, orders-api" in text


def test_ambiguous_name_lists_the_candidates_and_never_picks_one(ix: Index) -> None:
    with pytest.raises(QueryError) as err:
        who(ix, "orders", "writes")
    assert err.value.code == 2
    cands = err.value.payload["candidates"]
    assert [(c["kind"], c["store"], c["repos"], c["qualify"]) for c in cands] == [
        ("collection", "Firestore", [API, WEB], "collection:orders"),
        ("table", "Postgres", [BILL, API], "table:orders"),
    ]
    text = render_error(err.value, "text", "en")
    assert "'orders' names 2 different things" in text and "collection · Firestore · orders-api, shop-web" in text
    assert "→  table:orders" in text
    assert json.loads(render_error(err.value, "json", "en"))["error"] == "ambiguous"


def test_not_found_says_so(ix: Index) -> None:
    with pytest.raises(QueryError) as err:
        who(ix, "invoices", "writes")
    assert err.value.code == 1 and err.value.payload["error"] == "not_found"


def test_repo_qualified_and_column_forms(ix: Index) -> None:
    only_api = who(ix, f"{API}/table:orders", "writes")
    assert {x["repo"] for x in only_api["results"]} == {API}
    status = who(ix, "orders.status", "writes")
    assert status["entity"]["kind"] == "column"
    assert _fns(status) == {(BILL, "repo.mark_billed"), (API, "Repo.SetStatus")}
    assert _fns(who(ix, "column:orders.status", "writes")) == _fns(status)
    # the collection's fields are not the table's columns
    coll = who(ix, "collection:orders", "writes")
    assert _fns(coll) == {(API, "Service.Save")}


def test_derived_copies_are_not_writers(ix: Index) -> None:
    r = who(ix, "collection:orders", "writes")
    (save,) = r["results"]
    (entry,) = save["entrypoints"]
    assert entry["entrypoint"]["label"] == "POST /orders" and entry["handler"]["label"] == "OrdersHandler.Create"


def test_who_reads_the_collection_across_repos(ix: Index) -> None:
    r = who(ix, "collection:orders", "reads")
    assert r["entity"]["repos"] == [API, WEB]
    assert _fns(r) == {(API, "Job.Run"), (WEB, "orders.listOrders")}
    web = next(x for x in r["results"] if x["repo"] == WEB)
    # no entrypoint: the outermost caller is shown
    assert [e["entrypoint"]["label"] for e in web["entrypoints"]] == ["pages.Orders.OrdersPage"]


def test_path_flag_shows_every_function(ix: Index) -> None:
    data = build()
    # one more function between the handler and the writer
    a = f"{API}/function:"
    data["nodes"].append({"id": a + "internal/orders.Service.Validate", "type": "function", "name": "Service.Validate",
                          "evidence": {"file": "internal/orders/service.go", "line": 40}})
    data["edges"] = [e for e in data["edges"] if e["id"] != f"calls:{a}internal/orders.OrdersHandler.Create->{a}internal/orders.Service.Save"]
    data["edges"] += [
        {"id": "x1", "type": "calls", "from": a + "internal/orders.OrdersHandler.Create", "to": a + "internal/orders.Service.Validate", "confidence": "exact"},
        {"id": "x2", "type": "calls", "from": a + "internal/orders.Service.Validate", "to": a + "internal/orders.Service.Save", "confidence": "exact"},
    ]
    ix2 = Index.from_dict(data)
    r = who(ix2, "collection:orders", "writes")
    short = render(r)
    full = render(r, show_path=True)
    assert "POST /orders → OrdersHandler.Create → (via 1 function) → Service.Save" in short
    assert "POST /orders → OrdersHandler.Create → Service.Validate → Service.Save" in full
    assert [p["label"] for p in r["results"][0]["entrypoints"][0]["path"]] == [
        "POST /orders", "OrdersHandler.Create", "Service.Validate", "Service.Save"
    ]


def test_unresolved_writes_are_counted(ix: Index) -> None:
    r = who(ix, "table:orders", "writes")
    assert [(u["kind"], u["repo"], u["count"], [f["label"] for f in u["functions"]]) for u in r["unresolved"]] == [
        ("dynamic_table", BILL, 1, ["app.touch"]),
        ("dynamic_table", API, 1, ["Repo.BulkFix"]),
    ]
    assert ("2 write(s) with a table name built at runtime (billing-worker: 1, orders-api: 1) could not be resolved: "
            "app.touch [billing-worker], Repo.BulkFix [orders-api]") in render(r)
    one = who(ix, f"{BILL}/table:orders", "writes")
    assert "1 write(s) with a table name built at runtime (billing-worker: 1) could not be resolved: app.touch" in render(one)
    # reads of the collection: nothing unresolved, and the answer says so
    assert "Nothing unresolved in these repos could change this answer." in render(who(ix, "collection:orders", "reads"))


def test_json_is_stable_and_versioned(ix: Index) -> None:
    a = render(who(ix, "table:orders", "writes"), "json")
    b = render(who(Index.from_dict(build()), "table:orders", "writes"), "json")
    assert a == b
    doc = json.loads(a)
    assert doc["schema"] == SCHEMA == "bizmap.query/v1"
    assert set(doc) == {"schema", "query", "entity", "access", "results", "unresolved"}
    assert set(doc["entity"]) == {"kind", "name", "store", "repos", "nodes"}
    row = doc["results"][0]
    assert set(row) == {"function", "repo", "entity_node", "evidence", "columns", "confidence", "entrypoints", "entrypoints_partial"}
    assert set(row["entrypoints"][0]) == {"entrypoint", "handler", "wiring", "via", "path", "confidence"}
    assert set(row["function"]) >= {"id", "type", "label", "repo", "evidence"}


def test_markdown_and_portuguese(ix: Index) -> None:
    md = render(who(ix, "table:orders", "writes"), "md", "pt-BR")
    assert md.startswith("### Quem escreve orders (tabela · Postgres · billing-worker, orders-api)")
    assert "| PATCH /orders/{id}/status → OrdersHandler.UpdateStatus → Repo.SetStatus | orders (status) | exact | `internal/orders/repo.go:33` |" in md
    assert "**Pode mudar esta resposta**" in md
    text = render(trace(ix, "collection:orders"), "text", "pt-BR")
    assert "Para onde vai:" in text and "consumida por billing-worker" in text


# ── trace ──


def test_trace_collection_goes_to_postgres_and_through_the_topic_to_the_worker(ix: Index) -> None:
    r = trace(ix, "collection:orders")
    down = r["downstream"]
    feeds = [h for h in down if h["kind"] == "feeds"]
    assert [(h["from"]["id"], h["to"]["id"], h["confidence"]) for h in feeds] == [
        (f"{API}/collection:orders", f"{API}/table:orders", "heuristic")
    ]
    assert [v["label"] for v in feeds[0]["via"]] == ["Job.Run"]
    (q,) = [h for h in down if h["kind"] == "queue"]
    assert q["queue"]["name"] == "order-created" and q["trigger"]["label"] == "POST /orders"
    assert [p["label"] for p in q["producers"]] == ["Publisher.OrderCreated"]
    assert [(c["repo"], c["consumer"]["label"]) for c in q["consumers"]] == [(BILL, "pubsub order-created")]
    assert [w["id"] for w in q["writes"]] == [f"{BILL}/table:orders"]
    up = r["upstream"]
    (writer,) = [h for h in up if h["kind"] == "writer"]
    assert writer["trigger"]["label"] == "POST /orders" and writer["function"]["label"] == "Service.Save"
    assert [c["id"] for c in writer["http_callers"]] == [f"{WEB}/function:src.orders.createOrder"]
    assert [u["url"] for u in r["unresolved"]["http"]] == ["{FRAUD_API_URL}/check"]
    text = render(r)
    assert "feeds: orders (collection · Firestore · orders-api) → orders (table · Postgres · orders-api)  [heuristic]" in text
    assert "queue order-created: POST /orders → Publisher.OrderCreated publishes" in text
    assert "consumed by billing-worker: pubsub order-created" in text
    assert "1 HTTP call(s) from the functions involved matched no route" in text


def test_trace_table_upstream_finds_the_queue_producer_and_the_collection(ix: Index) -> None:
    r = trace(ix, "table:orders", direction="up")
    assert r["downstream"] == []
    worker = next(h for h in r["upstream"] if h["kind"] == "writer" and h["trigger"]["repo"] == BILL)
    assert [p["label"] for q in worker["queues"] for p in q["producers"]] == ["Publisher.OrderCreated"]
    assert any(h["kind"] == "feeds" and h["from"]["id"] == f"{API}/collection:orders" for h in r["upstream"])
    # depth 2 reaches who writes the collection
    assert any(h["kind"] == "writer" and h["depth"] == 2 and h["trigger"]["label"] == "POST /orders" for h in r["upstream"])
    one = trace(ix, "table:orders", direction="up", depth=1)
    assert all(h["depth"] == 1 for h in one["upstream"])
    assert [(d["repo"], d["count"]) for d in r["unresolved"]["dynamic_writes"]] == [(BILL, 1), (API, 1)]


def test_trace_down_only(ix: Index) -> None:
    r = trace(ix, "collection:orders", direction="down")
    assert r["upstream"] == [] and r["downstream"]
    assert "Upstream" not in render(r)


def test_trace_json_is_stable(ix: Index) -> None:
    a = json.loads(render(trace(ix, "collection:orders"), "json"))
    assert a["schema"] == SCHEMA and set(a) == {"schema", "query", "entity", "upstream", "downstream", "unresolved"}
    assert a["query"] == {"command": "trace", "entity": "collection:orders", "depth": 2, "direction": "both"}
    assert set(a["unresolved"]) == {"queues", "http", "dynamic_writes"}
    assert render(trace(Index.from_dict(build()), "collection:orders"), "json") == render(trace(ix, "collection:orders"), "json")


# ── size ──


def _big(n_fn: int = 60_000, n_calls: int = 110_000, seed: int = 7) -> dict:
    """~70k nodes / ~180k edges, tree-like call graph like a real scan."""
    rnd = random.Random(seed)
    g = build()
    nodes, edges = g["nodes"], g["edges"]
    tables = []
    for i in range(3000):
        r = f"svc{i % 16}"
        nodes.append({"id": f"{r}/table:t{i}", "type": "table", "name": f"t{i}"})
        tables.append(f"{r}/table:t{i}")
        nodes += [{"id": f"{r}/column:t{i}.c{c}", "type": "column", "table": f"t{i}", "name": f"c{c}"} for c in range(2)]
    fns = []
    for i in range(n_fn):
        fid = f"svc{i % 16}/function:pkg{i % 400}.F{i}"
        fns.append(fid)
        nodes.append({"id": fid, "type": "function", "name": f"F{i}", "evidence": {"file": f"pkg{i % 400}/f.go", "line": 1}})
    for i in range(1500):
        eid = f"svc{i % 16}/entrypoint:http:GET:/r{i}"
        nodes.append({"id": eid, "type": "entrypoint", "kind": "http", "method": "GET", "path": f"/r{i}"})
        edges.append({"id": f"t{i}", "type": "triggers", "from": eid, "to": fns[i * 37 % n_fn], "confidence": "exact"})
    for i in range(n_calls):
        b = rnd.randrange(1, n_fn)
        a = rnd.randrange(0, b)  # callers come "before": a DAG, like layered code
        edges.append({"id": f"c{i}", "type": "calls", "from": fns[a], "to": fns[b], "confidence": "exact"})
    for i in range(60_000):
        edges.append({"id": f"w{i}", "type": rnd.choice(["reads", "writes"]), "from": rnd.choice(fns),
                      "to": rnd.choice(tables), "confidence": "exact", "evidence": {"file": "x.go", "line": 1}})
    for i in range(200):
        edges.append({"id": f"p{i}", "type": "writes", "from": rnd.choice(fns), "to": "svc1/table:t1", "confidence": "exact"})
    return g


@pytest.mark.perf
def test_answers_fast_on_a_70k_node_graph(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    g = _big()
    assert len(g["nodes"]) >= 69_000 and len(g["edges"]) >= 170_000
    path.write_text(json.dumps(g), encoding="utf-8")
    for run in (
        lambda ix: who(ix, "svc1/table:t1", "writes"),
        lambda ix: who(ix, "table:orders", "reads"),
        lambda ix: trace(ix, "svc1/table:t1"),
    ):
        t = time.perf_counter()
        ix = Index.load(path)
        render(run(ix))
        assert time.perf_counter() - t < 2.0


def test_big_graph_rows_are_bounded() -> None:
    ix = Index.from_dict(_big(n_fn=8000, n_calls=16_000))
    r = who(ix, "svc1/table:t1", "writes")
    assert len(r["results"]) >= 200
    assert all(len(row["entrypoints"]) <= 25 for row in r["results"])
    t = trace(ix, "svc1/table:t1", direction="up", depth=1)
    writers = [h for h in t["upstream"] if h["kind"] == "writer"]
    assert len(writers) <= 50 and any(h["kind"] == "more_writers" for h in t["upstream"])


# ── CLI (imports the scanner: needs the tree-sitter parsers) ──


def test_cli_commands(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_go")
    from click.testing import CliRunner

    from bizmap.cli import main

    graph = tmp_path / "bizmap.graph.json"
    graph.write_text(json.dumps(build()), encoding="utf-8")
    run = CliRunner().invoke
    res = run(main, ["who-writes", "table:orders", "--graph", str(graph)])
    assert res.exit_code == 0, res.output
    assert "repo.mark_billed" in res.output and "Repo.SetStatus" in res.output
    res = run(main, ["who-writes", "orders", "--graph", str(graph)])
    assert res.exit_code == 2 and "names 2 different things" in res.output
    res = run(main, ["who-writes", "orders", "--graph", str(graph), "--format", "json"])
    assert res.exit_code == 2 and json.loads(res.output)["error"] == "ambiguous"
    res = run(main, ["who-reads", "nope", "--graph", str(graph)])
    assert res.exit_code == 1
    res = run(main, ["trace", "collection:orders", "--graph", str(graph), "--format", "md", "--lang", "pt-BR", "--depth", "3"])
    assert res.exit_code == 0 and "### Rastro orders" in res.output
    res = run(main, ["who-writes", "collection:orders", "--graph", str(graph), "--path", "--format", "json"])
    assert json.loads(res.output)["schema"] == "bizmap.query/v1"
    # --no-cache / --cache-dir, and nothing written next to the graph
    mine = tmp_path / "c"
    res = run(main, ["trace", "collection:orders", "--graph", str(graph), "--no-cache", "--cache-dir", str(mine)])
    assert res.exit_code == 0 and not mine.exists()
    res = run(main, ["who-reads", "collection:orders", "--graph", str(graph), "--cache-dir", str(mine)])
    assert res.exit_code == 0 and len(list(mine.glob("*.idx"))) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bizmap.graph.json", "c"]
    # a leftover 0.13.1 cache is pointed out, not used
    (tmp_path / "bizmap.graph.idx").write_bytes(b"old")
    res = run(main, ["who-writes", "table:orders", "--graph", str(graph)])
    assert res.exit_code == 0 and "no longer used" in res.output  # stderr is part of `output` in CliRunner


# ── round 2: names built at runtime in Go / TS, wiring, store label, cache ──


def test_dynamic_collection_writes_in_go_and_ts_are_counted(ix: Index) -> None:
    r = who(ix, "collection:orders", "writes")
    assert [(u["kind"], u["access"], u["repo"], u["count"], [f["label"] for f in u["functions"]]) for u in r["unresolved"]] == [
        ("dynamic_collection", "writes", API, 1, ["Service.Archive"]),
        ("dynamic_collection", "writes", WEB, 1, ["orders.saveTo"]),
    ]
    text = render(r)
    assert ("2 write(s) with a collection name built at runtime (orders-api: 1, shop-web: 1) could not be resolved: "
            "Service.Archive [orders-api], orders.saveTo [shop-web]") in text
    pt = render(r, "text", "pt-BR")
    assert "2 escrita(s) com nome de coleção montado em tempo de execução (orders-api: 1, shop-web: 1)" in pt
    t = trace(ix, "collection:orders")
    doc = json.loads(render(t, "json"))
    assert [(d["repo"], d["count"], [f["label"] for f in d["functions"]]) for d in doc["unresolved"]["dynamic_writes"]] == [
        (API, 1, ["Service.Archive"]), (WEB, 1, ["orders.saveTo"]),
    ]
    assert "2 write(s) with a collection name built at runtime (orders-api: 1, shop-web: 1)" in render(t)
    # the unknown collection is never an entity of its own
    with pytest.raises(QueryError):
        who(ix, "collection:unknown", "writes")


def test_dynamic_collection_reads_are_counted_too() -> None:
    data = build()
    data["edges"].append({"id": "rd", "type": "reads", "from": f"{WEB}/function:src.orders.listOrders",
                          "to": f"{WEB}/collection:unknown", "confidence": "unknown",
                          "evidence": {"file": "src/orders.ts", "line": 7}})
    r = who(Index.from_dict(data), "collection:orders", "reads")
    assert [(u["repo"], u["count"]) for u in r["unresolved"]] == [(WEB, 1)]
    assert "1 read(s) with a collection name built at runtime (shop-web: 1)" in render(r)


def test_short_path_skips_main_and_wiring_to_the_real_handler(ix: Index) -> None:
    r = who(ix, f"{API}/table:orders", "writes")
    row = next(x for x in r["results"] if x["function"]["label"] == "mirrorTx.WriteOrder")
    (entry,) = row["entrypoints"]
    assert entry["entrypoint"]["label"] == "main"
    assert (entry["wiring"], entry["handler"]["label"], entry["via"]) == (3, "Handle", 1)
    assert "main → … → Handle → (via 1 function) → mirrorTx.WriteOrder  writes orders  [inferred]" in render(r)
    full = render(r, show_path=True)
    assert "main → buildRouter → RegisterEvent → RegisterEvent.func1 → Handle → Service.Sync → mirrorTx.WriteOrder" in full
    # a route keeps its handler even when it is called `Register…`
    patch = next(x for x in r["results"] if x["function"]["label"] == "Repo.SetStatus")
    assert patch["entrypoints"][0]["wiring"] == 0


def test_one_store_label_for_one_table() -> None:
    data = build()
    for n in data["nodes"]:
        if n["id"] == f"{BILL}/table:orders":
            n.pop("engine")  # billing-worker's driver was not seen
    ix2 = Index.from_dict(data)
    r = who(ix2, "table:orders", "writes")
    assert r["entity"]["store"] == "Postgres"
    assert {n["store"] for n in r["entity"]["nodes"]} == {"Postgres"}
    with pytest.raises(QueryError) as err:
        who(ix2, "orders", "writes")
    assert [c["store"] for c in err.value.payload["candidates"]] == ["Firestore", "Postgres"]
    assert "table · Postgres · billing-worker, orders-api" in render_error(err.value, "text", "en")
    assert "SQL" not in render(r).split("Could change")[0].replace("SQL ", "")


def test_index_cache_is_written_reused_and_invalidated(tmp_path: Path) -> None:
    import os

    from bizmap.query import cache_dir, cache_path

    graph = tmp_path / "bizmap.graph.json"
    graph.write_text(json.dumps(build()), encoding="utf-8")
    first = Index.load(graph)
    assert not first.loaded_from_cache
    # outside the repo: in the cache folder, never next to the graph
    assert cache_path(graph).parent == cache_dir() and cache_path(graph).is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bizmap.graph.json"]
    second = Index.load(graph)
    assert second.loaded_from_cache
    assert render(who(second, "table:orders", "writes"), "json") == render(who(first, "table:orders", "writes"), "json")
    # a new scan (other size / mtime) rebuilds it
    data = build()
    data["nodes"] = [n for n in data["nodes"] if n["id"] != f"{BILL}/table:unknown"]
    data["edges"] = [e for e in data["edges"] if e["to"] != f"{BILL}/table:unknown"]
    graph.write_text(json.dumps(data), encoding="utf-8")
    third = Index.load(graph)
    assert not third.loaded_from_cache
    assert [u["repo"] for u in who(third, "table:orders", "writes")["unresolved"]] == [API]
    # same size and mtime, other content: the fingerprint notices
    st = graph.stat()
    text = graph.read_text(encoding="utf-8").replace('"status"', '"statuz"', 1)
    graph.write_text(text, encoding="utf-8")
    os.utime(graph, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert graph.stat().st_size == st.st_size and not Index.load(graph).loaded_from_cache
    # a broken cache is ignored and replaced
    cache_path(graph).write_bytes(b"not marshal")
    assert not Index.load(graph).loaded_from_cache and Index.load(graph).loaded_from_cache
    # no cache when asked
    assert not Index.load(graph, use_cache=False).loaded_from_cache


def test_cache_off_by_env_and_dir_by_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bizmap.query import cache_dir, cache_path

    graph = tmp_path / "repo" / "bizmap.graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps(build()), encoding="utf-8")
    monkeypatch.setenv("BIZMAP_NO_CACHE", "1")
    Index.load(graph)
    assert not Index.load(graph).loaded_from_cache and not cache_path(graph).exists()
    monkeypatch.delenv("BIZMAP_NO_CACHE")
    mine = tmp_path / "my-cache"
    Index.load(graph, cache_directory=mine)
    assert Index.load(graph, cache_directory=mine).loaded_from_cache
    assert [p.suffix for p in mine.iterdir()] == [".idx"] and not cache_path(graph).exists()
    # two repos never share a cache file
    other = tmp_path / "other" / "bizmap.graph.json"
    other.parent.mkdir()
    other.write_text(graph.read_text(encoding="utf-8"), encoding="utf-8")
    assert cache_path(other) != cache_path(graph)
    # the default folder is the user's cache, not the repo
    monkeypatch.delenv("BIZMAP_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    if sys.platform not in ("win32", "darwin"):
        assert cache_dir() == tmp_path / "xdg" / "bizmap" / "index"


def test_old_caches_are_pruned(tmp_path: Path) -> None:
    from bizmap.query import CACHE_KEEP, cache_dir

    for k in range(CACHE_KEEP + 4):
        g = tmp_path / f"r{k}" / "bizmap.graph.json"
        g.parent.mkdir()
        g.write_text(json.dumps(build()), encoding="utf-8")
        Index.load(g)
    (cache_dir() / "someone-else.idx").write_text("x")  # not ours: kept
    Index.load(tmp_path / "r0" / "bizmap.graph.json")
    ours = [p for p in cache_dir().glob("*.idx") if p.name != "someone-else.idx"]
    assert len(ours) == CACHE_KEEP and (cache_dir() / "someone-else.idx").exists()


def test_big_graph_cache_is_reused(tmp_path: Path) -> None:
    graph = tmp_path / "bizmap.graph.json"
    graph.write_text(json.dumps(_big(n_fn=20_000, n_calls=40_000)), encoding="utf-8")
    a = Index.load(graph)
    b = Index.load(graph)
    assert (a.loaded_from_cache, b.loaded_from_cache) == (False, True)
    assert render(who(a, "svc1/table:t1", "writes"), "json") == render(who(b, "svc1/table:t1", "writes"), "json")
    assert render(trace(a, "svc1/table:t1"), "json") == render(trace(b, "svc1/table:t1"), "json")
