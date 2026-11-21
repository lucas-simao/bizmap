"""Data flow between tables: ``feeds`` edges and column mappings.

A ``feeds`` edge ``table A -> table B`` says a function copies values read
from A into B (another database, often). It is only created when a value is
*seen* going from the read to the write:

- in one function: the row of ``SELECT id, valor FROM pedidos`` (``Scan``
  targets, ``rows`` / ``rows[0]`` / ``for p of rows``, ``cur.fetchone()``,
  ``row["valor"]``) reaches the arguments of ``INSERT`` / ``UPDATE`` /
  upsert, a Firestore ``set`` / ``update`` / ``add`` or an ORM write
  (``exact``); ``INSERT … SELECT`` and ``UPDATE … FROM`` map in one
  statement;
- through a call in the same service: a reader passes the value to a writer,
  or a writer uses what a reader function returns (``heuristic``);
- through a queue: the producer publishes a message built from A, the
  consumer of the same topic writes the message's fields to B
  (``heuristic``, ``via`` producer → queue → consumer; also across repos in a
  joint scan / ``registry merge``).

Each ``feeds`` has a ``column_map`` (``{from, to, fn, confidence,
evidence}``): ``exact`` when the value goes through unchanged, ``heuristic``
with ``transform: true`` when an expression or function is in between
(``sum(valor)``, ``Math.round(p.valor * 100)``), or ``match: name`` when only
the column names agree and no value path was seen. Nothing is mapped
otherwise, and a function that reads A and writes B with no value in common
(a lookup, a permission check) does not create ``feeds``.

This is a text pass over the function bodies the detectors already located
(``evidence`` / ``start_line`` / ``end_line``); it never changes their
reads / writes edges.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bizmap.graph import Confidence, Evidence, Graph, edge_id
from bizmap.srctext import LIT_RE, Masked, language_of, mask, match_close, split_top

MAX_VIA = 5
MAX_DEPTH = 2
MAX_BODY_CHARS = 400_000
GENERIC_COLUMNS = {"id", "created_at", "updated_at", "deleted_at", "createdat", "updatedat"}

# ---------------------------------------------------------------- SQL ----

_SQL_START = re.compile(r"^\s*\(?\s*(select|insert|update|with|upsert|replace)\b", re.I)
_KEYWORDS = {
    "select", "from", "as", "case", "when", "then", "else", "end", "and", "or", "not", "null", "true",
    "false", "is", "in", "distinct", "interval", "cast", "filter", "over", "partition", "by", "order",
    "asc", "desc", "like", "ilike", "between", "exists", "any", "all", "some", "array", "row", "date",
    "timestamp", "timestamptz", "time", "int", "integer", "text", "numeric", "bigint", "varchar",
    "boolean", "uuid", "jsonb", "json", "at", "zone", "extract", "epoch", "month", "year", "day", "hour",
    "minute", "second", "week", "nulls", "first", "last", "default", "on", "using", "where", "group",
    "having", "limit", "offset", "returning", "values", "set", "current_date", "current_timestamp",
    "localtimestamp", "now", "with", "recursive", "union", "except", "intersect", "lateral", "join",
    "left", "right", "inner", "outer", "full", "cross", "natural", "for", "update", "into", "conflict",
    "do", "nothing", "excluded", "only", "smallint", "real", "double", "precision", "float", "char",
    "character", "varying", "decimal", "money", "bool", "serial", "bigserial", "unknown", "leading",
    "trailing", "both", "collate", "similar", "escape", "isnull", "notnull", "of", "to",
}
_CLAUSE_END = re.compile(
    r"\b(where|group\s+by|order\s+by|having|limit|offset|union|except|intersect|returning|for\s+update"
    r"|on\s+conflict|window|fetch)\b",
    re.I,
)
_PARAM_RE = re.compile(r"\$(\d+)|\?|%\((\w+)\)s|%s|(?<![:\w]):([A-Za-z_]\w*)|@([A-Za-z_]\w*)")
_IDENT_REF = re.compile(r"(?<![\w.$:@'\"])(?:([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)(?![\w(]|\s*\()")


@dataclass
class SelItem:
    name: str | None
    refs: list[tuple[str | None, str]]  # (qualifier, column)
    transform: bool
    star: str | None = None  # "*" or qualifier for "t.*"


@dataclass
class SqlSelect:
    tables: dict[str, str]  # alias / name (lower) -> table name (lower)
    order: list[str]  # table names in FROM order
    items: list[SelItem]


@dataclass
class ValSpec:
    params: list[int | str] = field(default_factory=list)
    refs: list[tuple[str | None, str]] = field(default_factory=list)
    transform: bool = False
    excluded: bool = False


@dataclass
class SqlWrite:
    table: str
    cols: dict[str, ValSpec]
    from_tables: dict[str, str] = field(default_factory=dict)
    from_order: list[str] = field(default_factory=list)


def _sql_mask(sql: str) -> tuple[str, list[str]]:
    """Mask SQL '…' literals as ``'n'`` and unquote "identifiers"."""
    out: list[str] = []
    lits: list[str] = []
    i = 0
    while i < len(sql):
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < len(sql):
                if sql[j] == "'" and sql[j + 1 : j + 2] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            lits.append(sql[i + 1 : j])
            out.append(f" \x02{len(lits) - 1}\x02 ")
            i = j + 1
            continue
        if c == '"':
            j = sql.find('"', i + 1)
            j = len(sql) if j < 0 else j
            out.append(sql[i + 1 : j])
            i = j + 1
            continue
        if c == "-" and sql[i + 1 : i + 2] == "-":
            j = sql.find("\n", i)
            i = len(sql) if j < 0 else j
            continue
        out.append(c)
        i += 1
    text = "".join(out)
    text = re.sub(r"::\s*[A-Za-z_][\w ]*?(?:\[\])?(?=[\s,)]|$)", " ", text)  # casts
    return " ".join(text.split()), lits


def _norm_params(text: str) -> str:
    """Positional ``?`` / ``%s`` → ``$n``; named stay ``:name``."""
    counter = [0]

    def rep(m: re.Match[str]) -> str:
        if m.group(1):
            return f"${m.group(1)}"
        if m.group(2):
            return f":{m.group(2)}"
        if m.group(3):
            return f":{m.group(3)}"
        if m.group(4):
            name = m.group(4)
            if re.fullmatch(r"p\d+", name, re.I):
                return f"${name[1:]}"
            return f":{name}"
        counter[0] += 1
        return f"${counter[0]}"

    return _PARAM_RE.sub(rep, text)


def _refs(expr: str) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    for m in _IDENT_REF.finditer(re.sub(r"\$\d+|:\w+|\x02\d+\x02|\b\d+(?:\.\d+)?\b", " ", expr)):
        q, col = m.group(1), m.group(2)
        if col.lower() in _KEYWORDS and not q:
            continue
        if q and q.lower() in {"excluded", "new", "old"}:
            out.append((q.lower(), col.lower()))
            continue
        out.append((q.lower() if q else None, col.lower()))
    # `CAST(x AS integer)`: the type after AS is not a column
    return out


def _params(expr: str) -> list[int | str]:
    out: list[int | str] = []
    for m in re.finditer(r"\$(\d+)|(?<![:\w]):([A-Za-z_]\w*)", expr):
        out.append(int(m.group(1)) if m.group(1) else m.group(2))
    return out


_BARE_COL = re.compile(r"^(?:([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)$")
_BARE_PARAM = re.compile(r"^(?:\$(\d+)|:([A-Za-z_]\w*))$")


def _strip_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and match_close(expr, 0) == len(expr) - 1:
        expr = expr[1:-1].strip()
    return expr


def _split_alias(item: str) -> tuple[str, str | None]:
    m = re.match(r"^(.*\S)\s+as\s+([A-Za-z_]\w*)$", item, re.I | re.S)
    if m:
        return m.group(1), m.group(2).lower()
    m = re.match(r"^(.*[\w)\x02])\s+([A-Za-z_]\w*)$", item, re.S)
    if m and m.group(2).lower() not in _KEYWORDS and not re.search(r"\b(?:case|when)\b", item, re.I):
        return m.group(1), m.group(2).lower()
    return item, None


def _top_keyword(text: str, kw: str, start: int = 0) -> int:
    """Index of keyword ``kw`` at paren depth 0 (or -1)."""
    depth = 0
    rx = re.compile(rf"\b{kw}\b", re.I)
    i = start
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            m = rx.match(text, i)
            if m:
                return i
        i += 1
    return -1


def _clause_end(text: str, start: int) -> int:
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return i
        elif depth == 0 and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            if _CLAUSE_END.match(text, i):
                return i
        i += 1
    return len(text)


def _from_tables(text: str) -> tuple[dict[str, str], list[str]]:
    text = re.sub(
        r"\b(?:natural\s+)?(?:(?:left|right|full)\s+(?:outer\s+)?|inner\s+|cross\s+)?join\b",
        ",",
        text,
        flags=re.I,
    )
    aliases: dict[str, str] = {}
    order: list[str] = []
    for part in split_top(text):
        part = re.split(r"\b(?:on|using)\b", part, maxsplit=1, flags=re.I)[0].strip()
        if not part or part.startswith("("):
            continue
        part = re.sub(r"^lateral\s+", "", part, flags=re.I)
        m = re.match(r"^(?:only\s+)?([\w.]+)(?:\s+(?:as\s+)?([A-Za-z_]\w*))?", part, re.I)
        if not m:
            continue
        name = m.group(1).lower()
        if "(" in part[: m.end() + 1]:
            continue
        aliases[name] = name
        aliases[name.split(".")[-1]] = name
        if m.group(2) and m.group(2).lower() not in _KEYWORDS:
            aliases[m.group(2).lower()] = name
        order.append(name)
    return aliases, order


def _skip_with(text: str) -> str:
    """``WITH a AS (…), b AS (…) <main>`` → ``<main>``."""
    if not re.match(r"^\s*with\b", text, re.I):
        return text
    i = re.match(r"^\s*with\s+(?:recursive\s+)?", text, re.I).end()
    while True:
        m = re.compile(r"\s*[\w]+\s*(?:\([^)]*\))?\s+as\s+(?:not\s+)?(?:materialized\s+)?\(", re.I).match(text, i)
        if not m:
            return text[i:]
        j = match_close(text, m.end() - 1)
        i = j + 1
        m2 = re.compile(r"\s*,").match(text, i)
        if m2:
            i = m2.end()
            continue
        return text[i:]


def parse_select(text: str) -> SqlSelect | None:
    text = _strip_parens(_skip_with(text))
    m = re.match(r"^\s*select\s+(?:distinct\s+(?:on\s*\([^)]*\)\s*)?|all\s+)?", text, re.I)
    if not m:
        return None
    start = m.end()
    f = _top_keyword(text, "from", start)
    if f < 0:
        return SqlSelect(tables={}, order=[], items=[])
    sel = text[start:f]
    fend = _clause_end(text, f + 4)
    aliases, order = _from_tables(text[f + 4 : fend])
    items: list[SelItem] = []
    for raw in split_top(sel):
        expr, alias = _split_alias(raw)
        expr = _strip_parens(expr)
        if expr == "*":
            items.append(SelItem(name=None, refs=[], transform=False, star="*"))
            continue
        sm = re.match(r"^([A-Za-z_]\w*)\s*\.\s*\*$", expr)
        if sm:
            items.append(SelItem(name=None, refs=[], transform=False, star=sm.group(1).lower()))
            continue
        bm = _BARE_COL.match(expr)
        if bm and bm.group(2).lower() not in _KEYWORDS:
            q = bm.group(1).lower() if bm.group(1) else None
            col = bm.group(2).lower()
            items.append(SelItem(name=alias or col, refs=[(q, col)], transform=False))
            continue
        items.append(SelItem(name=alias, refs=_refs(expr), transform=True))
    return SqlSelect(tables=aliases, order=order, items=items)


def _val_spec(expr: str) -> ValSpec:
    expr = _strip_parens(expr)
    pm = _BARE_PARAM.match(expr)
    if pm:
        return ValSpec(params=[int(pm.group(1)) if pm.group(1) else pm.group(2)])
    bm = _BARE_COL.match(expr)
    if bm and bm.group(2).lower() not in _KEYWORDS:
        q = bm.group(1).lower() if bm.group(1) else None
        if q == "excluded":
            return ValSpec(excluded=True)
        return ValSpec(refs=[(q, bm.group(2).lower())])
    return ValSpec(params=_params(expr), refs=[r for r in _refs(expr) if r[0] not in ("excluded",)], transform=True)


def parse_write(text: str) -> SqlWrite | None:
    text = _skip_with(text).strip()
    m = re.match(r"^(?:insert|upsert|replace)\s+(?:or\s+\w+\s+)?into\s+([\w.]+)\s*(?:as\s+\w+\s*)?", text, re.I)
    if m:
        table = m.group(1).lower()
        rest = text[m.end() :]
        cols: list[str] = []
        if rest.startswith("("):
            j = match_close(rest, 0)
            inner = rest[1:j]
            if re.match(r"^\s*select\b", inner, re.I):
                cols = []
            else:
                cols = [c.strip().lower() for c in split_top(inner)]
                rest = rest[j + 1 :].strip()
        out = SqlWrite(table=table, cols={})
        if not cols:
            return out
        vm = re.match(r"^values\s*\(", rest, re.I)
        if vm:
            j = match_close(rest, vm.end() - 1)
            values = split_top(rest[vm.end() : j])
            for col, val in zip(cols, values, strict=False):
                out.cols[col] = _val_spec(val)
            rest = rest[j + 1 :]
        elif re.match(r"^\(?\s*(?:select|with)\b", rest, re.I):
            end = _top_keyword(rest, "on")
            sub = rest if end < 0 else rest[:end]
            sel = parse_select(sub)
            if sel is not None:
                out.from_tables, out.from_order = sel.tables, sel.order
                for col, item in zip(cols, sel.items, strict=False):
                    if item.star:
                        continue
                    out.cols[col] = ValSpec(refs=list(item.refs), transform=item.transform)
        cm = re.search(r"\bdo\s+update\s+set\s+", rest, re.I)
        if cm:
            body = rest[cm.end() :]
            body = body[: _clause_end(body, 0)]
            for assign in split_top(body):
                am = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", assign, re.S)
                if not am:
                    continue
                col = am.group(1).lower()
                spec = _val_spec(am.group(2))
                if spec.excluded:
                    continue
                if col in out.cols:
                    old = out.cols[col]
                    out.cols[col] = ValSpec(
                        params=old.params + spec.params,
                        refs=old.refs + spec.refs,
                        transform=old.transform or spec.transform,
                    )
                else:
                    out.cols[col] = spec
        return out
    m = re.match(r"^update\s+(?:only\s+)?([\w.]+)(?:\s+(?:as\s+)?([A-Za-z_]\w*))?\s+set\s+", text, re.I)
    if m:
        table = m.group(1).lower()
        alias = (m.group(2) or "").lower()
        rest = text[m.end() :]
        end = _clause_end(rest, 0)
        f = _top_keyword(rest[:end], "from")
        set_part = rest[: f if f >= 0 else end]
        out = SqlWrite(table=table, cols={})
        if f >= 0:
            fend = _clause_end(rest, f + 4)
            out.from_tables, out.from_order = _from_tables(rest[f + 4 : fend])
        own = {table, table.split(".")[-1], alias}
        for assign in split_top(set_part):
            am = re.match(r"^([A-Za-z_][\w.]*)\s*=\s*(.+)$", assign, re.S)
            if not am:
                continue
            col = am.group(1).lower().split(".")[-1]
            spec = _val_spec(am.group(2))
            spec.refs = [(q, c) for q, c in spec.refs if q not in own and (q is not None or f >= 0)]
            out.cols[col] = spec
        return out
    return None


def parse_sql(sql: str) -> tuple[str, Any] | None:
    """``("select", SqlSelect)`` / ``("write", SqlWrite)`` for one statement."""
    text, _ = _sql_mask(sql)
    text = _norm_params(text).strip().rstrip(";").strip()
    if ";" in text:
        text = [p for p in text.split(";") if p.strip()][0].strip()
    head = re.match(r"^\s*\(?\s*(\w+)", text)
    if not head:
        return None
    body = _skip_with(text) if head.group(1).lower() == "with" else text
    verb = (re.match(r"^\s*\(?\s*(\w+)", body) or head).group(1).lower()
    if verb == "select":
        sel = parse_select(text)
        return ("select", sel) if sel is not None else None
    if verb in ("insert", "update", "upsert", "replace"):
        w = parse_write(text)
        return ("write", w) if w is not None else None
    return None


# ------------------------------------------------------------- values ----


@dataclass(frozen=True)
class Src:
    table: str | None  # graph node id (None: a message field)
    col: str | None
    transform: bool = False
    hops: tuple[str, ...] = ()  # functions / queue the value crossed before this one
    topic: str | None = None
    field: str | None = None


@dataclass
class Scalar:
    srcs: frozenset[Src]


@dataclass
class Rows:
    sel: ResolvedSelect
    kind: str  # cursor | set | result


@dataclass
class Row:
    sel: ResolvedSelect


@dataclass
class Obj:
    fields: dict[str, Any]


@dataclass
class ListOf:
    elem: Any


@dataclass
class Msg:
    topic: str
    hops: tuple[str, ...] = ()
    local: bool = False


@dataclass
class Str:
    value: str
    dynamic: bool = False


@dataclass
class Arr:
    items: list[Any]


@dataclass
class ResolvedSelect:
    items: list[tuple[str | None, frozenset[Src]]]
    star_tables: list[str]
    tables: list[str]


def _with(srcs: frozenset[Src], *, transform: bool = False, hop: str | None = None) -> frozenset[Src]:
    if not transform and hop is None:
        return srcs
    return frozenset(
        Src(
            table=s.table,
            col=s.col,
            transform=s.transform or transform,
            hops=(hop, *s.hops) if hop else s.hops,
            topic=s.topic,
            field=s.field,
        )
        for s in srcs
    )


def sources(value: Any, depth: int = 0) -> frozenset[Src]:
    """Every source inside a value; whole rows / objects count table-level."""
    if value is None or depth > 4:
        return frozenset()
    if isinstance(value, Scalar):
        return value.srcs
    if isinstance(value, (Row, Rows)):
        out: set[Src] = set()
        for t in value.sel.tables:
            out.add(Src(table=t, col=None))
        return frozenset(out)
    if isinstance(value, Obj):
        acc: set[Src] = set()
        for v in value.fields.values():
            acc |= {Src(table=s.table, col=None, transform=s.transform, hops=s.hops, topic=s.topic) for s in sources(v, depth + 1)}
        return frozenset(acc)
    if isinstance(value, ListOf):
        return sources(value.elem, depth + 1)
    if isinstance(value, Arr):
        acc2: set[Src] = set()
        for v in value.items:
            acc2 |= sources(v, depth + 1)
        return frozenset(acc2)
    return frozenset()


def _hop_value(value: Any, hop: str) -> Any:
    """Mark every source in ``value`` as having crossed ``hop``."""
    if isinstance(value, Scalar):
        return Scalar(_with(value.srcs, hop=hop))
    if isinstance(value, Obj):
        return Obj({k: _hop_value(v, hop) for k, v in value.fields.items()})
    if isinstance(value, ListOf):
        return ListOf(_hop_value(value.elem, hop))
    if isinstance(value, Arr):
        return Arr([_hop_value(v, hop) for v in value.items])
    if isinstance(value, (Row, Rows)):
        sel = ResolvedSelect(
            items=[(n, _with(s, hop=hop)) for n, s in value.sel.items],
            star_tables=value.sel.star_tables,
            tables=value.sel.tables,
        )
        return Row(sel) if isinstance(value, Row) else Rows(sel, value.kind)
    if isinstance(value, Msg):
        return Msg(value.topic, (hop, *value.hops))
    return value


def _elem(value: Any) -> Any:
    if isinstance(value, Rows):
        return Row(value.sel)
    if isinstance(value, ListOf):
        return value.elem
    if isinstance(value, Arr) and value.items:
        return value.items[0]
    return None


# ----------------------------------------------------------- events ----


@dataclass
class WriteEvent:
    fn: str
    target: str  # graph node id
    col: str | None
    srcs: frozenset[Src]
    file: str
    line: int
    spec_transform: bool = False


@dataclass
class PublishEvent:
    fn: str
    topic: str
    payload: Any
    file: str
    line: int
    local: bool = False  # in-process event (`emit` / `on`): never links across repos


# ------------------------------------------------------------ context ----


@dataclass
class FnInfo:
    fid: str
    file: str
    start: int
    end: int
    lang: str
    name: str


class RepoContext:
    """What the analysis needs from the graph and the source files."""

    def __init__(self, graph: Graph, root: Path, texts: dict[str, str] | None = None, *, firestore: bool = False):
        self.graph = graph
        self.root = root
        self.texts = texts if texts is not None else {}
        self.masked: dict[str, Masked] = {}
        self.file_strings: dict[str, dict[str, Str]] = {}
        self.firestore = firestore
        self.schema = (graph.schema or {}).get("tables") or {}
        self.schema_lower = {str(k).lower(): v for k, v in self.schema.items()}
        self.fns: dict[str, FnInfo] = {}
        self.by_file: dict[str, list[FnInfo]] = defaultdict(list)
        self.touch: dict[str, set[str]] = defaultdict(set)  # fid -> tables it reads/writes (any)
        self.direct_w: dict[str, set[str]] = defaultdict(set)
        self.direct_r: dict[str, set[str]] = defaultdict(set)
        self.reads_any: set[str] = set()
        self.calls: dict[str, set[str]] = defaultdict(set)
        self.write_lines: dict[tuple[str, str], set[int]] = defaultdict(set)  # (file, table) -> lines
        self.queue_targets: dict[str, list[tuple[str, str]]] = defaultdict(list)  # fid -> [(queue, fn)]
        self.summaries: dict[str, Any] = {}
        self._index()

    def _index(self) -> None:
        g = self.graph
        for node in g.nodes.values():
            if node.type != "function":
                continue
            ev = node.attrs.get("evidence") or {}
            file, line = ev.get("file"), ev.get("line")
            end = node.attrs.get("end_line")
            if not file or not isinstance(line, int) or not isinstance(end, int):
                continue
            start = node.attrs.get("start_line") if isinstance(node.attrs.get("start_line"), int) else line
            info = FnInfo(
                fid=node.id,
                file=str(file),
                start=int(start),
                end=int(end),
                lang=language_of(str(file)) if not node.attrs.get("language") else _lang(node.attrs["language"]),
                name=str(node.attrs.get("name") or node.id.split(":", 1)[-1]),
            )
            if node.attrs.get("role") == "schema":
                continue
            if str(file).endswith(".sql"):
                info.lang = "sql"  # CREATE FUNCTION body: INSERT … SELECT / UPDATE … FROM
            self.fns[node.id] = info
            self.by_file[info.file].append(info)
        triggers: dict[str, set[str]] = defaultdict(set)
        for e in g.edges.values():
            if e.type in ("reads", "writes"):
                t = _table_of(g, e.to_id)
                if t is None:
                    continue
                self.touch[e.from_id].add(t)
                if e.type == "reads":
                    self.reads_any.add(e.from_id)
                if not e.attrs.get("derived") and not e.attrs.get("via"):
                    (self.direct_w if e.type == "writes" else self.direct_r)[e.from_id].add(t)
                    if e.type == "writes" and e.evidence is not None:
                        self.write_lines[(e.evidence.file, t)].add(int(e.evidence.line or 0))
            elif e.type == "calls":
                self.calls[e.from_id].add(e.to_id)
            elif e.type == "triggers" and e.from_id.split("/")[-1].startswith("queue:"):
                triggers[e.from_id].add(e.to_id)
        for e in g.edges.values():
            if e.type == "enqueues":
                for consumer in triggers.get(e.to_id, ()):
                    self.queue_targets[e.from_id].append((e.to_id, consumer))

    def text(self, rel: str) -> str | None:
        if rel not in self.texts:
            try:
                self.texts[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self.texts[rel] = ""
        return self.texts[rel] or None

    def masked_file(self, rel: str) -> Masked | None:
        if rel in self.masked:
            return self.masked[rel]
        text = self.text(rel)
        if not text:
            return None
        m = mask(text, language_of(rel))
        self.masked[rel] = m
        self.file_strings[rel] = _file_strings(m)
        return m

    def table_node(self, name: str, *, write: bool) -> str | None:
        name = name.lower()
        for cand in (name, name.split(".")[-1]):
            tid = f"table:{cand}"
            if tid in self.graph.nodes:
                return tid
            if not write:
                vid = f"view:{cand}"
                if vid in self.graph.nodes:
                    return vid
        return None

    def columns(self, tid: str) -> list[str] | None:
        name = tid.split(":", 1)[-1]
        spec = self.schema_lower.get(name)
        if not spec:
            return None
        return [str(c).lower() for c in (spec.get("columns") or {})]


def _lang(value: str) -> str:
    v = str(value).lower()
    if v.startswith("go"):
        return "go"
    if v.startswith("py"):
        return "python"
    return "javascript"


def _table_of(graph: Graph, to_id: str) -> str | None:
    bare = to_id.split("/", 1)[1] if "/" in to_id.split(":", 1)[0] else to_id
    prefix = to_id[: len(to_id) - len(bare)]
    if bare.startswith(("table:", "view:", "collection:")):
        return to_id
    if bare.startswith("column:"):
        return f"{prefix}table:{bare[len('column:'):].rsplit('.', 1)[0]}"
    return None


_FILE_STR_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:export\s+)?(?:const|var|let|final)?\s*([A-Za-z_]\w*)\s*(?::\s*\w+\s*)?=\s*((?:\x00\d+\x00\s*\+?\s*)+)(?=[;\n)]|$)"
)
_GO_CONST_BLOCK = re.compile(r"(?:const|var)\s*\(([^()]*)\)", re.S)
_GO_CONST_LINE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\w+\s*)?=\s*((?:\x00\d+\x00\s*\+?\s*)+)\s*$", re.M)


def _join_lits(m: Masked, expr: str) -> Str | None:
    ids = LIT_RE.findall(expr)
    if not ids:
        return None
    lits = [m.lits[int(i)] for i in ids]
    return Str("".join(lit.value for lit in lits), any(lit.dynamic for lit in lits))


def _file_strings(m: Masked) -> dict[str, Str]:
    out: dict[str, Str] = {}
    for rx in (_FILE_STR_RE,):
        for mm in rx.finditer(m.text):
            s = _join_lits(m, mm.group(2))
            if s is not None:
                out.setdefault(mm.group(1), s)
    for block in _GO_CONST_BLOCK.finditer(m.text):
        for mm in _GO_CONST_LINE.finditer(block.group(1)):
            s = _join_lits(m, mm.group(2))
            if s is not None:
                out.setdefault(mm.group(1), s)
    return out


# --------------------------------------------------------- statements ----


@dataclass
class Stmt:
    text: str
    line: int
    callback: Callback | None = None


@dataclass
class Callback:
    callee: str
    pre_args: str
    params: list[str]
    body: list[Stmt]
    line: int


_CB_TOKEN = re.compile(r"\x01(\d+)\x01")


def _is_literal_brace(text: str, i: int, lang: str) -> bool:
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0:
        return False
    prev = text[j]
    if prev in "=:(,[?|&!\x00{" or prev == "\n" and False:
        return True
    word = re.search(r"([A-Za-z_]\w*)\s*$", text[: j + 1])
    if word and word.group(1) in ("return", "yield", "await", "in", "of", "case", "const", "let", "var", "export"):
        return True
    if lang == "go" and j == i - 1 and (prev.isalnum() or prev in "_]}"):
        return True  # composite literal `Summary{`, `[]T{`, `map[K]V{`
    return False


def _cb_params(head: str, lang: str) -> list[str] | None:
    head = head.rstrip()
    if lang == "go":
        hits = list(re.finditer(r"\bfunc\s*\(", head))
        m = hits[-1] if hits else None
        if m is None:
            return None
        j = match_close(head, m.end() - 1)
        return _param_names(head[m.end() : j], lang)
    if head.endswith("=>"):
        h = head[:-2].rstrip()
        if h.endswith(")"):
            depth = 0
            for k in range(len(h) - 1, -1, -1):
                if h[k] == ")":
                    depth += 1
                elif h[k] == "(":
                    depth -= 1
                    if depth == 0:
                        return _param_names(h[k + 1 : -1], lang)
            return None
        m = re.search(r"([A-Za-z_$][\w$]*)$", h)
        return [m.group(1)] if m else None
    m = re.search(r"\bfunction\s*\*?\s*[\w$]*\s*\(([^()]*)\)\s*$", head)
    if m:
        return _param_names(m.group(1), lang)
    return None


def _param_names(text: str, lang: str) -> list[str]:
    names: list[str] = []
    for part in split_top(text):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"^(?:\.\.\.|\*\*?)", "", part)
        if lang == "go":
            m = re.match(r"([A-Za-z_]\w*)", part)
            names.append(m.group(1) if m else "_")
            continue
        if part.startswith("{") or part.startswith("["):
            close = match_close(part, 0)
            names.append(part[: close + 1])
            continue
        m = re.match(r"(?:(?:public|private|protected|readonly)\s+)*([A-Za-z_$][\w$]*)", part)
        names.append(m.group(1) if m else "_")
    return names


def split_statements(text: str, line0: int, lang: str) -> list[Stmt]:
    if lang == "python":
        return _split_python(text, line0)
    return _split_braces(text, line0, lang)


def _split_braces(text: str, line0: int, lang: str) -> list[Stmt]:
    out: list[Stmt] = []
    cur: list[str] = []
    cur_line = line0
    line = line0
    stack: list[tuple[str, int]] = []  # (kind, position in cur)
    i = 0
    n = len(text)

    blank = True  # nothing but whitespace in `cur` yet

    def emit() -> None:
        nonlocal cur, cur_line, blank
        s = "".join(cur).strip() if not blank else ""
        if s:
            out.append(Stmt(text=s, line=cur_line))
        cur = []
        cur_line = line
        blank = True

    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            if not any(k != "B" for k, _ in stack):
                emit()
                i += 1
                continue
            cur.append(" ")
            i += 1
            continue
        if blank and not c.isspace():
            cur_line = line
            blank = False
        if c in "([":
            stack.append((c, len(cur)))
        elif c in ")]":
            while stack and stack[-1][0] in ("L", "B") and False:
                stack.pop()
            if stack and stack[-1][0] in "([":
                stack.pop()
        elif c == "{":
            if _is_literal_brace(text, i, lang):
                stack.append(("L", len(cur)))
            elif any(k in "([L" for k, _ in stack):
                # a callback body inside a call: its own statements
                j = match_close(text, i)
                head = "".join(cur)
                params = _cb_params(head, lang) or []
                open_paren = next((pos for k, pos in reversed(stack) if k == "("), None)
                callee, pre = "", ""
                if open_paren is not None:
                    before = head[:open_paren]
                    cm = re.search(r"((?:new\s+)?[A-Za-z_$][\w$.]*(?:\([^()]*\))?(?:\.[A-Za-z_$][\w$]*)*)\s*$", before)
                    callee = cm.group(1) if cm else ""
                    pre = head[open_paren + 1 :]
                body = split_statements(text[i + 1 : j], line, lang)
                cb = Callback(callee=callee, pre_args=pre, params=params, body=body, line=line)
                out.append(Stmt(text="", line=line, callback=cb))
                line += text.count("\n", i, j)
                cur.append("null")
                blank = False
                i = j + 1
                continue
            else:
                emit()
                stack.append(("B", len(cur)))
                i += 1
                continue
        elif c == "}":
            if stack and stack[-1][0] == "L":
                stack.pop()
            elif stack and stack[-1][0] == "B":
                stack.pop()
                emit()
                i += 1
                continue
            else:
                emit()
                i += 1
                continue
        elif c == ";" and not any(k != "B" for k, _ in stack):
            emit()
            i += 1
            continue
        cur.append(c)
        i += 1
    emit()
    return out


def _split_python(text: str, line0: int) -> list[Stmt]:
    out: list[Stmt] = []
    buf: list[str] = []
    depth = 0
    start_line = line0
    for k, raw in enumerate(text.split("\n")):
        ln = line0 + k
        if not buf:
            start_line = ln
        buf.append(raw.strip())
        for c in raw:
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
        if depth > 0 or raw.rstrip().endswith("\\"):
            continue
        s = " ".join(b for b in buf if b).rstrip("\\").strip()
        buf = []
        depth = 0
        if not s:
            continue
        # `if x: y = 1` / `for r in rows: insert(r)`
        m = re.match(r"^((?:async\s+)?(?:for|with|if|elif|while|else|try|except)\b[^:]*:)\s*(\S.*)$", s)
        if m and not s.endswith(":"):
            out.append(Stmt(text=m.group(1), line=start_line))
            out.append(Stmt(text=m.group(2), line=start_line))
            continue
        for part in split_top(s, ";") if ";" in s else [s]:
            out.append(Stmt(text=part, line=start_line))
    return out


# --------------------------------------------------------- evaluator ----

_QUERY_METHODS = {
    "query", "queryrow", "queryrowcontext", "querycontext", "exec", "execcontext", "execute", "executemany",
    "fetch", "fetchrow", "fetchval", "fetchone", "one", "none", "any", "many", "oneornone", "manyornone",
    "unsafe", "raw", "get", "getcontext", "select", "selectcontext", "namedexec", "namedexeccontext",
    "namedquery", "mustexec", "queryx", "queryrowx", "run", "all", "prepare",
}
_ROW_METHODS = {"fetchone", "fetchrow", "first", "one", "one_or_none", "scalar", "fetchfirst", "next"}
_SET_METHODS = {"fetchall", "fetchmany", "all", "mappings", "dicts", "fetch", "tolist", "to_list"}
_PASS_METHODS = {"parse", "loads", "dict", "stringify", "dumps", "tojson", "toobject", "assign", "structuredclone",
                 "copy", "deepcopy", "freeze", "data", "json", "todict", "model_dump", "_asdict", "as_dict"}
_PUBLISH = {"publish", "publishjson", "send", "sendmessage", "emit", "enqueue", "add", "produce", "push_task"}
_CONSUME = {"subscribe", "on", "consume", "process", "handle", "listen", "addlistener", "once", "handler"}
_FS_WRITE = {"set", "update", "add", "create"}
_ORM_WRITE = {
    "create", "createmany", "insert", "insertone", "insertmany", "upsert", "update", "updatemany", "save",
    "bulkcreate", "values", "insert_one", "bulk_insert_mappings", "add", "merge",
}
_MSG_WRAPPERS = {"data", "body", "payload", "value", "message", "attributes", "params", "detail", "content"}
_ASSIGN_KW = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+")


def _find_assign(text: str) -> tuple[int, int] | None:
    """(start, end) of the top-level assignment operator in a statement."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0:
            if c == ":" and text[i + 1 : i + 2] == "=":
                return i, i + 2
            if c == "=":
                nxt = text[i + 1 : i + 2]
                prv = text[i - 1 : i] if i else ""
                if nxt in ("=", ">") or prv in "=!<>+-*/%&|^:~?":
                    i += 2 if nxt == "=" else 1
                    continue
                return i, i + 1
        i += 1
    return None


class Analyzer:
    def __init__(self, ctx: RepoContext, fn: FnInfo, masked: Masked, *, depth: int = 0, hop: tuple[str, ...] = ()):
        self.ctx = ctx
        self.fn = fn
        self.m = masked
        self.env: dict[str, Any] = {}
        self.writes: list[WriteEvent] = []
        self.publishes: list[PublishEvent] = []
        self.returns: list[Any] = []
        self.depth = depth
        self.hop = hop
        self.line = fn.start
        self.strings = ctx.file_strings.get(fn.file) or {}
        self.callees = self._callee_index()

    # --- env
    def _callee_index(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for cid in self.ctx.calls.get(self.fn.fid, ()):
            info = self.ctx.fns.get(cid)
            name = (info.name if info else cid.split(":", 1)[-1]).split(".")[-1]
            out[name].append(cid)
        for qid, cid in self.ctx.queue_targets.get(self.fn.fid, ()):
            info = self.ctx.fns.get(cid)
            name = (info.name if info else cid.split(":", 1)[-1]).split(".")[-1]
            out[name].append(cid)
            out["@queue:" + name].append(qid)
        return out

    def assign(self, path: str, value: Any) -> None:
        path = path.strip()
        if not path or path == "_":
            return
        for k in [k for k in self.env if k == path or k.startswith(path + ".")]:
            del self.env[k]
        if value is not None:
            self.env[path] = value

    def lookup(self, path: str) -> Any:
        if path in self.env:
            return self.env[path]
        prefix = path + "."
        subs = {k[len(prefix) :]: v for k, v in self.env.items() if k.startswith(prefix)}
        if subs:
            fields: dict[str, Any] = {}
            for k, v in subs.items():
                if "." not in k:
                    fields[k] = v
            if fields:
                return Obj(fields)
        if path in self.strings:
            return self.strings[path]
        return None

    # --- run
    def run(self, stmts: list[Stmt]) -> None:
        for st in stmts:
            self.line = st.line
            if st.callback is not None:
                self.run_callback(st.callback)
                continue
            try:
                self.exec_stmt(st.text)
            except RecursionError:
                continue

    def run_callback(self, cb: Callback) -> None:
        method = cb.callee.split(".")[-1].lower().strip()
        recv = cb.callee.rsplit(".", 1)[0] if "." in cb.callee else ""
        pre = split_top(cb.pre_args.rstrip().rstrip(","))
        topic = None
        if pre:
            lit = self.m.lit(pre[0])
            if lit is not None and not lit.dynamic:
                topic = lit.value
        is_worker = bool(re.search(r"(?:^|\s|\.)Worker$", cb.callee.strip()))
        if topic and cb.params and (method in _CONSUME or is_worker):
            local = method in ("on", "once", "addlistener")
            self.assign(cb.params[0], Msg(topic, local=local))
        elif cb.params and method in ("foreach", "map", "flatmap", "filter", "each", "for_each"):
            self.assign(cb.params[0], _elem(self.eval(recv)))
        elif cb.params and method in ("transaction", "tx", "begin", "runintx", "withtx"):
            pass
        self.run(cb.body)

    def exec_stmt(self, text: str) -> None:
        t = text.strip().rstrip(";").strip()
        if not t:
            return
        lang = self.fn.lang
        if lang == "javascript":
            m = re.match(r"^for\s*(?:await\s*)?\(\s*(?:const|let|var)\s+(.+?)\s+of\s+(.+)\)$", t, re.S)
            if m:
                self.bind(m.group(1), _elem(self.eval(m.group(2))))
                return
        if lang == "go":
            m = re.match(r"^for\s+(?:([\w]+)\s*,\s*)?([\w]+)\s*:?=\s*range\s+(.+)$", t, re.S)
            if m:
                if m.group(1):
                    self.assign(m.group(2), _elem(self.eval(m.group(3))))
                return
            if re.match(r"^(?:if|switch|for)\b", t) and ";" in t:
                for part in split_top(re.sub(r"^(?:if|switch|for)\s+", "", t), ";"):
                    self.exec_stmt(part)
                return
        if lang == "python":
            m = re.match(r"^(?:async\s+)?for\s+(.+?)\s+in\s+(.+?):$", t, re.S)
            if m:
                elem = _elem(self.eval(m.group(2)))
                targets = split_top(m.group(1).strip("() "))
                if len(targets) == 1:
                    self.assign(targets[0], elem)
                else:
                    for k, tgt in enumerate(targets):
                        self.assign(tgt, self._index(elem, k))
                return
            m = re.match(r"^(?:async\s+)?with\s+(.+):$", t, re.S)
            if m:
                for item in split_top(m.group(1)):
                    am = re.match(r"^(.+?)\s+as\s+(\w+)$", item, re.S)
                    if am:
                        self.assign(am.group(2), self.eval(am.group(1)))
                    else:
                        self.eval(item)
                return
            if re.match(r"^(?:if|elif|while)\b.*:$", t, re.S):
                self.eval(re.sub(r"^(?:if|elif|while)\s+|:$", "", t))
                return
            if re.match(r"^(?:def|class|async\s+def|try|except|finally|else)\b", t) or t.startswith("@"):
                return
        if re.match(r"^return\b", t):
            rest = t[6:].strip()
            if rest:
                parts = split_top(rest) if lang == "go" else [rest]
                self.returns.append(self.eval(parts[0]))
            return
        if lang != "python" and re.match(r"^(?:if|while|switch|else|try|catch|finally|do|case|default)\b", t):
            cond = re.match(r"^(?:if|while|switch)\s*\((.*)\)$", t, re.S)
            if cond:
                self.eval(cond.group(1))
            elif lang == "go" and re.match(r"^(?:if|switch)\s", t):
                self.exec_stmt(re.sub(r"^(?:if|switch)\s+", "", t))
            return
        t = re.sub(r"^defer\s+|^go\s+", "", t)
        decl = _ASSIGN_KW.match(t)
        body = t[decl.end() :] if decl else t
        if lang == "go":
            gm = re.match(r"^var\s+(.+)$", body)
            if gm:
                body = gm.group(1)
                if _find_assign(body) is None:
                    names = split_top(body.split(" ")[0])
                    for name in names:
                        self.assign(name, None)
                    return
        pos = _find_assign(body)
        if pos is None:
            self.eval(body)
            return
        lhs = body[: pos[0]].strip()
        rhs = body[pos[1] :].strip()
        if lang == "python" and ":" in lhs and not lhs.startswith(("{", "[")):
            lhs = lhs.split(":", 1)[0].strip()
        if lang == "javascript" and ":" in lhs and not lhs.startswith(("{", "[")):
            lhs = lhs.split(":", 1)[0].strip()
        if lang == "go" and " " in lhs.strip() and "," not in lhs:
            lhs = lhs.split()[0]  # `var x T = …`
        value = self.eval(rhs)
        self.bind(lhs, value, multi_first=(lang == "go"))

    def bind(self, lhs: str, value: Any, *, multi_first: bool = False) -> None:
        lhs = lhs.strip()
        if lhs.startswith("{"):
            close = match_close(lhs, 0)
            for part in split_top(lhs[1:close]):
                part = part.strip()
                if not part:
                    continue
                if part.startswith("..."):
                    continue
                km = re.match(r"^([\w$]+)\s*:\s*(.+)$", part, re.S)
                if km:
                    self.bind(km.group(2).split("=")[0], self._field(value, km.group(1)))
                else:
                    name = part.split("=")[0].strip()
                    self.assign(name, self._field(value, name))
            return
        if lhs.startswith("["):
            close = match_close(lhs, 0)
            for k, part in enumerate(split_top(lhs[1:close])):
                self.bind(part, self._index(value, k))
            return
        targets = split_top(lhs.strip("()")) if "," in lhs else [lhs]
        if len(targets) == 1:
            self.assign(targets[0], value)
            return
        if multi_first:
            self.assign(targets[0], value)
            for tgt in targets[1:]:
                self.assign(tgt, None)
            return
        for k, tgt in enumerate(targets):
            self.assign(tgt, self._index(value, k))

    # --- expressions
    def _field(self, value: Any, name: str) -> Any:
        key = name.lower()
        if isinstance(value, Row):
            for item_name, srcs in value.sel.items:
                if item_name == key:
                    return Scalar(srcs)
            for t in value.sel.star_tables:
                cols = self.ctx.columns(t)
                if cols is None or key in cols:
                    hops = _hops_of_sel(value.sel)
                    return Scalar(frozenset({Src(table=t, col=key, hops=hops)}))
            return None
        if isinstance(value, Rows):
            if key == "rows" or key == "recordset" or key == "records":
                return Rows(value.sel, "set")
            return None
        if isinstance(value, Obj):
            if name in value.fields:
                return value.fields[name]
            for k, v in value.fields.items():
                if k.lower() == key:
                    return v
            return None
        if isinstance(value, Msg):
            if key in _MSG_WRAPPERS:
                return value
            topic = f"local:{value.topic}" if value.local else value.topic
            return Scalar(frozenset({Src(table=None, col=None, topic=topic, field=name, hops=value.hops)}))
        if isinstance(value, Scalar):
            if key in ("length", "size"):
                return None
            return Scalar(_with(value.srcs, transform=True))
        return None

    def _index(self, value: Any, k: int) -> Any:
        if isinstance(value, Row):
            if 0 <= k < len(value.sel.items):
                return Scalar(value.sel.items[k][1])
            return None
        if isinstance(value, (Rows, ListOf)):
            return _elem(value)
        if isinstance(value, Arr):
            return value.items[k] if 0 <= k < len(value.items) else None
        return None

    def eval(self, expr: str, depth: int = 0) -> Any:
        if depth > 12:
            return None
        e = expr.strip()
        if not e:
            return None
        e = re.sub(r"^(?:await|yield|new|typeof|void)\s+(?=[\w(\[{])", "", e) if not e.startswith("new ") else e
        e = re.sub(r"^await\s+", "", e)
        e = re.sub(r"\s+as\s+[\w.<>\[\]| ]+$", "", e) if self.fn.lang == "javascript" else e
        e = e.rstrip("!").strip()
        while e[:1] in "&*" and e[1:2] not in ("", "=", "*"):
            e = e[1:].strip()
        if e.startswith("...") :
            e = e[3:]
        lit = self.m.lit(e)
        if lit is not None:
            return Str(lit.value, lit.dynamic)
        if LIT_RE.search(e) and re.fullmatch(r"(?:\x00\d+\x00\s*\+?\s*)+", e):
            return _join_lits(self.m, e)
        if e.startswith("(") and match_close(e, 0) == len(e) - 1:
            return self.eval(e[1:-1], depth + 1)
        # arrow / lambda functions are not values we follow
        if re.match(r"^(?:async\s*)?(?:\([^()]*\)|[\w$]+)\s*=>", e) or e.startswith(("lambda ", "func(", "function")):
            return None
        # ternary / binary operators at depth 0 → transform of every operand
        ops = _top_operands(e, self.fn.lang)
        if ops is not None:
            acc: set[Src] = set()
            for o in ops:
                acc |= sources(self.eval(o, depth + 1))
            return Scalar(_with(frozenset(acc), transform=True)) if acc else None
        if e.startswith("{"):
            close = match_close(e, 0)
            if close == len(e) - 1:
                return self._object(e[1:-1], depth)
        if e.startswith("["):
            close = match_close(e, 0)
            if close == len(e) - 1:
                inner = e[1:-1]
                if self.fn.lang == "python" and re.search(r"\bfor\b.+\bin\b", inner):
                    cm = re.match(r"^(.+?)\s+for\s+(.+?)\s+in\s+(.+?)(?:\s+if\s+.+)?$", inner, re.S)
                    if cm:
                        saved = dict(self.env)
                        self.assign(cm.group(2), _elem(self.eval(cm.group(3), depth + 1)))
                        el = self.eval(cm.group(1), depth + 1)
                        self.env = saved
                        return ListOf(el) if el is not None else None
                    return None
                return Arr([self.eval(p, depth + 1) for p in split_top(inner)])
        if self.fn.lang == "go":
            gm = re.match(r"^(?:\[\])?(?:map\[[^\]]*\][\w.*\[\]{}]*|[\w.]+)\s*\{", e)
            if gm and e.endswith("}"):
                ob = gm.end() - 1
                if match_close(e, ob) == len(e) - 1:
                    return self._object(e[ob + 1 : -1], depth, go=True)
            am = re.match(r"^append\(", e)
            if am:
                args = split_top(e[am.end() : match_close(e, am.end() - 1)])
                if len(args) >= 2:
                    base = self.eval(args[0], depth + 1)
                    el = self.eval(args[1], depth + 1)
                    if el is not None:
                        return ListOf(el)
                    return base
        if "," in e:
            parts = split_top(e)
            if len(parts) > 1:
                return Arr([self.eval(p, depth + 1) for p in parts])
        return self._chain(e, depth)

    def _object(self, inner: str, depth: int, go: bool = False) -> Any:
        fields: dict[str, Any] = {}
        for part in split_top(inner):
            part = part.strip()
            if not part:
                continue
            if part.startswith("..."):
                spread = self.eval(part[3:], depth + 1)
                if isinstance(spread, Obj):
                    fields.update(spread.fields)
                elif isinstance(spread, Row):
                    for name, srcs in spread.sel.items:
                        if name:
                            fields[name] = Scalar(srcs)
                continue
            if part.startswith("**"):
                continue
            km = re.match(r"^(\x00\d+\x00|\[?[\w$]+\]?)\s*:\s*(.+)$", part, re.S)
            if km:
                key = km.group(1)
                lit = self.m.lit(key)
                if lit is not None:
                    key = lit.value
                elif key.startswith("["):
                    continue
                fields[key] = self.eval(km.group(2), depth + 1)
                continue
            if re.fullmatch(r"[\w$]+", part) and not go:
                fields[part] = self.eval(part, depth + 1)
        return Obj(fields)

    def _chain(self, e: str, depth: int) -> Any:
        if e.startswith("("):
            j0 = match_close(e, 0)
            val: Any = self.eval(e[1:j0], depth + 1)
            path = "(…)"
            pure = False
            i = j0 + 1
        else:
            m = re.match(r"^(?:new\s+)?([A-Za-z_$][\w$]*)", e)
            if not m:
                return None
            path = m.group(1)
            val = self.lookup(path)
            pure = True  # still a plain dotted path
            i = m.end()
        n = len(e)
        chain_text = path
        fs_collection: str | None = None
        while i < n:
            while i < n and e[i] in " \t":
                i += 1
            if i >= n:
                break
            c = e[i]
            if e.startswith("?.", i):
                i += 1
                c = "."
            if c == "." or c == "!":
                if c == "!":
                    i += 1
                    continue
                nm = re.compile(r"\.\s*([A-Za-z_$][\w$]*)").match(e, i)
                if not nm:
                    return None
                name = nm.group(1)
                i = nm.end()
                if pure:
                    newpath = f"{path}.{name}"
                    hit = self.lookup(newpath)
                    if hit is not None or (val is None and not self._has_prefix(newpath)):
                        path = newpath
                        if hit is not None:
                            val = hit
                            chain_text = newpath
                            continue
                        val = None
                        chain_text = newpath
                        continue
                    path = newpath
                if val is not None and not (i < n and e[i] == "("):
                    val = self._field(val, name)
                    pure = pure and val is not None and path in self.env
                    chain_text += "." + name
                    continue
                chain_text += "." + name
                if not (i < n and e[i] == "("):
                    val = None if not pure else self.lookup(path)
                continue
            if c == "[":
                j = match_close(e, i)
                key = e[i + 1 : j].strip()
                i = j + 1
                lit = self.m.lit(key)
                pure = False
                if lit is not None:
                    val = self._field(val, lit.value)
                elif re.fullmatch(r"-?\d+", key):
                    val = self._index(val, int(key))
                else:
                    val = _elem(val) if isinstance(val, (Rows, ListOf)) else None
                continue
            if c == "(":
                j = match_close(e, i)
                args = split_top(e[i + 1 : j])
                i = j + 1
                method = chain_text.split(".")[-1]
                recv_text = chain_text.rsplit(".", 1)[0] if "." in chain_text else ""
                recv_val = self.lookup(recv_text) if recv_text and pure else None
                if method == "collection" and args:
                    lit = self.m.lit(args[0])
                    if lit is not None and not lit.dynamic:
                        fs_collection = lit.value
                result = self.call(chain_text, method, recv_text, recv_val, val, args, fs_collection, depth)
                if method in ("doc", "document", "collection", "Collection", "Doc", "where", "Where"):
                    result = result if result is not None else None
                val = result
                pure = False
                chain_text += "()"
                continue
            break
        return val

    def _has_prefix(self, path: str) -> bool:
        prefix = path + "."
        return any(k.startswith(prefix) for k in self.env)

    # --- calls
    def call(
        self,
        chain: str,
        method: str,
        recv_text: str,
        recv_val: Any,
        cur_val: Any,
        args: list[str],
        fs_collection: str | None,
        depth: int,
    ) -> Any:
        ml = method.lower()
        # SQL text in an argument
        sql_idx, sql = self._sql_arg(args)
        if sql is not None and (ml in _QUERY_METHODS or ml.startswith(("query", "exec", "select", "get", "fetch"))):
            return self._sql_call(sql, args[sql_idx + 1 :], recv_text)
        on_rows = cur_val if isinstance(cur_val, (Rows, Row)) else (recv_val if isinstance(recv_val, (Rows, Row)) else None)
        if on_rows is None and recv_text and not chain.endswith(")"):
            on_rows = None
        target_rows = self._receiver_rows(chain)
        if target_rows is not None:
            on_rows = target_rows
        if ml == "scan" and on_rows is not None:
            row = Row(on_rows.sel) if isinstance(on_rows, Rows) else on_rows
            for k, a in enumerate(args):
                a = a.strip().lstrip("&").strip()
                if k < len(row.sel.items):
                    self.assign(a, Scalar(row.sel.items[k][1]))
            return None
        if on_rows is not None:
            if ml in _ROW_METHODS:
                return Row(on_rows.sel)
            if ml in _SET_METHODS:
                return Rows(on_rows.sel, "set")
            if ml in ("scalars", "unique", "execute"):
                return on_rows
        # Firestore writes
        if fs_collection and ml in _FS_WRITE and self.ctx.firestore and args:
            payload_arg = args[1] if self.fn.lang == "go" and len(args) >= 2 else args[0]
            if ml == "add" or ".doc(" in chain or ".document(" in chain or ".Doc(" in chain or ml != "add":
                if (".doc(" in chain or ".document(" in chain or ".Doc(" in chain) or ml == "add":
                    payload = self.eval(payload_arg, depth + 1)
                    self._record_fields(f"collection:{fs_collection.lower()}", payload, collection=fs_collection)
                    return None
        # queue publish
        if ml in _PUBLISH and len(args) >= 2:
            lit = self.m.lit(args[0])
            if lit is not None and not lit.dynamic and not fs_collection:
                payload = self.eval(args[1], depth + 1)
                if payload is not None and sources(payload):
                    self.publishes.append(
                        PublishEvent(
                            fn=self.fn.fid, topic=lit.value, payload=payload, file=self.fn.file, line=self.line,
                            local=ml == "emit",
                        )
                    )
                return None
        # ORM write on a line the detectors saw writing a table
        if ml in _ORM_WRITE and args:
            tables = self._orm_tables()
            if tables:
                payload = None
                for a in args:
                    v = self.eval(a, depth + 1)
                    if isinstance(v, (Obj, Row)):
                        payload = v
                        break
                if isinstance(payload, Obj):
                    for key in ("data", "create", "update", "values", "set", "$set"):
                        sub = payload.fields.get(key)
                        if isinstance(sub, Obj):
                            for t in tables:
                                self._record_fields(t, sub, orm=True)
                    if not any(isinstance(payload.fields.get(k), Obj) for k in ("data", "create", "update", "values", "set", "$set")):
                        flat = Obj({k: v for k, v in payload.fields.items() if k not in ("where", "select", "include")})
                        for t in tables:
                            self._record_fields(t, flat, orm=True)
                    return None
        # pass-through helpers (JSON.parse, dict(row), copy…)
        if ml in _PASS_METHODS and args:
            v = self.eval(args[0], depth + 1)
            if isinstance(v, (Obj, Row, Msg, ListOf, Rows)):
                return v
            if isinstance(v, Scalar):
                return Scalar(_with(v.srcs, transform=True))
            if v is None and isinstance(cur_val, (Obj, Row, Msg)):
                return cur_val
        if ml in _PASS_METHODS and not args and isinstance(cur_val, (Obj, Row, Msg)):
            return cur_val
        # list building
        if ml in ("push", "append", "add") and len(args) == 1 and recv_text:
            el = self.eval(args[0], depth + 1)
            if el is not None:
                self.assign(recv_text, ListOf(el))
            return None
        # Celery / RQ style: `task.delay(a, b)`, `task.apply_async(args=[a, b])`
        if ml in ("delay", "apply_async", "send_with_options", "enqueue_call") and recv_text:
            name = recv_text.split(".")[-1]
            if self.callees.get("@queue:" + name):
                call_args = args
                if ml != "delay":
                    km = next((a for a in args if re.match(r"^args\s*=", a)), None)
                    call_args = split_top(re.sub(r"^args\s*=\s*", "", km).strip()[1:-1]) if km else []
                result = self._user_call(name, call_args, depth)
                return None if result is _NO_CALL else result
        # a function of this service
        result = self._user_call(method, args, depth)
        if result is not _NO_CALL:
            return result
        # unknown function of tainted values: a transformation
        acc: set[Src] = set()
        for a in args:
            acc |= sources(self.eval(a, depth + 1))
        if isinstance(cur_val, Scalar):
            acc |= cur_val.srcs
        return Scalar(_with(frozenset(acc), transform=True)) if acc else None

    def _receiver_rows(self, chain: str) -> Any:
        base = chain.rsplit(".", 1)[0] if "." in chain else ""
        if not base or base.endswith(")"):
            return None
        v = self.lookup(base)
        return v if isinstance(v, (Rows, Row)) else None

    def _sql_arg(self, args: list[str]) -> tuple[int, str | None]:
        for k, a in enumerate(args[:3]):
            a = a.strip()
            tm = re.match(r"^(?:sa\.|sqlalchemy\.)?text\((.*)\)$", a, re.S)
            if tm:
                a = tm.group(1)
            v = self.eval(a) if (LIT_RE.search(a) or re.fullmatch(r"[\w.]+", a)) else None
            if isinstance(v, Str) and not v.dynamic and _SQL_START.match(v.value):
                return k, v.value
            if isinstance(v, Str) and v.dynamic:
                return -1, None
        return -1, None

    def _sql_call(self, sql: str, rest: list[str], recv_text: str) -> Any:
        parsed = parse_sql(sql)
        if parsed is None:
            return None
        kind, obj = parsed
        if kind == "select":
            sel = self._resolve_select(obj)
            if sel is None:
                return None
            rows = Rows(sel, "cursor")
            if recv_text and self.fn.lang == "python" and re.fullmatch(r"[\w.]+", recv_text):
                self.assign(recv_text, rows)  # cursor.execute(SELECT…) → the cursor holds the rows
            return rows
        w: SqlWrite = obj
        target = self.ctx.table_node(w.table, write=True)
        if target is None or target not in self.ctx.touch.get(self.fn.fid, set()):
            return None
        params = self._params(rest)
        from_sel = None
        if w.from_tables:
            from_sel = SqlSelect(tables=w.from_tables, order=w.from_order, items=[])
        for col, spec in w.cols.items():
            srcs: set[Src] = set()
            for p in spec.params:
                v = params(p)
                s = sources(v)
                if spec.transform:
                    s = _with(s, transform=True)
                srcs |= s
            if spec.refs and from_sel is not None:
                for q, c in spec.refs:
                    t = self._resolve_ref(from_sel, q, c)
                    if t is not None:
                        srcs.add(Src(table=t, col=c, transform=spec.transform))
            if srcs:
                self.writes.append(
                    WriteEvent(
                        fn=self.fn.fid, target=target, col=col, srcs=frozenset(srcs), file=self.fn.file,
                        line=self.line, spec_transform=spec.transform,
                    )
                )
        if not w.cols:
            # INSERT without a column list: table-level only
            srcs2: set[Src] = set()
            for k in range(1, 30):
                srcs2 |= {Src(table=s.table, col=None, transform=s.transform, hops=s.hops, topic=s.topic) for s in sources(params(k))}
            if srcs2:
                self.writes.append(WriteEvent(fn=self.fn.fid, target=target, col=None, srcs=frozenset(srcs2), file=self.fn.file, line=self.line))
        return None

    def _params(self, rest: list[str]) -> Callable[[int | str], Any]:
        lang = self.fn.lang
        values: list[Any] = []
        named: dict[str, Any] = {}
        if lang in ("javascript", "python") and rest:
            v = self.eval(rest[0])
            if isinstance(v, Arr):
                values = v.items
            elif isinstance(v, Obj):
                named = v.fields
            elif isinstance(v, (Row, Scalar)) and lang == "python":
                values = [v]
            elif lang == "javascript" and len(rest) == 1:
                values = []
            else:
                values = [self.eval(a) for a in rest]
        else:
            values = [self.eval(a) for a in rest]
            if len(values) == 1 and isinstance(values[0], Obj):
                named = values[0].fields

        def get(p: int | str) -> Any:
            if isinstance(p, int):
                return values[p - 1] if 0 < p <= len(values) else None
            if p in named:
                return named[p]
            low = p.lower().replace("_", "")
            for k, v in named.items():
                if k.lower().replace("_", "") == low:
                    return v
            return None

        return get

    def _resolve_ref(self, sel: SqlSelect, q: str | None, col: str) -> str | None:
        if q:
            name = sel.tables.get(q)
            return self.ctx.table_node(name, write=False) if name else None
        if len(sel.order) == 1:
            return self.ctx.table_node(sel.order[0], write=False)
        hits = []
        for name in sel.order:
            tid = self.ctx.table_node(name, write=False)
            cols = self.ctx.columns(tid) if tid else None
            if cols and col in cols:
                hits.append(tid)
        return hits[0] if len(hits) == 1 else None

    def _resolve_select(self, sel: SqlSelect) -> ResolvedSelect | None:
        tables = [t for t in (self.ctx.table_node(n, write=False) for n in sel.order) if t]
        touched = self.ctx.touch.get(self.fn.fid, set())
        tables = [t for t in tables if t in touched]
        if not tables:
            return None
        items: list[tuple[str | None, frozenset[Src]]] = []
        stars: list[str] = []
        for it in sel.items:
            if it.star:
                if it.star == "*":
                    stars += tables
                else:
                    t = self.ctx.table_node(sel.tables.get(it.star, it.star), write=False)
                    if t:
                        stars.append(t)
                continue
            srcs: set[Src] = set()
            for q, c in it.refs:
                t = self._resolve_ref(sel, q, c)
                if t is not None and t in tables:
                    srcs.add(Src(table=t, col=c, transform=it.transform))
            items.append((it.name, frozenset(srcs)))
        return ResolvedSelect(items=items, star_tables=stars, tables=tables)

    def _orm_tables(self) -> list[str]:
        out = []
        for t in self.ctx.direct_w.get(self.fn.fid, ()):
            lines = self.ctx.write_lines.get((self.fn.file, t)) or set()
            if any(abs(ln - self.line) <= 2 for ln in lines):
                out.append(t)
        return sorted(out)

    def _record_fields(self, target: str, payload: Any, *, collection: str | None = None, orm: bool = False) -> None:
        if isinstance(payload, Row):
            payload = Obj({n: Scalar(s) for n, s in payload.sel.items if n})
        if not isinstance(payload, Obj):
            srcs = sources(payload)
            if srcs:
                self.writes.append(WriteEvent(fn=self.fn.fid, target=target, col=None, srcs=srcs, file=self.fn.file, line=self.line))
            return
        cols = self.ctx.columns(target) if orm else None
        for key, v in payload.fields.items():
            srcs = sources(v)
            if not srcs:
                continue
            col = key.lower()
            if cols is not None:
                snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()
                col = col if col in cols else (snake if snake in cols else None)
                if col is None:
                    continue
            if isinstance(v, (Obj, Row, Rows, ListOf, Arr)):
                srcs = frozenset(Src(table=s.table, col=None, transform=s.transform, hops=s.hops, topic=s.topic) for s in srcs)
            self.writes.append(
                WriteEvent(fn=self.fn.fid, target=target, col=col, srcs=srcs, file=self.fn.file, line=self.line)
            )

    def _user_call(self, method: str, args: list[str], depth: int) -> Any:
        if self.depth >= MAX_DEPTH:
            return _NO_CALL
        cands = self.callees.get(method)
        if not cands or len(set(cands)) != 1:
            return _NO_CALL
        cid = cands[0]
        info = self.ctx.fns.get(cid)
        if info is None or cid == self.fn.fid:
            return _NO_CALL
        hop_ids: tuple[str, ...] = (self.fn.fid,)
        queues = self.callees.get("@queue:" + method)
        if queues:
            hop_ids = (self.fn.fid, queues[0])
        arg_vals = [self.eval(a, depth + 1) for a in args]
        tainted = any(sources(v) for v in arg_vals)
        result: Any = None
        if tainted and self.ctx.direct_w.get(cid) is not None or tainted and cid in self.ctx.calls:
            sub = _analyze(self.ctx, info, depth=self.depth + 1, hop=(*self.hop, *hop_ids), args=arg_vals, method_call=True)
            if sub is not None:
                self.writes.extend(sub.writes)
                self.publishes.extend(sub.publishes)
        if self.ctx.touch.get(cid):
            summary = self.ctx.summaries.get(cid, _NO_CALL)
            if summary is _NO_CALL:
                self.ctx.summaries[cid] = None  # recursion guard
                sub = _analyze(self.ctx, info, depth=self.depth + 1, hop=(), args=None)
                summary = next((r for r in (sub.returns if sub else []) if r is not None), None)
                self.ctx.summaries[cid] = summary
            if summary is not None:
                result = _hop_value(summary, cid)
        return result


_NO_CALL = object()


def _hops_of_sel(sel: ResolvedSelect) -> tuple[str, ...]:
    for _, srcs in sel.items:
        for s in srcs:
            return s.hops
    return ()


def _top_operands(e: str, lang: str) -> list[str] | None:
    """Operands of a top-level binary / ternary expression, else None."""
    depth = 0
    cuts: list[tuple[int, int]] = []
    i = 0
    n = len(e)
    while i < n:
        c = e[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0:
            two = e[i : i + 2]
            three = e[i : i + 3]
            if three in ("===", "!==", "**="):
                return None
            if two in ("||", "&&", "??", "==", "!=", "<=", ">="):
                cuts.append((i, i + 2))
                i += 2
                continue
            if two in ("=>",):
                return None
            if c in "+-*/%?|" and i > 0:
                prev = e[:i].rstrip()
                if prev and prev[-1] not in "([{,=:?+-*/%!&|<>" and not (c == "-" and prev[-1] == "e"):
                    cuts.append((i, i + 1))
            elif c == ":" and any(e[a:b] == "?" for a, b in cuts):
                cuts.append((i, i + 1))
            if lang == "python":
                wm = re.compile(r"\s(or|and|if|else)\s").match(e, i - 1) if i > 0 else None
                if wm and depth == 0:
                    cuts.append((wm.start(1), wm.end(1)))
                    i = wm.end(1)
                    continue
        i += 1
    if not cuts:
        return None
    parts: list[str] = []
    last = 0
    for a, b in sorted(cuts):
        parts.append(e[last:a])
        last = b
    parts.append(e[last:])
    return [p for p in (x.strip() for x in parts) if p]


def _header_params(header: str, info: FnInfo) -> list[str]:
    name = info.name.split(".")[-1]
    lang = info.lang
    if lang == "python":
        m = re.search(rf"def\s+{re.escape(name)}\s*\(", header) or re.search(r"def\s+\w+\s*\(", header)
    elif lang == "go":
        m = re.search(rf"\b{re.escape(name)}\s*\(", header) or re.search(r"func\s*(?:\([^)]*\)\s*)?\w*\s*\(", header)
    else:
        m = re.search(rf"\b{re.escape(name)}\s*(?:=\s*(?:async\s*)?(?:function\s*)?)?\(", header) or re.search(r"\(", header)
    if not m:
        return []
    j = match_close(header, m.end() - 1)
    return _param_names(header[m.end() : j], lang)


_SQL_WRITE_START = re.compile(r"\b(insert\s+into|update)\b", re.I)


class _SqlFnResult:
    def __init__(self) -> None:
        self.writes: list[WriteEvent] = []
        self.publishes: list[PublishEvent] = []
        self.returns: list[Any] = []


def _analyze_sql(ctx: RepoContext, info: FnInfo) -> _SqlFnResult | None:
    """A SQL function's body: ``INSERT INTO b (…) SELECT … FROM a`` and
    ``UPDATE b SET x = a.y FROM a`` map columns in one statement."""
    text = ctx.text(info.file)
    if not text:
        return None
    lines = text.split("\n")[info.start - 1 : info.end]
    body = "\n".join(lines)
    res = _SqlFnResult()
    touched = ctx.touch.get(info.fid, set())
    pos = 0
    for m in _SQL_WRITE_START.finditer(body):
        if m.start() < pos:
            continue
        end = body.find(";", m.start())
        end = len(body) if end < 0 else end
        pos = end
        stmt = body[m.start() : end]
        parsed = parse_sql(stmt)
        if parsed is None or parsed[0] != "write":
            continue
        w: SqlWrite = parsed[1]
        target = ctx.table_node(w.table, write=True)
        if target is None or target not in touched or not w.from_tables:
            continue
        sel = SqlSelect(tables=w.from_tables, order=w.from_order, items=[])
        line = info.start + body.count("\n", 0, m.start())
        for col, spec in w.cols.items():
            srcs: set[Src] = set()
            for q, c in spec.refs:
                t = _resolve_sql_ref(ctx, sel, q, c)
                if t is not None and t in touched and t != target:
                    srcs.add(Src(table=t, col=c, transform=spec.transform))
            if srcs:
                res.writes.append(
                    WriteEvent(fn=info.fid, target=target, col=col, srcs=frozenset(srcs), file=info.file, line=line)
                )
    return res


def _resolve_sql_ref(ctx: RepoContext, sel: SqlSelect, q: str | None, col: str) -> str | None:
    if q:
        name = sel.tables.get(q)
        return ctx.table_node(name, write=False) if name else None
    if len(sel.order) == 1:
        return ctx.table_node(sel.order[0], write=False)
    hits = []
    for name in sel.order:
        tid = ctx.table_node(name, write=False)
        cols = ctx.columns(tid) if tid else None
        if cols and col in cols:
            hits.append(tid)
    return hits[0] if len(hits) == 1 else None


def _analyze(
    ctx: RepoContext,
    info: FnInfo,
    *,
    depth: int = 0,
    hop: tuple[str, ...] = (),
    args: list[Any] | None = None,
    method_call: bool = False,
) -> Analyzer | None:
    if info.lang == "sql":
        return _analyze_sql(ctx, info) if depth == 0 else None  # type: ignore[return-value]
    m = ctx.masked_file(info.file)
    if m is None:
        return None
    lines = m.text.split("\n")
    body_lines = lines[info.start - 1 : info.end]
    if info.name.endswith(".module") or info.name == "<module>":
        inner = [f for f in ctx.by_file.get(info.file, []) if f.fid != info.fid]
        for f in inner:
            for ln in range(max(f.start, info.start), min(f.end, info.end) + 1):
                body_lines[ln - info.start] = ""
    else:
        for f in ctx.by_file.get(info.file, []):
            if f.fid == info.fid:
                continue
            if f.start >= info.start and f.end <= info.end and (f.start, f.end) != (info.start, info.end) and f.start > info.start:
                for ln in range(f.start, f.end + 1):
                    body_lines[ln - info.start] = ""
    text = "\n".join(body_lines)
    if len(text) > MAX_BODY_CHARS or (body_lines and len(text) / len(body_lines) > 400):
        return None  # minified / bundled code: no statements to follow
    stmts = split_statements(text, info.start, info.lang)
    header = ""
    is_module = info.name.endswith(".module") or info.name == "<module>"
    if stmts and not is_module:
        first = stmts[0]
        if first.callback is None and re.search(r"\bfunc\b|\bfunction\b|=>|\bdef\b|^\s*(?:async\s+)?[\w$]+\s*\(", first.text):
            header = first.text
            stmts = stmts[1:]
        if info.lang == "python":
            while stmts and stmts[0].text.startswith("@"):
                stmts = stmts[1:]
            if stmts and re.match(r"^(?:async\s+)?def\b", stmts[0].text):
                header = stmts[0].text
                stmts = stmts[1:]
    an = Analyzer(ctx, info, m, depth=depth, hop=hop)
    if args is not None and header:
        params = _header_params(header, info)
        if info.lang == "python" and method_call and params and params[0] in ("self", "cls"):
            params = params[1:]
        if info.lang == "go":
            pass
        for k, p in enumerate(params):
            if k < len(args):
                val = _hop_value(args[k], hop[-1]) if hop and args[k] is not None else args[k]
                if p.startswith("{"):
                    an.bind(p, val)
                else:
                    an.assign(p, val)
    an.run(stmts)
    return an


# -------------------------------------------------------- feeds edges ----


def _evidence_dict(file: str, line: int, fid: str) -> dict[str, Any]:
    return {"file": file, "line": line, "symbol": fid.split(":", 1)[-1]}


def _name(graph: Graph, tid: str) -> str:
    node = graph.nodes.get(tid)
    if node is not None and node.attrs.get("name"):
        return str(node.attrs["name"])
    return tid.split(":", 1)[-1]


def _fk_only(graph: Graph, src: str, dst: str, entries: list[dict]) -> bool:
    """Every mapped column of ``dst`` is a foreign key to ``src``: a reference, not a copy."""
    if not entries:
        return False
    tables = (graph.schema or {}).get("tables") or {}
    spec = tables.get(_name(graph, dst)) or {}
    src_name = _name(graph, src).lower()
    fk_cols = {
        str(c).lower()
        for fk in spec.get("foreign_keys") or []
        if str(fk.get("ref_table") or "").lower().split(".")[-1] == src_name.split(".")[-1]
        for c in fk.get("columns") or []
    }
    if not fk_cols:
        return False
    return all(e["to"].split(".", 1)[1] in fk_cols for e in entries)


_ID_COL = re.compile(r"(?i:(?:^|_)(?:id|uuid|key)$)|[a-z0-9]Id$")


def _ids_only(entries: list[dict]) -> bool:
    """Only identifiers copied into identifier columns (``users.id -> orders.user_id``):
    the row points at the source, it is not fed from it."""
    if not entries:
        return False
    for e in entries:
        src_col = e["from"].split(".", 1)[1]
        dst_col = e["to"].split(".", 1)[1]
        if not (_ID_COL.search(src_col) and _ID_COL.search(dst_col)):
            return False
    return True


def _conf_rank(c: str) -> int:
    return {"exact": 2, "heuristic": 1}.get(c, 0)


def add_feed(
    graph: Graph,
    src: str,
    dst: str,
    *,
    path: list[str],
    confidence: str,
    evidence: dict[str, Any],
    entries: list[dict[str, Any]],
) -> None:
    """Add (or extend) ``feeds src -> dst``."""
    eid = edge_id("feeds", src, dst)
    edge = graph.edges.get(eid)
    via_item = {"path": list(path), "confidence": confidence, "evidence": evidence}
    if edge is None:
        edge = graph.add_edge(
            "feeds", src, dst, confidence=confidence, evidence=Evidence.from_dict(evidence),
        )
        edge.attrs["via"] = []
        edge.attrs["via_total"] = 0
        edge.attrs["column_map"] = []
    else:
        if _conf_rank(confidence) > _conf_rank(edge.confidence.value):
            edge.confidence = Confidence(confidence)
    merge_feed_attrs(edge.attrs, {"via": [via_item], "via_total": 1, "column_map": entries})


def merge_feed_attrs(into: dict[str, Any], other: dict[str, Any]) -> None:
    """Union of two ``feeds`` edges' ``via`` / ``column_map`` (dedup, best confidence)."""
    all_via = list(into.get("via_all") or into.get("via") or [])
    seen = {tuple(v.get("path") or []) for v in all_via}
    added = 0
    for v in other.get("via") or []:
        key = tuple(v.get("path") or [])
        if key in seen:
            continue
        seen.add(key)
        all_via.append(v)
        added += 1
    extra = max(int(other.get("via_total") or 0) - len(other.get("via") or []), 0)
    total = max(int(into.get("via_total") or 0), len(into.get("via") or [])) + added + extra
    all_via.sort(key=lambda v: (-_conf_rank(str(v.get("confidence"))), len(v.get("path") or []), v.get("path") or []))
    into["via"] = all_via[:MAX_VIA]
    into["via_total"] = total
    into.pop("via_all", None)
    cmap: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in list(into.get("column_map") or []) + list(other.get("column_map") or []):
        key = (e["from"], e["to"], e.get("fn") or "")
        old = cmap.get(key)
        if old is None or _conf_rank(e["confidence"]) > _conf_rank(old["confidence"]):
            cmap[key] = e
    into["column_map"] = sorted(cmap.values(), key=lambda e: (e["from"], e["to"], e.get("fn") or ""))


def _build_feeds(ctx: RepoContext, writes: list[WriteEvent], skip_queue: bool = True) -> None:
    graph = ctx.graph
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for w in writes:
        for s in w.srcs:
            if s.table is None or s.table == w.target:
                continue
            if s.col is None and s.transform:
                continue  # a whole row through some function: no value seen
            path = [*reversed(s.hops), w.fn]  # hops are newest-first
            # drop repeats (a hop can be the writer itself)
            dedup: list[str] = []
            for p in path:
                if not dedup or dedup[-1] != p:
                    dedup.append(p)
            path = dedup
            direct = len(path) == 1
            key = (s.table, w.target)
            bucket = pairs.setdefault(key, {"paths": {}, "entries": []})
            ev = _evidence_dict(w.file, w.line, w.fn)
            pconf = "exact" if direct else "heuristic"
            old = bucket["paths"].get(tuple(path))
            if old is None or _conf_rank(pconf) > _conf_rank(old[0]):
                bucket["paths"][tuple(path)] = (pconf, ev)
            if s.col and w.col:
                transform = s.transform
                entry: dict[str, Any] = {
                    "from": f"{_name(graph, s.table)}.{s.col}",
                    "to": f"{_name(graph, w.target)}.{w.col}",
                    "fn": w.fn,
                    "confidence": "exact" if direct and not transform else "heuristic",
                    "evidence": ev,
                }
                if transform:
                    entry["transform"] = True
                if not direct:
                    entry["path"] = path
                bucket["entries"].append(entry)
    for (src, dst), bucket in sorted(pairs.items()):
        entries = bucket["entries"]
        if _fk_only(graph, src, dst, entries) or _ids_only(entries):
            continue
        paths = sorted(bucket["paths"].items(), key=lambda kv: (-_conf_rank(kv[1][0]), len(kv[0]), kv[0]))
        if not entries:
            entries = _name_matches(ctx, src, dst, paths[0][0][-1], paths[0][1][1])
        best_conf = paths[0][1][0]
        first = True
        for path, (pconf, ev) in paths:
            add_feed(graph, src, dst, path=list(path), confidence=pconf, evidence=ev, entries=entries if first else [])
            first = False
        edge = graph.edges[edge_id("feeds", src, dst)]
        if _conf_rank(best_conf) > _conf_rank(edge.confidence.value):
            edge.confidence = Confidence(best_conf)


def _name_matches(ctx: RepoContext, src: str, dst: str, fn: str, ev: dict[str, Any]) -> list[dict[str, Any]]:
    a = ctx.columns(src) or []
    b = set(ctx.columns(dst) or [])
    out = []
    for col in a:
        if col in b and col not in GENERIC_COLUMNS:
            out.append(
                {
                    "from": f"{_name(ctx.graph, src)}.{col}",
                    "to": f"{_name(ctx.graph, dst)}.{col}",
                    "fn": fn,
                    "confidence": "heuristic",
                    "match": "name",
                    "evidence": ev,
                }
            )
    return out


# ---------------------------------------------------------- queues ----


def _stash_flows(ctx: RepoContext, publishes: list[PublishEvent], writes: list[WriteEvent]) -> None:
    graph = ctx.graph
    for p in publishes:
        node = graph.nodes.get(p.fn)
        if node is None or not isinstance(p.payload, Obj):
            continue
        fields: dict[str, list[dict[str, Any]]] = {}
        for key, v in p.payload.fields.items():
            items = []
            for s in sorted(sources(v), key=lambda s: (s.table or "", s.col or "")):
                if s.table is None:
                    continue
                item: dict[str, Any] = {"table": s.table, "column": s.col}
                if s.transform:
                    item["transform"] = True
                if s.hops:
                    item["hops"] = list(s.hops)
                items.append(item)
            if items:
                fields[key] = items
        if not fields:
            continue
        bucket = list(node.attrs.get("dataflow_out") or [])
        entry = {"topic": p.topic, "fields": fields, "evidence": _evidence_dict(p.file, p.line, p.fn)}
        if p.local:
            entry["local"] = True
        if entry not in bucket:
            bucket.append(entry)
        node.attrs["dataflow_out"] = bucket
    consumed: dict[tuple[str, str, int], dict[str, Any]] = {}
    for w in writes:
        for s in w.srcs:
            if s.topic is None or s.field is None:
                continue
            key = (w.fn, s.topic, w.line)
            local = s.topic.startswith("local:")
            topic = s.topic[len("local:") :] if local else s.topic
            entry = consumed.setdefault(
                key,
                {"topic": topic, "fields": {}, "evidence": _evidence_dict(w.file, w.line, w.fn), **({"local": True} if local else {})},
            )
            item: dict[str, Any] = {"table": w.target, "column": w.col}
            if s.transform:
                item["transform"] = True
            lst = entry["fields"].setdefault(s.field, [])
            if item not in lst:
                lst.append(item)
    for (fn, _t, _l), entry in sorted(consumed.items()):
        node = graph.nodes.get(fn)
        if node is None:
            continue
        bucket = list(node.attrs.get("dataflow_in") or [])
        if entry not in bucket:
            bucket.append(entry)
        node.attrs["dataflow_in"] = bucket


def _resolve_in(graph: Graph, node_id: str, local: str) -> str | None:
    repo = (graph.nodes.get(node_id).attrs.get("repo") if graph.nodes.get(node_id) else None)
    if repo:
        cand = f"{repo}/{local}"
        if cand in graph.nodes:
            return cand
    if local in graph.nodes:
        return local
    return None


def _repo_of(graph: Graph, node_id: str) -> str | None:
    node = graph.nodes.get(node_id)
    return str(node.attrs.get("repo")) if node is not None and node.attrs.get("repo") else None


def _queue_node(graph: Graph, topic: str) -> str:
    for node in graph.nodes.values():
        if node.type == "queue" and str(node.attrs.get("name") or "") == topic:
            return node.id
    qid = f"queue:topic:{topic}"
    graph.add_node(qid, "queue", kind="topic", name=topic)
    return qid


def link_queue_flows(graph: Graph) -> None:
    """Producer (``dataflow_out``) → consumer (``dataflow_in``) of the same topic → ``feeds``."""
    outs: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    ins: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for node in graph.nodes.values():
        for e in node.attrs.get("dataflow_out") or []:
            outs[str(e.get("topic"))].append((node.id, e))
        for e in node.attrs.get("dataflow_in") or []:
            ins[str(e.get("topic"))].append((node.id, e))
    for topic in sorted(set(outs) & set(ins)):
        qid: str | None = None
        for prod, pe in sorted(outs[topic], key=lambda x: x[0]):
            for cons, ce in sorted(ins[topic], key=lambda x: x[0]):
                if prod == cons:
                    continue
                if bool(pe.get("local")) != bool(ce.get("local")):
                    continue
                if pe.get("local") and _repo_of(graph, prod) != _repo_of(graph, cons):
                    continue
                pairs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
                for fname, srcs in sorted((pe.get("fields") or {}).items()):
                    dests = (ce.get("fields") or {}).get(fname) or []
                    for s in srcs:
                        st = _resolve_in(graph, prod, s["table"])
                        if st is None:
                            continue
                        for d in dests:
                            dt = _resolve_in(graph, cons, d["table"])
                            if dt is None or dt == st:
                                continue
                            ent: list[dict[str, Any]] = pairs[(st, dt)]
                            if s.get("column") and d.get("column"):
                                item = {
                                    "from": f"{_name(graph, st)}.{s['column']}",
                                    "to": f"{_name(graph, dt)}.{d['column']}",
                                    "fn": cons,
                                    "confidence": "heuristic",
                                    "evidence": ce.get("evidence"),
                                    "field": fname,
                                }
                                if s.get("transform") or d.get("transform"):
                                    item["transform"] = True
                                ent.append(item)
                if not pairs:
                    continue
                if qid is None:
                    qid = _queue_node(graph, topic)
                for (st, dt), entries in sorted(pairs.items()):
                    path = [prod, qid, cons]
                    for e in entries:
                        e["path"] = path
                    add_feed(graph, st, dt, path=path, confidence="heuristic", evidence=ce.get("evidence") or {}, entries=entries)


# ------------------------------------------------------------ entry ----

_QUEUE_HINT = re.compile(r"\.(?:publish|subscribe|send|emit|enqueue|consume|process|produce)\w*\(|\bWorker\(|\.on\(")
_FS_HINT = re.compile(r"\.(?:collection|Collection)\(")


def apply(graph: Graph, root: Path, texts: dict[str, str] | None = None, *, firestore: bool = False) -> None:
    """Add ``feeds`` edges (with ``column_map``) to a freshly scanned graph."""
    ctx = RepoContext(graph, root, texts, firestore=firestore)
    candidates: list[FnInfo] = []
    writer_ids = {f for f, ts in ctx.direct_w.items() if ts}
    hint_files: dict[str, bool] = {}
    for info in ctx.fns.values():
        fid = info.fid
        reads_any = fid in ctx.reads_any
        writes = bool(ctx.direct_w.get(fid))
        calls_writer = bool(ctx.calls.get(fid, set()) & writer_ids) or bool(ctx.queue_targets.get(fid))
        calls_reader = any(c in ctx.reads_any for c in ctx.calls.get(fid, ()))
        ok = (writes and (reads_any or calls_reader)) or ((reads_any or calls_reader) and calls_writer)
        if not ok:
            hint = hint_files.get(info.file)
            if hint is None:
                text = ctx.text(info.file) or ""
                hint = bool(_QUEUE_HINT.search(text) or (firestore and _FS_HINT.search(text)))
                hint_files[info.file] = hint
            ok = hint and (reads_any or writes or calls_reader or info.name.endswith(".module"))
        if ok:
            candidates.append(info)
    writes: list[WriteEvent] = []
    publishes: list[PublishEvent] = []
    for info in sorted(candidates, key=lambda f: f.fid):
        try:
            an = _analyze(ctx, info)
        except Exception:  # noqa: BLE001 - one odd function never costs the others their feeds
            continue
        if an is None:
            continue
        writes.extend(an.writes)
        publishes.extend(an.publishes)
    # Firestore destinations the detectors did not record become collection nodes
    for w in writes:
        if w.target.startswith("collection:") and w.target not in graph.nodes:
            if any(s.table for s in w.srcs):
                graph.add_node(w.target, "collection", name=w.target.split(":", 1)[1])
    _build_feeds(ctx, writes)
    _stash_flows(ctx, publishes, writes)
    link_queue_flows(graph)
    if firestore:
        for node in graph.nodes.values():
            if node.type == "collection" and not node.attrs.get("db"):
                node.attrs["db"] = firestore if isinstance(firestore, str) else "firestore:default"


def qualify_feed_attrs(attrs: dict[str, Any], qualify: Callable[[str], str]) -> dict[str, Any]:
    """Repo-qualify the function / queue ids inside a ``feeds`` edge (registry merge)."""
    out = dict(attrs)
    out["via"] = [
        {**v, "path": [qualify(p) for p in v.get("path") or []]} for v in attrs.get("via") or []
    ]
    cmap = []
    for e in attrs.get("column_map") or []:
        e2 = dict(e)
        if e2.get("fn"):
            e2["fn"] = qualify(e2["fn"])
        if e2.get("path"):
            e2["path"] = [qualify(p) for p in e2["path"]]
        cmap.append(e2)
    out["column_map"] = cmap
    return out


# ------------------------------------------------------------ queries ----


def feeds_into(graph: Graph, table_id: str) -> list[Any]:
    """``feeds`` edges whose destination is ``table_id``."""
    return sorted((e for e in graph.edges.values() if e.type == "feeds" and e.to_id == table_id), key=lambda e: e.from_id)


def feeds_from(graph: Graph, table_id: str) -> list[Any]:
    return sorted((e for e in graph.edges.values() if e.type == "feeds" and e.from_id == table_id), key=lambda e: e.to_id)


MAX_TRACE_HOPS = 3
MAX_TRACE_LINES = 60


def _table_id_of(graph: Graph, node_id: str) -> tuple[str, str | None]:
    """(table / collection id, column name or None) for a table, column or collection id."""
    bare = node_id.split("/", 1)[1] if "/" in node_id.split(":", 1)[0] else node_id
    prefix = node_id[: len(node_id) - len(bare)]
    if bare.startswith("column:"):
        node = graph.nodes.get(node_id)
        table = str((node.attrs.get("table") if node else None) or bare[len("column:") :].rsplit(".", 1)[0]).lower()
        col = str((node.attrs.get("name") if node else None) or bare.rsplit(".", 1)[-1]).lower()
        tid = f"{prefix}table:{table}"
        if tid not in graph.nodes and f"{prefix}view:{table}" in graph.nodes:
            tid = f"{prefix}view:{table}"
        return tid, col
    return node_id, None


def _short_fn(graph: Graph, fid: str) -> str:
    node = graph.nodes.get(fid)
    if node is None:
        return fid.split(":", 1)[-1]
    if node.type == "queue":
        return f"queue {node.attrs.get('name') or fid.split(':')[-1]}"
    name = str(node.attrs.get("name") or fid.split(":", 1)[-1])
    repo = node.attrs.get("repo")
    return f"{repo}/{name}" if repo and "/" in fid.split(":", 1)[0] else name


def _path_text(graph: Graph, entry: dict[str, Any]) -> str:
    path = entry.get("path") or ([entry["fn"]] if entry.get("fn") else [])
    return " → ".join(_short_fn(graph, p) for p in path)


@dataclass
class FlowHop:
    depth: int
    other: str  # table id on the other side
    other_col: str | None
    entry: dict[str, Any] | None  # column_map entry (None: table-level feeds)
    edge: Any


def trace(graph: Graph, node_id: str, direction: str, max_hops: int = MAX_TRACE_HOPS) -> list[FlowHop]:
    """Where a column / table comes from (``direction="in"``) or goes to (``"out"``),
    following ``feeds`` up to ``max_hops`` steps (cycles skipped)."""
    feeds_by_to: dict[str, list[Any]] = defaultdict(list)
    feeds_by_from: dict[str, list[Any]] = defaultdict(list)
    for e in graph.edges.values():
        if e.type == "feeds":
            feeds_by_to[e.to_id].append(e)
            feeds_by_from[e.from_id].append(e)
    tid, col = _table_id_of(graph, node_id)
    out: list[FlowHop] = []

    def walk(t: str, c: str | None, depth: int, ancestors: frozenset[tuple[str, str | None]]) -> None:
        if depth > max_hops or len(out) >= MAX_TRACE_LINES:
            return
        edges = feeds_by_to.get(t, []) if direction == "in" else feeds_by_from.get(t, [])
        for e in sorted(edges, key=lambda x: (x.from_id, x.to_id)):
            other = e.from_id if direction == "in" else e.to_id
            entries = e.attrs.get("column_map") or []
            mine = _name(graph, t)
            if c is not None:
                key = f"{mine}.{c}"
                hits = [x for x in entries if (x["to"] if direction == "in" else x["from"]) == key]
                for x in sorted(hits, key=lambda x: (-_conf_rank(x["confidence"]), x["from"], x["to"])):
                    other_col = (x["from"] if direction == "in" else x["to"]).split(".", 1)[1]
                    if (other, other_col) in ancestors or len(out) >= MAX_TRACE_LINES:
                        continue
                    out.append(FlowHop(depth, other, other_col, x, e))
                    walk(other, other_col, depth + 1, ancestors | {(other, other_col)})
                continue
            if (other, None) in ancestors or len(out) >= MAX_TRACE_LINES:
                continue
            out.append(FlowHop(depth, other, None, None, e))
            walk(other, None, depth + 1, ancestors | {(other, None)})

    walk(tid, col, 1, frozenset({(tid, col)}))
    return out


def why_lines(graph: Graph, node_id: str) -> list[str]:
    """``comes from`` / ``goes to`` sections for ``bizmap why`` (empty without feeds)."""
    from bizmap.dbident import table_label

    lines: list[str] = []
    for title, direction in (("comes from (data flow)", "in"), ("goes to (data flow)", "out")):
        hops = trace(graph, node_id, direction)
        if not hops:
            continue
        lines.append("")
        lines.append(f"{title}:")
        for h in hops:
            indent = "  " * h.depth
            arrow = "←" if direction == "in" else "→"
            target = table_label(graph, h.other) + (f".{h.other_col}" if h.other_col else "")
            if h.entry is not None:
                ev = h.entry.get("evidence") or {}
                flags = [h.entry["confidence"]]
                if h.entry.get("transform"):
                    flags.append("transform")
                if h.entry.get("match") == "name":
                    flags.append("same name")
                where = f", {ev.get('file')}:{ev.get('line')}" if ev.get("file") else ""
                lines.append(f"{indent}{arrow} {target} via {_path_text(graph, h.entry)} ({', '.join(flags)}{where})")
            else:
                via = (h.edge.attrs.get("via") or [{}])[0]
                ev = via.get("evidence") or {}
                n_cols = len(h.edge.attrs.get("column_map") or [])
                where = f", {ev.get('file')}:{ev.get('line')}" if ev.get("file") else ""
                more = int(h.edge.attrs.get("via_total") or 1) - 1
                extra = f" +{more} more" if more > 0 else ""
                cols = f" · {n_cols} column{'s' if n_cols != 1 else ''} mapped" if n_cols else ""
                lines.append(
                    f"{indent}{arrow} {target} via {_path_text(graph, {'path': via.get('path')})}{extra} "
                    f"({h.edge.confidence.value}{where}){cols}"
                )
    return lines


def flow_summary(data: dict[str, Any]) -> dict[str, Any] | None:
    """Database → database pairs for ``bizmap report`` (None without feeds)."""
    feeds = [e for e in data.get("edges") or [] if e.get("type") == "feeds"]
    if not feeds:
        return None
    nodes = {n["id"]: n for n in data.get("nodes") or []}
    default_db = data.get("database")

    def db_of(nid: str) -> str:
        node = nodes.get(nid) or {}
        if node.get("db"):
            return str(node["db"])
        if default_db:
            return str(default_db)
        repo = node.get("repo") or (nid.split("/", 1)[0] if "/" in nid.split(":", 1)[0] else data.get("repo"))
        return f"{repo or '?'} (repo)"

    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    exact = heuristic = 0
    with_cols = 0
    for e in feeds:
        key = (db_of(e["from"]), db_of(e["to"]))
        row = pairs.setdefault(key, {"from": key[0], "to": key[1], "feeds": 0, "tables": set(), "columns": 0,
                                     "exact": 0, "heuristic": 0, "feeds_exact": 0})
        row["feeds"] += 1
        row["tables"].add(e["to"])
        if e.get("confidence") == "exact":
            row["feeds_exact"] += 1
        cmap = e.get("column_map") or []
        if cmap:
            with_cols += 1
        for c in cmap:
            row["columns"] += 1
            if c.get("confidence") == "exact":
                row["exact"] += 1
                exact += 1
            else:
                row["heuristic"] += 1
                heuristic += 1
    rows = []
    for key in sorted(pairs):
        row = pairs[key]
        row["tables"] = len(row["tables"])
        rows.append(row)
    return {
        "feeds": len(feeds),
        "feeds_with_columns": with_cols,
        "columns": exact + heuristic,
        "columns_exact": exact,
        "columns_heuristic": heuristic,
        "pairs": rows,
    }

