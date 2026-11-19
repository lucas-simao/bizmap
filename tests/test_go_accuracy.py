"""Synthetic Go accuracy fixture: colliding method names, same-named packages."""

from __future__ import annotations

from pathlib import Path

from bizmap.files import path_excluded
from bizmap.graph import Graph, column_id, function_id, table_id
from bizmap.mermaid import render_mermaid
from bizmap.scan import scan_path


def _edge(graph: Graph, etype: str, frm: str, to: str):
    return graph.edges.get(f"{etype}:{frm}->{to}")


def _http(graph: Graph, method: str, path: str):
    return graph.get(f"entrypoint:http:{method}:{path}")


EXPECTED_ROUTES = {
    ("POST", "/api/appointments"): "controllers/orders.Handler.Create",
    ("GET", "/api/appointments"): "controllers/orders.Handler.List",
    ("GET", "/api/appointments/:id"): "controllers/orders.Handler.Get",
    ("PATCH", "/api/appointments/:id"): "controllers/orders.Handler.Update",
    ("POST", "/api/patients"): "controllers/users.Handler.Create",
    ("GET", "/api/patients"): "controllers/users.Handler.List",
    ("GET", "/api/patients/:id"): "controllers/users.Handler.Get",
    ("POST", "/api/pending/setup"): "controllers/orders.Handler.Setup",
    ("GET", "/email-prefs"): "controllers/users.Handler.Prefs",
    ("GET", "/profile/photo"): "profile.StaffPhoto",
    ("GET", "/api/version"): "controllers/orders.Handler.Version",
}

EXPECTED_TABLES = {
    "controllers/orders.Handler.Create": "appointments",
    "controllers/orders.Handler.List": "appointments",
    "controllers/orders.Handler.Get": "appointments",
    "controllers/orders.Handler.Update": "appointments",
    "controllers/orders.Handler.Setup": "appointments",
    "controllers/users.Handler.Create": "patients",
    "controllers/users.Handler.List": "patients",
    "controllers/users.Handler.Get": "patients",
    "controllers/users.Handler.Prefs": "email_prefs",
    "profile.StaffPhoto": "photos",
}


def test_exclude_globs() -> None:
    assert path_excluded("cmd/seed/main.go", ["cmd/seed", "demo/"])
    assert path_excluded("demo/setup.go", ["cmd/seed", "demo/"])
    assert not path_excluded("cmd/server/main.go", ["cmd/seed", "demo/"])
    assert path_excluded("demo/nested/x.go", ["demo/**"])


def test_clinic_route_handlers_and_tables(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)

    # 10 routes, each bound to the typed handler — never matches[0] across controllers.
    for (method, path), handler in EXPECTED_ROUTES.items():
        node = _http(graph, method, path)
        assert node is not None, f"missing route {method} {path}; have {[n.id for n in graph.nodes.values() if n.type == 'entrypoint' and n.attrs.get('kind') == 'http']}"
        assert node.attrs.get("symbol") == handler
        assert node.attrs.get("confidence", "exact") != "unknown" or path.startswith("{")
        trig = _edge(graph, "triggers", node.id, function_id(handler))
        assert trig is not None, f"{method} {path} did not trigger {handler}"
        table = EXPECTED_TABLES.get(handler)
        if table is None:
            continue
        # origin SQL may be column-level; propagation to the handler is table-level
        assert (
            _edge(graph, "writes", function_id(handler), table_id(table)) is not None
            or _edge(graph, "reads", function_id(handler), table_id(table)) is not None
            or any(
                e.from_id == function_id(handler)
                and e.type in {"reads", "writes"}
                and table in e.to_id
                for e in graph.edges.values()
            )
        ), f"{handler} does not touch {table}"

    http_eps = [
        n
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "http"
    ]
    assert len(http_eps) == 11

    # Same-named packages stay distinct.
    assert function_id("controllers/orders.Handler.Create") in graph.nodes
    assert function_id("controllers/users.Handler.Create") in graph.nodes
    assert function_id("repositories/orders.Repo.Create") in graph.nodes
    assert function_id("repositories/users.Repo.Create") in graph.nodes
    assert function_id("services/orders.Service.Create") in graph.nodes
    assert function_id("services/users.Service.Create") in graph.nodes

    create_orders = function_id("controllers/orders.Handler.Create")
    create_users = function_id("controllers/users.Handler.Create")
    assert _edge(graph, "calls", create_orders, function_id("services/orders.Service.Create"))
    assert _edge(graph, "calls", create_users, function_id("services/users.Service.Create"))
    assert not _edge(graph, "calls", create_orders, function_id("services/users.Service.Create"))
    assert not _edge(graph, "calls", create_users, function_id("services/orders.Service.Create"))


def test_clinic_no_false_call_edges(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    create = function_id("controllers/orders.Handler.Create")
    # Unscanned middleware.Current must not resolve to consent.Item.Current.
    assert _edge(graph, "calls", create, function_id("consent.Item.Current")) is None
    # Unknown Clock.Now must not resolve to exits.Service.Now.
    assert _edge(graph, "calls", create, function_id("exits.Service.Now")) is None
    # Those symbols still exist in the index.
    assert function_id("consent.Item.Current") in graph.nodes
    assert function_id("exits.Service.Now") in graph.nodes


def test_clinic_jobs_and_exclude_and_main(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    jobs = [
        n
        for n in graph.nodes.values()
        if n.type == "entrypoint" and n.attrs.get("kind") == "scheduled_job"
    ]
    symbols = {n.attrs.get("symbol") for n in jobs}
    assert any("ExpireJob.Run" in (s or "") for s in symbols)
    assert any(s == "jobs.Start" or (s or "").endswith(".Start") for s in symbols)
    assert any((s or "").endswith("runLoop") or s == "main.runLoop" for s in symbols)
    assert any("Poller.Run" in (s or "") for s in symbols)
    assert any("go.func" in (s or "") for s in symbols)
    assert len(jobs) >= 5

    # Seed / demo trees are excluded.
    for _nid, node in graph.nodes.items():
        ev = node.attrs.get("evidence") or {}
        file = ev.get("file") if isinstance(ev, dict) else ""
        assert "cmd/seed" not in str(file)
        assert not str(file).startswith("demo/")

    # cmd/server.main is not a writer entrypoint (dir-relative qualname).
    main_fn = function_id("cmd/server.main")
    assert not any(
        e.from_id == main_fn and e.type in {"reads", "writes"} for e in graph.edges.values()
    )


def test_clinic_phantoms_star_and_situations(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    table_names = {
        n.attrs.get("name") for n in graph.nodes.values() if n.type == "table"
    }
    assert "last" not in table_names
    assert "l" not in table_names
    assert "d" not in table_names
    assert "r" not in table_names
    assert "files" not in table_names
    assert "set" not in table_names
    assert not any(n.startswith("column:files.") for n in graph.nodes)
    assert not any(n.startswith("column:l.") for n in graph.nodes)
    assert "unknown" in table_names or "table:unknown" in graph.nodes

    pg = graph.get(table_id("pg_roles"))
    assert pg is not None
    assert pg.attrs.get("external") is True

    # SELECT * stays table-level at the origin and is not copied as columns to callers.
    list_users = function_id("repositories/users.Repo.List")
    col_edges = [
        e
        for e in graph.edges.values()
        if e.from_id == list_users and e.to_id.startswith("column:patients.")
    ]
    assert col_edges == []
    assert _edge(graph, "reads", list_users, table_id("patients")) is not None
    handler_list = function_id("controllers/users.Handler.List")
    assert _edge(graph, "reads", handler_list, table_id("patients")) is not None
    assert _edge(graph, "reads", handler_list, column_id("patients", "email")) is None

    # Const/iota arguments surface as situation on the function that received them.
    write_audit = function_id("repositories/orders.Repo.WriteAudit")
    audit_edges = [
        e
        for e in graph.edges.values()
        if e.from_id == write_audit and e.type == "writes" and "audit_log" in e.to_id
    ]
    assert audit_edges
    situations = []
    for e in audit_edges:
        sit = e.attrs.get("situation") or []
        if isinstance(sit, str):
            situations.append(sit)
        else:
            situations.extend(sit)
    assert any("AppointmentCanceled" in s for s in situations)
    assert any("AppointmentCreated" in s for s in situations)
    create_edges = [
        e
        for e in graph.edges.values()
        if e.from_id == function_id("repositories/orders.Repo.Create") and e.type == "writes"
    ]
    assert create_edges
    assert not any(e.attrs.get("situation") for e in create_edges)


def test_clinic_mermaid_rollup_is_readable(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    mmd = render_mermaid(graph)
    assert "flowchart LR" in mmd
    assert "POST /api/appointments" in mmd
    assert "appointments" in mmd
    # Default rollup does not dump every function/column.
    assert "subgraph functions" not in mmd
    focused = render_mermaid(graph, focus="appointments")
    assert "controllers/orders.Handler.Create" in focused or "Create" in focused
    assert len(mmd.splitlines()) < 80


def test_clinic_cross_package_const_paths(clinic_root: Path) -> None:
    graph = scan_path(clinic_root)
    setup = _http(graph, "POST", "/api/pending/setup")
    assert setup is not None
    assert "{" not in (setup.attrs.get("path") or "")
    prefs = _http(graph, "GET", "/email-prefs")
    assert prefs is not None
    # Truncated leftovers from dropping the const must not appear as their own routes.
    assert _http(graph, "POST", "/setup") is None
    assert _http(graph, "POST", "/api/setup") is None
    wrap = _http(graph, "GET", "/api/version")
    assert wrap is not None
    assert wrap.attrs.get("symbol") == "controllers/orders.Handler.Version"
    assert not any(
        n.attrs.get("method") == "OPTIONS" and "{c}" in str(n.attrs.get("path"))
        for n in graph.nodes.values()
        if n.type == "entrypoint"
    )
