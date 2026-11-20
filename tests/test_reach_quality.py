"""Round 38, part 2: DB triggers fire only on matching direct writes, data
links say where they come from, and three Go patterns no longer lose data."""

from __future__ import annotations

from pathlib import Path

from bizmap.report import build_report
from bizmap.scan import scan_path

GO_MOD = "module example.com/shop\n\ngo 1.22\n"


def _w(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


TRIGGER_SCHEMA = """
CREATE TABLE orders (id INT PRIMARY KEY, status TEXT, total INT);
CREATE TABLE audit (order_id INT, what TEXT);
CREATE TABLE order_log (order_id INT);
CREATE TABLE ledger (order_id INT);

CREATE OR REPLACE FUNCTION audit_status() RETURNS trigger AS $$
BEGIN
    INSERT INTO audit (order_id, what) VALUES (NEW.id, 'status');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_status AFTER UPDATE OF status ON orders
FOR EACH ROW EXECUTE FUNCTION audit_status();

CREATE OR REPLACE FUNCTION log_insert() RETURNS trigger AS $$
BEGIN
    INSERT INTO order_log (order_id) VALUES (NEW.id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_insert AFTER INSERT ON orders
FOR EACH ROW EXECUTE FUNCTION log_insert();

CREATE OR REPLACE FUNCTION ledger_from_log() RETURNS trigger AS $$
BEGIN
    INSERT INTO ledger (order_id) VALUES (NEW.order_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ledger AFTER INSERT ON order_log
FOR EACH ROW EXECUTE FUNCTION ledger_from_log();
"""

TRIGGER_REPO = """package store

import (
	"context"
	"database/sql"
)

type Store struct{ db *sql.DB }

func (s *Store) SetTotal(ctx context.Context, id, total int) error {
	_, err := s.db.ExecContext(ctx, "UPDATE orders SET total = $1 WHERE id = $2", total, id)
	return err
}

func (s *Store) SetStatus(ctx context.Context, id int, status string) error {
	_, err := s.db.ExecContext(ctx, "UPDATE orders SET status = $1 WHERE id = $2", status, id)
	return err
}

func (s *Store) Delete(ctx context.Context, id int) error {
	_, err := s.db.ExecContext(ctx, "DELETE FROM orders WHERE id = $1", id)
	return err
}

func (s *Store) Create(ctx context.Context, id int) error {
	_, err := s.db.ExecContext(ctx, "INSERT INTO orders (id, status, total) VALUES ($1, 'new', 0)", id)
	return err
}

func (s *Store) Pay(ctx context.Context, id int) error {
	return s.SetStatus(ctx, id, "paid")
}
"""


def _writers_of(graph, table: str) -> dict[str, tuple[str, str | None, str | None]]:
    out = {}
    for e in graph.edges.values():
        if e.type == "writes" and (e.to_id == f"table:{table}" or e.to_id.startswith(f"column:{table}.")):
            out[e.from_id] = (e.to_id, e.attrs.get("via"), e.attrs.get("derived"))
    return out


def test_triggers_fire_on_matching_direct_writes_only(tmp_path: Path) -> None:
    _w(tmp_path, "go.mod", GO_MOD)
    _w(tmp_path, "schema.sql", TRIGGER_SCHEMA)
    _w(tmp_path, "store/store.go", TRIGGER_REPO)
    graph = scan_path(tmp_path)

    audit = {k: v for k, v in _writers_of(graph, "audit").items() if k.startswith("function:store.")}
    # UPDATE OF status: SetStatus (direct) and Pay (its caller); not SetTotal / Delete / Create
    assert set(audit) == {"function:store.Store.SetStatus", "function:store.Store.Pay"}, audit
    assert audit["function:store.Store.SetStatus"] == ("table:audit", "db_trigger:trg_status", None)
    assert audit["function:store.Store.Pay"] == ("table:audit", "db_trigger:trg_status", "call")

    log = {k for k in _writers_of(graph, "order_log") if k.startswith("function:store.")}
    assert log == {"function:store.Store.Create"}, log  # AFTER INSERT only

    # chained: INSERT INTO order_log fires trg_ledger → ledger
    ledger = {k for k in _writers_of(graph, "ledger") if k.startswith("function:store.")}
    assert ledger == {"function:store.Store.Create"}, ledger

    # no column-level copies onto the application code
    assert not any(
        e.to_id.startswith("column:audit.") and e.from_id.startswith("function:store.") for e in graph.edges.values()
    )


def test_direct_and_derived_links_are_told_apart(tmp_path: Path) -> None:
    _w(tmp_path, "go.mod", GO_MOD)
    _w(tmp_path, "schema.sql", TRIGGER_SCHEMA)
    _w(tmp_path, "store/store.go", TRIGGER_REPO)
    graph = scan_path(tmp_path)
    direct = graph.edges.get("writes:function:store.Store.SetStatus->column:orders.status")
    assert direct is not None and "derived" not in direct.attrs and direct.attrs.get("ops") == ["UPDATE"]
    rep = build_report(graph.to_dict())
    links = rep["data_links"]
    assert links["derived_call"] >= 1 and links["db_trigger"] >= 3 and links["direct"]


NODATA = """package api

import (
	"context"
	"database/sql"

	"github.com/labstack/echo/v4"
)

type Repo struct{ db *sql.DB }

func newCartRepo(db *sql.DB) *Repo { return &Repo{db: db} }

func (r *Repo) List(ctx context.Context) error {
	_, err := r.db.QueryContext(ctx, "SELECT id FROM carts")
	return err
}

func (r *Repo) Submit(ctx context.Context) error {
	_, err := r.db.ExecContext(ctx, "UPDATE marks SET done = true")
	return err
}

// (b) handler factory whose argument is itself a call
func listCarts(repo *Repo) echo.HandlerFunc {
	return func(c echo.Context) error { return repo.List(c.Request().Context()) }
}

// (a) a method passed as a value
type Service struct{ repo *Repo }

func (s *Service) SubmitMarks(ctx context.Context) error { return s.repo.Submit(ctx) }

type Controller struct{ svc *Service }

func (c *Controller) observe(ctx context.Context, f func(context.Context) error) error { return f(ctx) }

func (c *Controller) Submit(ec echo.Context) error {
	return c.observe(ec.Request().Context(), c.svc.SubmitMarks)
}

func Register(e *echo.Echo, db *sql.DB, ctl *Controller) {
	e.GET("/carts", listCarts(newCartRepo(db)))
	e.POST("/marks", ctl.Submit)
}
"""


def test_factory_with_call_argument_and_method_values(tmp_path: Path) -> None:
    _w(tmp_path, "go.mod", GO_MOD)
    _w(tmp_path, "schema.sql", "CREATE TABLE carts (id INT); CREATE TABLE marks (done BOOL);\n")
    _w(tmp_path, "api/api.go", NODATA)
    graph = scan_path(tmp_path)
    route = graph.nodes["entrypoint:http:GET:/carts"]
    trig = {
        e.to_id: (e.confidence.value, e.attrs.get("via"))
        for e in graph.edges.values()
        if e.type == "triggers" and e.from_id == route.id
    }
    assert trig == {
        "function:api.listCarts": ("exact", None),
        "function:api.newCartRepo": ("inferred", "handler_factory"),
    }, trig
    calls = {(e.from_id, e.to_id): e.confidence.value for e in graph.edges.values() if e.type == "calls"}
    assert calls.get(("function:api.Controller.Submit", "function:api.Service.SubmitMarks")) == "inferred"
    reached = {e.to_id for e in graph.edges.values() if e.from_id == "entrypoint:http:POST:/marks" and e.type == "writes"}
    assert any("marks" in t for t in reached), reached


DECORATOR = """package orders

import "context"

type Store interface {
	Save(ctx context.Context) error
}

// Service implements Store too: it wraps the real store (not scanned here)
type Service struct{ repo Store }

func (s *Service) Save(ctx context.Context) error { return s.repo.Save(ctx) }
"""

UNSCANNED_IFACE = """package orders

import (
	"context"

	"example.com/shop/internal/billing"
)

type Payer struct{ client billing.Client }

func (p *Payer) Pay(ctx context.Context) error { return p.client.Charge(ctx) }

func helper() int { return 1 }

func (p *Payer) Total() int { return helper() }
"""


def test_decorator_and_unscanned_types_are_reported(tmp_path: Path) -> None:
    _w(tmp_path, "go.mod", GO_MOD)
    _w(tmp_path, "internal/orders/service.go", DECORATOR)
    _w(tmp_path, "internal/orders/payer.go", UNSCANNED_IFACE)
    graph = scan_path(tmp_path)
    save = graph.nodes["function:internal/orders.Service.Save"]
    assert save.attrs.get("unresolved_calls") == ["Store.Save"]
    assert (save.attrs.get("unresolved_call_reasons") or {}).get("Store.Save", "no_scanned_implementer") == "no_scanned_implementer"
    pay = graph.nodes["function:internal/orders.Payer.Pay"]
    assert pay.attrs.get("unresolved_calls") == ["Client.Charge"]
    assert pay.attrs["unresolved_call_reasons"]["Client.Charge"] == "type_not_scanned"
    # same-package call by exact name is exact
    edge = graph.edges["calls:function:internal/orders.Payer.Total->function:internal/orders.helper"]
    assert edge.confidence.value == "exact"


def test_report_explains_entrypoints_without_data(tmp_path: Path) -> None:
    data = {
        "nodes": [
            {"id": "entrypoint:http:GET:/a", "type": "entrypoint", "kind": "http", "method": "GET", "path": "/a"},
            {"id": "entrypoint:http:GET:/b", "type": "entrypoint", "kind": "http", "method": "GET", "path": "/b"},
            {"id": "entrypoint:http:GET:/c", "type": "entrypoint", "kind": "http", "method": "GET", "path": "/c"},
            {"id": "entrypoint:http:GET:/d", "type": "entrypoint", "kind": "http", "method": "GET", "path": "/d"},
            {"id": "entrypoint:http:GET:/x", "type": "entrypoint", "kind": "http", "method": "GET", "path": "/x"},
            {"id": "function:b", "type": "function", "unresolved_calls": ["Store.Get"]},
            {"id": "function:c", "type": "function"},
            {"id": "function:d", "type": "function"},
            {"id": "function:x", "type": "function"},
            {"id": "table:orders", "type": "table"},
        ],
        "edges": [
            {"type": "triggers", "from": "entrypoint:http:GET:/b", "to": "function:b"},
            {"type": "triggers", "from": "entrypoint:http:GET:/c", "to": "function:c"},
            {"type": "triggers", "from": "entrypoint:http:GET:/d", "to": "function:d"},
            {"type": "triggers", "from": "entrypoint:http:GET:/x", "to": "function:x"},
            {"type": "calls_http", "from": "function:c", "to": "entrypoint:http:GET:/x"},
            {"type": "writes", "from": "entrypoint:http:GET:/x", "to": "table:orders", "confidence": "inferred", "derived": "call"},
            {"type": "writes", "from": "function:x", "to": "table:orders", "confidence": "exact"},
        ],
    }
    rep = build_report(data)
    ep = rep["entrypoints"]
    assert ep["without_data_by_reason"] == {
        "handler_not_resolved": 1,
        "no_data_access": 1,
        "only_http": 1,
        "unresolved_call": 1,
    }
    assert ep["reaching_data_via_http"] == 1
    assert rep["data_links"] == {"direct": {"exact": 1}, "derived_call": 1, "db_trigger": 0}
    assert rep["unresolved_calls"] == 1


SAME_NAME = """package shop

import (
	"context"

	"github.com/labstack/echo/v4"
)

type orderReader interface {
	OrderStatus(ctx context.Context, id string) (string, error)
}

type OrderService struct{ repo orderReader }

func (s *OrderService) OrderStatus(ctx context.Context, id string) (string, error) {
	return s.repo.OrderStatus(ctx, id)
}

type Controller struct{ svc *OrderService }

func (c *Controller) OrderStatus(ctx echo.Context) error {
	_, err := c.svc.OrderStatus(ctx.Request().Context(), ctx.Param("id"))
	return err
}
"""


def test_interface_needs_matching_signatures(tmp_path: Path) -> None:
    """Round 39: the controller's OrderStatus(echo.Context) is not an
    implementer of orderReader just because the method name matches."""
    _w(tmp_path, "go.mod", GO_MOD)
    _w(tmp_path, "shop/shop.go", SAME_NAME)
    graph = scan_path(tmp_path)
    bad = "calls:function:shop.OrderService.OrderStatus->function:shop.Controller.OrderStatus"
    assert bad not in graph.edges
    svc = graph.nodes["function:shop.OrderService.OrderStatus"]
    assert svc.attrs.get("unresolved_calls") == ["orderReader.OrderStatus"]


STREAM = """package api

import (
	"database/sql"

	"github.com/labstack/echo/v4"
)

type Hub struct{}

func visibleTo(db *sql.DB) func() error {
	return func() error {
		_, err := db.Query("SELECT id FROM events")
		return err
	}
}

func streamEvents(h *Hub, all bool, filter func() error) echo.HandlerFunc {
	return func(c echo.Context) error { return filter() }
}

func Register(e *echo.Echo, h *Hub, db *sql.DB) {
	e.GET("/events", streamEvents(h, false, visibleTo(db)))
}
"""


def test_factory_arguments_bring_their_data(tmp_path: Path) -> None:
    _w(tmp_path, "go.mod", GO_MOD)
    _w(tmp_path, "schema.sql", "CREATE TABLE events (id INT);\n")
    _w(tmp_path, "api/api.go", STREAM)
    graph = scan_path(tmp_path)
    route = "entrypoint:http:GET:/events"
    handlers = {e.to_id for e in graph.edges.values() if e.type == "triggers" and e.from_id == route}
    assert handlers == {"function:api.streamEvents", "function:api.visibleTo"}, handlers
    reads = {e.to_id for e in graph.edges.values() if e.type == "reads" and e.from_id == route}
    assert any("events" in t for t in reads), reads
