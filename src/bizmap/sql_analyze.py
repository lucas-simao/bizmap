from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from bizmap.graph import Confidence

PLACEHOLDER_N = re.compile(r"\$(\d+)")
NAMED_PARAM = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
PY_PERCENT = re.compile(r"%s")
# Leading `-- comment` lines and `/* … */` blocks are allowed: sqlc puts
# `-- name: GetOrder :one` before every query.
SQL_START = re.compile(
    r"^\s*(?:(?:--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)\s*)*\(?\s*(WITH|SELECT|INSERT|UPDATE|DELETE|MERGE|EXECUTE)\b",
    re.IGNORECASE,
)
# Bare or quoted identifier, optionally schema-qualified (pg_dump style).
IDENT_ATOM = r'(?:"[^"]+"|`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)'
QUAL_IDENT = rf"{IDENT_ATOM}(?:\s*\.\s*{IDENT_ATOM})*"
TABLE_FROM_PREFIX = re.compile(
    r"(?is)\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN(?:\s+(?:INNER|LEFT|RIGHT|FULL|CROSS))?(?:\s+OUTER)?(?:\s+LATERAL)?)\s+"
    rf"(?:ONLY\s+)?({QUAL_IDENT})"
)
DYNAMIC_PLACEHOLDER = re.compile(r"\{[^{}]+\}")
# Concat/helper holes (`{helper(o)}`, `{t.o}`) — not JSON `{"a": 1}`.
SQL_HOLE = re.compile(r"\{(?![\"'])[^{}]+\}")
# Table-name holes: `"UPDATE "+t+" SET"`, `EXECUTE format('UPDATE %I ...')`.
# Strip ON CONFLICT / FOR UPDATE OF first so `DO UPDATE SET` / aliases are not holes.
DYNAMIC_TABLE_HOLE = re.compile(
    r"(?is)\b(?:INSERT\s+INTO|(?<!DO\s)UPDATE|DELETE\s+FROM)\s+"
    r"(?:ONLY\s+)?(?:%I|%s|%L|\?|\{[^{}]+\}|(?=\s*(?:SET|WHERE|VALUES|RETURNING)\b))"
)
_ON_CONFLICT_TAIL = re.compile(r"(?is)\bON\s+CONFLICT\b.*$")
_FOR_UPDATE_CLAUSE = re.compile(
    r"(?is)\bFOR\s+UPDATE(?:\s+OF\s+[A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)?"
)
_FORMAT_IDENT = re.compile(r"(?is)\bformat\s*\(\s*['\"][^'\"]*%I")
EXTERNAL_SCHEMAS = {
    "pg_catalog",
    "information_schema",
    "storage",
    "vault",
    "auth",
}
_CTE_HEAD = re.compile(r"(?is)\bWITH\s+(?:RECURSIVE\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+AS\b")
_CTE_MORE = re.compile(r"(?is),\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(")
_SUBQUERY_OPEN = re.compile(r"(?is)(?:\bLATERAL\s*)?\(\s*(?:SELECT|WITH|VALUES)\b")
_ALIAS_AFTER = re.compile(r"\s*(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)

SKIP_TABLES = {
    "new",
    "old",
    "excluded",
    "unnest",
    "generate_series",
    "jsonb_each",
    "jsonb_each_text",
    "jsonb_array_elements",
    "jsonb_array_elements_text",
    "json_each",
    "json_array_elements",
    "regexp_split_to_table",
}

# Words that are never table names (dynamic SQL holes like `"UPDATE "+t+" SET"`).
SQL_KEYWORDS = {
    "select",
    "insert",
    "update",
    "delete",
    "merge",
    "from",
    "where",
    "set",
    "into",
    "values",
    "table",
    "only",
    "as",
    "on",
    "join",
    "left",
    "right",
    "inner",
    "outer",
    "cross",
    "full",
    "lateral",
    "true",
    "false",
    "null",
    "and",
    "or",
    "not",
    "in",
    "is",
    "like",
    "ilike",
    "between",
    "exists",
    "case",
    "when",
    "then",
    "else",
    "end",
    "returning",
    "with",
    "recursive",
    "union",
    "all",
    "distinct",
    "order",
    "group",
    "having",
    "limit",
    "offset",
    "fetch",
    "for",
    "nowait",
    "do",
    "conflict",
    "nothing",
    "using",
    "by",
    "asc",
    "desc",
    "nulls",
    "first",
    "last",
    "over",
    "partition",
    "rows",
    "range",
    "unbounded",
    "preceding",
    "following",
    "current",
    "row",
    "natural",
    "window",
    "filter",
    "within",
    "unknown",
    # PL/pgSQL / type words that leak from function bodies
    "any",
    "if",
    "elsif",
    "elseif",
    "return",
    "raise",
    "begin",
    "declare",
    "exception",
    "loop",
    "while",
    "foreach",
    "exit",
    "continue",
    "notice",
    "warning",
    "debug",
    "info",
    "assert",
    "strict",
    "next",
    "perform",
    "found",
    "timestamp",
    "the",
    "void",
    "trigger",
    "record",
    "coalesce",
    "nullif",
    "greatest",
    "least",
    "integer",
    "bigint",
    "varchar",
    "boolean",
    "numeric",
    "jsonb",
    "bytea",
    "serial",
    "bigserial",
    "tg_op",
    "tg_name",
    "tg_when",
    "tg_level",
    "tg_relid",
    "tg_table_name",
    "tg_table_schema",
    "tg_nargs",
    "tg_argv",
    "sqlstate",
    "sqlerrm",
}

# Extra PL/pgSQL names never treated as tables (also merged into SQL_KEYWORDS).
PLPGSQL_KEYWORDS = {
    "found",
    "perform",
    "any",
    "if",
    "then",
    "else",
    "elsif",
    "elseif",
    "return",
    "raise",
    "begin",
    "end",
    "declare",
    "exception",
    "loop",
    "while",
    "foreach",
    "exit",
    "continue",
    "notice",
    "warning",
    "debug",
    "info",
    "assert",
    "strict",
    "next",
    "timestamp",
    "the",
    "void",
    "trigger",
    "record",
    "coalesce",
    "nullif",
    "greatest",
    "least",
    "now",
    "integer",
    "bigint",
    "varchar",
    "boolean",
    "numeric",
    "jsonb",
    "bytea",
    "serial",
    "bigserial",
    "tg_op",
    "tg_name",
    "tg_when",
    "tg_level",
    "tg_relid",
    "tg_table_name",
    "tg_table_schema",
    "tg_nargs",
    "tg_argv",
    "sqlstate",
    "sqlerrm",
}

PG_CATALOG_TABLES = {
    "pg_aggregate",
    "pg_am",
    "pg_amop",
    "pg_amproc",
    "pg_attrdef",
    "pg_attribute",
    "pg_authid",
    "pg_auth_members",
    "pg_cast",
    "pg_class",
    "pg_collation",
    "pg_constraint",
    "pg_conversion",
    "pg_database",
    "pg_db_role_setting",
    "pg_default_acl",
    "pg_depend",
    "pg_description",
    "pg_enum",
    "pg_event_trigger",
    "pg_extension",
    "pg_foreign_data_wrapper",
    "pg_foreign_server",
    "pg_foreign_table",
    "pg_index",
    "pg_indexes",
    "pg_inherits",
    "pg_init_privs",
    "pg_language",
    "pg_largeobject",
    "pg_largeobject_metadata",
    "pg_locks",
    "pg_namespace",
    "pg_opclass",
    "pg_operator",
    "pg_opfamily",
    "pg_partitioned_table",
    "pg_policy",
    "pg_proc",
    "pg_publication",
    "pg_publication_rel",
    "pg_range",
    "pg_replication_origin",
    "pg_rewrite",
    "pg_roles",
    "pg_rules",
    "pg_seclabel",
    "pg_sequence",
    "pg_settings",
    "pg_shadow",
    "pg_stat_activity",
    "pg_stat_all_tables",
    "pg_stat_statements",
    "pg_statio_user_tables",
    "pg_statistic",
    "pg_tables",
    "pg_tablespace",
    "pg_trigger",
    "pg_type",
    "pg_user",
    "pg_views",
}


@dataclass
class SqlAccess:
    kind: str  # read | write
    table: str
    column: str | None
    confidence: Confidence
    raw_sql: str
    dynamic: bool = False
    external: bool = False
    unknown_table: bool = False
    candidates: list[str] = field(default_factory=list)
    schema_qualified: bool = False
    unresolved: bool = False
    relation_kind: str | None = None  # external_relation | unresolved_relation


def ident_parts(raw: str | None) -> list[str]:
    """Split a possibly quoted, schema-qualified identifier into components."""
    if not raw:
        return []
    parts = [
        a or b or c
        for a, b, c in re.findall(r'"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*)', raw)
    ]
    return [p for p in parts if p]


def ident_leaf(raw: str | None) -> str:
    """Last component of a quoted/schema-qualified identifier, unquoted, lowercased."""
    parts = ident_parts(raw)
    if parts:
        return parts[-1].lower()
    return (raw or "").strip().strip('"').strip("`").lower()


def ident_is_qualified(raw: str | None) -> bool:
    """True when the identifier has a schema/catalog qualifier (dot)."""
    return len(ident_parts(raw)) >= 2 or ("." in (raw or ""))


def has_dynamic_table_hole(sql: str | None) -> bool:
    """True when a *table name* is interpolated, not a SET/EXCLUDED column.

    ``"UPDATE "+t+" SET"`` and ``EXECUTE format('%I')`` are holes.
    Literal ``INSERT ... ON CONFLICT (...) DO UPDATE SET c = EXCLUDED.c`` is not.
    """
    if not sql or not sql.strip():
        return False
    if _FORMAT_IDENT.search(sql) or (
        re.search(r"(?is)\bEXECUTE\s+format\s*\(", sql) and "%I" in sql
    ):
        return True
    stripped = _ON_CONFLICT_TAIL.sub("", sql)
    stripped = _FOR_UPDATE_CLAUSE.sub("", stripped)
    if DYNAMIC_TABLE_HOLE.search(stripped):
        return True
    if DYNAMIC_PLACEHOLDER.search(stripped) and re.search(
        r"(?is)\b(?:INSERT|UPDATE|DELETE|MERGE)\b", stripped
    ):
        if not re.search(r"(?is)\b(?:FROM|INTO|UPDATE|JOIN)\s+[A-Za-z_]", stripped):
            return True
    return False


def looks_like_sql(text: str | None) -> bool:
    if not text:
        return False
    return bool(SQL_START.search(text.strip()))


def mask_sql_holes(sql: str) -> str:
    """Replace concat/helper `{placeholders}` so sqlglot cannot invent columns."""
    return SQL_HOLE.sub(" TRUE ", sql or "")


def hole_column_names(sql: str | None) -> set[str]:
    """Column-like tokens that only exist inside `{...}` concat holes."""
    names: set[str] = set()
    for match in SQL_HOLE.finditer(sql or ""):
        inner = match.group(0).strip("{}")
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", inner):
            names.add(tok.lower())
        if "." in inner:
            names.add(inner.split(".")[-1].lower().split("(")[0])
    return names


def _column_only_in_holes(sql: str | None, column: str | None) -> bool:
    """True when ``column`` appears only inside a concat `{placeholder}`."""
    if not sql or not column:
        return False
    name = column.lower()
    if name not in hole_column_names(sql):
        return False
    masked = SQL_HOLE.sub(" ", sql)
    return re.search(rf"\b{re.escape(name)}\b", masked, re.IGNORECASE) is None


def normalize_placeholders(sql: str) -> str:
    sql = mask_sql_holes(sql)
    sql = PLACEHOLDER_N.sub(r"@p\1", sql)
    sql = PY_PERCENT.sub("?", sql)
    sql = NAMED_PARAM.sub(r"@\1", sql)
    return sql


def is_external_table(name: str | None) -> bool:
    if not name:
        return False
    raw = name.lower().strip()
    parts = ident_parts(raw) or [p for p in raw.split(".") if p]
    if parts and parts[0] in EXTERNAL_SCHEMAS:
        return True
    if raw.startswith("information_schema.") or raw == "information_schema":
        return True
    if raw.startswith("pg_catalog.") or raw == "pg_catalog":
        return True
    leaf = parts[-1] if parts else raw.split(".")[-1]
    if leaf.startswith("pg_") or leaf in PG_CATALOG_TABLES:
        return True
    return False


def is_skipped_table_name(name: str | None, extra: set[str] | None = None) -> bool:
    if not name:
        return True
    leaf = ident_leaf(name)
    if extra and (leaf in extra or name.lower() in extra):
        return True
    if leaf in SKIP_TABLES or leaf in SQL_KEYWORDS or leaf in PLPGSQL_KEYWORDS:
        return True
    if leaf.startswith("{") or "}" in leaf:
        return True
    return False


def _safe_name(node: object) -> str:
    """``node.name`` without raising: sqlglot computes it from children that
    can be missing (`Dot(this=…, expression=None)` for `UPDATE orders ...`)."""
    try:
        name = getattr(node, "name", None)
    except (AttributeError, TypeError):
        return ""
    return str(name) if name else ""


def _ident(node: exp.Expression | str | None) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    name = _safe_name(node)
    if name:
        return name
    this = getattr(node, "this", None)
    if this is not None and this is not node:
        return _ident(this)
    return ""


def _table_name(table: exp.Table | exp.Expression | None) -> str:
    if table is None:
        return ""
    if isinstance(table, exp.Table):
        name = _safe_name(table)
        db = _ident(table.args.get("db"))
        if db:
            return f"{db}.{name}" if name else db
        return name
    return _ident(table)


def _alias_name(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    alias = getattr(node, "alias", None)
    if alias:
        return str(alias).lower()
    alias_node = node.args.get("alias") if hasattr(node, "args") else None
    if alias_node is not None:
        return _ident(alias_node).lower()
    return ""


def _alias_ident(node: exp.Expression | str | None) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node.lower()
    name = _alias_name(node) if isinstance(node, exp.Expression) else ""
    if name:
        return name
    ident = _ident(node)
    if ident:
        return ident.lower()
    this = getattr(node, "this", None)
    if this is not None and this is not node:
        return _ident(this).lower()
    return ""


def _matching_paren_close(sql: str, open_idx: int) -> int | None:
    depth = 0
    i = open_idx
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def text_phantom_aliases(sql: str) -> set[str]:
    """CTE / derived-table / LATERAL aliases extracted from SQL text.

    Used when sqlglot misses an alias (or the fallback path runs). Identifiers
    that are SQL keywords are ignored.
    """
    names: set[str] = set()
    if not sql:
        return names
    head = _CTE_HEAD.search(sql)
    if head:
        names.add(head.group(1).lower())
        names.update(m.group(1).lower() for m in _CTE_MORE.finditer(sql))
    for match in _SUBQUERY_OPEN.finditer(sql):
        paren = sql.find("(", match.start())
        if paren < 0:
            continue
        close = _matching_paren_close(sql, paren)
        if close is None:
            continue
        alias_m = _ALIAS_AFTER.match(sql, close + 1)
        if not alias_m:
            continue
        ident = alias_m.group(1).lower()
        if ident in SQL_KEYWORDS or ident in SKIP_TABLES:
            continue
        names.add(ident)
    return names


def _phantom_names(root: exp.Expression, extra: set[str] | None = None) -> set[str]:
    """CTE / subquery / LATERAL / UDTF aliases that must not become table nodes."""
    names: set[str] = set(extra or ())
    for cte in root.find_all(exp.CTE):
        n = _ident(cte.args.get("alias") or cte.this)
        if n:
            names.add(n.lower())
    for sub in root.find_all(exp.Subquery):
        n = _alias_ident(sub) or _alias_ident(sub.args.get("alias") if hasattr(sub, "args") else None)
        if n:
            names.add(n)
    if hasattr(exp, "Lateral"):
        for lat in root.find_all(exp.Lateral):
            n = _alias_ident(lat) or _alias_ident(lat.args.get("alias") if hasattr(lat, "args") else None)
            if n:
                names.add(n)
    for udtf in root.find_all(exp.UDTF):
        n = _alias_ident(udtf) or _alias_ident(udtf.args.get("alias") if hasattr(udtf, "args") else None)
        if n:
            names.add(n)
        inner = _ident(udtf.this) if hasattr(udtf, "this") else ""
        # Function names (unnest, jsonb_each, ...) are not tables; skip
        # copying the function identifier when it already lives in SKIP_TABLES.
        if inner and inner.lower() not in SKIP_TABLES:
            # LATERAL (subquery) AS alias: inner is a Select, not a table name.
            if not isinstance(getattr(udtf, "this", None), (exp.Select, exp.Subquery, exp.Query)):
                names.add(inner.lower())
    for unnest in root.find_all(exp.Unnest):
        n = _alias_ident(unnest)
        if n:
            names.add(n)
    return names


def _is_lock_table(table: exp.Expression) -> bool:
    """``FOR UPDATE OF alias`` is a lock target, not a FROM table."""
    parent = getattr(table, "parent", None)
    return parent is not None and type(parent).__name__ == "Lock"


def _alias_map(root: exp.Expression, extra_phantoms: set[str] | None = None) -> dict[str, str]:
    aliases: dict[str, str] = {}
    phantoms = _phantom_names(root, extra_phantoms)
    for table in root.find_all(exp.Table):
        if _is_lock_table(table):
            continue
        name = _table_name(table)
        if not name:
            continue
        leaf = name.lower().split(".")[-1]
        if leaf in SKIP_TABLES or leaf in SQL_KEYWORDS or leaf in phantoms:
            continue
        alias = (table.alias_or_name or name).lower()
        aliases[alias] = name
        aliases[name.lower()] = name
        aliases[leaf] = name
    for phantom in phantoms:
        aliases.pop(phantom, None)
    return aliases


def _resolve_table(
    raw_table: str | None,
    aliases: dict[str, str],
    fallback: str | None,
    phantoms: set[str],
) -> str | None:
    if raw_table:
        key = raw_table.lower()
        leaf = key.split(".")[-1]
        if leaf in SKIP_TABLES or leaf in SQL_KEYWORDS or leaf in phantoms or key in phantoms:
            return None
        if "." in raw_table:
            raw_table = raw_table.split(".")[-1]
            key = raw_table.lower()
            leaf = key
            if leaf in SKIP_TABLES or leaf in SQL_KEYWORDS or leaf in phantoms:
                return None
        resolved = aliases.get(key, aliases.get(leaf))
        if resolved:
            rleaf = resolved.lower().split(".")[-1]
            if rleaf in phantoms or rleaf in SQL_KEYWORDS:
                return None
            return resolved
        if key in phantoms or leaf in phantoms:
            return None
        # Unresolved qualifier: only accept it when it is a real FROM/JOIN
        # table we already recorded. Never invent tables from subquery aliases
        # such as ``files.file_id`` or ``l.*``.
        real_leaves = {v.lower().split(".")[-1] for v in aliases.values()}
        real_full = {v.lower() for v in aliases.values()}
        if leaf in real_leaves or key in real_full:
            return raw_table.split(".")[-1]
        return None
    if fallback:
        return fallback
    real = {v for v in aliases.values() if v.lower().split(".")[-1] not in phantoms}
    if len({v.lower() for v in real}) == 1:
        return next(iter(real))
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
    *,
    unknown_table: bool = False,
    candidates: list[str] | None = None,
    skip_names: set[str] | None = None,
) -> None:
    if unknown_table:
        key = (kind, "unknown", (column or "").lower(), confidence.value, dynamic, True)
        if key in seen:
            return
        seen.add(key)
        out.append(
            SqlAccess(
                kind=kind,
                table="unknown",
                column=column.lower() if column else None,
                confidence=Confidence.UNKNOWN,
                raw_sql=sql,
                dynamic=True,
                unknown_table=True,
                candidates=list(candidates or []),
            )
        )
        return
    if column and ("{" in column or "}" in column):
        column = None
    if column and _column_only_in_holes(sql, column):
        column = None
    if not table or is_skipped_table_name(table, skip_names):
        return
    qualified = ident_is_qualified(table)
    external = is_external_table(table)
    if not external:
        table = ident_leaf(table)
        external = is_external_table(table)
    else:
        # Keep catalog/extension schema qualification (storage.objects, auth.users)
        # to avoid colliding with app tables such as ``columns`` / ``users``.
        parts = ident_parts(table)
        if len(parts) >= 2 and parts[0] in EXTERNAL_SCHEMAS:
            table = f"{parts[0]}.{parts[-1]}".lower()
        elif qualified:
            table = table.lower()
    if skip_names and ident_leaf(table) in skip_names:
        return
    key = (kind, table.lower(), (column or "").lower(), confidence.value, dynamic, external)
    if key in seen:
        return
    seen.add(key)
    relation_kind = None
    if external:
        relation_kind = "external_relation"
    out.append(
        SqlAccess(
            kind=kind,
            table=table,
            column=column.lower() if column else None,
            confidence=confidence,
            raw_sql=sql,
            dynamic=dynamic,
            external=external,
            schema_qualified=qualified,
            relation_kind=relation_kind,
        )
    )


def _real_tables(aliases: dict[str, str], phantoms: set[str]) -> set[str]:
    out: set[str] = set()
    for value in aliases.values():
        leaf = value.lower().split(".")[-1]
        if leaf in phantoms or is_skipped_table_name(leaf):
            continue
        out.add(value)
    return out


def _table_allowed(
    table: str | None,
    known_tables: set[str] | None,
    phantoms: set[str] | None = None,
    *,
    schema_qualified: bool = False,
    skip_names: set[str] | None = None,
) -> bool:
    """Whether ``table`` may become a table/column access.

    With a non-empty DDL set, only those tables (plus catalogs) are kept,
    except schema-qualified names which are allowed through. Without DDL,
    any real-looking identifier is kept except phantoms.
    """
    if not table:
        return False
    leaf = ident_leaf(table)
    if phantoms and (leaf in phantoms or table.lower() in phantoms):
        return False
    if skip_names and (leaf in skip_names or table.lower() in skip_names):
        return False
    if is_skipped_table_name(leaf, skip_names):
        return False
    if is_external_table(table):
        return True
    known = {t.lower() for t in (known_tables or ())}
    if known:
        if leaf in known or table.lower() in known:
            return True
        if schema_qualified or ident_is_qualified(table):
            return True
        # Named FROM/JOIN/INSERT/UPDATE relations that are not in DDL are
        # kept as unresolved_relation nodes (not silently dropped).
        return True
    return True


def _analyze_statement(
    stmt: exp.Expression,
    sql: str,
    schema_columns: dict[str, list[str]] | None,
    dynamic: bool,
    expand_star: bool,
    out: list[SqlAccess],
    seen: set[tuple],
    extra_phantoms: set[str] | None = None,
    known_tables: set[str] | None = None,
    skip_names: set[str] | None = None,
) -> None:
    aliases = _alias_map(stmt, extra_phantoms)
    phantoms = _phantom_names(stmt, extra_phantoms)
    if skip_names:
        phantoms = set(phantoms) | {s.lower() for s in skip_names}
    conf = Confidence.UNKNOWN if dynamic else Confidence.EXACT

    def known_cols(table: str) -> list[str]:
        if not schema_columns:
            return []
        return schema_columns.get(table.lower(), schema_columns.get(table, []))

    if isinstance(stmt, exp.Insert):
        table = _table_name(stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this)
        if is_skipped_table_name(table, skip_names):
            return
        cols = _column_writes_from_schema(stmt)
        if cols:
            for col in cols:
                _add(out, "write", table, col, conf, sql, dynamic, seen, skip_names=skip_names)
        else:
            # Column-less INSERT stays table-level (no schema explosion).
            _add(
                out,
                "write",
                table,
                None,
                Confidence.INFERRED if not dynamic else Confidence.UNKNOWN,
                sql,
                dynamic,
                seen,
                skip_names=skip_names,
            )
        conflict = stmt.args.get("conflict") if hasattr(stmt, "args") else None
        if conflict is not None:
            for expr in conflict.args.get("expressions") or []:
                name = ""
                if isinstance(expr, exp.EQ):
                    name = _ident(expr.this)
                else:
                    name = _ident(expr)
                if name and name.lower() not in SKIP_TABLES:
                    _add(out, "write", table, name, conf, sql, dynamic, seen, skip_names=skip_names)
        for inner in stmt.find_all(exp.Select):
            if inner is stmt:
                continue
            _analyze_select(
                inner, sql, schema_columns, dynamic, expand_star, out, seen, aliases, phantoms,
                known_tables=known_tables,
            )
        return

    if isinstance(stmt, exp.Update):
        table = _table_name(stmt.this)
        if is_skipped_table_name(table, skip_names):
            return
        cols = _set_columns(stmt)
        if cols:
            for col in cols:
                _add(out, "write", table, col, conf, sql, dynamic, seen, skip_names=skip_names)
        else:
            _add(
                out,
                "write",
                table,
                None,
                Confidence.UNKNOWN,
                sql,
                dynamic,
                seen,
                skip_names=skip_names,
            )
        where = stmt.args.get("where")
        if where:
            for col in where.find_all(exp.Column):
                t = _resolve_table(col.table, aliases, table, phantoms)
                _add(out, "read", t, col.name, conf, sql, dynamic, seen, skip_names=skip_names)
        for inner in stmt.find_all(exp.Select):
            _analyze_select(
                inner, sql, schema_columns, dynamic, expand_star, out, seen, aliases, phantoms,
                known_tables=known_tables,
            )
        return

    if isinstance(stmt, exp.Delete):
        table = _table_name(stmt.this)
        if is_skipped_table_name(table, skip_names):
            return
        _add(out, "write", table, None, conf, sql, dynamic, seen, skip_names=skip_names)
        where = stmt.args.get("where")
        if where:
            for col in where.find_all(exp.Column):
                t = _resolve_table(col.table, aliases, table, phantoms)
                _add(out, "read", t, col.name, conf, sql, dynamic, seen, skip_names=skip_names)
        return

    if isinstance(stmt, exp.Select) or stmt.find(exp.Select):
        select = stmt if isinstance(stmt, exp.Select) else stmt.find(exp.Select)
        if select:
            _analyze_select(
                select, sql, schema_columns, dynamic, expand_star, out, seen, aliases, phantoms,
                known_tables=known_tables,
            )
        return


def _analyze_select(
    stmt: exp.Select,
    sql: str,
    schema_columns: dict[str, list[str]] | None,
    dynamic: bool,
    expand_star: bool,
    out: list[SqlAccess],
    seen: set[tuple],
    aliases: dict[str, str] | None = None,
    phantoms: set[str] | None = None,
    known_tables: set[str] | None = None,
) -> None:
    aliases = aliases or _alias_map(stmt, phantoms)
    phantoms = phantoms if phantoms is not None else _phantom_names(stmt)
    conf = Confidence.UNKNOWN if dynamic else Confidence.EXACT
    tables = _real_tables(aliases, phantoms)
    fallback = next(iter(tables)) if len(tables) == 1 else None
    star_on_real = False
    for col in stmt.find_all(exp.Column):
        if col.name != "*":
            continue
        qual = str(col.table or "").lower()
        if qual and (qual in phantoms or qual.split(".")[-1] in phantoms):
            continue
        star_on_real = True
        break
    if not star_on_real:
        for star in stmt.find_all(exp.Star):
            parent = star.parent
            qual = ""
            if isinstance(parent, exp.Column):
                qual = str(parent.table or "").lower()
            if qual and (qual in phantoms or qual.split(".")[-1] in phantoms):
                continue
            star_on_real = True
            break
    if star_on_real:
        targets = [fallback] if fallback else list(tables)
        for table in targets:
            if not table or is_skipped_table_name(table) or table.lower().split(".")[-1] in phantoms:
                continue
            if not _table_allowed(table, known_tables, phantoms):
                continue
            if expand_star:
                cols = (schema_columns or {}).get(table.lower(), []) if table else []
                if cols:
                    for col in cols:
                        _add(out, "read", table, col, Confidence.INFERRED, sql, dynamic, seen)
                else:
                    _add(out, "read", table, None, Confidence.INFERRED, sql, dynamic, seen)
            else:
                _add(out, "read", table, None, Confidence.INFERRED, sql, dynamic, seen)
    for col in stmt.find_all(exp.Column):
        if col.name == "*":
            continue
        qual = str(col.table or "").lower()
        if qual and (qual in phantoms or qual.split(".")[-1] in phantoms):
            # Alias output (files.file_id, r.x) — inner query already recorded
            # the real table/column. Never emit the alias as a table.
            continue
        table = _resolve_table(col.table, aliases, fallback, phantoms)
        if table is None:
            continue
        if table.lower().split(".")[-1] in phantoms:
            continue
        if not _table_allowed(table, known_tables, phantoms):
            continue
        _add(out, "read", table, col.name, conf, sql, dynamic, seen)


def _kind_from_sql(sql: str) -> str:
    stripped = sql.strip()
    if re.match(r"(?is)^(INSERT|UPDATE|DELETE|MERGE)\b", stripped):
        return "write"
    # ``EXECUTE format('UPDATE %I SET ...')`` is a write of an unknown table.
    if re.match(r"(?is)^EXECUTE\b", stripped) and re.search(
        r"(?is)\b(INSERT|UPDATE|DELETE|MERGE)\b", stripped
    ):
        return "write"
    return "read"


def _is_fromless_select_stmt(stmt: exp.Expression | None) -> bool:
    """True for ``SELECT now()`` / ``SELECT set_config(...)`` with no FROM/JOIN."""
    if stmt is None:
        return False
    select: exp.Select | None
    if isinstance(stmt, exp.Select):
        select = stmt
    elif isinstance(stmt, exp.With) and isinstance(stmt.this, exp.Select):
        select = stmt.this
    else:
        return False
    if select.args.get("from") or select.args.get("joins"):
        return False
    for table in select.find_all(exp.Table):
        name = _table_name(table)
        if name and not is_skipped_table_name(name):
            return False
    return True


def _sql_looks_fromless_select(sql: str) -> bool:
    stripped = sql.strip().lstrip("(").strip()
    if not re.match(r"(?is)^SELECT\b", stripped):
        return False
    if re.search(r"(?is)\b(FROM|JOIN|INTO|UPDATE|DELETE)\s+", stripped):
        return False
    return True


def _fallback_table_accesses(
    sql: str,
    dynamic: bool,
    known_tables: set[str] | None = None,
    phantoms: set[str] | None = None,
    skip_names: set[str] | None = None,
) -> list[SqlAccess]:
    out: list[SqlAccess] = []
    seen: set[tuple] = set()
    kind = _kind_from_sql(sql)
    conf = Confidence.UNKNOWN
    known = {t.lower() for t in (known_tables or ())}
    phantoms = set(phantoms or ()) | text_phantom_aliases(sql)
    found_real = False
    for match in TABLE_FROM_PREFIX.finditer(sql):
        raw = match.group(1)
        qualified = ident_is_qualified(raw)
        if is_skipped_table_name(raw, skip_names):
            continue
        if not _table_allowed(
            raw, known or None, phantoms, schema_qualified=qualified, skip_names=skip_names
        ):
            continue
        prefix = sql[: match.start()].upper()
        edge_kind = kind
        if "JOIN" in prefix[-20:] or re.search(r"FROM\s*$", prefix.strip()):
            if kind == "write":
                ahead = sql[max(0, match.start() - 32) : match.start()].upper()
                if not re.search(r"INSERT\s+INTO\s*$", ahead):
                    edge_kind = "read"
        _add(out, edge_kind, raw, None, conf, sql, True, seen, skip_names=skip_names)
        found_real = True
    if not found_real:
        match = re.search(rf"(?is)\b(?:INTO|UPDATE|FROM)\s+({QUAL_IDENT})", sql)
        if match:
            raw = match.group(1)
            if _table_allowed(
                raw,
                known or None,
                phantoms,
                schema_qualified=ident_is_qualified(raw),
                skip_names=skip_names,
            ):
                _add(out, kind, raw, None, conf, sql, True, seen, skip_names=skip_names)
                found_real = True
    if found_real:
        return out
    if has_dynamic_table_hole(sql) or (
        dynamic
        and re.search(r"(?is)^(INSERT|UPDATE|DELETE|MERGE)\b", sql.strip())
        and not _sql_looks_fromless_select(sql)
    ):
        candidates = sorted(t for t in known if t not in SQL_KEYWORDS) if known else []
        _add(
            out,
            kind,
            None,
            None,
            Confidence.UNKNOWN,
            sql,
            True,
            seen,
            unknown_table=True,
            candidates=candidates,
        )
        return out
    # Aliases-only (or FROM-less) SQL: do not invent table:unknown when DDL exists.
    if known or phantoms:
        return out
    candidates = sorted(t for t in known if t not in SQL_KEYWORDS) if known else []
    _add(
        out,
        kind,
        None,
        None,
        Confidence.UNKNOWN,
        sql,
        True,
        seen,
        unknown_table=True,
        candidates=candidates,
    )
    return out


# SQL strings whose analysis raised: (first 80 chars, error), collected for
# the scan summary by `drain_sql_failures()`.
_SQL_FAILURES: list[tuple[str, str]] = []


def drain_sql_failures() -> list[tuple[str, str]]:
    out = list(_SQL_FAILURES)
    _SQL_FAILURES.clear()
    return out


def analyze_sql(sql: str, **kwargs: Any) -> list[SqlAccess]:
    """See `_analyze_sql`. A statement that cannot be analysed (sqlglot can
    raise on odd input such as `UPDATE orders ...`) yields no accesses and is
    reported, instead of failing the scan."""
    try:
        return _analyze_sql(sql, **kwargs)
    except RecursionError:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad statement must not stop the scan
        snippet = " ".join((sql or "").split())[:80]
        if len(_SQL_FAILURES) < 1000:
            _SQL_FAILURES.append((snippet, f"{type(exc).__name__}: {exc}"[:160]))
        return []


def _analyze_sql(
    sql: str,
    *,
    schema_columns: dict[str, list[str]] | None = None,
    dynamic: bool = False,
    expand_star: bool = False,
    known_tables: set[str] | None = None,
    require_known: bool = True,
    skip_names: set[str] | None = None,
) -> list[SqlAccess]:
    """Parse a SQL string (postgres dialect) into per-column or table-level accesses.

    Column edges are emitted only when the SQL names columns. ``SELECT *`` and
    INSERT without a column list stay table-level unless ``expand_star`` is set
    (SELECT * only). CTE / subquery / LATERAL aliases and SQL keywords never
    become tables. When ``known_tables`` (DDL) is non-empty, only those tables
    (plus catalogs) are emitted, except schema-qualified names.
    """
    if not sql or not sql.strip():
        return []
    known = {t.lower() for t in (known_tables or ())}
    if schema_columns:
        known.update(k.lower() for k in schema_columns.keys())
    if not require_known:
        known = set()
    skip = {s.lower() for s in (skip_names or ())}
    phantoms = text_phantom_aliases(sql) | skip
    if has_dynamic_table_hole(sql) or (
        DYNAMIC_PLACEHOLDER.search(sql)
        and not re.search(r"(?is)\b(?:FROM|INTO|UPDATE|JOIN)\s+[A-Za-z_]", sql)
        and re.search(r"(?is)\b(?:INSERT|UPDATE|DELETE|MERGE)\b", sql)
    ):
        # e.g. UPDATE {table} SET ... / EXECUTE format('%I') — table name interpolated.
        # Literal ON CONFLICT ... DO UPDATE SET c = EXCLUDED.c is not a hole.
        kind = _kind_from_sql(sql)
        return [
            SqlAccess(
                kind=kind,
                table="unknown",
                column=None,
                confidence=Confidence.UNKNOWN,
                raw_sql=sql,
                dynamic=True,
                unknown_table=True,
                candidates=sorted(known),
            )
        ]
    normalized = normalize_placeholders(sql)
    stmts: list[exp.Expression | None]
    try:
        stmts = sqlglot.parse(normalized, read="postgres", error_level=ErrorLevel.IGNORE)
    except Exception:
        return _fallback_table_accesses(
            sql, dynamic=True, known_tables=known, phantoms=phantoms, skip_names=skip
        )

    out: list[SqlAccess] = []
    seen: set[tuple] = set()
    parsed_any = False
    parsed_stmts: list[exp.Expression] = []
    for stmt in stmts:
        if stmt is None:
            continue
        if isinstance(stmt, exp.Command):
            continue
        parsed_any = True
        parsed_stmts.append(stmt)
        _analyze_statement(
            stmt,
            sql,
            schema_columns,
            dynamic,
            expand_star,
            out,
            seen,
            extra_phantoms=phantoms,
            known_tables=known,
            skip_names=skip,
        )
    kept: list[SqlAccess] = []
    for a in out:
        if a.unknown_table or _table_allowed(
            a.table,
            known or None,
            phantoms,
            schema_qualified=a.schema_qualified,
            skip_names=skip,
        ):
            if (
                not a.unknown_table
                and not a.external
                and known
                and ident_leaf(a.table) not in known
                and a.table.lower() not in known
            ):
                a.unresolved = True
                a.relation_kind = a.relation_kind or "unresolved_relation"
            kept.append(a)
    out = kept
    if not parsed_any or not out:
        # FROM-less SELECT (set_config, now(), SELECT 1) touches no table.
        # Functions are never tables; do not invent table:unknown.
        fromless = False
        if parsed_stmts:
            fromless = all(_is_fromless_select_stmt(s) for s in parsed_stmts)
        if fromless or (not parsed_any and _sql_looks_fromless_select(sql)):
            return []
        if parsed_any and not out and _sql_looks_fromless_select(sql):
            return []
        return _fallback_table_accesses(
            sql, dynamic=True, known_tables=known, phantoms=phantoms, skip_names=skip
        )
    if dynamic:
        for access in out:
            access.dynamic = True
            if access.confidence == Confidence.EXACT:
                access.confidence = Confidence.UNKNOWN
    return out
