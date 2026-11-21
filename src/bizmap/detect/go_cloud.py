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


@dataclass
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
        name = self.env_values.get(env) or default or None
        return GoTopic(name, env)

    def _scan(self, gf: _GoFile) -> None:
        t = gf.text
        for mm in _CONST_LINE.finditer(t):
            v = _lit(gf.m, mm.group(2))
            if v is not None:
                self._put_const(mm.group(1), v)
        for mm in _FIELD_START.finditer(t):
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
                        GoTopic(self.env_values.get(env) or (dm.group(1) if dm else None) or None, env)
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
            v = _lit(gf.m, args[0])
            if v is None and re.fullmatch(_IDENT, args[0].strip()):
                v = idx.consts.get(args[0].strip())
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
    refs: dict[str, list[str]] = {}  # var -> collections
    for mm in re.finditer(rf"\b({_IDENT})\s*(?:,\s*{_IDENT}\s*)?:?=\s*[\w.()\[\]]*?(?=\.\s*Collection\s*\()", t):
        start = mm.end()
        colls, end, op, _ = _chain(gf, idx, start, None)
        if colls and op is None:
            refs[mm.group(1)] = colls
    used: set[int] = set()
    sites: list[tuple[str, list[str], int, list[str]]] = []
    for mm in re.finditer(r"\.\s*Collection\s*\(", t):
        if mm.start() in used:
            continue
        colls, end, op, last = _chain(gf, idx, mm.start(), None)
        for sub in re.finditer(r"\.\s*Collection\s*\(", t[mm.start() : end]):
            used.add(mm.start() + sub.start())
        if colls and op:
            sites.append((op, colls, mm.start(), last))
    for name, colls in refs.items():
        for mm in re.finditer(rf"(?<![\w.]){re.escape(name)}\s*(?=\.)", t):
            got, end, op, last = _chain(gf, idx, mm.end(), colls)
            if got and op:
                sites.append((op, got, mm.start(), last))
        # tx.Get(ref) / tx.Set(ref, …) / batch.Create(ref, …)
        for mm in re.finditer(rf"\.\s*(Get|Set|Update|Delete|Create)\s*\(\s*{re.escape(name)}\b", t):
            j = match_close(t, t.index("(", mm.start()))
            args = split_top(t[t.index("(", mm.start()) + 1 : j])
            op = "read" if mm.group(1) == "Get" else "write"
            sites.append((op, colls, mm.start(), args[1:]))
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


def _publish_args(t: str, open_paren: int) -> list[str]:
    return split_top(t[open_paren + 1 : match_close(t, open_paren)])


def _pubsub(files: dict[str, _GoFile], texts: dict[str, str], idx: _Index, res: GoCloudResult) -> None:
    wrappers: dict[str, tuple[str, int]] = {}  # wrapper type -> (constructor name, topic param index)
    wrapper_fns: set[str] = set()
    for rel, gf in files.items():
        if not PS_IMPORT.search(texts[rel]):
            continue
        for fn in gf.fns:
            body = gf.body(fn)
            # direct: client.Publisher(T).Publish(ctx, msg)
            for mm in _DIRECT.finditer(body):
                topics = idx.topics(gf, mm.group(2), body)
                if topics is None:
                    continue  # a constructor parameter: a wrapper, below
                args = _publish_args(body, mm.end() - 1)
                fields = _fields_of_data(gf, idx, args[1], body) if len(args) > 1 else []
                line = fn.start + body.count("\n", 0, mm.start())
                res.publishes.append(GoPublish(fn.qual, rel, line, topics, fields))
            # through a variable: p := client.Publisher(T) … p.Publish(ctx, msg)
            for mm in _PUB_VAR.finditer(body):
                var, expr = mm.group(1), mm.group(3)
                topics = idx.topics(gf, expr, body)
                for pm in re.finditer(rf"(?<![\w.]){re.escape(var)}\s*\.\s*Publish\s*\(", body):
                    if topics is None:
                        continue
                    args = _publish_args(body, pm.end() - 1)
                    fields = _fields_of_data(gf, idx, args[1], body) if len(args) > 1 else []
                    line = fn.start + body.count("\n", 0, pm.start())
                    res.publishes.append(GoPublish(fn.qual, rel, line, topics, fields))
            # a constructor: client.Publisher(<param>) / for _, id := range <param> { client.Publisher(id) }
            header = body.split("{", 1)[0]
            pm2 = re.search(rf"\bfunc\s+({_IDENT})\s*\((.*?)\)\s*\(?\s*\*?({_IDENT})", header, re.S)
            if not pm2:
                continue
            params = [re.match(rf"\s*({_IDENT})", p).group(1) for p in split_top(pm2.group(2)) if re.match(rf"\s*{_IDENT}", p)]
            for mm in re.finditer(r"\.\s*(?:Publisher|Topic)\s*\(\s*(" + _IDENT + r")\s*\)", body):
                arg = mm.group(1)
                src = arg
                rm = re.search(rf"for\s+(?:{_IDENT}\s*,\s*)?{re.escape(arg)}\s*:=\s*range\s+({_IDENT})", body)
                if rm:
                    src = rm.group(1)
                if src in params:
                    wrappers[pm2.group(3)] = (pm2.group(1), params.index(src))
                    wrapper_fns.add(fn.qual)
    if not wrappers:
        return
    # the wrapper's topics: the constructor's argument where it is built
    topics_of: dict[str, list[GoTopic]] = {}
    for wtype, (ctor, index) in wrappers.items():
        found: list[GoTopic] = []
        for gf in files.values():
            for mm in re.finditer(rf"(?<![\w]){re.escape(ctor)}\s*\(", gf.text):
                before = gf.text[max(0, mm.start() - 6) : mm.start()]
                if re.search(r"func\s*$", before):
                    continue
                args = _publish_args(gf.text, mm.end() - 1)
                if index >= len(args):
                    continue
                fn = gf.fn_at(gf.line(mm.start()))
                got = idx.topics(gf, args[index], gf.body(fn) if fn else "")
                if got:
                    found += got
        uniq: dict[tuple[str | None, str | None], GoTopic] = {}
        for tp in found:
            uniq[(tp.name, tp.env)] = tp
        if uniq:
            topics_of[wtype] = list(uniq.values())
    all_topics: dict[tuple[str | None, str | None], GoTopic] = {}
    for tps in topics_of.values():
        for tp in tps:
            all_topics[(tp.name, tp.env)] = tp
    if not all_topics:
        return
    via = sorted(topics_of)[0] if len(topics_of) == 1 else None
    # callers: x.Publish(ctx, data, …) that is not the gcloud Publisher itself
    for gf in files.values():
        rel = gf.rel
        for fn in gf.fns:
            if fn.qual in wrapper_fns or any(fn.qual.endswith(f"{w}.Publish") for w in wrappers):
                continue
            body = gf.body(fn)
            for mm in re.finditer(rf"(?<![\w])({_IDENT}(?:\.{_IDENT})*)\s*\.\s*Publish\s*\(", body):
                recv = mm.group(1)
                if recv.split(".")[-1] in ("Publisher", "Topic"):
                    continue
                args = _publish_args(body, mm.end() - 1)
                if len(args) < 2 or re.match(r"^&?(?:\w+\.)?Message\s*\{", args[1].strip()):
                    continue
                line = fn.start + body.count("\n", 0, mm.start())
                fields = _fields_of_data(gf, idx, args[1], body)
                res.publishes.append(GoPublish(fn.qual, rel, line, list(all_topics.values()), fields, via))


def valid_topic(name: str | None) -> bool:
    return bool(name) and bool(_TOPIC_NAME.match(str(name)))
