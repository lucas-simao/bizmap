from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from bizmap.graph import Confidence

PLACEHOLDER_N = re.compile(r"\$(\d+)")
NAMED_PARAM = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
PY_PERCENT = re.compile(r"%s")
SQL_START = re.compile(
    r"^\s*\(?\s*(WITH|SELECT|INSERT|UPDATE|DELETE|MERGE)\b",
    re.IGNORECASE,
)
TABLE_FROM_PREFIX = re.compile(
    r"(?is)\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)"
)
SKIP_TABLES = {"new", "old", "excluded", "unnest"}


@dataclass
class SqlAccess:
    kind: str  # read | write
    table: str
    column: str | None
    confidence: Confidence
    raw_sql: str
    dynamic: bool = False


def looks_like_sql(text: str | None) -> bool:
    if not text:
        return False
    return bool(SQL_START.search(text.strip()))


def normalize_placeholders(sql: str) -> str:
    sql = PLACEHOLDER_N.sub(r"@p\1", sql)
    sql = PY_PERCENT.sub("?", sql)
    sql = NAMED_PARAM.sub(r"@\1", sql)
    return sql


def _ident(node: exp.Expression | str | None) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    name = getattr(node, "name", None)
    if name:
        return str(name)
    this = getattr(node, "this", None)
    if this is not None and this is not node:
        return _ident(this)
    return ""


def _table_name(table: exp.Table | exp.Expression | None) -> str:
    if table is None:
        return ""
    if isinstance(table, exp.Table):
        catalog_db_table = table.name
        return catalog_db_table
    return _ident(table)


def _alias_map(root: exp.Expression) -> dict[str, str]:
    aliases: dict[str, str] = {}
    cte_names = {_ident(c.args.get("alias") or c.this) for c in root.find_all(exp.CTE)}
    cte_names = {n.lower() for n in cte_names if n}
    for table in root.find_all(exp.Table):
        name = _table_name(table)
        if not name:
            continue
        if name.lower() in SKIP_TABLES:
            continue
        alias = table.alias_or_name or name
        aliases[alias.lower()] = name
        aliases[name.lower()] = name
    for cte in cte_names:
        aliases.pop(cte, None)
    return aliases


def _resolve_table(raw_table: str | None, aliases: dict[str, str], fallback: str | None) -> str | None:
    if raw_table:
        key = raw_table.lower()
        if key in SKIP_TABLES:
            return None
        if "." in raw_table:
            raw_table = raw_table.split(".")[-1]
            key = raw_table.lower()
        return aliases.get(key, raw_table)
    if fallback:
        return fallback
    if len({v.lower() for v in aliases.values()}) == 1:
        return next(iter(aliases.values()))
    return None


def _column_writes_from_schema(insert: exp.Insert) -> list[str]:
    target = insert.this
    names: list[str] = []
    if isinstance(target, exp.Schema):
        for expr in target.expressions or []:
            name = _ident(expr)
            if name:
                names.append(name)
    return names


def _set_columns(update: exp.Update) -> list[str]:
    names: list[str] = []
    for expr in update.expressions or []:
        if isinstance(expr, exp.EQ):
            name = _ident(expr.this)
            if name:
                names.append(name)
        else:
            name = _ident(expr)
            if name:
                names.append(name)
    return names


def _add(
    out: list[SqlAccess],
    kind: str,
    table: str | None,
    column: str | None,
    confidence: Confidence,
    sql: str,
    dynamic: bool,
    seen: set[tuple],
) -> None:
    if not table or table.lower() in SKIP_TABLES:
        return
    table = table.split(".")[-1]
    key = (kind, table.lower(), (column or "").lower(), confidence.value, dynamic)
    if key in seen:
        return
    seen.add(key)
    out.append(
        SqlAccess(
            kind=kind,
            table=table,
            column=column.lower() if column else None,
            confidence=confidence,
            raw_sql=sql,
            dynamic=dynamic,
        )
    )


def _analyze_statement(
    stmt: exp.Expression,
    sql: str,
    schema_columns: dict[str, list[str]] | None,
    dynamic: bool,
    out: list[SqlAccess],
    seen: set[tuple],
) -> None:
    aliases = _alias_map(stmt)
    conf = Confidence.UNKNOWN if dynamic else Confidence.EXACT

    def known_cols(table: str) -> list[str]:
        if not schema_columns:
            return []
        return schema_columns.get(table.lower(), schema_columns.get(table, []))

    if isinstance(stmt, exp.Insert):
        table = _table_name(stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this)
        cols = _column_writes_from_schema(stmt)
        if cols:
            for col in cols:
                _add(out, "write", table, col, conf, sql, dynamic, seen)
        else:
            inferred = known_cols(table)
            if inferred:
                for col in inferred:
                    _add(out, "write", table, col, Confidence.INFERRED, sql, dynamic, seen)
            else:
                _add(out, "write", table, None, Confidence.UNKNOWN, sql, dynamic, seen)
        # subquery reads
        for inner in stmt.find_all(exp.Select):
            if inner is stmt:
                continue
            _analyze_select(inner, sql, schema_columns, dynamic, out, seen, aliases)
        return

    if isinstance(stmt, exp.Update):
        table = _table_name(stmt.this)
        cols = _set_columns(stmt)
        if cols:
            for col in cols:
                _add(out, "write", table, col, conf, sql, dynamic, seen)
        else:
            _add(out, "write", table, None, Confidence.UNKNOWN, sql, dynamic, seen)
        where = stmt.args.get("where")
        if where:
            for col in where.find_all(exp.Column):
                t = _resolve_table(col.table, aliases, table)
                _add(out, "read", t, col.name, conf, sql, dynamic, seen)
        for inner in stmt.find_all(exp.Select):
            _analyze_select(inner, sql, schema_columns, dynamic, out, seen, aliases)
        return

    if isinstance(stmt, exp.Delete):
        table = _table_name(stmt.this)
        _add(out, "write", table, None, conf, sql, dynamic, seen)
        where = stmt.args.get("where")
        if where:
            for col in where.find_all(exp.Column):
                t = _resolve_table(col.table, aliases, table)
                _add(out, "read", t, col.name, conf, sql, dynamic, seen)
        return

    if isinstance(stmt, exp.Select) or stmt.find(exp.Select):
        select = stmt if isinstance(stmt, exp.Select) else stmt.find(exp.Select)
        if select:
            _analyze_select(select, sql, schema_columns, dynamic, out, seen, aliases)
        return


def _analyze_select(
    stmt: exp.Select,
    sql: str,
    schema_columns: dict[str, list[str]] | None,
    dynamic: bool,
    out: list[SqlAccess],
    seen: set[tuple],
    aliases: dict[str, str] | None = None,
) -> None:
    aliases = aliases or _alias_map(stmt)
    conf = Confidence.UNKNOWN if dynamic else Confidence.EXACT
    tables = {v for v in aliases.values() if v.lower() not in SKIP_TABLES}
    fallback = next(iter(tables)) if len(tables) == 1 else None
    has_star = bool(list(stmt.find_all(exp.Star)))
    if has_star:
        targets = [fallback] if fallback else list(tables)
        for table in targets:
            cols = (schema_columns or {}).get(table.lower(), []) if table else []
            if cols:
                for col in cols:
                    _add(out, "read", table, col, Confidence.INFERRED, sql, dynamic, seen)
            else:
                _add(out, "read", table, None, Confidence.INFERRED, sql, dynamic, seen)
    for col in stmt.find_all(exp.Column):
        if col.name == "*":
            continue
        table = _resolve_table(col.table, aliases, fallback)
        _add(out, "read", table, col.name, conf, sql, dynamic, seen)


def _fallback_table_accesses(sql: str, dynamic: bool) -> list[SqlAccess]:
    out: list[SqlAccess] = []
    seen: set[tuple] = set()
    stripped = sql.strip()
    kind = "read"
    if re.match(r"(?is)^(INSERT|UPDATE|DELETE|MERGE)\b", stripped):
        kind = "write"
        if re.search(r"(?is)^SELECT\b", stripped):
            kind = "read"
    if re.match(r"(?is)^INSERT\b", stripped):
        kind = "write"
    elif re.match(r"(?is)^UPDATE\b", stripped):
        kind = "write"
    elif re.match(r"(?is)^DELETE\b", stripped):
        kind = "write"
    conf = Confidence.UNKNOWN if dynamic else Confidence.UNKNOWN
    for match in TABLE_FROM_PREFIX.finditer(sql):
        table = match.group(1).split(".")[-1]
        _add(out, kind, table, None, conf, sql, True, seen)
        # INSERT/UPDATE/DELETE primary table should be write; FROM/JOIN extras are reads
        prefix = sql[: match.start()].upper()
        if "JOIN" in prefix[-12:] or prefix.strip().endswith("FROM"):
            if kind == "write" and not re.search(r"(?is)INSERT\s+INTO\s+" + re.escape(match.group(1)), sql):
                _add(out, "read", table, None, Confidence.UNKNOWN, sql, True, seen)
    if not out:
        match = re.search(r"(?is)\b(?:INTO|UPDATE|FROM)\s+([A-Za-z_][A-Za-z0-9_.]*)", sql)
        if match:
            _add(out, kind, match.group(1), None, Confidence.UNKNOWN, sql, True, seen)
    return out


def analyze_sql(
    sql: str,
    *,
    schema_columns: dict[str, list[str]] | None = None,
    dynamic: bool = False,
) -> list[SqlAccess]:
    """Parse a SQL string (postgres dialect) into per-column read/write accesses."""
    if not sql or not sql.strip():
        return []
    normalized = normalize_placeholders(sql)
    stmts: list[exp.Expression | None]
    try:
        stmts = sqlglot.parse(normalized, read="postgres", error_level=ErrorLevel.IGNORE)
    except Exception:
        return _fallback_table_accesses(sql, dynamic=True)

    out: list[SqlAccess] = []
    seen: set[tuple] = set()
    parsed_any = False
    for stmt in stmts:
        if stmt is None:
            continue
        if isinstance(stmt, exp.Command):
            continue
        parsed_any = True
        _analyze_statement(stmt, sql, schema_columns, dynamic, out, seen)
    if not parsed_any or not out:
        return _fallback_table_accesses(sql, dynamic=True)
    if dynamic:
        for access in out:
            access.dynamic = True
            if access.confidence == Confidence.EXACT:
                access.confidence = Confidence.UNKNOWN
    return out
