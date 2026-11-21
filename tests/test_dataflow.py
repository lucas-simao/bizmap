"""Data flow between databases: `feeds` edges, `column_map`, `db`.

Repro (fictional): service `order-sync` reads `pedidos` from database A and
upserts `resumo_pedido` in database B with renamed columns; a monthly rollup
in the same database; a queue variant (producer and consumer in two repos);
a Firestore mirror.

The unit tests build the graph the detectors would emit (function spans and
reads/writes edges) and run only the text pass, so they need no tree-sitter.
The `test_scan_*` tests run the whole scan and need the parsers installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bizmap import dataflow, dbident
from bizmap.graph import Evidence, Graph
from bizmap.registry import merge_graphs
from bizmap.srctext import source_texts

# ── repro sources ─────────────────────────────────────────────────────────────

GO_SYNC = """package sync

import (
\t"context"
\t"database/sql"
\t"os"
\t"time"
)

var src, _ = sql.Open("pgx", os.Getenv("ORDERS_DATABASE_URL"))
var dst, _ = sql.Open("pgx", os.Getenv("SUMMARY_DATABASE_URL"))

type Summary struct{ OrderID string; Total int64; Customer string; PlacedAt time.Time }

func SyncOrders(ctx context.Context) error {
\trows, _ := src.QueryContext(ctx, `SELECT id, valor_centavos, cliente_id, criado_em FROM pedidos WHERE status = 'pago'`)
\tfor rows.Next() {
\t\tvar id, cliente string; var valor int64; var criado time.Time
\t\trows.Scan(&id, &valor, &cliente, &criado)
\t\ts := Summary{OrderID: id, Total: valor, Customer: cliente, PlacedAt: criado}
\t\tdst.ExecContext(ctx, `INSERT INTO resumo_pedido (pedido_id, total, cliente, data_pedido) VALUES ($1,$2,$3,$4)
\t\t\tON CONFLICT (pedido_id) DO UPDATE SET total = EXCLUDED.total`, s.OrderID, s.Total, s.Customer, s.PlacedAt)
\t}
\treturn nil
}

func RollupMonthly(ctx context.Context) error {
\t_, err := src.ExecContext(ctx, `INSERT INTO resumo_mensal (mes, receita) SELECT date_trunc('month', criado_em), sum(valor_centavos) FROM pedidos GROUP BY 1`)
\treturn err
}
"""

TS_SYNC = """import { Pool } from 'pg';
import { getFirestore } from 'firebase-admin/firestore';

const a = new Pool({ connectionString: process.env.ORDERS_DATABASE_URL });
const b = new Pool({ connectionString: process.env.SUMMARY_DATABASE_URL });

export async function syncOrders() {
  const { rows } = await a.query('SELECT id, valor_centavos, cliente_id, criado_em FROM pedidos');
  for (const p of rows) {
    await b.query(
      'INSERT INTO resumo_pedido (pedido_id, total, cliente, data_pedido) VALUES ($1, $2, $3, $4)',
      [p.id, p.valor_centavos, p.cliente_id, p.criado_em]);
  }
}

export async function mirrorOrders() {
  const { rows } = await a.query('SELECT id, valor_centavos, cliente_id FROM pedidos');
  for (const p of rows) {
    await getFirestore().collection('resumos').doc(p.id).set({ total: p.valor_centavos, cliente: p.cliente_id });
  }
}
"""

TS_PRODUCER = """import { Pool } from 'pg';
import { queue } from './queue';

const a = new Pool({ connectionString: process.env.ORDERS_DATABASE_URL });

export async function onOrderPaid(id: string) {
  const o = (await a.query('SELECT id, valor_centavos FROM pedidos WHERE id = $1', [id])).rows[0];
  await queue.publish('pedido-pago', { orderId: o.id, amount: o.valor_centavos });
}
"""

TS_CONSUMER = """import { Pool } from 'pg';
import { queue } from './queue';

const b = new Pool({ connectionString: process.env.SUMMARY_DATABASE_URL });

queue.subscribe('pedido-pago', async (msg: { orderId: string; amount: number }) => {
  await b.query('INSERT INTO resumo_pedido (pedido_id, total) VALUES ($1, $2)', [msg.orderId, msg.amount]);
});
"""

PEDIDOS = ["id", "valor_centavos", "cliente_id", "criado_em", "status"]
RESUMO = ["pedido_id", "total", "cliente", "data_pedido"]
MENSAL = ["mes", "receita"]

SCHEMA_SQL = """
CREATE TABLE pedidos (id text PRIMARY KEY, valor_centavos bigint, cliente_id text, criado_em timestamptz, status text);
CREATE TABLE resumo_pedido (pedido_id text PRIMARY KEY, total bigint, cliente text, data_pedido timestamptz);
CREATE TABLE resumo_mensal (mes timestamptz, receita bigint);
"""


# ── helpers ───────────────────────────────────────────────────────────────────


def _line(src: str, needle: str, nth: int = 1) -> int:
    pos = -1
    for _ in range(nth):
        pos = src.index(needle, pos + 1)
    return src.count("\n", 0, pos) + 1


def _span(src: str, start_needle: str) -> tuple[int, int]:
    """Lines of the definition starting at ``start_needle`` (to its closing brace)."""
    start = _line(src, start_needle)
    lines = src.split("\n")
    depth = 0
    seen = False
    for k in range(start - 1, len(lines)):
        depth += lines[k].count("{") - lines[k].count("}")
        seen = seen or "{" in lines[k]
        if seen and depth <= 0:
            return start, k + 1
    return start, len(lines)


def _py_span(src: str, needle: str) -> tuple[int, int]:
    start = _line(src, needle)
    lines = src.split("\n")
    end = start
    for k in range(start, len(lines)):
        if lines[k].strip() and not lines[k].startswith((" ", "\t")):
            break
        if lines[k].strip():
            end = k + 1
    return start, end


def _graph(
    root: Path,
    files: dict[str, str],
    fns: list[tuple[str, str, str]],
    edges: list[tuple[str, str, str, str, str]],
    tables: dict[str, list[str]],
    *,
    repo: str | None = None,
    raw_config: dict | None = None,
) -> Graph:
    """Graph as the detectors emit it: fns = (id, file, definition start text);
    edges = (type, from, to, file, text on the evidence line)."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    g = Graph()
    g.repo = repo
    g.attrs["repo"] = repo
    g.schema = {"tables": {t: {"columns": {c: {"type": "text", "nullable": True} for c in cols}} for t, cols in tables.items()}}
    for t, cols in tables.items():
        g.add_node(f"table:{t}", "table", name=t)
        for c in cols:
            g.add_node(f"column:{t}.{c}", "column", table=t, name=c, data_type="text", nullable=True)
    for fid, rel, needle in fns:
        src = files[rel]
        if needle == "<module>":
            start, end = 1, src.count("\n") + 1
        else:
            start, end = (_py_span if rel.endswith(".py") else _span)(src, needle)
        g.add_node(
            fid, "function", name=fid.split(":", 1)[1], language={"go": "go", "py": "python"}.get(rel.rsplit(".", 1)[-1], "javascript"),
            evidence={"file": rel, "line": start, "symbol": fid.split(":", 1)[1]}, end_line=end,
        )
    for etype, frm, to, rel, needle in edges:
        if to.startswith("collection:") and to not in g.nodes:
            g.add_node(to, "collection", name=to.split(":", 1)[1], kind="collection")
        line = _line(files[rel], needle) if needle else 1
        g.add_edge(etype, frm, to, evidence=Evidence(rel, line, frm.split(":", 1)[1]))
    texts = source_texts(root, [root / r for r in files])
    idx = dbident.build_index(texts, raw_config or {})
    dbident.assign_dbs(g, texts, idx)
    dataflow.apply(g, root, texts, firestore=idx.firestore or False)
    return g


def _feeds(g: Graph) -> dict[tuple[str, str], tuple[str, set[tuple]]]:
    """(from, to) -> (confidence, {(from col, to col, confidence, transform)})."""
    out = {}
    for e in g.edges.values():
        if e.type != "feeds":
            continue
        cols = {
            (c["from"].split(".", 1)[1], c["to"].split(".", 1)[1], c["confidence"], bool(c.get("transform")))
            for c in e.attrs["column_map"]
        }
        out[(e.from_id, e.to_id)] = (e.confidence.value, cols)
    return out


def _go_graph(tmp_path: Path) -> Graph:
    f = "sync/orders.go"
    s, r = "function:sync.SyncOrders", "function:sync.RollupMonthly"
    return _graph(
        tmp_path / "go",
        {f: GO_SYNC},
        [(s, f, "func SyncOrders"), (r, f, "func RollupMonthly")],
        [
            ("reads", s, "column:pedidos.id", f, "src.QueryContext"),
            ("reads", s, "column:pedidos.status", f, "src.QueryContext"),
            ("writes", s, "column:resumo_pedido.total", f, "dst.ExecContext"),
            ("writes", s, "column:resumo_pedido.pedido_id", f, "dst.ExecContext"),
            # as the SQL detector emits it today: the INSERT … SELECT's writes, no reads of pedidos
            ("writes", r, "column:resumo_mensal.mes", f, "INSERT INTO resumo_mensal"),
            ("writes", r, "column:resumo_mensal.receita", f, "INSERT INTO resumo_mensal"),
        ],
        {"pedidos": PEDIDOS, "resumo_pedido": RESUMO, "resumo_mensal": MENSAL},
    )


SYNC_MAP = {
    ("valor_centavos", "total", "exact", False),
    ("id", "pedido_id", "exact", False),
    ("cliente_id", "cliente", "exact", False),
    ("criado_em", "data_pedido", "exact", False),
}


# ── the repro ─────────────────────────────────────────────────────────────────


def test_go_sync_and_sql_rollup(tmp_path: Path) -> None:
    g = _go_graph(tmp_path)
    assert _feeds(g) == {
        ("table:pedidos", "table:resumo_pedido"): ("exact", SYNC_MAP),
        ("table:pedidos", "table:resumo_mensal"): (
            "exact",
            {("criado_em", "mes", "heuristic", True), ("valor_centavos", "receita", "heuristic", True)},
        ),
    }
    edge = g.edges["feeds:table:pedidos->table:resumo_pedido"]
    assert edge.attrs["via"][0]["path"] == ["function:sync.SyncOrders"]
    assert edge.attrs["via_total"] == 1
    assert edge.evidence.file == "sync/orders.go"
    assert {c["fn"] for c in edge.attrs["column_map"]} == {"function:sync.SyncOrders"}
    # `status` is only in the WHERE clause
    assert not any("status" in c["from"] for e in g.edges.values() if e.type == "feeds" for c in e.attrs["column_map"])
    assert g.nodes["table:pedidos"].attrs["db"] == "orders_db"
    assert g.nodes["table:resumo_pedido"].attrs["db"] == "summary_db"
    assert g.nodes["table:resumo_mensal"].attrs["db"] == "orders_db"


def _ts_graph(tmp_path: Path) -> Graph:
    f = "src/sync.ts"
    s, m = "function:src/sync.syncOrders", "function:src/sync.mirrorOrders"
    return _graph(
        tmp_path / "ts",
        {f: TS_SYNC},
        [(s, f, "export async function syncOrders"), (m, f, "export async function mirrorOrders")],
        [
            ("reads", s, "column:pedidos.id", f, "a.query('SELECT id, valor_centavos, cliente_id, criado_em"),
            ("writes", s, "column:resumo_pedido.total", f, "'INSERT INTO resumo_pedido"),
            ("reads", m, "column:pedidos.id", f, "a.query('SELECT id, valor_centavos, cliente_id FROM"),
        ],
        {"pedidos": PEDIDOS, "resumo_pedido": RESUMO},
    )


def test_ts_sync_and_firestore_mirror(tmp_path: Path) -> None:
    g = _ts_graph(tmp_path)
    assert _feeds(g) == {
        ("table:pedidos", "table:resumo_pedido"): ("exact", SYNC_MAP),
        ("table:pedidos", "collection:resumos"): (
            "exact",
            {("valor_centavos", "total", "exact", False), ("cliente_id", "cliente", "exact", False)},
        ),
    }
    assert g.nodes["table:pedidos"].attrs["db"] == "orders_db"
    assert g.nodes["table:resumo_pedido"].attrs["db"] == "summary_db"
    assert g.nodes["collection:resumos"].attrs["db"] == "firestore:default"


def _queue_graphs(tmp_path: Path) -> tuple[Graph, Graph]:
    fp, fc = "src/orders.ts", "src/consumer.ts"
    prod = "function:src/orders.onOrderPaid"
    cons = "function:src/consumer.module"
    gp = _graph(
        tmp_path / "pedidos",
        {fp: TS_PRODUCER},
        [(prod, fp, "export async function onOrderPaid")],
        [("reads", prod, "column:pedidos.id", fp, "a.query(")],
        {"pedidos": PEDIDOS},
        repo="pedidos",
    )
    gc = _graph(
        tmp_path / "resumo",
        {fc: TS_CONSUMER},
        [(cons, fc, "<module>")],
        [("writes", cons, "column:resumo_pedido.total", fc, "b.query(")],
        {"resumo_pedido": RESUMO},
        repo="resumo",
    )
    return gp, gc


def test_queue_producer_and_consumer_in_two_repos(tmp_path: Path) -> None:
    gp, gc = _queue_graphs(tmp_path)
    assert not any(e.type == "feeds" for e in gp.edges.values())  # nothing to link alone
    assert gp.nodes["function:src/orders.onOrderPaid"].attrs["dataflow_out"][0]["topic"] == "pedido-pago"
    assert gc.nodes["function:src/consumer.module"].attrs["dataflow_in"][0]["topic"] == "pedido-pago"
    merged = merge_graphs([gp, gc])
    dataflow.link_queue_flows(merged)
    dbident.link_same_tables(merged)
    assert _feeds(merged) == {
        ("pedidos/table:pedidos", "resumo/table:resumo_pedido"): (
            "heuristic",
            {("id", "pedido_id", "heuristic", False), ("valor_centavos", "total", "heuristic", False)},
        ),
    }
    edge = merged.edges["feeds:pedidos/table:pedidos->resumo/table:resumo_pedido"]
    assert edge.attrs["via"][0]["path"] == [
        "pedidos/function:src/orders.onOrderPaid",
        "queue:topic:pedido-pago",
        "resumo/function:src/consumer.module",
    ]
    assert {c.get("field") for c in edge.attrs["column_map"]} == {"orderId", "amount"}
    assert merged.nodes["pedidos/table:pedidos"].attrs["db"] == "orders_db"
    assert merged.nodes["resumo/table:resumo_pedido"].attrs["db"] == "summary_db"
    # linking again (registry relink) adds nothing
    before = json.dumps(merged.to_dict()["edges"], sort_keys=True)
    dataflow.link_queue_flows(merged)
    assert json.dumps(merged.to_dict()["edges"], sort_keys=True) == before


def test_merge_keeps_feeds_and_marks_same_table(tmp_path: Path) -> None:
    g1 = _go_graph(tmp_path)
    g1.repo = g1.attrs["repo"] = "order-sync"
    gp, gc = _queue_graphs(tmp_path)
    merged = merge_graphs([g1, gp, gc])
    dataflow.link_queue_flows(merged)
    dbident.link_same_tables(merged)
    e = merged.edges["feeds:order-sync/table:pedidos->order-sync/table:resumo_pedido"]
    assert e.attrs["via"][0]["path"] == ["order-sync/function:sync.SyncOrders"]
    assert {c["fn"] for c in e.attrs["column_map"]} == {"order-sync/function:sync.SyncOrders"}
    assert merged.nodes["order-sync/table:pedidos"].attrs["same_as"] == ["pedidos/table:pedidos"]
    assert merged.nodes["resumo/table:resumo_pedido"].attrs["same_as"] == ["order-sync/table:resumo_pedido"]


def test_same_repo_queue_and_direct_sync_share_one_edge(tmp_path: Path) -> None:
    fp, fc = "src/orders.ts", "src/consumer.ts"
    prod, cons = "function:src/orders.onOrderPaid", "function:src/consumer.module"
    g = _graph(
        tmp_path / "one",
        {fp: TS_PRODUCER, fc: TS_CONSUMER},
        [(prod, fp, "export async function onOrderPaid"), (cons, fc, "<module>")],
        [
            ("reads", prod, "column:pedidos.id", fp, "a.query("),
            ("writes", cons, "column:resumo_pedido.total", fc, "b.query("),
        ],
        {"pedidos": PEDIDOS, "resumo_pedido": RESUMO},
    )
    assert _feeds(g) == {
        ("table:pedidos", "table:resumo_pedido"): (
            "heuristic",
            {("id", "pedido_id", "heuristic", False), ("valor_centavos", "total", "heuristic", False)},
        ),
    }
    assert g.nodes["queue:topic:pedido-pago"].attrs["kind"] == "topic"


# ── Python and calls ──────────────────────────────────────────────────────────

PY_SYNC = '''import os
import psycopg2

src = psycopg2.connect(os.environ["ORDERS_DATABASE_URL"])
dst = psycopg2.connect(os.environ["SUMMARY_DATABASE_URL"])


def sync_orders():
    cur = src.cursor()
    cur.execute("SELECT id, valor_centavos, cliente_id FROM pedidos WHERE status = 'pago'")
    out = dst.cursor()
    for row in cur.fetchall():
        out.execute(
            "INSERT INTO resumo_pedido (pedido_id, total, cliente) VALUES (%s, %s, %s)",
            (row["id"], round(row["valor_centavos"] / 100), row["cliente_id"]),
        )
'''


def test_python_cursor_rows(tmp_path: Path) -> None:
    f = "svc.py"
    fid = "function:svc.sync_orders"
    g = _graph(
        tmp_path / "py",
        {f: PY_SYNC},
        [(fid, f, "def sync_orders")],
        [
            ("reads", fid, "column:pedidos.id", f, 'cur.execute("SELECT'),
            ("writes", fid, "column:resumo_pedido.total", f, "out.execute("),
        ],
        {"pedidos": PEDIDOS, "resumo_pedido": RESUMO},
    )
    assert _feeds(g) == {
        ("table:pedidos", "table:resumo_pedido"): (
            "exact",
            {
                ("id", "pedido_id", "exact", False),
                ("cliente_id", "cliente", "exact", False),
                ("valor_centavos", "total", "heuristic", True),
            },
        ),
    }
    assert g.nodes["table:pedidos"].attrs["db"] == "orders_db"
    assert g.nodes["table:resumo_pedido"].attrs["db"] == "summary_db"


GO_CHAIN = """package repo

func (r *Repo) ListPaid(ctx context.Context) ([]Order, error) {
\trows, err := r.db.QueryContext(ctx, `SELECT id, valor_centavos FROM pedidos`)
\tvar out []Order
\tfor rows.Next() {
\t\tvar o Order
\t\tif err := rows.Scan(&o.ID, &o.Total); err != nil {
\t\t\treturn nil, err
\t\t}
\t\tout = append(out, o)
\t}
\treturn out, nil
}

func (s *Service) Sync(ctx context.Context) error {
\torders, err := s.repo.ListPaid(ctx)
\tfor _, o := range orders {
\t\ts.writeSummary(ctx, o)
\t}
\treturn nil
}

func (s *Service) writeSummary(ctx context.Context, o Order) error {
\t_, err := s.out.ExecContext(ctx, "INSERT INTO resumo_pedido (pedido_id, total) VALUES ($1, $2)", o.ID, o.Total)
\treturn err
}
"""


def test_reader_and_writer_linked_through_calls(tmp_path: Path) -> None:
    f = "repo/repo.go"
    lst, sync, wr = "function:repo.Repo.ListPaid", "function:repo.Service.Sync", "function:repo.Service.writeSummary"
    g = _graph(
        tmp_path / "chain",
        {f: GO_CHAIN},
        [(lst, f, "func (r *Repo) ListPaid"), (sync, f, "func (s *Service) Sync"), (wr, f, "func (s *Service) writeSummary")],
        [
            ("reads", lst, "column:pedidos.id", f, "r.db.QueryContext"),
            ("calls", sync, lst, f, "s.repo.ListPaid"),
            ("calls", sync, wr, f, "s.writeSummary"),
            ("writes", wr, "column:resumo_pedido.total", f, "s.out.ExecContext"),
        ],
        {"pedidos": PEDIDOS, "resumo_pedido": RESUMO},
    )
    # the edge is heuristic (through calls); the values go through unchanged, so the columns are exact
    assert _feeds(g) == {
        ("table:pedidos", "table:resumo_pedido"): (
            "heuristic",
            {("id", "pedido_id", "exact", False), ("valor_centavos", "total", "exact", False)},
        ),
    }
    edge = g.edges["feeds:table:pedidos->table:resumo_pedido"]
    assert edge.attrs["via"][0]["path"] == [lst, sync, wr]


# ── no false feeds ────────────────────────────────────────────────────────────

NEGATIVE = """package svc

func (s *Svc) CreateOrder(ctx context.Context, userID string, total int64) error {
\tvar admin bool
\ts.db.QueryRowContext(ctx, `SELECT admin FROM users WHERE id = $1`, userID).Scan(&admin)
\tif !admin {
\t\treturn errForbidden
\t}
\t_, err := s.db.ExecContext(ctx, `INSERT INTO pedidos (id, valor_centavos) VALUES ($1, $2)`, newID(), total)
\treturn err
}

func (s *Svc) Assign(ctx context.Context) error {
\tvar uid string
\ts.db.QueryRowContext(ctx, `SELECT id FROM users WHERE email = $1`, "x").Scan(&uid)
\t_, err := s.db.ExecContext(ctx, `UPDATE pedidos SET cliente_id = $1 WHERE id = $2`, uid, "p1")
\treturn err
}

func (s *Svc) Bump(ctx context.Context) error {
\tvar v int64
\ts.db.QueryRowContext(ctx, `SELECT valor_centavos FROM pedidos WHERE id = $1`, "p").Scan(&v)
\t_, err := s.db.ExecContext(ctx, `UPDATE pedidos SET valor_centavos = $1 WHERE id = $2`, v+1, "p")
\treturn err
}

func (s *Svc) Dynamic(ctx context.Context, table string) error {
\tvar v int64
\ts.db.QueryRowContext(ctx, `SELECT valor_centavos FROM pedidos`).Scan(&v)
\t_, err := s.db.ExecContext(ctx, "INSERT INTO "+table+" (total) VALUES ($1)", v)
\treturn err
}
"""


def test_lookups_references_and_self_updates_are_not_feeds(tmp_path: Path) -> None:
    f = "svc/svc.go"
    c, a, b, d = (f"function:svc.Svc.{n}" for n in ("CreateOrder", "Assign", "Bump", "Dynamic"))
    g = _graph(
        tmp_path / "neg",
        {f: NEGATIVE},
        [(c, f, "func (s *Svc) CreateOrder"), (a, f, "func (s *Svc) Assign"), (b, f, "func (s *Svc) Bump"), (d, f, "func (s *Svc) Dynamic")],
        [
            ("reads", c, "column:users.admin", f, "SELECT admin"),
            ("writes", c, "column:pedidos.valor_centavos", f, "INSERT INTO pedidos"),
            ("reads", a, "column:users.id", f, "SELECT id FROM users"),
            ("writes", a, "column:pedidos.cliente_id", f, "UPDATE pedidos SET cliente_id"),
            ("reads", b, "column:pedidos.valor_centavos", f, "SELECT valor_centavos FROM pedidos WHERE"),
            ("writes", b, "column:pedidos.valor_centavos", f, "UPDATE pedidos SET valor_centavos"),
            ("reads", d, "column:pedidos.valor_centavos", f, "SELECT valor_centavos FROM pedidos`"),
            ("writes", d, "table:unknown", f, '"INSERT INTO "'),
        ],
        {"pedidos": PEDIDOS, "users": ["id", "admin", "email"], "resumo_pedido": RESUMO},
    )
    assert _feeds(g) == {}
    assert not any(n.attrs.get("dataflow_out") or n.attrs.get("dataflow_in") for n in g.nodes.values())


def test_no_named_connection_leaves_db_out(tmp_path: Path) -> None:
    f = "svc/svc.go"
    g = _graph(tmp_path / "nodb", {f: NEGATIVE}, [], [], {"pedidos": PEDIDOS})
    assert "db" not in g.nodes["table:pedidos"].attrs


# ── database names ────────────────────────────────────────────────────────────


def test_databases_in_bizmap_yaml_name_the_connections(tmp_path: Path) -> None:
    src = GO_SYNC.replace("ORDERS_DATABASE_URL", "PG_A").replace("SUMMARY_DATABASE_URL", "PG_B")
    f = "sync/orders.go"
    s = "function:sync.SyncOrders"
    g = _graph(
        tmp_path / "yaml",
        {f: src},
        [(s, f, "func SyncOrders")],
        [
            ("reads", s, "column:pedidos.id", f, "src.QueryContext"),
            ("writes", s, "column:resumo_pedido.total", f, "dst.ExecContext"),
        ],
        {"pedidos": PEDIDOS, "resumo_pedido": RESUMO},
        raw_config={"databases": {"vendas": {"env": "PG_A"}, "painel": "PG_B"}},
    )
    assert g.nodes["table:pedidos"].attrs["db"] == "vendas"
    assert g.nodes["table:resumo_pedido"].attrs["db"] == "painel"


# ── SQL mini-parser ───────────────────────────────────────────────────────────


def test_sql_parser_shapes() -> None:
    kind, sel = dataflow.parse_sql("SELECT p.id, p.valor AS total, sum(x) receita FROM pedidos p JOIN itens i ON i.p = p.id")
    assert kind == "select"
    assert [(i.name, i.refs, i.transform) for i in sel.items] == [
        ("id", [("p", "id")], False),
        ("total", [("p", "valor")], False),
        ("receita", [(None, "x")], True),
    ]
    assert sel.tables["p"] == "pedidos" and sel.tables["i"] == "itens"
    kind, w = dataflow.parse_sql("INSERT INTO t (a, b) VALUES (?, ?) ON CONFLICT (a) DO UPDATE SET b = EXCLUDED.b")
    assert kind == "write" and {k: v.params for k, v in w.cols.items()} == {"a": [1], "b": [2]}
    kind, w = dataflow.parse_sql("UPDATE resumo r SET total = p.valor FROM pedidos p WHERE p.id = r.pedido_id")
    assert w.table == "resumo" and w.cols["total"].refs == [("p", "valor")] and w.from_tables["p"] == "pedidos"
    kind, w = dataflow.parse_sql("insert into t (a) values (%(x)s)")
    assert w.cols["a"].params == ["x"]


# ── outputs ───────────────────────────────────────────────────────────────────


def test_why_shows_where_a_column_comes_from_and_goes(tmp_path: Path) -> None:
    from bizmap.why import format_why

    g = _go_graph(tmp_path)
    text = format_why(g, "resumo_pedido.total")
    assert text.endswith(
        "comes from (data flow):\n"
        f"  ← orders_db.pedidos.valor_centavos via sync.SyncOrders (exact, sync/orders.go:{_line(GO_SYNC, 'dst.ExecContext')})\n"
    )
    text = format_why(g, "pedidos.valor_centavos")
    assert "goes to (data flow):\n" in text
    assert "  → orders_db.resumo_mensal.receita via sync.RollupMonthly (heuristic, transform, sync/orders.go:" in text
    assert "  → summary_db.resumo_pedido.total via sync.SyncOrders (exact, sync/orders.go:" in text
    table = format_why(g, "pedidos")
    assert "  → summary_db.resumo_pedido via sync.SyncOrders (exact, sync/orders.go:" in table
    assert "· 4 columns mapped" in table


def test_why_follows_several_hops(tmp_path: Path) -> None:
    from bizmap.why import format_why

    g = _go_graph(tmp_path)
    dataflow.add_feed(
        g, "table:resumo_pedido", "table:resumo_mensal", path=["function:sync.RollupMonthly"], confidence="exact",
        evidence={"file": "sync/orders.go", "line": 1, "symbol": "x"},
        entries=[{"from": "resumo_pedido.total", "to": "resumo_mensal.receita", "fn": "function:sync.RollupMonthly",
                  "confidence": "exact", "evidence": {"file": "sync/orders.go", "line": 1}}],
    )
    text = format_why(g, "resumo_mensal.receita")
    assert "  ← summary_db.resumo_pedido.total via sync.RollupMonthly (exact, sync/orders.go:1)\n" in text
    assert "    ← orders_db.pedidos.valor_centavos via sync.SyncOrders (exact," in text


def test_report_has_a_database_flow_section(tmp_path: Path) -> None:
    from bizmap.report import build_report, format_report

    g = _go_graph(tmp_path)
    rep = build_report(g.to_dict())
    assert rep["data_flow"]["pairs"] == [
        {"from": "orders_db", "to": "orders_db", "feeds": 1, "tables": 1, "columns": 2, "exact": 0, "heuristic": 2, "feeds_exact": 1},
        {"from": "orders_db", "to": "summary_db", "feeds": 1, "tables": 1, "columns": 4, "exact": 4, "heuristic": 0, "feeds_exact": 1},
    ]
    text = format_report(rep)
    assert (
        "data flow between databases: 2 feeds, 2 with columns mapped, 6 column mappings (exact 4, heuristic 2, 67% exact)\n"
        "  orders_db -> orders_db: 1 table, 2 columns (exact 0, heuristic 2)\n"
        "  orders_db -> summary_db: 1 table, 4 columns (exact 4, heuristic 0)\n"
    ) in text
    redacted = build_report(g.to_dict(), redact=True)
    assert all(row["from"].startswith("h:") for row in redacted["data_flow"]["pairs"])


def test_report_without_feeds_is_unchanged(tmp_path: Path) -> None:
    from bizmap.report import build_report

    g = Graph()
    assert "data_flow" not in build_report(g.to_dict())


def test_impact_lists_downstream_columns(tmp_path: Path) -> None:
    from bizmap.impact import compute_impact, render_markdown

    g = _go_graph(tmp_path)
    line = _line(GO_SYNC, "s := Summary")
    diff = f"diff --git a/sync/orders.go b/sync/orders.go\n--- a/sync/orders.go\n+++ b/sync/orders.go\n@@ -{line},1 +{line},1 @@\n-x\n+y\n"
    imp = compute_impact(g, diff)
    assert [(d["from"], d["to"], d["confidence"]) for d in imp.downstream] == [
        ("orders_db.pedidos.cliente_id", "summary_db.resumo_pedido.cliente", "exact"),
        ("orders_db.pedidos.criado_em", "summary_db.resumo_pedido.data_pedido", "exact"),
        ("orders_db.pedidos.id", "summary_db.resumo_pedido.pedido_id", "exact"),
        ("orders_db.pedidos.valor_centavos", "summary_db.resumo_pedido.total", "exact"),
    ]
    assert imp.to_dict()["downstream"]
    md = render_markdown(g, imp)
    assert "<b>Downstream</b> · 4 destinations fed from the changed data" in md
    assert "| `summary_db.resumo_pedido.total` | `orders_db.pedidos.valor_centavos` | `sync.SyncOrders` | exact |" in md


def test_impact_of_a_writer_of_the_source_column(tmp_path: Path) -> None:
    from bizmap.impact import compute_impact

    g = _go_graph(tmp_path)
    g.add_node("function:orders.Pay", "function", name="orders.Pay", evidence={"file": "orders/pay.go", "line": 3}, end_line=9)
    g.add_edge("writes", "function:orders.Pay", "column:pedidos.valor_centavos", evidence=Evidence("orders/pay.go", 5, "orders.Pay"))
    diff = "diff --git a/orders/pay.go b/orders/pay.go\n--- a/orders/pay.go\n+++ b/orders/pay.go\n@@ -5,1 +5,1 @@\n-x\n+y\n"
    imp = compute_impact(g, diff)
    assert {(d["from"], d["to"]) for d in imp.downstream} == {
        ("orders_db.pedidos.valor_centavos", "summary_db.resumo_pedido.total"),
        ("orders_db.pedidos.valor_centavos", "orders_db.resumo_mensal.receita"),
    }


def test_impact_without_feeds_has_no_downstream_key(tmp_path: Path) -> None:
    from bizmap.impact import compute_impact

    assert "downstream" not in compute_impact(Graph(), "").to_dict()


def test_html_table_page_ships_flow_and_escapes(tmp_path: Path) -> None:
    from bizmap.export import export_graph

    g = _go_graph(tmp_path)
    g.nodes["table:pedidos"].attrs["db"] = "orders<db>"
    html = export_graph(g.to_dict())
    assert '"flow":' in html
    assert "<db>" not in html.split("<script", 1)[1].split("</script>", 1)[0]  # JSON data is escaped
    data = g.to_dict()
    for e in data["edges"]:
        e.pop("column_map", None)
    no_feeds = {**data, "edges": [e for e in data["edges"] if e["type"] != "feeds"]}
    assert '"flow":' not in export_graph(no_feeds)


def test_graph_without_flow_is_unchanged(tmp_path: Path) -> None:
    """Same graph with and without the pass when nothing feeds anything."""
    f = "svc/svc.go"
    files = {f: NEGATIVE}
    fns = [("function:svc.Svc.CreateOrder", f, "func (s *Svc) CreateOrder")]
    edges = [
        ("reads", "function:svc.Svc.CreateOrder", "column:users.admin", f, "SELECT admin"),
        ("writes", "function:svc.Svc.CreateOrder", "column:pedidos.valor_centavos", f, "INSERT INTO pedidos"),
    ]
    g = _graph(tmp_path / "same", files, fns, edges, {"pedidos": PEDIDOS, "users": ["id", "admin"]})
    plain = Graph()
    plain.schema = g.schema
    for nid, n in g.nodes.items():
        plain.add_node(nid, n.type, **{k: v for k, v in n.attrs.items()})
    for e in g.edges.values():
        plain.add_edge(e.type, e.from_id, e.to_id, confidence=e.confidence, evidence=e.evidence, **e.attrs)
    assert json.dumps(g.to_dict()["edges"]) == json.dumps(plain.to_dict()["edges"])
    assert all(e.type != "feeds" for e in g.edges.values())


# ── whole scan (needs the tree-sitter parsers) ────────────────────────────────


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _scan(root: Path):  # noqa: ANN202
    pytest.importorskip("tree_sitter_go")
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_path

    return scan_path(root)


def _bare(feeds: dict) -> dict:
    return {(a.split("/")[-1], b.split("/")[-1]): v for (a, b), v in feeds.items()}


def test_scan_go_repro(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA_SQL)
    _write(tmp_path, "go.mod", "module example.com/ordersync\n\ngo 1.22\n")
    _write(tmp_path, "sync/orders.go", GO_SYNC)
    g = _scan(tmp_path)
    feeds = _bare(_feeds(g))
    assert feeds == {
        ("table:pedidos", "table:resumo_pedido"): ("exact", SYNC_MAP),
        ("table:pedidos", "table:resumo_mensal"): (
            "exact",
            {("criado_em", "mes", "heuristic", True), ("valor_centavos", "receita", "heuristic", True)},
        ),
    }
    assert g.nodes["table:pedidos"].attrs["db"] == "orders_db"
    assert g.nodes["table:resumo_pedido"].attrs["db"] == "summary_db"


def test_scan_ts_repro(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", SCHEMA_SQL)
    _write(tmp_path, "package.json", '{"name": "order-sync"}\n')
    _write(tmp_path, "src/sync.ts", TS_SYNC)
    g = _scan(tmp_path)
    assert _bare(_feeds(g)) == {
        ("table:pedidos", "table:resumo_pedido"): ("exact", SYNC_MAP),
        ("table:pedidos", "collection:resumos"): (
            "exact",
            {("valor_centavos", "total", "exact", False), ("cliente_id", "cliente", "exact", False)},
        ),
    }
    assert g.nodes["collection:resumos"].attrs["db"] == "firestore:default"


def test_scan_queue_repro_two_repos(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_typescript")
    from bizmap.scan import scan_paths

    _write(tmp_path, "pedidos/schema.sql", SCHEMA_SQL)
    _write(tmp_path, "pedidos/bizmap.yaml", "name: pedidos\n")
    _write(tmp_path, "pedidos/src/orders.ts", TS_PRODUCER)
    _write(tmp_path, "resumo/schema.sql", SCHEMA_SQL)
    _write(tmp_path, "resumo/bizmap.yaml", "name: resumo\n")
    _write(tmp_path, "resumo/src/consumer.ts", TS_CONSUMER)
    g = scan_paths([tmp_path / "pedidos", tmp_path / "resumo"], base=tmp_path)
    feeds = _feeds(g)
    assert feeds == {
        ("pedidos/table:pedidos", "resumo/table:resumo_pedido"): (
            "heuristic",
            {("id", "pedido_id", "heuristic", False), ("valor_centavos", "total", "heuristic", False)},
        ),
    }
    edge = g.edges["feeds:pedidos/table:pedidos->resumo/table:resumo_pedido"]
    path = edge.attrs["via"][0]["path"]
    assert path[0].startswith("pedidos/function:") and path[1] == "queue:topic:pedido-pago"
    assert path[2].startswith("resumo/function:")


SQL_FN = """CREATE TABLE pedidos (id text, valor_centavos bigint, criado_em timestamptz);
CREATE TABLE resumo_mensal (mes timestamptz, receita bigint);

CREATE OR REPLACE FUNCTION rollup_mensal() RETURNS void AS $$
BEGIN
  INSERT INTO resumo_mensal (mes, receita)
  SELECT date_trunc('month', criado_em), sum(valor_centavos) FROM pedidos GROUP BY 1;
END;
$$ LANGUAGE plpgsql;
"""


def test_sql_function_insert_select(tmp_path: Path) -> None:
    f = "db/rollup.sql"
    fid = "function:sql.rollup_mensal"
    root = tmp_path / "sqlfn"
    g = _graph(root, {f: SQL_FN}, [], [], {"pedidos": PEDIDOS, "resumo_mensal": MENSAL})
    start = _line(SQL_FN, "CREATE OR REPLACE FUNCTION")
    g.add_node(fid, "function", name="rollup_mensal", language="plpgsql",
               evidence={"file": f, "line": start, "symbol": "rollup_mensal"}, end_line=_line(SQL_FN, "$$ LANGUAGE"))
    g.add_edge("reads", fid, "table:pedidos", evidence=Evidence(f, start, "rollup_mensal"))
    g.add_edge("writes", fid, "table:resumo_mensal", evidence=Evidence(f, start, "rollup_mensal"))
    dataflow.apply(g, root)
    assert _feeds(g) == {
        ("table:pedidos", "table:resumo_mensal"): (
            "exact",
            {("criado_em", "mes", "heuristic", True), ("valor_centavos", "receita", "heuristic", True)},
        ),
    }


def test_insert_select_without_qualifier_and_without_reads_edge(tmp_path: Path) -> None:
    src = GO_SYNC.replace(
        "SELECT date_trunc('month', criado_em), sum(valor_centavos) FROM pedidos GROUP BY 1",
        "SELECT criado_em, valor_centavos FROM pedidos",
    )
    f = "sync/orders.go"
    r = "function:sync.RollupMonthly"
    g = _graph(
        tmp_path / "plain",
        {f: src},
        [(r, f, "func RollupMonthly")],
        [("writes", r, "column:resumo_mensal.mes", f, "INSERT INTO resumo_mensal")],
        {"pedidos": PEDIDOS, "resumo_mensal": MENSAL},
    )
    assert _feeds(g) == {
        ("table:pedidos", "table:resumo_mensal"): (
            "exact",
            {("criado_em", "mes", "exact", False), ("valor_centavos", "receita", "exact", False)},
        ),
    }


PLPGSQL = """CREATE TABLE stores (id uuid, name text);
CREATE TABLE branches (id uuid, store_id uuid, name text);

CREATE FUNCTION only_branch(store UUID) RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE found UUID;
BEGIN
  found := gen_random_uuid();
  INSERT INTO branches (id, store_id, name) SELECT found, store, s.name FROM stores s WHERE s.id = store;
  RETURN found;
END $$;
"""


def test_plpgsql_variables_are_not_source_columns(tmp_path: Path) -> None:
    f = "db/fn.sql"
    fid = "function:sql.only_branch"
    root = tmp_path / "plpgsql"
    g = _graph(root, {f: PLPGSQL}, [], [], {"stores": ["id", "name"], "branches": ["id", "store_id", "name"]})
    start = _line(PLPGSQL, "CREATE FUNCTION")
    g.add_node(fid, "function", name="only_branch", language="plpgsql",
               evidence={"file": f, "line": start, "symbol": "only_branch"}, end_line=_line(PLPGSQL, "END $$"))
    g.add_edge("writes", fid, "table:branches", evidence=Evidence(f, start, "only_branch"))
    g.add_edge("reads", fid, "table:stores", evidence=Evidence(f, start, "only_branch"))
    dataflow.apply(g, root)
    assert _feeds(g) == {("table:stores", "table:branches"): ("exact", {("name", "name", "exact", False)})}


def test_column_not_in_the_source_schema_is_never_mapped(tmp_path: Path) -> None:
    src = TS_SYNC.replace("SELECT id, valor_centavos, cliente_id, criado_em FROM pedidos", "SELECT id, total_bruto, cliente_id, criado_em FROM pedidos")
    f = "src/sync.ts"
    s = "function:src/sync.syncOrders"
    g = _graph(
        tmp_path / "noschema",
        {f: src},
        [(s, f, "export async function syncOrders")],
        [
            ("reads", s, "column:pedidos.id", f, "a.query('SELECT id, total_bruto"),
            ("writes", s, "column:resumo_pedido.total", f, "'INSERT INTO resumo_pedido"),
        ],
        {"pedidos": PEDIDOS, "resumo_pedido": RESUMO},
    )
    cols = _feeds(g)[("table:pedidos", "table:resumo_pedido")][1]
    assert not any(c[0] in ("total_bruto", "valor_centavos") for c in cols)


def test_queue_path_keeps_its_order_in_json(tmp_path: Path) -> None:
    gp, gc = _queue_graphs(tmp_path)
    merged = merge_graphs([gp, gc])
    dataflow.link_queue_flows(merged)
    data = merged.to_dict()
    feed = next(e for e in data["edges"] if e["type"] == "feeds")
    want = ["pedidos/function:src/orders.onOrderPaid", "queue:topic:pedido-pago", "resumo/function:src/consumer.module"]
    assert feed["via"][0]["path"] == want
    assert all(c["path"] == want for c in feed["column_map"])
    loaded = Graph.from_dict(json.loads(json.dumps(data)))
    text = "\n".join(dataflow.why_lines(loaded, "resumo/column:resumo_pedido.total"))
    assert "via pedidos/src/orders.onOrderPaid → queue pedido-pago → resumo/src/consumer.module" in text


def test_untouched_table_gets_no_db(tmp_path: Path) -> None:
    gp, gc = _queue_graphs(tmp_path)
    gc.add_node("table:pedidos", "table", name="pedidos")  # only in the copied schema.sql
    texts = source_texts(tmp_path / "resumo", [tmp_path / "resumo" / "src/consumer.ts"])
    dbident.assign_dbs(gc, texts, dbident.build_index(texts, {}))
    assert "db" not in gc.nodes["table:pedidos"].attrs
    assert gc.nodes["table:resumo_pedido"].attrs["db"] == "summary_db"
