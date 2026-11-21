"""Firestore and Pub/Sub in Go (a text pass over the Go files that use them).

Runs after the Go detector and attributes each call to the Go function whose
lines contain it:

- **Firestore** (``cloud.google.com/go/firestore``): chains
  ``x.Collection("orders").Doc(id).Get(ctx)`` / ``.Documents(ctx)`` /
  ``.Where(…)…`` (read), ``.Set / .Update / .Delete / .Create`` and
  ``Collection(…).Add`` (write), a reference kept in a variable, and
  ``tx.Get(ref)`` / ``tx.Set(ref, …)`` / ``batch.Set(ref, …)``. The collection
  comes from a literal or a package constant.
- **Pub/Sub publish** (``cloud.google.com/go/pubsub``, v1 and v2):
  ``client.Publisher(t).Publish(ctx, msg)`` / ``client.Topic(t).Publish(…)``
  (also through a variable), and a **publisher wrapper**: a type whose
  constructor takes the topic id(s) and calls ``client.Publisher(id)`` /
  ``client.Topic(id)`` on them, and whose ``Publish`` method publishes. Its
  topics are the constructor's arguments where it is built
  (``NewPublisher(ctx, cfg.ProjectID, []string{cfg.OrderTopic})``); a call
  ``x.Publish(ctx, data, …)`` through an interface publishes to them (one
  topic → that topic; several → ``candidates``).
- A topic is a literal, a package constant, or a config field filled by an
  env lookup (``OrderTopic: getEnv("ORDER_ANNOUNCE_TOPIC", "order-announce")``,
  ``os.Getenv("X")``, or a struct tag ``env:"X" envDefault:"y"``): the default
  is the name and the variable is kept (``env``); without a default the
  topic is dynamic unless ``env:`` in bizmap.yaml gives its value.
- The message ``fields`` come from what is marshalled: the ``json`` tags of
  the struct passed to ``json.Marshal`` (a literal, a variable of that type,
  or a function returning it), or a map literal's keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bizmap.detect.js_firestore import FsAccess
from bizmap.srctext import Masked, mask, match_close, split_top

FS_IMPORT = re.compile(r'"cloud\.google\.com/go/firestore"')
PS_IMPORT = re.compile(r'"cloud\.google\.com/go/pubsub(?:/v2)?"')
_IDENT = r"[A-Za-z_]\w*"
_CONST_LINE = re.compile(rf"(?:^|\n)\s*(?:const\s+)?({_IDENT})\s*(?:{_IDENT}\s*)?=\s*(\x00\d+\x00)\s*(?=\n|;|\))")
_ENV_CALL = re.compile(
    r"\b(?:os\.Getenv|os\.LookupEnv|\w*[Ee]nv\w*|\w*[Gg]etenv\w*)\(\s*(\x00\d+\x00)\s*(?:,\s*(\x00\d+\x00)\s*)?\)"
)
# `OrderTopic: getEnv(…)` in a composite literal, `cfg.OrderTopic = getEnv(…)`
_FIELD_START = re.compile(rf"(?:(?:^|[{{,\s])({_IDENT})\s*:(?!=)|\.({_IDENT})\s*=(?!=))\s*")


def _expr_at(t: str, i: int) -> str:
    """The expression starting at ``i``, up to a top-level `,` `}` `;` or newline."""
    depth = 0
    j = i
    while j < len(t):
        c = t[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and c in ",;\n":
            break
        j += 1
    return t[i:j]
_STRUCT = re.compile(rf"\btype\s+({_IDENT})\s+struct\s*\{{")
_FUNC_RET = re.compile(rf"\bfunc\s+(?:\([^)]*\)\s*)?({_IDENT})\s*\(")
_CHAIN_READ = {"Get", "Documents", "Snapshots", "GetAll"}
_CHAIN_WRITE = {"Set", "Update", "Delete", "Create", "Add"}
_CHAIN_PASS = {"Doc", "Collection", "Where", "WhereEntity", "OrderBy", "Limit", "LimitToLast", "Offset", "Select",
               "StartAt", "StartAfter", "EndAt", "EndBefore", "Query", "Parent"}
_TOPIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~%+-]{0,254}$")


@dataclass(frozen=True)
class GoTopic:
    name: str | None  # None: dynamic (an env var without a default)
    env: str | None = None


@dataclass
class GoPublish:
    symbol: str
    file: str
    line: int
    topics: list[GoTopic]
    fields: list[str] = field(default_factory=list)
    via: str | None = None  # the wrapper type
    candidates: bool = False  # several wrapper instances could be the receiver: which one is unknown
    confidence: str = "exact"


@dataclass
class GoCloudResult:
    accesses: list[FsAccess] = field(default_factory=list)
    publishes: list[GoPublish] = field(default_factory=list)
    json_tags: dict[str, dict[str, str]] = field(default_factory=dict)  # struct -> Go field -> json name
    fs_tags: dict[str, dict[str, str]] = field(default_factory=dict)  # struct -> Go field -> firestore name
    func_returns: dict[str, str] = field(default_factory=dict)  # func name -> returned type


@dataclass
class _Fn:
    qual: str
    start: int
    end: int


class _GoFile:
    def __init__(self, rel: str, text: str, fns: list[_Fn]):
        self.rel = rel
        self.m: Masked = mask(text, "go")
        self.text = self.m.text
        self.fns = fns
        self.lines_at = [0] + [m.end() for m in re.finditer("\n", self.text)]

    def line(self, pos: int) -> int:
        lo, hi = 0, len(self.lines_at)
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self.lines_at[mid] <= pos:
                lo = mid
            else:
                hi = mid
        return lo + 1

    def fn_at(self, line: int) -> _Fn | None:
        best = None
        for f in self.fns:
            if f.start <= line <= f.end and (best is None or f.end - f.start <= best.end - best.start):
                best = f
        return best

    def body(self, f: _Fn) -> str:
        a = self.lines_at[f.start - 1] if f.start - 1 < len(self.lines_at) else 0
        b = self.lines_at[f.end] if f.end < len(self.lines_at) else len(self.text)
        return self.text[a:b]


def _lit(m: Masked, expr: str) -> str | None:
    lit = m.lit(expr.strip())
    return None if lit is None or lit.dynamic else lit.value


class _Index:
    """Repo-wide: package constants, env-filled config fields, struct tags, return types."""

    def __init__(self, files: dict[str, _GoFile], env_values: dict[str, str]):
        self.consts: dict[str, str | None] = {}
        self.fields: dict[str, list[GoTopic]] = {}
        self.json_tags: dict[str, dict[str, str]] = {}
        self.fs_tags: dict[str, dict[str, str]] = {}
        self.returns: dict[str, str] = {}
        self.env_values = env_values
        self.env_defaults: dict[str, str] = {}  # env var -> its default wherever one is given
        self.field_values: dict[str, set[str]] = {}  # struct field -> literal / constant values set to it
        for gf in files.values():
            for em in _ENV_CALL.finditer(gf.text):
                env = _lit(gf.m, em.group(1))
                default = _lit(gf.m, em.group(2)) if em.group(2) else None
                if env and default:
                    self.env_defaults.setdefault(env, default)
        for gf in files.values():
            self._scan(gf)

    def _put_const(self, name: str, value: str) -> None:
        if name in self.consts and self.consts[name] != value:
            self.consts[name] = None  # two values: unknown
        else:
            self.consts[name] = value

    def env_topic(self, gf: _GoFile, expr: str) -> GoTopic | None:
        em = _ENV_CALL.search(expr)
        if not em:
            return None
        env = _lit(gf.m, em.group(1))
        if not env:
            return None
        default = _lit(gf.m, em.group(2)) if em.group(2) else None
        # `os.Getenv("X")` in a command, `getEnv("X", "d")` in the config package: the same default
        name = self.env_values.get(env) or default or self.env_defaults.get(env) or None
        return GoTopic(name, env)

    def _scan(self, gf: _GoFile) -> None:
        t = gf.text
        for mm in _CONST_LINE.finditer(t):
            v = _lit(gf.m, mm.group(2))
            if v is not None:
                self._put_const(mm.group(1), v)
        for mm in _FIELD_START.finditer(t):
            expr = _expr_at(t, mm.end()).strip()
            fname = mm.group(1) or mm.group(2)
            if fname and expr:
                v = _lit(gf.m, expr)
                if v is None and re.fullmatch(_IDENT, expr):
                    v = "\x01" + expr  # a constant: resolved once every file is read
                if v is not None:
                    self.field_values.setdefault(fname, set()).add(v)
            if "nv(" not in t[mm.end() : mm.end() + 60] and "Getenv" not in t[mm.end() : mm.end() + 60]:
                continue
            topic = self.env_topic(gf, _expr_at(t, mm.end()))
            if topic is not None:
                self.fields.setdefault(mm.group(1) or mm.group(2), []).append(topic)
        for mm in _STRUCT.finditer(t):
            close = match_close(t, mm.end() - 1)
            body = t[mm.end() : close]
            jt: dict[str, str] = {}
            ft: dict[str, str] = {}
            for line in re.split(r"[\n;]", body):
                fm = re.match(rf"\s*({_IDENT})\s+[^\x00\n]*?(\x00\d+\x00)\s*$", line)
                if not fm:
                    continue
                lit = gf.m.lit(fm.group(2))
                tag = lit.value if lit is not None else ""
                for key, store in (("json", jt), ("firestore", ft)):
                    tm = re.search(rf'\b{key}:"([^"]*)"', tag)
                    if tm:
                        name = tm.group(1).split(",")[0]
                        if name and name != "-":
                            store[fm.group(1)] = name
                em = re.search(r'\benv:"(\w+)"', tag)
                if em:
                    dm = re.search(r'\benvDefault:"([^"]*)"', tag)
                    env = em.group(1)
                    self.fields.setdefault(fm.group(1), []).append(
                        GoTopic(self.env_values.get(env) or (dm.group(1) if dm else None) or self.env_defaults.get(env), env)
                    )
            if jt:
                self.json_tags[mm.group(1)] = jt
            if ft:
                self.fs_tags[mm.group(1)] = ft
        for mm in _FUNC_RET.finditer(t):
            j = match_close(t, mm.end() - 1)
            rest = t[j + 1 : j + 120].split("{", 1)[0].strip()
            rm = re.match(rf"^\(?\s*\*?(?:{_IDENT}\.)?({_IDENT})", rest)
            if rm and rm.group(1) not in ("error", "string", "bool", "int", "int64", "float64", "any"):
                self.returns.setdefault(mm.group(1), rm.group(1))

    def field_value(self, name: str) -> str | None:
        """The one value a struct field is set to (`collection: "orders"` in its constructor)."""
        vals = set()
        for v in self.field_values.get(name, ()):
            if v.startswith("\x01"):
                c = self.consts.get(v[1:])
                if c is None:
                    continue
                v = c
            vals.add(v)
        return next(iter(vals)) if len(vals) == 1 else None

    def topics(self, gf: _GoFile, expr: str, fn_body: str = "") -> list[GoTopic] | None:
        """Topics an expression names (None: not resolvable)."""
        e = expr.strip().lstrip("&")
        v = _lit(gf.m, e)
        if v is not None:
            return [GoTopic(v)]
        lm = re.match(r"^\[\]string\s*\{(.*)\}$", e, re.S)
        if lm:
            out: list[GoTopic] = []
            for part in split_top(lm.group(1)):
                got = self.topics(gf, part, fn_body)
                if got is None:
                    return None
                out += got
            return out
        env = self.env_topic(gf, e)
        if env is not None:
            return [env]
        if re.fullmatch(rf"{_IDENT}(?:\.{_IDENT})*", e):
            last = e.split(".")[-1]
            if "." not in e:
                # a local `topic := "x"` / `topic := cfg.T` in the function
                am = re.search(rf"\b{re.escape(e)}\s*:?=\s*([^\n;]+)", fn_body)
                if am and am.group(1).strip() != e:
                    got = self.topics(gf, am.group(1), "")
                    if got is not None:
                        return got
            if self.consts.get(last):
                return [GoTopic(self.consts[last])]
            if last in self.fields:
                seen: dict[tuple[str | None, str | None], GoTopic] = {}
                for t in self.fields[last]:
                    seen[(t.name, t.env)] = t
                return list(seen.values())
        return None


def _fields_of_data(gf: _GoFile, idx: _Index, expr: str, fn_body: str) -> list[str]:
    """Message fields of the published bytes: what `json.Marshal` received."""
    e = expr.strip()
    mm = re.match(rf"^&?(?:{_IDENT}\.)?Message\s*\{{(.*)\}}$", e, re.S)
    if mm:
        for part in split_top(mm.group(1)):
            dm = re.match(r"^Data\s*:\s*(.+)$", part.strip(), re.S)
            if dm:
                return _fields_of_data(gf, idx, dm.group(1), fn_body)
        return []
    target = None
    if re.fullmatch(_IDENT, e):
        am = re.search(rf"\b{re.escape(e)}\s*(?:,\s*{_IDENT}\s*)?:?=\s*json\.Marshal\(", fn_body)
        if am:
            j = match_close(fn_body, am.end() - 1)
            target = fn_body[am.end() : j]
    jm = re.match(r"^json\.Marshal\((.*)\)$", e, re.S)
    if jm:
        target = jm.group(1)
    if target is None:
        return []
    return _fields_of_value(gf, idx, target.strip(), fn_body)


def _fields_of_value(gf: _GoFile, idx: _Index, e: str, fn_body: str) -> list[str]:
    e = e.lstrip("&").strip()
    mm = re.match(r"^map\[[^\]]*\][\w.{}*]*\s*\{(.*)\}$", e, re.S)
    if mm:
        keys = []
        for part in split_top(mm.group(1)):
            km = re.match(r"^(\x00\d+\x00)\s*:", part.strip())
            if km:
                v = _lit(gf.m, km.group(1))
                if v:
                    keys.append(v)
        return sorted(set(keys))
    tm = re.match(rf"^(?:{_IDENT}\.)?({_IDENT})\s*\{{", e)
    type_name = tm.group(1) if tm else None
    if type_name is None:
        cm = re.match(rf"^(?:{_IDENT}\.)*({_IDENT})\s*\(", e)
        if cm:
            type_name = idx.returns.get(cm.group(1))
    if type_name is None and re.fullmatch(_IDENT, e):
        vm = re.search(rf"\b(?:var\s+{re.escape(e)}\s+\*?(?:{_IDENT}\.)?({_IDENT})|{re.escape(e)}\s*:=\s*&?(?:{_IDENT}\.)?({_IDENT})\s*\{{)", fn_body)
        if vm:
            type_name = vm.group(1) or vm.group(2)
    if type_name and type_name in idx.json_tags:
        return sorted(set(idx.json_tags[type_name].values()))
    return []


def scan_go(
    texts: dict[str, str],
    functions: list[tuple[str, str, int, int]],
    env_values: dict[str, str] | None = None,
) -> GoCloudResult:
    """``functions``: (qualname, file, start line, end line) of the Go functions."""
    res = GoCloudResult()
    go_texts = {rel: t for rel, t in texts.items() if rel.endswith(".go") and not rel.endswith("_test.go")}
    if not any(FS_IMPORT.search(t) or PS_IMPORT.search(t) for t in go_texts.values()):
        return res
    by_file: dict[str, list[_Fn]] = {}
    for qual, file, start, end in functions:
        if file in go_texts:
            by_file.setdefault(file, []).append(_Fn(qual, start, end))
    files = {rel: _GoFile(rel, t, by_file.get(rel, [])) for rel, t in go_texts.items()}
    idx = _Index(files, env_values or {})
    res.json_tags, res.fs_tags, res.func_returns = idx.json_tags, idx.fs_tags, idx.returns
    for rel, gf in files.items():
        if FS_IMPORT.search(go_texts[rel]):
            _firestore(gf, idx, res)
    _pubsub(files, go_texts, idx, res)
    return res


# ---------------------------------------------------------------- Firestore ----


def _chain(gf: _GoFile, idx: _Index, start: int, base: list[str] | None) -> tuple[list[str] | None, int, str | None, list[str]]:
    """Follow `.Collection(…).Doc(…)…` from ``start``; (collections, end, op, last args)."""
    t = gf.text
    colls: list[str] = list(base or [])
    i = start
    op = None
    last: list[str] = []
    while True:
        mm = re.compile(r"\s*\.\s*([A-Z]\w*)\s*\(").match(t, i)
        if not mm:
            break
        name = mm.group(1)
        j = match_close(t, mm.end() - 1)
        args = split_top(t[mm.end() : j])
        i = j + 1
        if name == "Collection":
            if not args:
                return None, i, None, []
            a0 = args[0].strip()
            v = _lit(gf.m, a0)
            if v is None and re.fullmatch(_IDENT, a0):
                v = idx.consts.get(a0)
            if v is None:
                fm = re.fullmatch(rf"{_IDENT}\.({_IDENT})", a0)
                if fm:
                    v = idx.consts.get(fm.group(1)) or idx.field_value(fm.group(1))  # pkg.Const / r.collection
            if v is None:
                return None, i, None, []
            colls.append(v.strip("/").split("/")[-1] if "/" in v else v)
            continue
        if name in _CHAIN_PASS:
            continue
        if name in _CHAIN_READ:
            op = "read"
        elif name in _CHAIN_WRITE:
            op = "write"
            last = args
        break
    return (colls or None), i, op, last


def _write_fields(gf: _GoFile, idx: _Index, args: list[str], body: str) -> list[str]:
    for a in args:
        a = a.strip()
        if a == "ctx" or a.startswith("ctx"):
            continue
        mm = re.match(r"^\[\](?:firestore\.)?Update\s*\{(.*)\}$", a, re.S)
        if mm:
            return sorted({v for v in (_lit(gf.m, p) for p in re.findall(r"Path\s*:\s*(\x00\d+\x00)", mm.group(1))) if v})
        if a.startswith("map["):
            return _fields_of_value(gf, idx, a, body)
        tm = re.match(rf"^&?(?:{_IDENT}\.)?({_IDENT})\s*\{{", a)
        if tm and tm.group(1) in idx.fs_tags:
            return sorted(set(idx.fs_tags[tm.group(1)].values()))
    return []


def _firestore(gf: _GoFile, idx: _Index, res: GoCloudResult) -> None:
    t = gf.text
    sites: list[tuple[str, list[str], int, list[str]]] = []
    used: set[int] = set()
    for mm in re.finditer(r"\.\s*Collection\s*\(", t):
        if mm.start() in used:
            continue
        colls, end, op, last = _chain(gf, idx, mm.start(), None)
        for sub in re.finditer(r"\.\s*Collection\s*\(", t[mm.start() : end]):
            used.add(mm.start() + sub.start())
        if colls and op:
            sites.append((op, colls, mm.start(), last))
    # references kept in a variable (or a slice element), per function
    spans = [(gf.lines_at[f.start - 1], gf.lines_at[f.end] if f.end < len(gf.lines_at) else len(t)) for f in gf.fns] or [(0, len(t))]
    for a, b in spans:
        body = t[a:b]
        refs: dict[str, list[str]] = {}
        for mm in re.finditer(rf"\b({_IDENT})(?:\s*\[[^\]\n]*\])?\s*(?:,\s*{_IDENT}\s*)?:?=\s*[\w.()\[\]]*?(?=\.\s*Collection\s*\()", body):
            colls, _end, op, _ = _chain(gf, idx, a + mm.end(), None)
            if colls and op is None:
                refs[mm.group(1)] = colls
        for name, colls in refs.items():
            for mm in re.finditer(rf"(?<![\w.]){re.escape(name)}(?:\s*\[[^\]\n]*\])?\s*(?=\.)", body):
                got, _end, op, last = _chain(gf, idx, a + mm.end(), colls)
                if got and op:
                    sites.append((op, got, a + mm.start(), last))
            # client.GetAll(ctx, refs): the references built above (in a loop)
            for mm in re.finditer(rf"\.\s*GetAll\s*\(\s*{_IDENT}\s*,\s*{re.escape(name)}\b", body):
                sites.append(("read", colls, a + mm.start(), []))
            # tx.Get(ref) / tx.Set(ref, …) / batch.Update(refs[i], …)
            for mm in re.finditer(rf"\.\s*(Get|Set|Update|Delete|Create)\s*\(\s*{re.escape(name)}\b", body):
                op_paren = body.index("(", mm.start())
                j = match_close(body, op_paren)
                args = split_top(body[op_paren + 1 : j])
                op = "read" if mm.group(1) == "Get" else "write"
                sites.append((op, colls, a + mm.start(), args[1:]))
    for op, colls, pos, last in sites:
        line = gf.line(pos)
        fn = gf.fn_at(line)
        if fn is None:
            continue
        coll = colls[-1]
        fields = _write_fields(gf, idx, last, gf.body(fn)) if op == "write" else []
        path = "/{}/".join(colls) if len(colls) > 1 else coll
        res.accesses.append(FsAccess(op, coll, path, gf.rel, line, fn.qual, fields))


# ---------------------------------------------------------------- Pub/Sub ----

_DIRECT = re.compile(r"\.\s*(Publisher|Topic)\s*\(\s*([^()]*?)\s*\)\s*\.\s*Publish\s*\(")
_PUB_VAR = re.compile(rf"\b({_IDENT})\s*(?:,\s*{_IDENT}\s*)?:?=\s*[\w.]+\s*\.\s*(Publisher|Topic)\s*\(\s*([^()]*?)\s*\)")
_HEADER = re.compile(rf"\bfunc\s*(?:\(\s*({_IDENT})?\s*\*?\s*({_IDENT})\s*(?:\[[^\]]*\])?\s*\)\s*)?({_IDENT})\s*\(")
_IFACE = re.compile(rf"\btype\s+({_IDENT})\s+interface\s*\{{")
_BUILTIN_TYPES = {"string", "bool", "int", "int64", "int32", "float64", "float32", "byte", "rune", "error", "any", "uint", "uint64"}

# An instance of a publisher wrapper: the map key it was built for (``None``
# when it was not built in a keyed loop) and the topics it publishes to.
_Inst = tuple[str | None, tuple[GoTopic, ...]]


def _publish_args(t: str, open_paren: int) -> list[str]:
    return split_top(t[open_paren + 1 : match_close(t, open_paren)])


def _norm_type(t: str) -> str:
    t = re.sub(r"\s+", "", t.strip()).lstrip("*")
    if t.startswith(("[]", "map[")) or t.startswith("..."):
        return t.replace("context.", "")
    return t.split(".")[-1]


def _params(text: str) -> list[tuple[str, str]]:
    """(name, type) of a Go parameter list; grouped names share the type."""
    parts = [p.strip() for p in split_top(text) if p.strip()]
    out: list[tuple[str, str]] = []
    pending: list[str] = []
    for part in parts:
        m = re.match(rf"^({_IDENT})\s+(.+)$", part, re.S)
        if m:
            for n in pending:
                out.append((n, m.group(2)))
            pending = []
            out.append((m.group(1), m.group(2)))
        elif re.fullmatch(_IDENT, part):
            pending.append(part)
        else:
            out.append(("_", part))  # an unnamed parameter: its type
    for n in pending:
        out.append(("_", n))  # unnamed parameters: types only
    return out


@dataclass
class _FnInfo:
    fn: _Fn
    gf: _GoFile
    body: str
    name: str
    pkg: str  # package directory's last segment
    recv: str | None  # receiver variable
    recv_type: str | None
    params: list[tuple[str, str]]


def _fn_infos(files: dict[str, _GoFile]) -> list[_FnInfo]:
    out: list[_FnInfo] = []
    for gf in files.values():
        pkg = gf.rel.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in gf.rel else ""
        for fn in gf.fns:
            body = gf.body(fn)
            hm = _HEADER.search(body)
            if not hm:
                continue
            open_p = hm.end() - 1
            close = match_close(body, open_p)
            out.append(_FnInfo(fn, gf, body, hm.group(3), pkg, hm.group(1), hm.group(2), _params(body[open_p + 1 : close])))
    return out


class _Flow:
    """Which wrapper instance each variable, struct field and parameter holds.

    A constructor call (``NewPublisher(ctx, p, []string{cfg.OrderTopic})``) is
    an instance; it flows through assignments, ``m[k] = p``, struct literals
    (``&Announcer{pub: pub}``), ``s.pub = pub`` and call arguments into the
    callee's parameter. A ``x.Publish(…)`` call publishes to the instances
    ``x`` holds."""

    def __init__(self, idx: _Index, fns: list[_FnInfo], ctors: dict[str, int], files: dict[str, _GoFile]):
        self.idx = idx
        self.fns = fns
        self.ctors = ctors  # constructor name -> topic parameter index
        self.files = files
        self.vals: dict[tuple, frozenset[_Inst]] = {}
        self.by_name: dict[str, list[_FnInfo]] = {}
        for f in fns:
            self.by_name.setdefault(f.name, []).append(f)
        self.struct_fields: dict[str, list[str]] = {}  # struct -> field names in order
        self.field_types: dict[tuple[str, str], str] = {}
        for gf in files.values():
            for mm in _STRUCT.finditer(gf.text):
                close = match_close(gf.text, mm.end() - 1)
                names: list[str] = []
                for line in re.split(r"[\n;]", gf.text[mm.end() : close]):
                    fm = re.match(rf"\s*({_IDENT}(?:\s*,\s*{_IDENT})*)\s+([^\x00\n]+?)\s*(?:\x00\d+\x00)?\s*$", line)
                    if not fm:
                        em = re.match(rf"\s*\*?(?:{_IDENT}\.)?({_IDENT})\s*$", line)
                        if em:
                            names.append(em.group(1))  # embedded
                            self.field_types[(mm.group(1), em.group(1))] = em.group(1)
                        continue
                    for n in re.split(r"\s*,\s*", fm.group(1)):
                        names.append(n)
                        self.field_types[(mm.group(1), n)] = _norm_type(fm.group(2))
                self.struct_fields[mm.group(1)] = names

    # -- values
    def _get(self, key: tuple) -> frozenset[_Inst]:
        return self.vals.get(key, frozenset())

    def _put(self, key: tuple, insts: frozenset[_Inst]) -> bool:
        if not insts:
            return False
        old = self.vals.get(key, frozenset())
        new = old | insts
        if new != old:
            self.vals[key] = new
            return True
        return False

    def _local_key(self, f: _FnInfo, name: str) -> tuple | None:
        for k, (pn, _) in enumerate(f.params):
            if pn == name:
                return ("param", f.fn.qual, k)
        return ("var", f.fn.qual, name)

    def _built(self, f: _FnInfo, ctor: str, args: list[str]) -> frozenset[_Inst]:
        index = self.ctors[ctor]
        if index >= len(args):
            return frozenset()
        arg = args[index].strip()
        lm = re.match(r"^\[\]string\s*\{(.*)\}$", arg, re.S)
        elems = split_top(lm.group(1)) if lm else [arg]
        fixed: list[GoTopic] = []
        keyed: list[tuple[str, list[GoTopic]]] | None = None
        for el in elems:
            el = el.strip()
            km = re.fullmatch(rf"({_IDENT})\s*\[\s*([^\]]+?)\s*\]", el)
            if km:
                entries = self._map_literal(f, km.group(1))
                if entries is not None:
                    key = km.group(2).split(".")[-1]
                    if key in dict(entries):
                        fixed += dict(entries)[key]
                    else:
                        keyed = entries  # `topics[k]` in a loop: one instance per key
                    continue
            got = self.idx.topics(f.gf, el, f.body)
            if got is None:
                return frozenset()
            fixed += got
        if keyed is not None:
            return frozenset((k, tuple(_uniq(fixed + v))) for k, v in keyed)
        return frozenset({(None, tuple(_uniq(fixed)))}) if fixed else frozenset()

    def _map_literal(self, f: _FnInfo, name: str) -> list[tuple[str, list[GoTopic]]] | None:
        mm = re.search(rf"\b{re.escape(name)}\s*:?=\s*map\[[^\]]*\]\s*\w*\s*\{{", f.body)
        if not mm:
            return None
        close = match_close(f.body, mm.end() - 1)
        out: list[tuple[str, list[GoTopic]]] = []
        for part in split_top(f.body[mm.end() : close]):
            kv = re.match(r"^\s*([^:]+?)\s*:\s*(.+?)\s*$", part, re.S)
            if not kv:
                continue
            got = self.idx.topics(f.gf, kv.group(2), f.body)
            if got:
                key = kv.group(1).strip()
                lit = _lit(f.gf.m, key)
                out.append((lit if lit is not None else key.split(".")[-1], got))
        return out or None

    def value(self, f: _FnInfo, expr: str, depth: int = 0) -> frozenset[_Inst]:
        e = expr.strip().lstrip("&").strip()
        if depth > 4 or not e:
            return frozenset()
        cm = re.match(rf"^(?:{_IDENT}\.)?({_IDENT})\s*\(", e)
        if cm and cm.group(1) in self.ctors and match_close(e, cm.end() - 1) == len(e) - 1:
            return self._built(f, cm.group(1), _publish_args(e, cm.end() - 1))
        im = re.fullmatch(r"(.+?)\s*\[\s*([^\[\]]+?)\s*\]", e)
        if im:
            base = self.value(f, im.group(1), depth + 1)
            key = _lit(f.gf.m, im.group(2)) or im.group(2).split(".")[-1]
            hit = frozenset(i for i in base if i[0] == key)
            return hit or base
        if re.fullmatch(_IDENT, e):
            key = self._local_key(f, e)
            return self._get(key) if key else frozenset()
        fm = re.fullmatch(rf"({_IDENT})\.({_IDENT})", e)
        if fm:
            owner = self._type_of(f, fm.group(1))
            if owner:
                return self._get(("field", owner, fm.group(2)))
        return frozenset()

    def _type_of(self, f: _FnInfo, var: str) -> str | None:
        if var == f.recv:
            return f.recv_type
        for pn, pt in f.params:
            if pn == var:
                return _norm_type(pt)
        vm = re.search(rf"\b(?:var\s+{re.escape(var)}\s+\*?(?:{_IDENT}\.)?({_IDENT})|{re.escape(var)}\s*:?=\s*&?(?:{_IDENT}\.)?({_IDENT})\s*\{{)", f.body)
        if vm:
            return vm.group(1) or vm.group(2)
        return None

    def static_type(self, f: _FnInfo, expr: str) -> str | None:
        e = expr.strip()
        if re.fullmatch(_IDENT, e):
            return self._type_of(f, e)
        fm = re.fullmatch(rf"({_IDENT})\.({_IDENT})", e)
        if fm:
            owner = self._type_of(f, fm.group(1))
            if owner:
                return self.field_types.get((owner, fm.group(2)))
        return None

    def _callees(self, f: _FnInfo, callee: str) -> list[_FnInfo]:
        parts = callee.split(".")
        name = parts[-1]
        cands = self.by_name.get(name, [])
        if len(parts) == 1:
            return [c for c in cands if c.pkg == f.pkg and c.recv_type is None]
        head = ".".join(parts[:-1])
        if len(parts) == 2 and self._type_of(f, parts[0]) is None and self._local_key(f, parts[0])[0] != "param":
            if not re.search(rf"\b{re.escape(parts[0])}\s*:?=", f.body):
                return [c for c in cands if c.pkg == parts[0] and c.recv_type is None]  # pkg.Func
        owner = self.static_type(f, head)
        if owner:
            typed = [c for c in cands if c.recv_type == owner]
            if typed:
                return typed
        methods = [c for c in cands if c.recv_type is not None]
        return methods if len(methods) == 1 else []

    def propagate(self) -> None:
        for _ in range(6):
            changed = False
            for f in self.fns:
                changed |= self._step(f)
            if not changed:
                return

    def _step(self, f: _FnInfo) -> bool:
        body = f.body
        changed = False
        # x := <expr> / x, err := <expr> / x = <expr> / m[k] = <expr> / s.f = <expr>
        for mm in re.finditer(rf"(?:^|[\n;{{]|\bfor\s)\s*((?:{_IDENT}(?:\.{_IDENT})?(?:\[[^\]\n]*\])?)(?:\s*,\s*{_IDENT})*)\s*:?=(?!=)\s*", body):
            lhs = mm.group(1).split(",")[0].strip()
            rhs = _expr_at(body, mm.end())
            insts = self.value(f, rhs)
            if not insts:
                continue
            km = re.fullmatch(rf"({_IDENT})\s*\[\s*([^\]]+?)\s*\]", lhs)
            if km:
                key = _lit(f.gf.m, km.group(2)) or km.group(2).split(".")[-1]
                if all(i[0] is None for i in insts):
                    insts = frozenset((key, i[1]) for i in insts)
                target = self._local_key(f, km.group(1))
            elif "." in lhs:
                v, fld = lhs.split(".", 1)
                owner = self._type_of(f, v)
                target = ("field", owner, fld) if owner else None
            else:
                target = self._local_key(f, lhs)
            if target:
                changed |= self._put(target, insts)
        # struct literals: &T{pub: x} / T{a, b, x}
        for mm in re.finditer(rf"(?<![\w.])(?:{_IDENT}\.)?({_IDENT})\s*\{{", body):
            stype = mm.group(1)
            if stype not in self.struct_fields:
                continue
            if body[: mm.start()].rstrip().rstrip("*").rstrip().endswith(")"):
                continue  # `func New(…) *Service {`: a result type, not a literal
            close = match_close(body, mm.end() - 1)
            parts = split_top(body[mm.end() : close])
            for k, part in enumerate(parts):
                kv = re.match(rf"^\s*({_IDENT})\s*:\s*(.+?)\s*$", part, re.S)
                fname, expr = (kv.group(1), kv.group(2)) if kv else (
                    (self.struct_fields[stype][k], part) if k < len(self.struct_fields[stype]) else (None, None))
                if fname is None or expr is None:
                    continue
                changed |= self._put(("field", stype, fname), self.value(f, expr))
        # call arguments flow into the callee's parameters
        for mm in re.finditer(rf"(?<![\w.])({_IDENT}(?:\.{_IDENT})*)\s*\(", body):
            callee = mm.group(1)
            if callee in ("func", "if", "for", "switch", "return") or mm.start() == 0:
                continue
            args = _publish_args(body, mm.end() - 1)
            vals = [self.value(f, a) for a in args]
            if not any(vals):
                continue
            for c in self._callees(f, callee):
                for k, v in enumerate(vals):
                    if v and k < len(c.params):
                        changed |= self._put(("param", c.fn.qual, k), v)
        return changed


def _uniq(topics: list[GoTopic]) -> list[GoTopic]:
    seen: dict[tuple[str | None, str | None], GoTopic] = {}
    for t in topics:
        seen.setdefault((t.name, t.env), t)
    return list(seen.values())


def _publish_sig(f: _FnInfo) -> tuple[str, ...]:
    return tuple(_norm_type(t) for _, t in f.params)


def _iface_sigs(files: dict[str, _GoFile]) -> dict[str, tuple[str, ...]]:
    """Interfaces declaring ``Publish(…)``: name -> parameter types."""
    out: dict[str, tuple[str, ...]] = {}
    for gf in files.values():
        for mm in _IFACE.finditer(gf.text):
            close = match_close(gf.text, mm.end() - 1)
            body = gf.text[mm.end() : close]
            pm = re.search(r"\bPublish\s*\(", body)
            if pm:
                pclose = match_close(body, pm.end() - 1)
                out[mm.group(1)] = tuple(_norm_type(t) for _, t in _params(body[pm.end() : pclose]))
    return out


def _pubsub(files: dict[str, _GoFile], texts: dict[str, str], idx: _Index, res: GoCloudResult) -> None:
    infos = _fn_infos(files)
    ctors: dict[str, int] = {}  # wrapper constructor -> topic parameter index
    wrappers: dict[str, str] = {}  # wrapper type -> constructor
    wrapper_fns: set[str] = set()
    for f in infos:
        gf, fn, body = f.gf, f.fn, f.body
        if not PS_IMPORT.search(texts[gf.rel]):
            continue
        # direct: client.Publisher(T).Publish(ctx, msg)
        for mm in _DIRECT.finditer(body):
            topics = idx.topics(gf, mm.group(2), body)
            if topics is None:
                continue  # a constructor parameter: a wrapper, below
            args = _publish_args(body, mm.end() - 1)
            fields = _fields_of_data(gf, idx, args[1], body) if len(args) > 1 else []
            line = fn.start + body.count("\n", 0, mm.start())
            res.publishes.append(GoPublish(fn.qual, gf.rel, line, topics, fields))
        # through a variable: p := client.Publisher(T) … p.Publish(ctx, msg)
        for mm in _PUB_VAR.finditer(body):
            var, expr = mm.group(1), mm.group(3)
            topics = idx.topics(gf, expr, body)
            if topics is None:
                continue
            for pm in re.finditer(rf"(?<![\w.]){re.escape(var)}\s*\.\s*Publish\s*\(", body):
                args = _publish_args(body, pm.end() - 1)
                fields = _fields_of_data(gf, idx, args[1], body) if len(args) > 1 else []
                line = fn.start + body.count("\n", 0, pm.start())
                res.publishes.append(GoPublish(fn.qual, gf.rel, line, topics, fields))
        # a constructor: client.Publisher(<param>) / for _, id := range <param> { client.Publisher(id) }
        if f.recv_type is not None:
            continue
        ret = re.match(r"\s*\(?\s*\*?(?:\w+\.)?(\w+)", body[match_close(body, _HEADER.search(body).end() - 1) + 1 :])
        if not ret:
            continue
        params = [n for n, _ in f.params]
        for mm in re.finditer(r"\.\s*(?:Publisher|Topic)\s*\(\s*(" + _IDENT + r")\s*\)", body):
            arg = mm.group(1)
            rm = re.search(rf"for\s+(?:{_IDENT}\s*,\s*)?{re.escape(arg)}\s*:=\s*range\s+({_IDENT})", body)
            src = rm.group(1) if rm else arg
            if src in params:
                ctors[f.name] = params.index(src)
                wrappers[ret.group(1)] = f.name
                wrapper_fns.add(fn.qual)
    if not ctors:
        return
    # the wrapper's own Publish method: the signature a caller must match
    sigs = {f.recv_type: _publish_sig(f) for f in infos if f.name == "Publish" and f.recv_type in wrappers}
    wrapper_sigs = set(sigs.values())
    ifaces = {name for name, sig in _iface_sigs(files).items() if sig in wrapper_sigs}
    flow = _Flow(idx, infos, ctors, files)
    flow.propagate()
    all_insts: set[_Inst] = set()
    for v in flow.vals.values():
        all_insts |= v
    for f in infos:
        if f.fn.qual in wrapper_fns or f.recv_type in wrappers:
            continue
        body = f.body
        for mm in re.finditer(rf"(?<![\w])({_IDENT}(?:\.{_IDENT})*(?:\[[^\]\n]*\])?)\s*\.\s*Publish\s*\(", body):
            recv = mm.group(1)
            if recv.split(".")[-1] in ("Publisher", "Topic"):
                continue
            args = _publish_args(body, mm.end() - 1)
            if len(args) < 2 or re.match(r"^&?(?:\w+\.)?Message\s*\{", args[1].strip()):
                continue
            if not any(len(args) == len(sig) for sig in wrapper_sigs):
                continue  # `store.Publish(ctx, id)`: another Publish
            insts = flow.value(f, recv)
            stype = flow.static_type(f, re.sub(r"\[[^\]]*\]$", "", recv))
            if stype is not None and stype not in ifaces and stype not in wrappers:
                continue  # a receiver of another type
            confidence = "exact"
            if not insts:
                if stype is None or not all_insts:
                    continue  # nothing says it is a publisher
                insts = frozenset(all_insts)  # a publisher interface, instance unknown
                confidence = "inferred"
            line = f.fn.start + body.count("\n", 0, mm.start())
            fields = _fields_of_data(f.gf, idx, args[1], body)
            groups = {i[1] for i in insts}
            topics = _uniq([t for g in sorted(groups, key=lambda g: [x.name or "" for x in g]) for t in g])
            res.publishes.append(
                GoPublish(f.fn.qual, f.gf.rel, line, topics, fields, wrappers and _via(insts, wrappers),
                          candidates=len(groups) > 1, confidence=confidence)
            )


def _via(insts: frozenset[_Inst], wrappers: dict[str, str]) -> str | None:
    return sorted(wrappers)[0] if len(wrappers) == 1 else None


def valid_topic(name: str | None) -> bool:
    return bool(name) and bool(_TOPIC_NAME.match(str(name)))
