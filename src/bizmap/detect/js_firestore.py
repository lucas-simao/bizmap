"""Firestore reads and writes in JavaScript / TypeScript (a text pass).

Runs after the JS detector, on the files that use Firestore, and attributes
each access to the function the detector found around it (its
``functions`` / ``function_ends``), or to ``<module>.module``:

- web modular SDK: ``getDoc(s)`` / ``onSnapshot`` / ``getCountFromServer``
  (read), ``setDoc`` / ``updateDoc`` / ``addDoc`` / ``deleteDoc`` (write) on
  ``collection(db, …)`` / ``doc(db, …)`` / ``query(…)`` / ``collectionGroup``
  or a reference kept in a variable (``doc(ordersRef, id)``), and
  ``batch.set(ref, …)`` / ``tx.get(ref)``;
- admin / server SDK chains: ``db.collection('x').doc(id).get()`` …
  ``.set / .update / .delete / .create / .add``;
- the collection name from a literal, an exported constant, an enum member
  or a template path (``customers/${id}/notes``); a name passed to a
  helper's parameter (``listAll('customers')``) is put on the caller;
- Cloud Functions Firestore triggers (``onDocumentWritten('orders/{id}', …)``,
  ``functions.firestore.document('orders/{id}').onWrite(…)``) and Pub/Sub
  publishes (``pubsub.topic('x').publishMessage({ json })``).

Subcollections follow the Python detector: the node is the last collection
of the path (``customers/{id}/notes`` → ``collection:notes``), with the full
path in its ``path`` attribute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bizmap.srctext import Masked, mask, match_close, split_top

FS_FILE = re.compile(
    r"""['"](?:firebase/firestore(?:/lite)?|firebase/compat/firestore|firebase-admin(?:/firestore)?|@google-cloud/firestore"""
    r"""|firebase-functions(?:/v[12])?(?:/[a-z]+)?|@google-cloud/pubsub)['"]|\bgetFirestore\s*\(|\.firestore\s*\(\s*\)"""
)
_MOD_READ = {
    "getDoc": "doc", "getDocs": "query", "getDocFromServer": "doc", "getDocsFromServer": "query",
    "getDocFromCache": "doc", "getDocsFromCache": "query", "onSnapshot": "any", "getCountFromServer": "query",
    "getAggregateFromServer": "query",
}
_MOD_WRITE = {"setDoc", "updateDoc", "addDoc", "deleteDoc"}
_MOD_METHOD = {"setDoc": "set", "updateDoc": "update", "addDoc": "add", "deleteDoc": "delete"}
_MOD_CALL = re.compile(r"(?<![\w$.])(" + "|".join(sorted({*_MOD_READ, *_MOD_WRITE}, key=len, reverse=True)) + r")\s*\(")
_CHAIN_READ = {"get", "stream", "onSnapshot", "listDocuments", "count"}
_CHAIN_WRITE = {"set", "update", "delete", "create", "add"}
_CHAIN_PASS = {"where", "orderBy", "limit", "limitToLast", "offset", "startAt", "startAfter", "endAt", "endBefore",
               "withConverter", "select", "doc", "collection", "count"}
_TX_CALL = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\.\s*(set|update|delete|create|get)\s*\(")
_ADMIN_START = re.compile(r"\.\s*collection\s*\(")
# `db.doc('orders/' + id).set(…)` straight on a Firestore instance
_ADMIN_DOC_START = re.compile(r"(?<![\w$.)\]])([A-Za-z_$][\w$]*)\s*\.\s*doc\s*\(")
_FS_INSTANCE_DECL = re.compile(
    r"(?<![\w$.])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=;\n]+)?=\s*(?:await\s+)?"
    r"(?:getFirestore\s*\(|initializeFirestore\s*\(|new\s+Firestore\s*\(|[A-Za-z_$][\w$.]*\.\s*firestore\s*\(\s*\))"
)
_FS_INSTANCE_NAMES = {"db", "firestore", "fs", "fsdb", "firestoreDb", "adminDb", "fdb", "store"}
_ADMIN_REF_ARG = re.compile(r"^[A-Za-z_$][\w$]*\s*\.\s*(?:doc|collection)\s*\(")
_REF_START = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\.\s*(doc|collection|where|orderBy|limit|get|set|update|delete|create|add|onSnapshot|stream)\s*\(")
_DECL = re.compile(r"(?<![\w$.])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=;\n]+)?=\s*(?:await\s+)?")
# bare (`onDocumentWritten(`) or namespaced (`firestore.onDocumentWritten(`, `firestoreV2.…`)
_TRIGGER_V2 = re.compile(r"(?<![\w$])(onDocumentWritten|onDocumentCreated|onDocumentUpdated|onDocumentDeleted"
                         r"|onDocumentWrittenWithAuthContext|onDocumentCreatedWithAuthContext"
                         r"|onDocumentUpdatedWithAuthContext|onDocumentDeletedWithAuthContext)\s*\(")
_TRIGGER_V1 = re.compile(r"\.\s*document\s*\(\s*(\x00\d+\x00)\s*\)\s*\.\s*(onWrite|onCreate|onUpdate|onDelete)\s*\(")
# Cloud Functions entrypoints other than Firestore triggers (v2 bare or
# namespaced, v1 `functions.https.onCall`): the kind each becomes
_FN_KINDS = {
    "onCall": "firebase_call", "onCallGenkit": "firebase_call", "onRequest": "firebase_http",
    "onSchedule": "scheduled_job", "onMessagePublished": "pubsub", "onTaskDispatched": "firebase_task",
    "beforeUserCreated": "auth", "beforeUserSignedIn": "auth",
}
_FN_V2 = re.compile(r"(?<![\w$])(?:[A-Za-z_$][\w$]*\s*\.\s*)*(" + "|".join(_FN_KINDS) + r")\s*\(")
_FN_V1 = re.compile(
    r"\.\s*(schedule|topic)\s*\(\s*(\x00\d+\x00)\s*\)(?:\s*\.\s*[A-Za-z_$][\w$]*\s*\([^()]*\))*\s*\.\s*(onRun|onPublish)\s*\("
)
_AUTH_V1 = re.compile(r"(?<![\w$])auth\s*\.\s*user\s*\(\s*\)\s*\.\s*(onCreate|onDelete)\s*\(")
_V2_EVENT = {"Written": "written", "Created": "created", "Updated": "updated", "Deleted": "deleted"}
_V1_EVENT = {"onWrite": "written", "onCreate": "created", "onUpdate": "updated", "onDelete": "deleted"}
_PUBLISH = re.compile(r"\.\s*topic\s*\(\s*([^()]*?)\s*\)\s*\.\s*(publishMessage|publishJSON|publish)\s*\(")
_NAME_BEFORE = re.compile(r"(?:export\s+const|const|let|var|exports\.|module\.exports\.)\s*([A-Za-z_$][\w$]*)\s*=\s*$")


UNKNOWN = "unknown"  # the collection of an access whose name is only known at runtime


@dataclass
class FsAccess:
    kind: str  # read | write
    collection: str
    path: str
    file: str
    line: int
    symbol: str
    fields: list[str] = field(default_factory=list)
    via: str | None = None  # helper whose parameter named the collection
    method: str | None = None  # the write call: set | update | add | create | delete (None: unknown)


@dataclass
class FsTrigger:
    qual: str
    file: str
    start: int
    end: int
    collection: str
    document: str
    event: str
    id_param: str | None


@dataclass
class FsFunction:
    """A Cloud Function that is not a Firestore trigger (callable, HTTPS,
    schedule, Pub/Sub, auth). ``name`` is its export name in the file."""

    qual: str
    name: str
    file: str
    start: int
    end: int
    kind: str  # firebase_call | firebase_http | scheduled_job | pubsub | firebase_task | auth
    schedule: str | None = None
    topic: str | None = None
    event: str | None = None


@dataclass
class FsPublish:
    symbol: str
    topic: str
    fields: list[str]
    file: str
    line: int


@dataclass
class FsResult:
    accesses: list[FsAccess] = field(default_factory=list)
    triggers: list[FsTrigger] = field(default_factory=list)
    functions: list[FsFunction] = field(default_factory=list)
    publishes: list[FsPublish] = field(default_factory=list)


@dataclass
class _Param:
    index: int


@dataclass
class _Target:
    segments: list[object]  # str (literal / "{}" for an id) | _Param
    is_doc: bool


@dataclass
class _Fn:
    qual: str
    start: int
    end: int
    params: list[str]


def _module(rel: str) -> str:
    return rel.replace("/", ".").rsplit(".", 1)[0]


_RAW_CONST = re.compile(
    r"""(?:^|\n)[ \t]*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::\s*[\w.<>|\[\] ]+)?=\s*(['"`])([^'"`\n\\$]*)\2"""
)
_RAW_ENUM = re.compile(r"(?:^|\n)[ \t]*(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)\s*\{([^{}]*)\}")
_RAW_OBJ = re.compile(r"(?:^|\n)[ \t]*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*\{([^{}]*)\}")
_RAW_MEMBER = re.compile(r"""([A-Za-z_$][\w$]*)\s*[:=]\s*(['"])([^'"\n]*)\2""")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def collect_constants(texts: dict[str, str], wanted: set[str] | None = None) -> dict[str, str]:
    """``export const ORDERS = 'orders'``, ``enum C { A = 'a' }`` and
    ``const C = { A: 'a' }`` across the repo, for the names in ``wanted``
    (a name defined twice with different values is dropped)."""
    out: dict[str, str] = {}
    bad: set[str] = set()

    def put(name: str, value: str) -> None:
        if name in out and out[name] != value:
            bad.add(name)
        out[name] = value

    for text in texts.values():
        if "const" not in text and "enum" not in text:
            continue
        for mm in _RAW_CONST.finditer(text):
            if wanted is None or mm.group(1) in wanted:
                put(mm.group(1), mm.group(3))
        for rx in (_RAW_ENUM, _RAW_OBJ):
            for mm in rx.finditer(text):
                if wanted is not None and mm.group(1) not in wanted:
                    continue
                for em in _RAW_MEMBER.finditer(mm.group(2)):
                    put(f"{mm.group(1)}.{em.group(1)}", em.group(3))
    for name in bad:
        out.pop(name, None)
    return out


def _params_of(header: str) -> list[str]:
    """Parameter names of a JS function header (``function f(a, b)`` /
    ``const f = async (a: T, b) =>`` / ``f(a) {``)."""
    m = re.search(r"\(", header)
    if not m:
        am = re.search(r"([A-Za-z_$][\w$]*)\s*=>", header)
        return [am.group(1)] if am else []
    j = match_close(header, m.start())
    names = []
    for part in split_top(header[m.start() + 1 : j]):
        pm = re.match(r"^(?:\.\.\.)?([A-Za-z_$][\w$]*)", part.strip())
        names.append(pm.group(1) if pm else "_")
    return names


class _FileScan:
    def __init__(self, rel: str, text: str, fns: list[_Fn], consts: dict[str, str]):
        self.rel = rel
        self.m: Masked = mask(text, "javascript")
        self.text = self.m.text
        self.fns = sorted(fns, key=lambda f: (f.start, -f.end))
        # this file's own constants first (`const COLLECTION = 'order_events'`, not exported)
        self.consts = {**consts, **collect_constants({rel: text})}
        self.module = _module(rel)
        self.refs: list[tuple[int, str, _Target]] = []  # (pos, name, target)
        self.used: set[int] = set()  # positions of `.collection(` already in a chain
        self.methods: dict[int, str | None] = {}  # write site position -> set | update | add | create | delete
        self.last_method: str | None = None
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

    # --- values
    def _piece(self, expr: str) -> str:
        """One piece of a path: a literal (templates with ``${CONST}`` filled in),
        a string constant, or ``{}`` for a value only known at runtime."""
        e = expr.strip()
        while e.startswith("(") and match_close(e, 0) == len(e) - 1:
            e = e[1:-1].strip()
        lit = self.m.lit(e)
        if lit is not None:
            if not lit.dynamic:
                return lit.value

            def fill(m: re.Match[str]) -> str:
                name = m.group(1).strip()
                val = self.consts.get(name)
                return val if isinstance(val, str) and "{" not in val else "{}"

            return re.sub(r"\$\{([^}]*)\}", fill, lit.value)
        if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?", e) and e in self.consts:
            return self.consts[e]
        return "{}"

    def segments_of(self, expr: str, fn: _Fn | None) -> list[object] | None:
        """Path segments of one argument of collection() / doc():
        `'orders'`, `` `${ORDERS}/${id}` ``, `'notes/' + id`, `ORDERS + '/' + id`."""
        e = expr.strip()
        lit = self.m.lit(e)
        if lit is not None:
            value = self._piece(e)
            return [s for s in value.strip("/").split("/") if s] or None
        parts = _split_plus(e)
        if len(parts) > 1:
            value = "".join(self._piece(p) for p in parts)
            # `'orders' + suffix`: the name itself is built → `orders{}` (unknown)
            return [s for s in value.strip("/").split("/") if s] or None
        if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?", e):
            if e in self.consts:
                return [s for s in self.consts[e].strip("/").split("/") if s]
            if fn is not None and e in fn.params:
                return [_Param(fn.params.index(e))]
            return ["{}"]  # an id held in a variable
        return ["{}"]

    def ref(self, name: str, pos: int) -> _Target | None:
        best = None
        for p, n, t in self.refs:
            if n == name and p < pos:
                best = t
        return best

    def target(self, expr: str, pos: int, fn: _Fn | None) -> _Target | None:
        e = expr.strip()
        e = re.sub(r"\.withConverter\s*\([^()]*\)\s*$", "", e)
        if re.fullmatch(r"[A-Za-z_$][\w$]*", e):
            return self.ref(e, pos)
        cm = re.match(r"^(collection|doc|query|collectionGroup)\s*\(", e)
        if cm and match_close(e, cm.end() - 1) == len(e) - 1:
            args = split_top(e[cm.end() : -1])
            kind = cm.group(1)
            if not args:
                return None
            if kind == "query":
                return self.target(args[0], pos, fn)
            if kind == "collectionGroup":
                segs = self.segments_of(args[1], fn) if len(args) > 1 else None
                return _Target(segs, False) if segs else None
            base = self.target(args[0], pos, fn) if re.fullmatch(r"[A-Za-z_$][\w$]*", args[0].strip()) else None
            if base is None and args[0].strip().startswith(("collection(", "doc(")):
                base = self.target(args[0], pos, fn)
            segs: list[object] = list(base.segments) if base is not None else []
            for a in args[1:]:
                s = self.segments_of(a, fn)
                if s is None:
                    return None
                segs += s
            if base is None and not segs:
                return None
            if kind == "doc" and len(args) == 1 and base is not None:
                segs.append("{}")  # doc(colRef): a new id
            return _Target(segs, kind == "doc")
        # admin chain held in a variable: db.collection('x').doc(id)
        if ".collection(" in e.replace(" ", "") or re.match(r"^[A-Za-z_$][\w$]*\s*\.\s*doc\s*\(", e):
            t, _end, _op, _args = self.chain(e, 0, fn, pos)
            return t
        return None

    def chain(self, e: str, start: int, fn: _Fn | None, pos: int) -> tuple[_Target | None, int, str | None, list[str]]:
        """Follow ``.collection(…).doc(…)…`` from ``start`` (a ``.`` or a ref name)."""
        segs: list[object] = []
        is_doc = False
        i = start
        n = len(e)
        rm = re.match(r"([A-Za-z_$][\w$]*)\s*(?=\.)", e[i:])
        if rm and not e[i:].lstrip().startswith("."):
            base = self.ref(rm.group(1), pos)
            if base is not None:
                segs = list(base.segments)
                is_doc = base.is_doc
            i += rm.end()
        op = None
        last_args: list[str] = []
        while i < n:
            mm = re.compile(r"\s*(?:\?\.|\.)\s*([A-Za-z_$][\w$]*)\s*").match(e, i)
            if not mm:
                break
            name = mm.group(1)
            j = mm.end()
            args: list[str] = []
            if j < n and e[j] == "(":
                k = match_close(e, j)
                args = split_top(e[j + 1 : k])
                j = k + 1
            elif name not in ("docs", "size", "empty", "exists"):
                break
            if name == "collection" and args:
                s = self.segments_of(args[0], fn)
                if s is None:
                    return None, j, None, []
                segs += s
                is_doc = False
            elif name == "doc":
                segs += (self.segments_of(args[0], fn) or ["{}"]) if args else ["{}"]
                is_doc = True
            elif name in _CHAIN_READ:
                op = "read"
                i = j
                break
            elif name in _CHAIN_WRITE:
                op = "write"
                last_args = args
                self.last_method = name
                i = j
                break
            elif name not in _CHAIN_PASS:
                break
            i = j
        if not segs:
            return None, i, op, last_args
        return _Target(segs, is_doc), i, op, last_args


def _split_plus(e: str) -> list[str]:
    """``a + b + c`` at the top level (not inside calls / brackets)."""
    parts, depth, last = [], 0, 0
    for k, c in enumerate(e):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "+" and depth == 0 and e[k + 1 : k + 2] not in ("+", "=") and e[k - 1 : k] != "+":
            parts.append(e[last:k])
            last = k + 1
    parts.append(e[last:])
    return [p for p in (x.strip() for x in parts) if p] if len(parts) > 1 else [e]


def _collection(t: _Target) -> tuple[str | _Param, str] | None:
    """(collection name or the helper parameter, path) of a target."""
    segs = list(t.segments)
    if t.is_doc and len(segs) % 2 == 0:
        segs = segs[:-1]  # doc(db, 'settings', 'pricing'): the collection is `settings`
    if not segs:
        return None
    if len(segs) % 2 == 0:  # an odd collection path is malformed: ignore
        return None
    last = segs[-1]
    # a parameter in an id position is just an id; in the last position it names the collection
    path = "/".join(s if isinstance(s, str) else ("{param}" if k == len(segs) - 1 else "{}") for k, s in enumerate(segs))
    if isinstance(last, _Param):
        return last, path
    if not isinstance(last, str) or last in ("{}",) or "{" in last:
        # `collection(db, name)` with a name built at runtime: counted, not named
        return UNKNOWN, path
    return last, path


def _fields(m: Masked, expr: str | None) -> list[str]:
    if not expr:
        return []
    e = expr.strip()
    if not (e.startswith("{") and match_close(e, 0) == len(e) - 1):
        return []
    out = []
    for part in split_top(e[1:-1]):
        part = part.strip()
        km = re.match(r"^(\x00\d+\x00|[A-Za-z_$][\w$]*)\s*(?::|$)", part)
        if not km or part.startswith("..."):
            continue
        key = km.group(1)
        lit = m.lit(key)
        out.append(lit.value if lit is not None else key)
    return sorted(set(out))


def _callee_args(text: str, name: str, line_text_pos: int) -> list[str] | None:
    m = re.compile(rf"(?<![\w$]){re.escape(name)}\s*\(").search(text, line_text_pos)
    if not m:
        return None
    j = match_close(text, m.end() - 1)
    return split_top(text[m.end() : j])


def scan_files(
    texts: dict[str, str],
    functions: dict[str, tuple[str, int]],
    function_ends: dict[str, int],
    calls: list[tuple[str, str, str, int]] | None = None,
    consts: dict[str, str] | None = None,
) -> FsResult:
    """Firestore accesses in the files that use it.

    ``functions`` / ``function_ends`` are the JS detector's (qual → (file,
    line) / end line); ``calls`` its (caller, callee, file, line)."""
    res = FsResult()
    fs_files = {rel: t for rel, t in texts.items() if FS_FILE.search(t)}
    if not fs_files:
        return res
    if consts is None:
        wanted = {w for t in fs_files.values() for w in _IDENT.findall(t)}
        consts = collect_constants(texts, wanted)
    by_file: dict[str, list[_Fn]] = {}
    for qual, (file, line) in functions.items():
        if file in fs_files:
            end = function_ends.get(qual) or line
            header = "\n".join(fs_files[file].split("\n")[line - 1 : min(line + 3, end)])
            hm = re.search(r"\)\s*(?::\s*[^={]+)?(?:=>|\{)", header)
            by_file.setdefault(file, []).append(_Fn(qual, line, end, _params_of(header[: hm.end()] if hm else header)))
    pending: list[tuple[str, int, str, str, int, list[str], str, str | None]] = []  # helper, param, kind, file, line, fields, path, method
    scans: dict[str, _FileScan] = {}
    for rel in sorted(fs_files):
        fs = _FileScan(rel, fs_files[rel], by_file.get(rel, []), consts)
        scans[rel] = fs
        _triggers(fs, res)
        _functions(fs, res)
        _collect_refs(fs)
        for kind, target, pos, fields in _sites(fs):
            method = fs.methods.get(pos)
            line = fs.line(pos)
            fn = fs.fn_at(line)
            got = _collection(target)
            if got is None:
                continue
            coll, path = got
            sym = fn.qual if fn is not None else f"{fs.module}.module"
            if isinstance(coll, _Param):
                if fn is not None:
                    pending.append((fn.qual, coll.index, kind, rel, line, fields, path, method))
                continue
            res.accesses.append(FsAccess(kind, coll, path, rel, line, sym, fields, method=method))
        _publishes(fs, res)
    # a collection named by the caller of a helper: `listAll('customers')`
    for helper, index, kind, rel, hline, fields, _hpath, method in pending:
        short = helper.split(".")[-1]
        sites: list[tuple[str, str, int]] = []
        for caller, callee, cfile, cline in calls or []:
            if callee == helper and cfile in texts:
                sites.append((caller, cfile, cline))
        if not sites:  # same file, by name
            fs = scans[rel]
            for mm in re.finditer(rf"(?<![\w$.]){re.escape(short)}\s*\(", fs.text):
                cline = fs.line(mm.start())
                caller_fn = fs.fn_at(cline)
                if caller_fn is not None and caller_fn.qual == helper:
                    continue
                sites.append((caller_fn.qual if caller_fn else f"{fs.module}.module", rel, cline))
        if not sites:
            # nobody in the scan names it (`export function saveTo(name, …)`):
            # the helper writes to a collection only known at runtime
            res.accesses.append(FsAccess(kind, UNKNOWN, UNKNOWN, rel, hline, helper, fields, method=method))
            continue
        for caller, cfile, cline in sites:
            cscan = scans.get(cfile) or _FileScan(cfile, texts[cfile], [], consts)
            scans[cfile] = cscan
            pos = cscan.lines_at[cline - 1] if cline - 1 < len(cscan.lines_at) else 0
            args = _callee_args(cscan.text, short, pos)
            if not args or index >= len(args):
                continue
            segs = cscan.segments_of(args[index], None)
            if not segs or not all(isinstance(s, str) for s in segs):
                continue
            got = _collection(_Target(segs, False))
            if got is None or isinstance(got[0], _Param):
                continue
            res.accesses.append(FsAccess(kind, got[0], got[1], cfile, cline, caller, fields, via=helper, method=method))
    return res


def _collect_refs(fs: _FileScan) -> None:
    for mm in _DECL.finditer(fs.text):
        name = mm.group(1)
        start = mm.end()
        # the initialiser up to the end of the statement
        depth = 0
        k = start
        while k < len(fs.text):
            c = fs.text[k]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    break
            elif c in ";\n" and depth == 0:
                break
            k += 1
        expr = fs.text[start:k].strip()
        if not re.match(r"^(?:collection|doc|query|collectionGroup)\s*\(|^[A-Za-z_$][\w$.()]*\.\s*(?:collection|doc)\s*\(", expr):
            continue
        tail = re.search(r"\.\s*(?:get|set|update|delete|add|create|onSnapshot|stream)\s*\([^()]*\)\s*$", expr)
        if tail:
            continue  # an access, not a reference
        fn = fs.fn_at(fs.line(mm.start()))
        t = fs.target(expr, mm.start(), fn)
        if t is not None:
            fs.refs.append((mm.start(), name, t))


def _sites(fs: _FileScan) -> list[tuple[str, _Target, int, list[str]]]:
    out: list[tuple[str, _Target, int, list[str]]] = []
    text = fs.text
    # modular SDK
    for mm in _MOD_CALL.finditer(text):
        fn_name = mm.group(1)
        j = match_close(text, mm.end() - 1)
        args = split_top(text[mm.end() : j])
        if not args:
            continue
        fn = fs.fn_at(fs.line(mm.start()))
        t = fs.target(args[0], mm.start(), fn)
        if t is None:
            continue
        if fn_name in _MOD_WRITE:
            if fn_name == "addDoc":
                t = _Target(list(t.segments), False)
            fs.methods[mm.start()] = _MOD_METHOD.get(fn_name)
            out.append(("write", t, mm.start(), _fields(fs.m, args[1] if len(args) > 1 else None)))
        else:
            out.append(("read", t, mm.start(), []))
    # batch / transaction: batch.set(ref, data), tx.get(ref)
    for mm in _TX_CALL.finditer(text):
        recv, method = mm.group(1), mm.group(2)
        j = match_close(text, mm.end() - 1)
        args = split_top(text[mm.end() : j])
        if not args or fs.ref(recv, mm.start()) is not None:
            continue
        a0 = args[0].strip()
        if not (
            a0.startswith(("doc(", "collection("))
            or (re.fullmatch(r"[A-Za-z_$][\w$]*", a0) and fs.ref(a0, mm.start()))
            # admin SDK ref built in place: batch.update(db.doc(`orders/${id}`), …),
            # tx.set(db.collection('orders').doc(id), …)
            or _ADMIN_REF_ARG.match(a0)
        ):
            continue
        fn = fs.fn_at(fs.line(mm.start()))
        t = fs.target(a0, mm.start(), fn)
        if t is None:
            continue
        kind = "read" if method == "get" else "write"
        if kind == "write":
            fs.methods[mm.start()] = method
        out.append((kind, t, mm.start(), _fields(fs.m, args[1] if kind == "write" and len(args) > 1 else None)))
    # straight on the instance: db.doc(`orders/${id}`).update(…)
    instances = set(_FS_INSTANCE_DECL.findall(text)) | _FS_INSTANCE_NAMES
    for mm in _ADMIN_DOC_START.finditer(text):
        recv = mm.group(1)
        if recv not in instances or fs.ref(recv, mm.start()) is not None:
            continue
        fn = fs.fn_at(fs.line(mm.start()))
        t, end, op, last_args = fs.chain(text, mm.start(), fn, mm.start())
        for sub in _ADMIN_START.finditer(text, mm.start(), end):
            fs.used.add(sub.start())  # db.doc(x).collection('y'): this chain, not a second one
        if t is None or op is None:
            continue  # an in-place ref (batch.update(db.doc(…), …)) is handled above
        if op == "write":
            fs.methods[mm.start()] = fs.last_method
        out.append((op, t, mm.start(), _fields(fs.m, last_args[0] if last_args else None) if op == "write" else []))
    # admin chains: x.collection('a').doc(id).get()
    for mm in _ADMIN_START.finditer(text):
        if mm.start() in fs.used:
            continue
        fn = fs.fn_at(fs.line(mm.start()))
        t, end, op, last_args = fs.chain(text, mm.start(), fn, mm.start())
        for sub in _ADMIN_START.finditer(text, mm.start(), end):
            fs.used.add(sub.start())
        if t is None or op is None:
            continue
        if op == "write":
            fs.methods[mm.start()] = fs.last_method
        out.append((op, t, mm.start(), _fields(fs.m, last_args[0] if last_args else None) if op == "write" else []))
    # chains from a reference variable: ordersRef.doc(id).get()
    for mm in _REF_START.finditer(text):
        ref = fs.ref(mm.group(1), mm.start())
        if ref is None:
            continue
        fn = fs.fn_at(fs.line(mm.start()))
        t, end, op, last_args = fs.chain(text, mm.start(), fn, mm.start())
        for sub in _ADMIN_START.finditer(text, mm.start(), end):
            fs.used.add(sub.start())
        if t is None or op is None:
            continue
        if op == "write":
            fs.methods[mm.start()] = fs.last_method
        out.append((op, t, mm.start(), _fields(fs.m, last_args[0] if last_args else None) if op == "write" else []))
    return out


def _handler_span(fs: _FileScan, call_open: int) -> tuple[int, int] | None:
    """Lines of the last (callback) argument of the call opened at ``call_open``."""
    text = fs.text
    close = match_close(text, call_open)
    depth = 0
    last = call_open + 1
    for k in range(call_open + 1, close):
        c = text[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            last = k + 1
    arg = text[last:close]
    am = re.search(r"=>|\bfunction\b", arg)
    if am is None:
        return None
    b = arg.find("{", am.end())
    if b < 0:
        return None
    end = match_close(arg, b)
    return fs.line(last), fs.line(last + end)


def _export_name(fs: _FileScan, pos: int) -> str | None:
    """The name a factory call at ``pos`` is exported as (`exports.x =`,
    `export const x =`), walking back over a chain (`functions.https.onCall`)."""
    k = pos
    while k > 0:
        nm = _NAME_BEFORE.search(fs.text[max(0, k - 200) : k])
        if nm is not None:
            return nm.group(1)
        # `exports.x = functions.firestore.document(...)`: walk back over the chain
        prev = fs.text[max(0, k - 200) : k]
        dot = re.search(r"([A-Za-z_$][\w$.]*(?:\s*\.\s*)?)\s*$", prev)
        if not dot or dot.start() == 0 or not dot.group(1).strip():
            return None
        k = max(0, k - 200) + dot.start()
    return None


def _option(fs: _FileScan, expr: str, key: str) -> str | None:
    """A string option: the argument itself (`'every 5 minutes'`, a constant)
    or `{ key: '…' }`."""
    e = expr.strip()
    lit = fs.m.lit(e)
    if lit is not None:
        return lit.value if not lit.dynamic else None
    if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?", e):
        return fs.consts.get(e)
    om = re.search(rf"(?<![\w$]){key}\s*:\s*(\x00\d+\x00|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)", e)
    if om:
        lit = fs.m.lit(om.group(1))
        return (lit.value if not lit.dynamic else None) if lit is not None else fs.consts.get(om.group(1))
    return None


def _functions(fs: _FileScan, res: FsResult) -> None:
    """Callable / HTTPS / scheduled / Pub/Sub / auth Cloud Functions."""
    found: list[tuple[int, int, str, str | None, str | None, str | None]] = []  # pos, open, kind, schedule, topic, event
    for mm in _FN_V2.finditer(fs.text):
        factory = mm.group(1)
        kind = _FN_KINDS[factory]
        open_p = mm.end() - 1
        args = split_top(fs.text[open_p + 1 : match_close(fs.text, open_p)])
        if not args:
            continue
        schedule = _option(fs, args[0], "schedule") if kind == "scheduled_job" and len(args) > 1 else None
        topic = _option(fs, args[0], "topic") if kind == "pubsub" and len(args) > 1 else None
        event = {"beforeUserCreated": "before_create", "beforeUserSignedIn": "before_sign_in"}.get(factory)
        found.append((mm.start(), open_p, kind, schedule, topic, event))
    for mm in _FN_V1.finditer(fs.text):
        lit = fs.m.lit(mm.group(2))
        value = lit.value if lit is not None and not lit.dynamic else None
        if mm.group(1) == "schedule" and mm.group(3) == "onRun":
            found.append((mm.start(), mm.end() - 1, "scheduled_job", value, None, None))
        elif mm.group(1) == "topic" and mm.group(3) == "onPublish":
            found.append((mm.start(), mm.end() - 1, "pubsub", None, value, None))
    for mm in _AUTH_V1.finditer(fs.text):
        found.append((mm.start(), mm.end() - 1, "auth", None, None, "created" if mm.group(1) == "onCreate" else "deleted"))
    for pos, open_p, kind, schedule, topic, event in found:
        span = _handler_span(fs, open_p)
        if span is None:
            continue
        name = _export_name(fs, pos)
        if not name:
            continue  # not exported: not a deployed function
        stmt_start = fs.text.rfind("\n", 0, pos) + 1
        start = fs.line(stmt_start)
        qual = f"{fs.module}.{name}"
        res.functions.append(FsFunction(qual, name, fs.rel, start, span[1], kind, schedule, topic, event))
        fs.fns.append(_Fn(qual, start, span[1], []))


def _doc_path(fs: _FileScan, lit) -> str | None:  # noqa: ANN001 - srctext literal
    """A trigger's document path; `${CONST}` in a template is the constant's
    value (`${COLLECTION}/{id}`), any other `${…}` stays unresolved."""
    if not lit.dynamic:
        return lit.value
    out = []
    for part in re.split(r"(\$\{[^}]*\})", lit.value):
        if part.startswith("${"):
            name = part[2:-1].strip()
            if name not in fs.consts:
                return None
            out.append(fs.consts[name])
        else:
            out.append(part)
    return "".join(out)


def _triggers(fs: _FileScan, res: FsResult) -> None:
    found: list[tuple[int, int, str, str]] = []  # (call pos, open paren, document path, event)
    for mm in _TRIGGER_V2.finditer(fs.text):
        open_p = mm.end() - 1
        args = split_top(fs.text[open_p + 1 : match_close(fs.text, open_p)])
        if not args:
            continue
        doc = None
        lit = fs.m.lit(args[0])
        if lit is not None:
            doc = _doc_path(fs, lit)
        else:
            dm = re.search(r"document\s*:\s*(\x00\d+\x00|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)", args[0])
            if dm:
                lit2 = fs.m.lit(dm.group(1))
                doc = _doc_path(fs, lit2) if lit2 is not None else fs.consts.get(dm.group(1))
        if not doc:
            continue
        ev = next((v for k, v in _V2_EVENT.items() if k in mm.group(1)), "written")
        found.append((mm.start(), open_p, doc, ev))
    for mm in _TRIGGER_V1.finditer(fs.text):
        lit = fs.m.lit(mm.group(1))
        if lit is None:
            continue
        found.append((mm.start(), mm.end() - 1, lit.value, _V1_EVENT[mm.group(2)]))
    for pos, open_p, doc, event in found:
        span = _handler_span(fs, open_p)
        if span is None:
            continue
        stmt_start = fs.text.rfind("\n", 0, pos) + 1
        name = _export_name(fs, pos) or f"{event}_{fs.line(pos)}"
        parts = [p for p in doc.strip("/").split("/") if p]
        coll = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")
        if not coll or "{" in coll:
            continue
        idm = re.fullmatch(r"\{(\w+)\}", parts[-1]) if parts else None
        start = fs.line(stmt_start)
        res.triggers.append(
            FsTrigger(
                qual=f"{fs.module}.{name}", file=fs.rel, start=start, end=span[1], collection=coll.lower(),
                document=doc, event=event, id_param=idm.group(1) if idm else None,
            )
        )
        fs.fns.append(_Fn(f"{fs.module}.{name}", start, span[1], []))


def _publishes(fs: _FileScan, res: FsResult) -> None:
    for mm in _PUBLISH.finditer(fs.text):
        segs = fs.segments_of(mm.group(1), None) if mm.group(1).strip() else None
        if not segs or len(segs) != 1 or not isinstance(segs[0], str) or segs[0] == "{}":
            continue
        topic = segs[0]
        j = match_close(fs.text, mm.end() - 1)
        args = split_top(fs.text[mm.end() : j])
        fields: list[str] = []
        if args:
            a0 = args[0].strip()
            if mm.group(2) == "publishMessage" and a0.startswith("{"):
                jm = re.search(r"(?:^|[{,\s])json\s*:\s*(\{)", a0)
                if jm:
                    fields = _fields(fs.m, a0[jm.start(1) : match_close(a0, jm.start(1)) + 1])
            elif mm.group(2) == "publishJSON":
                fields = _fields(fs.m, a0)
        line = fs.line(mm.start())
        fn = fs.fn_at(line)
        res.publishes.append(FsPublish(fn.qual if fn else f"{fs.module}.module", topic, fields, fs.rel, line))


