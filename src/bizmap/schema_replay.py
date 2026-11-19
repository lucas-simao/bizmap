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
from bizmap.sql_analyze import SqlAccess, analyze_sql

logging.getLogger("sqlglot").setLevel(logging.ERROR)

DOLLAR_TAG = re.compile(r"\$[A-Za-z0-9_]*\$")
CREATE_FN = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([A-Za-z_][A-Za-z0-9_.]*)",
    re.IGNORECASE,
)
RETURNS = re.compile(r"RETURNS\s+([A-Za-z_][A-Za-z0-9_.\[\]]*)", re.IGNORECASE)
CREATE_TRG = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+([A-Za-z_][A-Za-z0-9_.]*)",
    re.IGNORECASE,
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
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    constraints: dict[str, str] = field(default_factory=dict)


@dataclass
class DbFunction:
    name: str
    returns: str = ""
    language: str = "plpgsql"
    body: str = ""
    accesses: list[SqlAccess] = field(default_factory=list)
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


@dataclass
class Schema:
    tables: dict[str, Table] = field(default_factory=dict)
    functions: dict[str, DbFunction] = field(default_factory=dict)
    triggers: dict[str, DbTrigger] = field(default_factory=dict)

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
        return {"tables": tables}


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
    key = name.lower()
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
    table = _table(schema, table_name)
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
        schema.triggers[name.lower()] = DbTrigger(
            name=name.lower(),
            table=table.lower(),
            timing=timing,
            events=events or ["INSERT"],
            function=function.lower(),
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
    m = re.search(r"LANGUAGE\s+([A-Za-z0-9_]+)", sql, re.IGNORECASE)
    return m.group(1).lower() if m else "plpgsql"


def extract_sql_from_plpgsql(body: str) -> list[str]:
    """Pull SQL verbs out of a plpgsql / DO body."""
    found: list[str] = []
    i = 0
    upper = body
    while True:
        m = SQL_VERB.search(upper, i)
        if not m:
            break
        start = m.start()
        # scan to semicolon respecting quotes / dollars
        chunk = body[start:]
        parts = split_sql_statements(chunk)
        if parts:
            stmt = parts[0][1]
            found.append(stmt)
            i = start + max(len(stmt), 1)
        else:
            i = m.end()
    return found


def _apply_function(schema: Schema, sql: str, evidence: Evidence) -> None:
    m = CREATE_FN.search(sql)
    if not m:
        return
    name = m.group(1).split(".")[-1].lower()
    ret = RETURNS.search(sql)
    _, body = _extract_dollar_body(sql)
    fn = DbFunction(
        name=name,
        returns=ret.group(1) if ret else "",
        language=_function_language(sql),
        body=body.strip(),
        evidence=evidence,
    )
    colmap = schema.column_names()
    for inner in extract_sql_from_plpgsql(fn.body):
        fn.accesses.extend(analyze_sql(inner, schema_columns=colmap))
    schema.functions[name] = fn


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
    name = m.group(1).lower()
    timing_m = re.search(r"\b(BEFORE|AFTER|INSTEAD\s+OF)\b", sql, re.I)
    table_m = re.search(r"\bON\s+([A-Za-z_][A-Za-z0-9_.]*)", sql, re.I)
    fn_m = re.search(
        r"EXECUTE\s+(?:FUNCTION|PROCEDURE)\s+([A-Za-z_][A-Za-z0-9_.]*)",
        sql,
        re.I,
    )
    events = [e.upper() for e in re.findall(r"\b(INSERT|UPDATE|DELETE|TRUNCATE)\b", sql, re.I)]
    # first event keywords after timing, skip those in function name
    schema.triggers[name] = DbTrigger(
        name=name,
        table=(table_m.group(1).split(".")[-1].lower() if table_m else ""),
        timing=(timing_m.group(1).upper() if timing_m else "AFTER"),
        events=events or ["INSERT"],
        function=(fn_m.group(1).split(".")[-1].lower() if fn_m else ""),
        evidence=evidence,
    )


def apply_statement(schema: Schema, sql: str, evidence: Evidence) -> None:
    stripped = sql.strip()
    if not stripped:
        return
    try:
        parsed = sqlglot.parse(stripped, read="postgres", error_level=ErrorLevel.IGNORE)
    except Exception:
        parsed = []

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
                handled = True
            elif kind == "FUNCTION":
                _apply_function(schema, stripped, evidence)
                handled = True
        elif isinstance(stmt, exp.Alter):
            _apply_alter(schema, stmt)
            handled = True
        elif isinstance(stmt, exp.Command):
            cmd = str(stmt.this or "").upper()
            if cmd == "DO":
                _apply_do_block(schema, stripped, evidence)
                handled = True
            elif cmd == "CREATE" and re.search(r"(?is)FUNCTION", str(stmt.args.get("expression") or stripped)):
                _apply_function(schema, stripped, evidence)
                handled = True
            elif cmd == "CREATE" and re.search(r"(?is)TRIGGER", str(stmt.args.get("expression") or stripped)):
                _apply_trigger_regex(schema, stripped, evidence)
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


def _stmt_symbol(stmt: str) -> str:
    m = CREATE_FN.search(stmt)
    if m:
        return m.group(1).split(".")[-1]
    m = CREATE_TRG.search(stmt)
    if m:
        return m.group(1)
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
    return schema


def replay_files(files: list[Path], root: Path) -> Schema:
    schema = Schema()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel_file = rel(path, root)
        for line, stmt in split_sql_statements(text):
            evidence = Evidence(file=rel_file, line=line, symbol=_stmt_symbol(stmt))
            apply_statement(schema, stmt, evidence)
    return schema
