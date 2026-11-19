"""SQLAlchemy Core/ORM and Alembic schema extraction from Python ASTs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from bizmap.detect.py_resolve import (
    ImportMap,
    ResolvedString,
    call_kwargs,
    call_posargs,
    resolve_expr,
    unquote,
)
from bizmap.detect.tsutil import call_callee_name, line_of, text, walk
from bizmap.graph import Evidence
from bizmap.schema_replay import Column, ForeignKey, Schema, _table, apply_statement

SA_TYPE_MAP = {
    "Integer": "INTEGER",
    "BigInteger": "BIGINT",
    "SmallInteger": "SMALLINT",
    "String": "VARCHAR",
    "Text": "TEXT",
    "Unicode": "VARCHAR",
    "UnicodeText": "TEXT",
    "Boolean": "BOOLEAN",
    "DateTime": "TIMESTAMP",
    "Date": "DATE",
    "Time": "TIME",
    "Float": "FLOAT",
    "Numeric": "NUMERIC",
    "JSON": "JSONB",
    "JSONB": "JSONB",
    "Uuid": "UUID",
    "UUID": "UUID",
    "LargeBinary": "BYTEA",
    "Enum": "TEXT",
    "Interval": "INTERVAL",
    "ARRAY": "ARRAY",
}
PY_TYPE_MAP = {
    "int": "INTEGER",
    "str": "TEXT",
    "bool": "BOOLEAN",
    "float": "FLOAT",
    "bytes": "BYTEA",
    "dict": "JSONB",
    "datetime": "TIMESTAMP",
    "date": "DATE",
    "time": "TIME",
    "UUID": "UUID",
    "uuid": "UUID",
    "Decimal": "NUMERIC",
}


@dataclass
class AlembicRevision:
    revision: str
    down_revision: str | None
    file: str
    path: Path
    tree: Node
    source: bytes
    module: str
    upgrade: Node | None = None


@dataclass
class ModelInfo:
    class_name: str
    table: str
    file: str
    columns: dict[str, Column] = field(default_factory=dict)


def _leaf_call_name(node: Node) -> str:
    raw = call_callee_name(node) if node.type == "call" else text(node)
    return raw.split(".")[-1]


def _type_from_expr(node: Node | None) -> str:
    if node is None:
        return "unknown"
    if node.type == "call":
        return _type_from_expr(node.child_by_field_name("function") or node.named_children[0])
    raw = text(node)
    leaf = raw.split(".")[-1].split("[")[0]
    if leaf in SA_TYPE_MAP:
        return SA_TYPE_MAP[leaf]
    if leaf in PY_TYPE_MAP:
        return PY_TYPE_MAP[leaf]
    inner = node.named_children[-1] if node.named_children else None
    if inner is not None and inner is not node:
        mapped = _type_from_expr(inner)
        if mapped != "unknown":
            return mapped
    return "unknown"


def _mapped_python_type(type_node: Node | None) -> tuple[str, bool]:
    """Return (sql_type, nullable) from `Mapped[int]` / `Mapped[str | None]`."""
    if type_node is None:
        return "unknown", True
    raw = text(type_node)
    nullable = "None" in raw or "Optional" in raw
    inner = raw
    if "Mapped[" in raw:
        inner = raw.split("Mapped[", 1)[1]
        if inner.endswith("]"):
            inner = inner[:-1]
    inner = inner.replace("Optional[", "").replace("]", "")
    first = inner.split("|")[0].strip().split(".")[-1]
    sql_type = PY_TYPE_MAP.get(first, SA_TYPE_MAP.get(first, "unknown"))
    if first in PY_TYPE_MAP and "None" not in inner and "Optional" not in raw:
        nullable = False
    return sql_type, nullable


def _column_from_call(
    call: Node,
    *,
    fallback_name: str | None,
    resolve,
) -> Column | None:
    name = fallback_name
    data_type = "unknown"
    nullable = True
    default = None
    pos = call_posargs(call)
    kw = call_kwargs(call)
    callee = _leaf_call_name(call)
    if callee in {"Column", "column"}:
        if pos:
            resolved = resolve(pos[0])
            if resolved.fully_static and resolved.text:
                name = unquote(resolved.text) if resolved.text[:1] in "\"'" else resolved.text
            elif pos[0].type == "string":
                name = unquote(text(pos[0]))
        if len(pos) > 1:
            data_type = _type_from_expr(pos[1])
    elif callee in {"mapped_column", "mappedColumn"}:
        if pos:
            data_type = _type_from_expr(pos[0])
        if "type_" in kw:
            data_type = _type_from_expr(kw["type_"])
    else:
        return None
    if "nullable" in kw:
        nullable = text(kw["nullable"]) not in {"False", "false"}
    if "primary_key" in kw and text(kw["primary_key"]) in {"True", "true"}:
        nullable = False
    if "default" in kw:
        default = text(kw["default"])
    if not name:
        return None
    return Column(name=name.lower(), data_type=data_type, nullable=nullable, default=default)


def _fk_from_column_call(call: Node, table_name: str, col_name: str, resolve) -> ForeignKey | None:
    for node in walk(call):
        if node.type != "call":
            continue
        if _leaf_call_name(node) not in {"ForeignKey", "foreign_key"}:
            continue
        pos = call_posargs(node)
        if not pos:
            continue
        target = resolve(pos[0])
        ref = target.path_text if target.usable else unquote(text(pos[0]))
        ref = ref.strip()
        if "." not in ref:
            continue
        ref_table, ref_col = ref.split(".", 1)
        return ForeignKey(
            table=table_name,
            columns=[col_name],
            ref_table=ref_table.lower(),
            ref_columns=[ref_col.lower()],
        )
    return None


def extract_core_tables(
    tree: Node,
    *,
    file: str,
    resolve,
    schema: Schema,
    bindings: dict[str, str],
) -> None:
    for node in walk(tree):
        if node.type != "assignment":
            continue
        right = node.child_by_field_name("right")
        left = node.child_by_field_name("left")
        if right is None or right.type != "call":
            continue
        if _leaf_call_name(right) != "Table":
            continue
        pos = call_posargs(right)
        if not pos:
            continue
        name_r = resolve(pos[0])
        tname = name_r.path_text if name_r.fully_static else None
        if not tname:
            continue
        tname = tname.lower()
        table = _table(schema, tname)
        if left is not None and left.type == "identifier":
            bindings[text(left)] = tname
        for arg in pos[2:]:
            call = arg if arg.type == "call" else None
            if call is None:
                continue
            col = _column_from_call(call, fallback_name=None, resolve=resolve)
            if col:
                table.columns[col.name] = col
                fk = _fk_from_column_call(call, tname, col.name, resolve)
                if fk:
                    table.foreign_keys.append(fk)


def extract_orm_models(
    tree: Node,
    *,
    file: str,
    resolve,
    schema: Schema,
    models: dict[str, ModelInfo],
    bindings: dict[str, str],
) -> None:
    for node in walk(tree):
        if node.type != "class_definition":
            continue
        cname = text(node.child_by_field_name("name"))
        if not cname:
            continue
        body = node.child_by_field_name("body")
        if body is None:
            continue
        table_name: str | None = None
        columns: dict[str, Column] = {}
        fks: list[ForeignKey] = []
        for stmt in body.named_children:
            assign = None
            if stmt.type == "expression_statement" and stmt.named_children:
                inner = stmt.named_children[0]
                if inner.type == "assignment":
                    assign = inner
            if assign is None:
                continue
            left = assign.child_by_field_name("left")
            right = assign.child_by_field_name("right")
            type_n = assign.child_by_field_name("type")
            lname = text(left) if left is not None else ""
            if lname == "__tablename__" and right is not None:
                resolved = resolve(right)
                if resolved.fully_static and resolved.text:
                    table_name = resolved.text.lower()
                continue
            if lname == "__table__" and right is not None:
                key = text(right)
                if key in bindings:
                    table_name = bindings[key]
                continue
            if right is not None and right.type == "call":
                col = _column_from_call(right, fallback_name=lname, resolve=resolve)
                if col:
                    if type_n is not None and col.data_type == "unknown":
                        sql_t, nullable = _mapped_python_type(type_n)
                        col.data_type = sql_t
                        if "nullable" not in call_kwargs(right):
                            col.nullable = nullable
                    columns[col.name] = col
                    if table_name:
                        fk = _fk_from_column_call(right, table_name, col.name, resolve)
                        if fk:
                            fks.append(fk)
                    continue
            if type_n is not None and lname and not lname.startswith("_"):
                sql_t, nullable = _mapped_python_type(type_n)
                columns[lname.lower()] = Column(
                    name=lname.lower(), data_type=sql_t, nullable=nullable
                )
        if not table_name:
            continue
        table = _table(schema, table_name)
        for col in columns.values():
            table.columns.setdefault(col.name, col)
        for fk in fks:
            table.foreign_keys.append(fk)
        models[cname] = ModelInfo(class_name=cname, table=table_name, file=file, columns=columns)
        bindings[cname] = table_name


def _const_str(node: Node | None, resolve) -> str | None:
    if node is None:
        return None
    r = resolve(node)
    if r.fully_static and r.text:
        return r.text
    if node.type == "string":
        return unquote(text(node))
    return None


def collect_alembic_revisions(
    files: list[tuple[Path, str, bytes, Node, str]],
    root: Path,
) -> list[AlembicRevision]:
    found: list[AlembicRevision] = []
    for path, rel_file, source, tree, module in files:
        parts = rel_file.replace("\\", "/").lower().split("/")
        if not (
            ("alembic" in parts and "versions" in parts)
            or "migrations" in parts
            or "migrate" in parts
        ):
            continue
        if not rel_file.endswith(".py"):
            continue
        revision = None
        down: str | None = None
        upgrade = None
        for node in tree.named_children:
            if node.type == "expression_statement" and node.named_children:
                inner = node.named_children[0]
                if inner.type != "assignment":
                    continue
                name = text(inner.child_by_field_name("left"))
                right = inner.child_by_field_name("right")
                if name == "revision" and right is not None:
                    revision = unquote(text(right)) if right.type == "string" else text(right).strip("\"'")
                elif name == "down_revision":
                    if right is None or text(right) in {"None", "none"}:
                        down = None
                    elif right.type == "string":
                        down = unquote(text(right))
                    elif right.type == "tuple":
                        first = right.named_children[0] if right.named_children else None
                        down = unquote(text(first)) if first is not None else None
                    else:
                        raw = text(right).strip("\"'")
                        down = None if raw in {"None", "none", ""} else raw
            if node.type == "function_definition":
                if text(node.child_by_field_name("name")) == "upgrade":
                    upgrade = node
            if node.type == "decorated_definition":
                fn = next((c for c in node.named_children if c.type == "function_definition"), None)
                if fn is not None and text(fn.child_by_field_name("name")) == "upgrade":
                    upgrade = fn
        if revision or upgrade is not None:
            found.append(
                AlembicRevision(
                    revision=revision or rel_file,
                    down_revision=down,
                    file=rel_file,
                    path=path,
                    tree=tree,
                    source=source,
                    module=module,
                    upgrade=upgrade,
                )
            )
    return found


def order_alembic(revs: list[AlembicRevision]) -> list[AlembicRevision]:
    if not revs:
        return []
    by_id = {r.revision: r for r in revs}
    dependents: dict[str, list[str]] = {r.revision: [] for r in revs}
    roots: list[str] = []
    for r in revs:
        parent = r.down_revision
        if parent and parent in by_id:
            dependents[parent].append(r.revision)
        else:
            roots.append(r.revision)
    ordered: list[AlembicRevision] = []
    seen: set[str] = set()
    queue = list(roots)
    # stable: filename order among roots
    queue.sort(key=lambda rid: by_id[rid].file)
    while queue:
        rid = queue.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        ordered.append(by_id[rid])
        kids = sorted(dependents.get(rid, []), key=lambda k: by_id[k].file)
        queue.extend(kids)
    for r in sorted(revs, key=lambda x: x.file):
        if r.revision not in seen:
            ordered.append(r)
    return ordered


def apply_alembic_upgrade(schema: Schema, rev: AlembicRevision, resolve) -> None:
    if rev.upgrade is None:
        return
    evidence = Evidence(file=rev.file, line=line_of(rev.upgrade), symbol=f"{rev.module}.upgrade")
    _apply_ops(schema, rev.upgrade, resolve, evidence, batch_table=None)


def _apply_ops(
    schema: Schema,
    root: Node,
    resolve,
    evidence: Evidence,
    batch_table: str | None,
) -> None:
    for node in walk(root):
        if node.type == "with_statement":
            _apply_batch_with(schema, node, resolve, evidence)
            continue
        if node.type != "call":
            continue
        name = _leaf_call_name(node)
        pos = call_posargs(node)
        if name == "create_table" and pos:
            tname = _const_str(pos[0], resolve)
            if not tname:
                continue
            table = _table(schema, tname.lower())
            for arg in pos[1:]:
                call = arg if arg.type == "call" else None
                if call is None:
                    continue
                col = _column_from_call(call, fallback_name=None, resolve=resolve)
                if col:
                    table.columns[col.name] = col
                    fk = _fk_from_column_call(call, table.name, col.name, resolve)
                    if fk:
                        table.foreign_keys.append(fk)
        elif name == "add_column" and len(pos) >= 2:
            tname = _const_str(pos[0], resolve) or batch_table
            if not tname:
                continue
            table = _table(schema, tname.lower())
            col_node = pos[1] if pos[1].type == "call" else None
            if col_node is not None:
                col = _column_from_call(col_node, fallback_name=None, resolve=resolve)
                if col:
                    table.columns[col.name] = col
        elif name == "drop_column" and pos:
            tname = batch_table
            col_name = None
            if batch_table and len(pos) >= 1:
                col_name = _const_str(pos[0], resolve)
            elif len(pos) >= 2:
                tname = _const_str(pos[0], resolve)
                col_name = _const_str(pos[1], resolve)
            if tname and col_name:
                table = schema.tables.get(tname.lower())
                if table:
                    table.columns.pop(col_name.lower(), None)
        elif name == "drop_table" and pos:
            tname = _const_str(pos[0], resolve)
            if tname:
                schema.tables.pop(tname.lower(), None)
        elif name == "rename_table" and len(pos) >= 2:
            old = _const_str(pos[0], resolve)
            new = _const_str(pos[1], resolve)
            if old and new and old.lower() in schema.tables:
                table = schema.tables.pop(old.lower())
                table.name = new.lower()
                schema.tables[new.lower()] = table
        elif name == "execute" and pos:
            sql_r = resolve(pos[0])
            sql = sql_r.path_text if sql_r.usable else None
            if sql and not sql_r.unresolved:
                apply_statement(schema, sql, evidence)


def _apply_batch_with(schema: Schema, node: Node, resolve, evidence: Evidence) -> None:
    table_name = None
    for inner in walk(node):
        if inner.type == "call" and _leaf_call_name(inner) == "batch_alter_table":
            pos = call_posargs(inner)
            kw = call_kwargs(inner)
            arg = pos[0] if pos else kw.get("table_name")
            table_name = _const_str(arg, resolve) if arg is not None else None
            break
    if not table_name:
        return
    body = node.child_by_field_name("body") or node.named_children[-1]
    _apply_ops(schema, body, resolve, evidence, batch_table=table_name.lower())


def is_schema_rel(rel_file: str) -> bool:
    parts = rel_file.replace("\\", "/").lower().split("/")
    if "alembic" in parts:
        return True
    if "migrations" in parts or "migrate" in parts:
        return True
    return False


def apply_python_schema(
    schema: Schema,
    parsed: list[tuple[Path, str, bytes, Node, str, ImportMap]],
    consts_by_module: dict[str, dict[str, ResolvedString]],
    root: Path,
) -> tuple[dict[str, ModelInfo], dict[str, str]]:
    """Fill ``schema`` from ORM models, Core Table() and Alembic upgrades.

    Returns (models_by_class_name, bindings name->table).
    """
    models: dict[str, ModelInfo] = {}
    bindings: dict[str, str] = {}

    def resolver_for(imap: ImportMap, local: dict[str, ResolvedString]):
        def resolve(node: Node | None) -> ResolvedString:
            return resolve_expr(node, local, local, imap, consts_by_module)

        return resolve

    alembic_inputs = [(p, relf, src, tree, mod) for p, relf, src, tree, mod, _imap in parsed]
    revs = order_alembic(collect_alembic_revisions(alembic_inputs, root))
    # Apply Alembic first (revision order), then models overlay remaining columns.
    for rev in revs:
        imap = next((im for p, rf, _s, _t, _m, im in parsed if rf == rev.file), ImportMap())
        local = consts_by_module.get(rev.module) or {}
        apply_alembic_upgrade(schema, rev, resolver_for(imap, local))

    for _path, rel_file, _src, tree, module, imap in parsed:
        if is_schema_rel(rel_file):
            continue
        local = consts_by_module.get(module) or {}
        resolve = resolver_for(imap, local)
        extract_core_tables(tree, file=rel_file, resolve=resolve, schema=schema, bindings=bindings)
        extract_orm_models(
            tree,
            file=rel_file,
            resolve=resolve,
            schema=schema,
            models=models,
            bindings=bindings,
        )
    return models, bindings
