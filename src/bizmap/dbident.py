"""Which database a table lives in (the ``db`` attribute).

Table ids stay ``table:<name>`` (one per repo). This pass only *labels*
table / view / collection nodes with ``db`` when the code says which
connection reaches them:

- a connection built from a DSN variable or config field
  (``sql.Open("pgx", os.Getenv("ORDERS_DATABASE_URL"))``,
  ``pgxpool.New(ctx, cfg.OrdersDatabaseURL)``,
  ``new Pool({ connectionString: process.env.SUMMARY_DATABASE_URL })``,
  ``create_engine(settings.ORDERS_DATABASE_URL)``) gets a name from that
  variable (``orders_db``), or from ``databases:`` in bizmap.yaml;
- the SQL edge's call (``src.QueryContext``, ``b.query``, ``cur.execute``)
  names the connection (through cursor / transaction aliases);
- a repo with exactly one named connection puts every table on it;
- Firestore clients (``firestore.NewClient``, ``getFirestore()``,
  ``firestore.client()``) put collections on ``firestore:<project|default>``.

When nothing says which database it is, ``db`` is left out (the table is in
the repo's database, as before).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from bizmap.graph import Graph

_ENV_RES = [
    re.compile(r"os\.(?:Getenv|LookupEnv)\(\s*\"(\w+)\""),
    re.compile(r"process\.env\.(\w+)"),
    re.compile(r"process\.env\[\s*['\"](\w+)['\"]"),
    re.compile(r"Deno\.env\.get\(\s*['\"](\w+)['\"]"),
    re.compile(r"os\.environ\[\s*['\"](\w+)['\"]"),
    re.compile(r"os\.(?:environ\.get|getenv)\(\s*['\"](\w+)['\"]"),
    re.compile(r"\benv\(\s*['\"](\w+)['\"]"),
    re.compile(r"\bconfig\(\s*['\"](\w+)['\"]"),
]
# config struct / settings object fields that look like a DSN
_FIELD_RE = re.compile(
    r"\b(?:cfg|config|conf|settings|c|opts|options|env|Config|Settings|s\.cfg|s\.config|app\.config)"
    r"\.(\w*(?:(?i:database|dsn|postgres|mysql)|DB|Db|_db|db_|^db|PG|_pg|pg_)\w*)"
)
_CTOR_RE = re.compile(
    r"\b(?:sql\.Open|sqlx\.(?:Open|Connect|MustConnect|MustOpen)|pgxpool\.(?:New|NewWithConfig|Connect|ParseConfig)"
    r"|pgx\.Connect|gorm\.Open|postgres\.Open|mysql\.Open"
    r"|new\s+(?:pg\.)?Pool|new\s+(?:pg\.)?Client|new\s+Sequelize|new\s+PrismaClient|postgres\(|knex\(|mysql\.createPool"
    r"|mysql\.createConnection|createPool\(|drizzle\("
    r"|create_engine|create_async_engine|psycopg2?\.connect|psycopg\.(?:connect|AsyncConnection\.connect)"
    r"|asyncpg\.(?:connect|create_pool)|pymysql\.connect|aiopg\.create_pool|databases\.Database)"
)
_ASSIGN_BEFORE = [
    re.compile(r"(?:const|let|var)\s+(\w+)\s*(?::\s*[\w.<>\[\]]+\s*)?=\s*(?:await\s+)?[^=]*$"),
    re.compile(r"(?:var\s+)?([\w.]+)\s*(?:,\s*\w+)?\s*:?=\s*(?:await\s+)?[^=]*$"),
    re.compile(r"[\"']?(\w+)[\"']?\s*:\s*(?:await\s+)?[^:]*$"),
]
_DSN_VAR_RE = re.compile(r"(?:(?:const|let|var)\s+)?(\w+)\s*(?:,\s*\w+)?\s*:?=\s*(.+)$")
_ALIAS_RES = [
    # cur = conn.cursor() / tx, err := db.BeginTx(ctx) / client = await pool.connect()
    re.compile(
        r"(?:const|let|var\s+)?(\w+)\s*(?:,\s*\w+)?\s*:?=\s*(?:await\s+)?([\w.]+)\."
        r"(?:cursor|connect|begin|Begin|BeginTx|BeginTxx|Beginx|acquire|Acquire|transaction|session|Conn|connection)\("
    ),
]
_WITH_ALIAS_RE = re.compile(
    r"with\s+(?:await\s+)?([\w.]+)\.(?:cursor|connect|begin|acquire|transaction)\([^)]*\)\s+as\s+(\w+)"
)
_CB_ALIAS_RE = re.compile(
    r"([\w.]+)\.(?:transaction|tx|begin|Transaction|RunInTx|WithTx)\(\s*(?:async\s*)?(?:function\s*)?\(?\s*(\w+)"
)
_CALL_RE = re.compile(
    r"([\w.]+?)\s*(?:\)\s*)?\.\s*(QueryContext|QueryRowContext|ExecContext|Query|QueryRow|Exec|Get|GetContext|Select"
    r"|SelectContext|NamedExec|NamedExecContext|NamedQuery|Raw|query|execute|executemany|fetch|fetchrow|fetchval"
    r"|one|none|any|many|oneOrNone|manyOrNone|unsafe|raw)\s*[(`]"
)
_TAGGED_RE = re.compile(r"\b(\w+)\s*`")
_FS_PROJECT_RES = [
    re.compile(r"firestore\.NewClient\(\s*\w+\s*,\s*\"([^\"]+)\""),
    re.compile(r"new\s+Firestore\(\s*\{[^}]*projectId\s*:\s*['\"]([^'\"]+)['\"]", re.S),
    re.compile(r"firestore\.(?:Async)?Client\(\s*project\s*=\s*['\"]([^'\"]+)['\"]"),
]
_FS_CLIENT_RE = re.compile(
    r"firestore\.NewClient\(|getFirestore\(|admin\.firestore\(|new\s+Firestore\(|firestore\.(?:Async)?Client\("
    r"|firestore\.client\(|@google-cloud/firestore|firebase-admin/firestore"
)

_STRIP_TAIL = {
    "URL", "URI", "DSN", "CONN", "CONNECTION", "STRING", "DATABASE", "DB", "PG", "POSTGRES",
    "POSTGRESQL", "MYSQL", "ADDR", "ADDRESS", "DBURL", "DBURI", "DBDSN", "PGURL", "PGDSN", "DATABASEURL",
}
_STRIP_HEAD = {"PG", "POSTGRES", "POSTGRESQL", "DB", "DATABASE", "MYSQL"}


def db_name_from_env(env: str) -> str | None:
    """``ORDERS_DATABASE_URL`` → ``orders_db``; ``DATABASE_URL`` → None."""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", _upper_snake(env)) if p]
    while parts and parts[-1] in _STRIP_TAIL:
        parts.pop()
    while parts and parts[0] in _STRIP_HEAD:
        parts.pop(0)
    if not parts:
        return None
    return "_".join(parts).lower() + "_db"


def _upper_snake(name: str) -> str:
    if name.isupper() or "_" in name and name.upper() == name:
        return name
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return s.upper()


def config_databases(raw: dict[str, Any] | None) -> dict[str, str]:
    """``databases: {orders_db: {env: ORDERS_DATABASE_URL}}`` → env → name.

    Also ``{orders_db: ORDERS_DATABASE_URL}`` and ``env: [A, B]``."""
    out: dict[str, str] = {}
    spec = (raw or {}).get("databases")
    if not isinstance(spec, dict):
        return out
    for name, val in spec.items():
        envs: list[str] = []
        if isinstance(val, str):
            envs = [val]
        elif isinstance(val, dict):
            env = val.get("env") or val.get("envs")
            if isinstance(env, str):
                envs = [env]
            elif isinstance(env, list):
                envs = [str(e) for e in env]
        for env in envs:
            out[_upper_snake(env)] = str(name)
    return out


@dataclass
class ConnIndex:
    """Connection variable (last name segment) → database, per file / dir / repo."""

    by_file: dict[str, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(set)))
    by_dir: dict[str, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(set)))
    by_repo: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    aliases: dict[str, dict[str, str]] = field(default_factory=lambda: defaultdict(dict))
    named: set[str] = field(default_factory=set)  # every database name seen
    unnamed_conns: int = 0
    firestore: str | None = None

    def add(self, rel: str, var: str, db: str | None) -> None:
        if not db:
            self.unnamed_conns += 1
            return
        key = var.split(".")[-1]
        self.by_file[rel][key].add(db)
        self.by_dir[os.path.dirname(rel)][key].add(db)
        self.by_repo[key].add(db)
        self.named.add(db)

    def resolve(self, rel: str, receiver: str) -> str | None:
        key = receiver.split(".")[-1]
        seen = set()
        aliases = self.aliases.get(rel) or {}
        while key in aliases and key not in seen:
            seen.add(key)
            key = aliases[key].split(".")[-1]
        for table in (
            self.by_file.get(rel),
            self.by_dir.get(os.path.dirname(rel)),
            self.by_repo,
        ):
            if not table:
                continue
            dbs = table.get(key)
            if dbs:
                return next(iter(dbs)) if len(dbs) == 1 else None
        return None

    def only_db(self) -> str | None:
        """The repo's single database, when it has one named connection only."""
        if len(self.named) == 1 and not self.unnamed_conns:
            return next(iter(self.named))
        return None


def _env_names(window: str, dsn_vars: dict[str, str]) -> list[str]:
    envs: list[str] = []
    for rx in _ENV_RES:
        envs += rx.findall(window)
    envs += [_upper_snake(f) for f in _FIELD_RE.findall(window)]
    if not envs:
        for ident in re.findall(r"\b(\w+)\b", window):
            if ident in dsn_vars:
                envs.append(dsn_vars[ident])
    return envs


def _window(text: str, start: int) -> str:
    """From ``start`` to where the brackets opened after it close."""
    depth = 0
    i = start
    n = len(text)
    opened = False
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
            opened = True
        elif c in ")]}":
            depth -= 1
            if opened and depth <= 0:
                return text[start : i + 1]
        elif c == "\n" and not opened:
            return text[start:i]
        i += 1
        if i - start > 600:
            break
    return text[start:i]


def build_index(texts: dict[str, str], raw_config: dict[str, Any] | None) -> ConnIndex:
    """Find connections in the repo's source files (``rel path → text``)."""
    cfg = config_databases(raw_config)
    idx = ConnIndex()
    projects: set[str] = set()
    fs_seen = False
    for rel in sorted(texts):
        text = texts[rel]
        if _FS_CLIENT_RE.search(text):
            fs_seen = True
            for rx in _FS_PROJECT_RES:
                projects.update(rx.findall(text))
        if not _CTOR_RE.search(text) and "cursor(" not in text and "Begin" not in text and "connect(" not in text:
            continue
        dsn_vars: dict[str, str] = {}
        for line in text.splitlines():
            m = _DSN_VAR_RE.search(line.strip())
            if not m or _CTOR_RE.search(line):
                continue
            envs = _env_names(m.group(2), {})
            if envs:
                dsn_vars[m.group(1)] = envs[0]
        for m in _CTOR_RE.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            before = text[line_start : m.start()]
            var = None
            for rx in _ASSIGN_BEFORE:
                am = rx.search(before)
                if am:
                    var = am.group(1)
                    break
            if var is None:
                # `db, err :=` on the previous line of a multi-line call is rare; skip
                continue
            window = _window(text, m.start())
            envs = _env_names(window, dsn_vars)
            db = None
            for env in envs:
                env_u = _upper_snake(env)
                db = cfg.get(env_u) or db_name_from_env(env_u)
                if db:
                    break
            idx.add(rel, var, db)
        aliases = idx.aliases[rel]
        for line in text.splitlines():
            if _CTOR_RE.search(line):
                continue
            for rx in _ALIAS_RES:
                am = rx.search(line)
                if am and am.group(1) != am.group(2).split(".")[-1]:
                    aliases.setdefault(am.group(1), am.group(2))
            wm = _WITH_ALIAS_RE.search(line)
            if wm:
                aliases.setdefault(wm.group(2), wm.group(1))
            cm = _CB_ALIAS_RE.search(line)
            if cm and cm.group(2) not in {"async", "function"}:
                aliases.setdefault(cm.group(2), cm.group(1))
    if fs_seen:
        idx.firestore = f"firestore:{next(iter(projects))}" if len(projects) == 1 else "firestore:default"
    return idx


_CHAINED_RE = re.compile(
    r"([\w.]+)\.(?:cursor|connect|acquire|begin|Begin|session|connection)\([^()]*\)\s*\.\s*"
    r"(?:execute|executemany|query|Query|Exec|QueryContext|ExecContext|fetch|fetchrow)\s*\("
)


def receiver_at(lines: list[str], line: int) -> str | None:
    """Connection receiver of the SQL call on ``line`` (or, when that line
    continues a call opened above, the line that opened it)."""
    for ln in range(line, max(line - 5, 0), -1):
        if ln - 1 >= len(lines) or ln < 1:
            continue
        text = lines[ln - 1]
        if ln < line and text.count("(") <= text.count(")") and not text.rstrip().endswith(("(", ",", "+", "`")):
            return None
        cm = _CHAINED_RE.search(text)
        if cm:
            return cm.group(1)
        hits = list(_CALL_RE.finditer(text))
        if hits:
            return hits[-1].group(1).lstrip(".")
        tm = _TAGGED_RE.search(text)
        if tm and tm.group(1) not in {"return", "await", "const", "let", "var"}:
            return tm.group(1)
    return None


def _table_of(to_id: str) -> str | None:
    tail = to_id.split("/", 1)[1] if "/" in to_id.split(":", 1)[0] else to_id
    if tail.startswith(("table:", "view:")):
        return to_id
    if tail.startswith("column:"):
        prefix = to_id[: len(to_id) - len(tail)]
        name = tail[len("column:") :].rsplit(".", 1)[0]
        return f"{prefix}table:{name}"
    return None


def assign_dbs(graph: Graph, texts: dict[str, str], idx: ConnIndex) -> None:
    """Set ``db`` on table / view / collection nodes the code places in a database."""
    if idx.firestore:
        for node in graph.nodes.values():
            if node.type == "collection" and not node.attrs.get("db"):
                node.attrs["db"] = idx.firestore
    if not idx.named:
        return
    only = idx.only_db()
    votes: dict[str, set[str]] = defaultdict(set)
    split_lines: dict[str, list[str]] = {}
    for edge in graph.edges.values():
        if edge.type not in ("reads", "writes") or edge.attrs.get("derived"):
            continue
        tid = _table_of(edge.to_id)
        if tid is None or edge.evidence is None:
            continue
        node = graph.nodes.get(tid)
        if node is None or node.attrs.get("external"):
            continue
        rel = edge.evidence.file
        if rel not in texts:
            continue
        lines = split_lines.get(rel)
        if lines is None:
            lines = split_lines[rel] = texts[rel].splitlines()
        recv = receiver_at(lines, int(edge.evidence.line or 0))
        db = idx.resolve(rel, recv) if recv else None
        if db is None:
            db = only
        if db:
            votes[tid].add(db)
    for tid, dbs in votes.items():
        node = graph.nodes.get(tid)
        if node is None:
            continue
        if len(dbs) == 1:
            node.attrs["db"] = next(iter(dbs))
        else:
            node.attrs["db_candidates"] = sorted(dbs)
    if only:
        for node in graph.nodes.values():
            if node.type in ("table", "view") and not node.attrs.get("external") and not node.attrs.get("db"):
                if node.attrs.get("db_candidates") or node.id.endswith("table:unknown"):
                    continue
                node.attrs["db"] = only


def table_db(graph: Graph, node_id: str) -> str | None:
    """The database label of a table / collection node (``db``, else the graph's ``database``)."""
    node = graph.nodes.get(node_id)
    if node is not None and node.attrs.get("db"):
        return str(node.attrs["db"])
    return graph.database or None


def table_label(graph: Graph, node_id: str) -> str:
    """``orders_db.pedidos`` (or ``pedidos`` when the database is not known)."""
    node = graph.nodes.get(node_id)
    name = str((node.attrs.get("name") if node else None) or node_id.split(":", 1)[-1])
    db = table_db(graph, node_id)
    return f"{db}.{name}" if db else name


def link_same_tables(graph: Graph) -> None:
    """In a merged graph: the same table (name + ``db``) seen by two repos →
    ``same_as`` on each node (ids and nodes are kept)."""
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for node in graph.nodes.values():
        if node.type not in ("table", "collection", "view"):
            continue
        db = node.attrs.get("db")
        if not db:
            continue
        name = str(node.attrs.get("name") or node.id.split(":", 1)[-1]).lower()
        groups[(node.type, str(db), name)].append(node.id)
    for ids in groups.values():
        if len(ids) < 2:
            continue
        for nid in ids:
            graph.nodes[nid].attrs["same_as"] = sorted(i for i in ids if i != nid)
