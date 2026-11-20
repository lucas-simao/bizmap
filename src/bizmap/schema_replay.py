from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from bizmap.files import rel
from bizmap.graph import Evidence
from bizmap.sql_analyze import (
    PLPGSQL_KEYWORDS,
    QUAL_IDENT,
    SqlAccess,
    analyze_sql,
    ident_leaf,
    is_skipped_table_name,
)

logging.getLogger("sqlglot").setLevel(logging.ERROR)

DOLLAR_TAG = re.compile(r"\$[A-Za-z0-9_]*\$")
CREATE_FN = re.compile(
    rf"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+({QUAL_IDENT})",
    re.IGNORECASE,
)
RETURNS = re.compile(
    rf"RETURNS\s+({QUAL_IDENT}(?:\s*\[\s*\])?)",
    re.IGNORECASE,
)
CREATE_TRG = re.compile(
    rf"CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+({QUAL_IDENT})",
    re.IGNORECASE,
)
CREATE_VIEW = re.compile(
    rf"CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?({QUAL_IDENT})",
    re.IGNORECASE,
)
CREATE_TABLE_HEAD = re.compile(
    rf"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({QUAL_IDENT})",
    re.IGNORECASE,
)
DROP_TABLE = re.compile(
    rf"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?({QUAL_IDENT}(?:\s*,\s*{QUAL_IDENT})*)",
    re.IGNORECASE,
)
CREATE_POLICY = re.compile(
    rf"""CREATE\s+POLICY\s+(?:"([^"]+)"|'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))\s+ON\s+({QUAL_IDENT})""",
    re.IGNORECASE,
)
POLICY_FOR = re.compile(r"\bFOR\s+(ALL|SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
POLICY_TO = re.compile(r"\bTO\s+([A-Za-z_][A-Za-z0-9_, ]*)", re.IGNORECASE)
ENABLE_RLS = re.compile(
    rf"ALTER\s+TABLE\s+({QUAL_IDENT})\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)
ON_TABLE = re.compile(rf"\bON\s+({QUAL_IDENT})", re.IGNORECASE)
EXECUTE_FN = re.compile(
    rf"EXECUTE\s+(?:FUNCTION|PROCEDURE)\s+({QUAL_IDENT})",
    re.IGNORECASE,
)
RENAME_COLUMN = re.compile(
    rf"(?is)^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({QUAL_IDENT})\s+RENAME\s+(?:COLUMN\s+)?"
    r'("?[A-Za-z_]\w*"?)\s+TO\s+("?[A-Za-z_]\w*"?)\s*;?\s*$'
)
SQL_VERB = re.compile(
    r"(?is)\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|SELECT|WITH)\b"
)


@dataclass
class Column:
    name: str
    data_type: str = "unknown"
    nullable: bool = True
    default: str | None = None


@dataclass
class ForeignKey:
    table: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    name: str | None = None


@dataclass
class Policy:
    name: str
    table: str
    command: str = "ALL"
    role: str | None = None
    using: str | None = None
    with_check: str | None = None
    evidence: Evidence | None = None


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    constraints: dict[str, str] = field(default_factory=dict)
    policies: list[Policy] = field(default_factory=list)
    rls: bool = False
    dropped: bool = False


@dataclass
class DbView:
    name: str
    materialized: bool = False
    query: str = ""
    tables: list[str] = field(default_factory=list)
    accesses: list[SqlAccess] = field(default_factory=list)
    evidence: Evidence | None = None


@dataclass
class DbFunction:
    name: str
    returns: str = ""
    language: str = "plpgsql"
    body: str = ""
    accesses: list[SqlAccess] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    evidence: Evidence | None = None


@dataclass
class DbTrigger:
    name: str
    table: str
    timing: str = "AFTER"
    events: list[str] = field(default_factory=list)
    function: str = ""
    for_each: str = "ROW"
    evidence: Evidence | None = None
    # `UPDATE OF a, b`: the trigger fires only when one of these is written
    update_columns: list[str] = field(default_factory=list)
    # False when the events could not be read (then any write may fire it)
    events_known: bool = False


# `AFTER INSERT OR UPDATE OF status, total ON orders` → the event clause
TRIGGER_EVENTS = re.compile(
    r"\b(?:BEFORE|AFTER|INSTEAD\s+OF)\s+(.*?)\s+ON\s", re.IGNORECASE | re.DOTALL
)


def trigger_events(sql: str) -> tuple[list[str], list[str]]:
    """(events, UPDATE OF columns) from a CREATE TRIGGER statement; ([], [])
    when the clause cannot be found."""
    m = TRIGGER_EVENTS.search(sql)
    if not m:
        return [], []
    clause = m.group(1)
    events: list[str] = []
    columns: list[str] = []
    for part in re.split(r"\s+OR\s+", clause, flags=re.IGNORECASE):
        part = part.strip()
        word = part.split(None, 1)[0].upper() if part else ""
        if word not in {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}:
            continue
        if word not in events:
            events.append(word)
        of = re.match(r"UPDATE\s+OF\s+(.+)$", part, re.IGNORECASE | re.DOTALL)
        if of:
            for col in of.group(1).split(","):
                name = col.strip().strip('"').lower()
                if name and name not in columns:
                    columns.append(name)
    return events, columns


@dataclass
class Schema:
    tables: dict[str, Table] = field(default_factory=dict)
    functions: dict[str, DbFunction] = field(default_factory=dict)
    triggers: dict[str, DbTrigger] = field(default_factory=dict)
    views: dict[str, DbView] = field(default_factory=dict)

    def column_names(self) -> dict[str, list[str]]:
        return {t.lower(): [c for c in table.columns] for t, table in self.tables.items()}

    def to_dict(self) -> dict:
        tables = {}
        for name, table in sorted(self.tables.items()):
            tables[name] = {
                "columns": {
                    col.name: {
                        "type": col.data_type,
                        "nullable": col.nullable,
                        **({"default": col.default} if col.default else {}),
                    }
                    for col in table.columns.values()
                },
                "foreign_keys": [
                    {
                        **({"name": fk.name} if fk.name else {}),
                        "columns": fk.columns,
                        "ref_table": fk.ref_table,
                        "ref_columns": fk.ref_columns,
                    }
                    for fk in table.foreign_keys
                ],
            }
            if table.policies:
                tables[name]["policies"] = [
                    {
                        "name": p.name,
                        "command": p.command,
                        **({"role": p.role} if p.role else {}),
                        **({"using": p.using} if p.using else {}),
                        **({"with_check": p.with_check} if p.with_check else {}),
                    }
                    for p in table.policies
                ]
            if table.rls:
                tables[name]["rls"] = True
            if table.dropped:
                tables[name]["dropped"] = True
        payload: dict = {"tables": tables}
        if self.views:
            payload["views"] = {
                name: {
                    "materialized": view.materialized,
                    "tables": list(view.tables),
                }
                for name, view in sorted(self.views.items())
            }
        return payload


def split_sql_statements(sql: str) -> list[tuple[int, str]]:
    """Split SQL into statements, respecting dollar-quotes, strings, and comments."""
    statements: list[tuple[int, str]] = []
    i = 0
    n = len(sql)
    start = 0
    start_line = 1
    line = 1
    in_line_comment = False
    in_block_comment = False
    in_single = False
    in_double = False
    dollar: str | None = None
    in_stmt = False

    def finish(end: int) -> None:
        stmt = sql[start:end].strip().rstrip(";").strip()
        if stmt:
            statements.append((start_line, stmt))

    def mark_trivia_end(next_i: int) -> None:
        nonlocal start
        if not in_stmt:
            start = next_i

    while i < n:
        c = sql[i]
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
                line += 1
                mark_trivia_end(i + 1)
            i += 1
            continue
        if in_block_comment:
            if c == "\n":
                line += 1
            elif c == "*" and i + 1 < n and sql[i + 1] == "/":
                in_block_comment = False
                mark_trivia_end(i + 2)
                i += 2
                continue
            i += 1
            continue
        if dollar:
            if c == "\n":
                line += 1
            if sql.startswith(dollar, i):
                i += len(dollar)
                dollar = None
                continue
            i += 1
            continue
        if in_single:
            if c == "\n":
                line += 1
            if c == "'" and i + 1 < n and sql[i + 1] == "'":
                i += 2
                continue
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\n":
                line += 1
            if c == '"':
                in_double = False
            i += 1
            continue

        if not in_stmt and c == "-" and i + 1 < n and sql[i + 1] == "-":
            in_line_comment = True
            i += 2
            continue
        if not in_stmt and c == "/" and i + 1 < n and sql[i + 1] == "*":
            in_block_comment = True
            i += 2
            continue
        if not in_stmt and c in " \t\r\n":
            if c == "\n":
                line += 1
            mark_trivia_end(i + 1)
            i += 1
            continue

        if not in_stmt:
            start = i
            start_line = line
            in_stmt = True

        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            in_block_comment = True
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == "$":
            m = DOLLAR_TAG.match(sql, i)
            if m:
                dollar = m.group(0)
                i = m.end()
                continue
        if c == "\n":
            line += 1
            i += 1
            continue
        if c == ";":
            finish(i)
            i += 1
            start = i
            in_stmt = False
            continue
        i += 1

    if in_stmt:
        finish(n)
    return statements


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


def _table(schema: Schema, name: str) -> Table:
    key = ident_leaf(name) or name.lower()
    table = schema.tables.get(key)
    if not table:
        table = Table(name=key)
        schema.tables[key] = table
    return table


def _type_sql(kind: exp.Expression | None) -> str:
    if kind is None:
        return "unknown"
    try:
        return kind.sql(dialect="postgres")
    except Exception:
        return str(kind)


def _add_fk(table: Table, fk: ForeignKey) -> None:
    sig = (tuple(fk.columns), fk.ref_table, tuple(fk.ref_columns), fk.name)
    for existing in table.foreign_keys:
        if (tuple(existing.columns), existing.ref_table, tuple(existing.ref_columns), existing.name) == sig:
            return
    table.foreign_keys.append(fk)


def _reference_fk(table: str, col: str, ref: exp.Expression) -> ForeignKey | None:
    target = ref.this if hasattr(ref, "this") else ref
    ref_table = ""
    ref_cols: list[str] = []
    if isinstance(target, exp.Schema):
        ref_table = _ident(target.this)
        ref_cols = [_ident(x) for x in (target.expressions or []) if _ident(x)]
    else:
        ref_table = _ident(target)
        if hasattr(target, "expressions"):
            ref_cols = [_ident(x) for x in (target.expressions or []) if _ident(x)]
    if not ref_table:
        return None
    if not ref_cols:
        ref_cols = ["id"]
    return ForeignKey(table=table, columns=[col], ref_table=ref_table.lower(), ref_columns=ref_cols)


def _apply_column_def(table: Table, coldef: exp.ColumnDef) -> None:
    name = _ident(coldef.this)
    if not name:
        return
    data_type = _type_sql(coldef.args.get("kind"))
    nullable = True
    default = None
    for cons in coldef.args.get("constraints") or []:
        kind = cons.args.get("kind") if isinstance(cons, exp.Expression) else None
        if isinstance(kind, (exp.NotNullColumnConstraint, exp.PrimaryKeyColumnConstraint)):
            nullable = False
        if isinstance(kind, exp.DefaultColumnConstraint):
            try:
                default = kind.this.sql(dialect="postgres") if kind.this else None
            except Exception:
                default = str(kind.this) if kind.this else None
        if isinstance(kind, exp.Reference):
            fk = _reference_fk(table.name, name, kind)
            if fk:
                _add_fk(table, fk)
        if kind and kind.__class__.__name__ == "GeneratedAsIdentityColumnConstraint":
            nullable = False
    table.columns[name.lower()] = Column(
        name=name.lower(),
        data_type=data_type,
        nullable=nullable,
        default=default,
    )


def _extract_fk_from_constraint(table: Table, node: exp.Expression) -> None:
    name = _ident(node.this) if isinstance(node, exp.Constraint) else None
    for child in node.walk():
        if isinstance(child, exp.ForeignKey):
            cols = [_ident(x) for x in (child.expressions or []) if _ident(x)]
            ref = child.args.get("reference")
            ref_table = ""
            ref_cols: list[str] = []
            if ref is not None:
                target = ref.this if hasattr(ref, "this") else ref
                if isinstance(target, exp.Schema):
                    ref_table = _ident(target.this)
                    ref_cols = [_ident(x) for x in (target.expressions or []) if _ident(x)]
                else:
                    ref_table = _ident(target)
            if cols and ref_table:
                _add_fk(
                    table,
                    ForeignKey(
                        name=name,
                        table=table.name,
                        columns=cols,
                        ref_table=ref_table.lower(),
                        ref_columns=ref_cols or ["id"],
                    ),
                )


def _apply_create_table(schema: Schema, stmt: exp.Create) -> None:
    this = stmt.this
    table_name = ""
    coldefs: list[exp.Expression] = []
    if isinstance(this, exp.Schema):
        table_name = _ident(this.this)
        coldefs = list(this.expressions or [])
    else:
        table_name = _ident(this)
    if not table_name:
        return
    table = _table(schema, ident_leaf(table_name) or table_name)
    table.dropped = False
    for expr in coldefs:
        if isinstance(expr, exp.ColumnDef):
            _apply_column_def(table, expr)
        else:
            _extract_fk_from_constraint(table, expr)
            if isinstance(expr, exp.Constraint):
                cname = _ident(expr.this)
                if cname:
                    table.constraints[cname] = expr.sql(dialect="postgres")


def _apply_alter(schema: Schema, stmt: exp.Alter) -> None:
    table_name = _ident(stmt.this)
    if not table_name:
        return
    table = _table(schema, table_name)
    for action in stmt.args.get("actions") or []:
        if isinstance(action, exp.ColumnDef):
            _apply_column_def(table, action)
        elif isinstance(action, exp.AddConstraint):
            for expr in action.expressions or []:
                _extract_fk_from_constraint(table, expr)
                cname = _ident(expr.this) if hasattr(expr, "this") else None
                if cname:
                    table.constraints[cname] = expr.sql(dialect="postgres")
        elif isinstance(action, exp.Drop):
            kind = (action.args.get("kind") or "").upper() if isinstance(action.args.get("kind"), str) else str(action.args.get("kind") or "").upper()
            if "COLUMN" in kind:
                for dropped in action.args.get("tables") or []:
                    col = _ident(dropped).lower()
                    table.columns.pop(col, None)
            if "CONSTRAINT" in kind:
                for dropped in action.args.get("tables") or []:
                    cname = _ident(dropped)
                    table.constraints.pop(cname, None)
                    table.foreign_keys = [fk for fk in table.foreign_keys if fk.name != cname]


def _apply_trigger_create(schema: Schema, stmt: exp.Create, evidence: Evidence) -> None:
    name = _ident(stmt.this)
    props = None
    properties = stmt.args.get("properties")
    if properties:
        for p in properties.expressions or []:
            if p.__class__.__name__ == "TriggerProperties" or isinstance(p, exp.Properties):
                props = p
                break
        if props is None and properties.expressions:
            props = properties.expressions[0]
    table = ""
    timing = "AFTER"
    events: list[str] = []
    function = ""
    for_each = "ROW"
    if props is not None:
        table = _ident(props.args.get("table"))
        timing = str(props.args.get("timing") or "AFTER").upper()
        events = [str(e.this or e).upper() for e in (props.args.get("events") or [])]
        execute = props.args.get("execute")
        if execute is not None:
            function = _ident(execute.this if hasattr(execute, "this") else execute)
        for_each = str(props.args.get("for_each") or "ROW").upper()
    if name:
        schema.triggers[ident_leaf(name)] = DbTrigger(
            name=ident_leaf(name),
            table=ident_leaf(table) if table else "",
            timing=timing,
            events=events or ["INSERT"],
            function=ident_leaf(function) if function else "",
            for_each=for_each,
            evidence=evidence,
        )


def _extract_dollar_body(sql: str) -> tuple[str | None, str]:
    m = DOLLAR_TAG.search(sql)
    if not m:
        return None, ""
    tag = m.group(0)
    start = m.end()
    end = sql.find(tag, start)
    if end < 0:
        return tag, sql[start:]
    return tag, sql[start:end]


def _function_language(sql: str) -> str:
    m = re.search(rf"LANGUAGE\s+({QUAL_IDENT})", sql, re.IGNORECASE)
    return ident_leaf(m.group(1)) if m else "plpgsql"


def mask_sql_trivia(sql: str) -> str:
    """Replace comments and single-quoted / dollar-quoted strings with spaces.

    Double-quoted identifiers are kept so quoted table names survive analysis.
    Newlines are preserved.
    """
    chars = list(sql)
    i = 0
    n = len(sql)
    in_line = False
    in_block = False
    in_single = False
    dollar: str | None = None

    def blank(start: int, end: int) -> None:
        for j in range(start, min(end, n)):
            if chars[j] != "\n":
                chars[j] = " "

    while i < n:
        c = sql[i]
        if dollar:
            if sql.startswith(dollar, i):
                blank(i, i + len(dollar))
                i += len(dollar)
                dollar = None
                continue
            if c != "\n":
                chars[i] = " "
            i += 1
            continue
        if in_line:
            if c == "\n":
                in_line = False
            else:
                chars[i] = " "
            i += 1
            continue
        if in_block:
            if c == "*" and i + 1 < n and sql[i + 1] == "/":
                chars[i] = " "
                chars[i + 1] = " "
                in_block = False
                i += 2
                continue
            if c != "\n":
                chars[i] = " "
            i += 1
            continue
        if in_single:
            if c == "'" and i + 1 < n and sql[i + 1] == "'":
                chars[i] = " "
                chars[i + 1] = " "
                i += 2
                continue
            if c == "'":
                chars[i] = " "
                in_single = False
                i += 1
                continue
            if c != "\n":
                chars[i] = " "
            i += 1
            continue
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            in_line = True
            chars[i] = " "
            chars[i + 1] = " "
            i += 2
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            in_block = True
            chars[i] = " "
            chars[i + 1] = " "
            i += 2
            continue
        if c == "'":
            in_single = True
            chars[i] = " "
            i += 1
            continue
        if c == "$":
            m = DOLLAR_TAG.match(sql, i)
            if m:
                dollar = m.group(0)
                blank(i, m.end())
                i = m.end()
                continue
        i += 1
    return "".join(chars)


def _blank_declare_section(sql: str) -> str:
    m = re.search(r"(?is)\bDECLARE\b(.*?)\bBEGIN\b", sql)
    if not m:
        return sql
    inner = m.group(1)
    blanked = "".join("\n" if ch == "\n" else " " for ch in inner)
    return sql[: m.start(1)] + blanked + sql[m.end(1) :]


def plpgsql_declared_names(body: str) -> set[str]:
    """Variable names from a PL/pgSQL DECLARE section."""
    masked = mask_sql_trivia(body)
    m = re.search(r"(?is)\bDECLARE\b(.*?)\bBEGIN\b", masked)
    if not m:
        return set()
    names: set[str] = set()
    for stmt in m.group(1).split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        mm = re.match(rf"({QUAL_IDENT})", stmt)
        if mm:
            names.add(ident_leaf(mm.group(1)))
    return names


def strip_plpgsql_into(sql: str) -> str:
    """Remove PL/pgSQL ``INTO var`` targets (not ``INSERT INTO`` / ``COPY``)."""
    pieces: list[str] = []
    last = 0
    for m in re.finditer(r"(?is)\bINTO\b", sql):
        before = sql[: m.start()].rstrip()
        if re.search(r"(?i)INSERT$", before) or re.search(r"(?i)\bCOPY$", before):
            continue
        rest = sql[m.end() :]
        mm = re.match(
            rf"(?is)\s+(?:STRICT\s+)?({QUAL_IDENT})(?:\s*,\s*{QUAL_IDENT})*",
            rest,
        )
        if not mm:
            continue
        pieces.append(sql[last : m.start()])
        last = m.end() + mm.end()
    pieces.append(sql[last:])
    return "".join(pieces)


def _unquote_sql_literal(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    m = DOLLAR_TAG.match(s)
    if m:
        tag = m.group(0)
        end = s.find(tag, len(tag))
        if end > 0:
            return s[len(tag) : end]
    return s


def extract_execute_sql(body: str) -> list[tuple[str, bool]]:
    """SQL strings passed to ``EXECUTE`` / ``EXECUTE format(...)``.

    ``format('UPDATE %I ...')`` is marked dynamic (table name is a hole).
    """
    out: list[tuple[str, bool]] = []
    for m in re.finditer(
        r"(?is)\bEXECUTE\s+(?:format\s*\(\s*)?(?:E)?('(?:''|[^'])*'|\$[A-Za-z0-9_]*\$)",
        body,
    ):
        sql = _unquote_sql_literal(m.group(1))
        if not sql.strip():
            continue
        dynamic = bool(re.search(r"%[IDsL]", sql)) or "format" in m.group(0).lower()
        out.append((sql, dynamic))
    return out


def extract_sql_from_plpgsql(body: str) -> list[str]:
    """Pull SQL verbs out of a plpgsql / DO body.

    Comments and string literals are masked first so ``-- SELECT * FROM any``
    and quoted prose never become table names.
    """
    search = _blank_declare_section(mask_sql_trivia(body))
    found: list[str] = []
    i = 0
    while True:
        m = SQL_VERB.search(search, i)
        if not m:
            break
        start = m.start()
        chunk = search[start:]
        parts = split_sql_statements(chunk)
        if parts:
            stmt = strip_plpgsql_into(parts[0][1])
            if stmt.strip():
                found.append(stmt)
            i = start + max(len(parts[0][1]), 1)
        else:
            i = m.end()
    return found


def _analyze_fn_body(schema: Schema, fn: DbFunction) -> None:
    """(Re)scan a plpgsql body against the current reconstructed schema."""
    colmap = schema.column_names()
    known = set(colmap) | set(schema.views)
    declared = plpgsql_declared_names(fn.body)
    skip = declared | {k.lower() for k in PLPGSQL_KEYWORDS}
    accesses: list[SqlAccess] = []
    unresolved: list[str] = []
    seen_u: set[str] = set()

    def take(access: SqlAccess) -> None:
        tname = ident_leaf(access.table) if access.table else ""
        if access.unknown_table:
            accesses.append(access)
            return
        if not tname:
            return
        if tname in skip or is_skipped_table_name(tname, skip):
            return
        accesses.append(access)
        if (
            known
            and tname not in known
            and not access.schema_qualified
            and not access.external
            and tname not in seen_u
        ):
            seen_u.add(tname)
            unresolved.append(tname)

    for inner in extract_sql_from_plpgsql(fn.body):
        for access in analyze_sql(
            inner,
            schema_columns=colmap,
            require_known=False,
            skip_names=skip,
        ):
            take(access)
    for inner, dynamic in extract_execute_sql(fn.body):
        for access in analyze_sql(
            inner,
            schema_columns=colmap,
            dynamic=dynamic,
            require_known=False,
            skip_names=skip,
        ):
            take(access)
    fn.accesses = accesses
    fn.unresolved = unresolved


def finalize_schema(schema: Schema) -> None:
    """Re-resolve function/view bodies after the full DDL replay.

    pg_dump emits ``CREATE FUNCTION`` before ``CREATE TABLE``; table names
    that were unresolved on first pass become real accesses here.
    """
    for fn in schema.functions.values():
        _analyze_fn_body(schema, fn)
    colmap = schema.column_names()
    skip = {k.lower() for k in PLPGSQL_KEYWORDS}
    for view in schema.views.values():
        if not view.query:
            continue
        accesses = analyze_sql(
            view.query,
            schema_columns=colmap,
            require_known=False,
            skip_names=skip,
        )
        view.accesses = accesses
        tables: list[str] = []
        seen: set[str] = set()
        for access in accesses:
            tname = ident_leaf(access.table) if access.table else ""
            if not tname or access.unknown_table or tname in seen:
                continue
            if is_skipped_table_name(tname):
                continue
            seen.add(tname)
            tables.append(tname)
        view.tables = tables


def _apply_function(schema: Schema, sql: str, evidence: Evidence) -> None:
    m = CREATE_FN.search(sql)
    if not m:
        return
    name = ident_leaf(m.group(1))
    ret = RETURNS.search(sql)
    _, body = _extract_dollar_body(sql)
    fn = DbFunction(
        name=name,
        returns=ident_leaf(ret.group(1)) if ret else "",
        language=_function_language(sql),
        body=body.strip(),
        evidence=evidence,
    )
    _analyze_fn_body(schema, fn)
    schema.functions[name] = fn


def _apply_drop_table(schema: Schema, sql: str) -> None:
    m = DROP_TABLE.search(sql)
    if not m:
        return
    for raw in re.split(r"\s*,\s*", m.group(1)):
        name = ident_leaf(raw)
        if not name:
            continue
        table = schema.tables.get(name)
        if table is not None:
            table.dropped = True
        else:
            schema.tables[name] = Table(name=name, dropped=True)


def _apply_do_block(schema: Schema, sql: str, evidence: Evidence) -> None:
    _, body = _extract_dollar_body(sql)
    if not body:
        return
    # Apply nested DDL (including inside IF ... THEN).
    for m in re.finditer(
        r"(?is)\bALTER\s+TABLE\b.*?;|\bCREATE\s+TABLE\b.*?;|\bDROP\s+TABLE\b.*?;",
        body if body.endswith(";") else body + ";",
    ):
        apply_statement(schema, m.group(0), evidence)


def _apply_trigger_regex(schema: Schema, sql: str, evidence: Evidence) -> None:
    m = CREATE_TRG.search(sql)
    if not m:
        return
    name = ident_leaf(m.group(1))
    timing_m = re.search(r"\b(BEFORE|AFTER|INSTEAD\s+OF)\b", sql, re.I)
    clause_events, update_columns = trigger_events(sql)
    table_m = ON_TABLE.search(sql)
    fn_m = EXECUTE_FN.search(sql)
    events = clause_events or [
        e.upper() for e in re.findall(r"\b(INSERT|UPDATE|DELETE|TRUNCATE)\b", sql, re.I)
    ]
    events = list(dict.fromkeys(events))
    table = ident_leaf(table_m.group(1)) if table_m else ""
    function = ident_leaf(fn_m.group(1)) if fn_m else ""
    existing = schema.triggers.get(name)
    if existing is not None:
        if not existing.table and table:
            existing.table = table
        if not existing.function and function:
            existing.function = function
        if timing_m and (not existing.timing or existing.timing == "AFTER"):
            existing.timing = timing_m.group(1).upper()
        if clause_events:
            existing.events = clause_events
            existing.update_columns = update_columns
            existing.events_known = True
        elif events and existing.events == ["INSERT"]:
            existing.events = events
        if evidence and existing.evidence is None:
            existing.evidence = evidence
        return
    schema.triggers[name] = DbTrigger(
        name=name,
        table=table,
        timing=(timing_m.group(1).upper() if timing_m else "AFTER"),
        events=events or ["INSERT"],
        function=function,
        evidence=evidence,
        update_columns=update_columns,
        events_known=bool(clause_events),
    )


def _apply_create_view(
    schema: Schema,
    sql: str,
    evidence: Evidence,
    stmt: exp.Create | None = None,
) -> None:
    name = ""
    materialized = bool(re.search(r"(?is)MATERIALIZED\s+VIEW", sql))
    if stmt is not None:
        this = stmt.this
        if isinstance(this, exp.Schema):
            name = ident_leaf(_ident(this.this) or _ident(this))
        else:
            name = ident_leaf(_ident(this))
        kind = str(stmt.args.get("kind") or "").upper()
        if "MATERIALIZED" in kind:
            materialized = True
    if not name:
        m = CREATE_VIEW.search(sql)
        if m:
            name = ident_leaf(m.group(1))
    if not name:
        return
    query = ""
    as_m = re.search(r"(?is)\bAS\b", sql)
    if as_m:
        query = sql[as_m.end() :].strip().rstrip(";").strip()
    colmap = schema.column_names()
    accesses = (
        analyze_sql(
            query,
            schema_columns=colmap,
            require_known=False,
            skip_names={k.lower() for k in PLPGSQL_KEYWORDS},
        )
        if query
        else []
    )
    tables: list[str] = []
    seen: set[str] = set()
    for access in accesses:
        tname = ident_leaf(access.table) if access.table else ""
        if not tname or access.unknown_table or tname in seen:
            continue
        if is_skipped_table_name(tname):
            continue
        seen.add(tname)
        tables.append(tname)
    schema.views[name] = DbView(
        name=name,
        materialized=materialized,
        query=query,
        tables=tables,
        accesses=accesses,
        evidence=evidence,
    )


def _paren_expr(sql: str, keyword: str) -> str | None:
    m = re.search(rf"\b{keyword}\s*\(", sql, re.IGNORECASE)
    if not m:
        return None
    i = m.end()
    depth = 1
    start = i
    while i < len(sql) and depth:
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[start:i].strip()
        i += 1
    return sql[start:].strip() or None


def _apply_policy(schema: Schema, sql: str, evidence: Evidence) -> None:
    m = CREATE_POLICY.search(sql)
    if not m:
        return
    name = next(g for g in m.groups()[:3] if g)
    table_name = ident_leaf(m.group(4))
    command_m = POLICY_FOR.search(sql)
    role_m = POLICY_TO.search(sql)
    table = _table(schema, table_name)
    table.policies.append(
        Policy(
            name=name,
            table=table_name,
            command=(command_m.group(1).upper() if command_m else "ALL"),
            role=(role_m.group(1).strip() if role_m else None),
            using=_paren_expr(sql, "USING"),
            with_check=_paren_expr(sql, "WITH\\s+CHECK"),
            evidence=evidence,
        )
    )
    table.rls = True


def apply_statement(schema: Schema, sql: str, evidence: Evidence) -> None:
    stripped = sql.strip()
    if not stripped:
        return
    try:
        parsed = sqlglot.parse(stripped, read="postgres", error_level=ErrorLevel.IGNORE)
    except Exception:
        parsed = []

    m = RENAME_COLUMN.search(stripped)
    if m:
        # `ALTER TABLE t RENAME [COLUMN] a TO b`: sqlglot's shape varies by
        # version, so it is applied here and the statement is done
        table = _table(schema, ident_leaf(m.group(1)))
        old, new = m.group(2).strip('"').lower(), m.group(3).strip('"').lower()
        col = table.columns.pop(old, None)
        if col is not None:
            col.name = new
            table.columns[new] = col
        return

    handled = False
    for stmt in parsed or []:
        if stmt is None:
            continue
        if isinstance(stmt, exp.Create):
            kind = str(stmt.args.get("kind") or "").upper()
            if kind == "TABLE":
                _apply_create_table(schema, stmt)
                handled = True
            elif kind == "TRIGGER":
                _apply_trigger_create(schema, stmt, evidence)
                _apply_trigger_regex(schema, stripped, evidence)
                handled = True
            elif kind == "FUNCTION":
                _apply_function(schema, stripped, evidence)
                handled = True
            elif "VIEW" in kind:
                _apply_create_view(schema, stripped, evidence, stmt)
                handled = True
        elif isinstance(stmt, exp.Alter):
            _apply_alter(schema, stmt)
            handled = True
            m_rls = ENABLE_RLS.search(stripped)
            if m_rls:
                _table(schema, ident_leaf(m_rls.group(1))).rls = True
        elif isinstance(stmt, exp.Drop):
            kind = str(stmt.args.get("kind") or "").upper()
            if "TABLE" in kind:
                _apply_drop_table(schema, stripped)
                handled = True
        elif isinstance(stmt, exp.Command):
            cmd = str(stmt.this or "").upper()
            expr = str(stmt.args.get("expression") or stripped)
            if cmd == "DO":
                _apply_do_block(schema, stripped, evidence)
                handled = True
            elif cmd == "CREATE" and re.search(r"(?is)FUNCTION", expr):
                _apply_function(schema, stripped, evidence)
                handled = True
            elif cmd == "CREATE" and re.search(r"(?is)TRIGGER", expr):
                _apply_trigger_regex(schema, stripped, evidence)
                handled = True
            elif cmd == "CREATE" and re.search(r"(?is)POLICY", expr):
                _apply_policy(schema, stripped, evidence)
                handled = True
            elif cmd == "CREATE" and re.search(r"(?is)VIEW", expr):
                _apply_create_view(schema, stripped, evidence)
                handled = True
            elif cmd == "CREATE" and re.search(r"(?is)\bTABLE\b", expr):
                m_tbl = CREATE_TABLE_HEAD.search(stripped)
                if m_tbl:
                    _table(schema, ident_leaf(m_tbl.group(1)))
                    handled = True

    if handled:
        return
    if re.match(r"(?is)^DO\b", stripped):
        _apply_do_block(schema, stripped, evidence)
        return
    if CREATE_FN.search(stripped):
        _apply_function(schema, stripped, evidence)
        return
    if CREATE_TRG.search(stripped):
        _apply_trigger_regex(schema, stripped, evidence)
        return
    if CREATE_VIEW.search(stripped):
        _apply_create_view(schema, stripped, evidence)
        return
    if CREATE_POLICY.search(stripped):
        _apply_policy(schema, stripped, evidence)
        return
    m_tbl = CREATE_TABLE_HEAD.match(stripped)
    if m_tbl and re.search(r"(?is)^CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\b", stripped):
        table = _table(schema, ident_leaf(m_tbl.group(1)))
        table.dropped = False
        return
    if DROP_TABLE.search(stripped) and re.search(r"(?is)^DROP\s+TABLE\b", stripped):
        _apply_drop_table(schema, stripped)
        return
    m_rls = ENABLE_RLS.search(stripped)
    if m_rls:
        table = _table(schema, ident_leaf(m_rls.group(1)))
        table.rls = True


def _stmt_symbol(stmt: str) -> str:
    m = CREATE_FN.search(stmt)
    if m:
        return ident_leaf(m.group(1))
    m = CREATE_TRG.search(stmt)
    if m:
        return ident_leaf(m.group(1))
    m = CREATE_VIEW.search(stmt)
    if m:
        return ident_leaf(m.group(1))
    tokens = stmt.split()
    if not tokens:
        return "sql"
    if tokens[0].upper() == "CREATE" and len(tokens) >= 3:
        return " ".join(tokens[:3])
    if tokens[0].upper() == "ALTER" and len(tokens) >= 3:
        return " ".join(tokens[:3])
    return tokens[0]


def replay_sql(sql: str, *, file: str = "schema.sql") -> Schema:
    schema = Schema()
    for line, stmt in split_sql_statements(sql):
        evidence = Evidence(file=file, line=line, symbol=_stmt_symbol(stmt))
        apply_statement(schema, stmt, evidence)
    finalize_schema(schema)
    return schema


def replay_files(files: list[Path], root: Path) -> Schema:
    schema = Schema()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel_file = rel(path, root)
        for line, stmt in split_sql_statements(text):
            evidence = Evidence(file=rel_file, line=line, symbol=_stmt_symbol(stmt))
            apply_statement(schema, stmt, evidence)
    finalize_schema(schema)
    return schema
