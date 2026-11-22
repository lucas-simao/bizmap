"""Routes mounted through a registration helper in Go (a text pass).

Services often register Cloud Scheduler jobs and Eventarc / Pub/Sub push
consumers through a small helper whose path comes from a method of an
interface argument::

    type ScheduledJob interface { Name() string; Run(ctx context.Context) error }

    func RegisterScheduled(e *echo.Echo, auth echo.MiddlewareFunc, job ScheduledJob) {
        e.POST(JobsPrefix+job.Name(), handler(job), auth)
    }

    // routes.go
    entrypoint.RegisterScheduled(e, oidc, jobs.NewReleaseNotes(gen))

    // jobs/release_notes.go
    func (c *ReleaseNotes) Name() string { return "release-notes" }

The route detector sees one route, ``POST /internal/jobs/{job.Name()}`` (or
none, for ``e.POST(h.Path(), …)``). This pass finds such helpers, resolves
each call site's argument to its concrete type (``pkg.NewX(…)``,
``&pkg.X{…}``, or a variable assigned from one), reads the literal or
constant its ``Name()`` / ``Path()`` method returns, and emits one route per
call site, triggering the type's other interface methods (``Run``,
``Handle``, …).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from bizmap.srctext import Masked, mask, match_close, split_top

_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "Any", "Handle")
_FUNC = re.compile(r"(?m)^func\s+([A-Za-z_]\w*)\s*\(")
_ROUTE = re.compile(r"(?<![\w.])[A-Za-z_]\w*\s*\.\s*(" + "|".join(_VERBS) + r")\s*\(")
_IFACE = re.compile(r"(?m)^type\s+([A-Za-z_]\w*)\s+interface\s*\{")
_CONST_ONE = re.compile(r"(?m)^\s*(?:const\s+)?([A-Za-z_]\w*)\s*(?:[A-Za-z_][\w.]*\s*)?=\s*(\x00\d+\x00)\s*$")
_IMPORT_LINE = re.compile(r"(?m)^\s*(?:([A-Za-z_]\w*|\.)\s+)?(\x00\d+\x00)\s*$")


@dataclass
class Helper:
    pkg: str  # package dir
    name: str
    param_index: int
    param: str
    iface: str
    method: str  # the path method on the interface (`Name`, `Path`)
    verb: str
    prefix: str  # literal/constant part of the path before the method value
    suffix: str
    handlers: list[str] = field(default_factory=list)  # other interface methods (`Run`, `Handle`)


@dataclass
class HelperRoute:
    verb: str
    path: str
    file: str
    line: int
    helper: str
    type_pkg: str
    type_name: str
    handlers: list[str]


class _Pkgs:
    def __init__(self, texts: dict[str, str], module: str | None):
        self.texts = {k: v for k, v in texts.items() if k.endswith(".go")}
        self.module = module
        self.masked: dict[str, Masked] = {}
        self.by_dir: dict[str, list[str]] = {}
        for rel in self.texts:
            self.by_dir.setdefault(posixpath.dirname(rel), []).append(rel)
        self._consts: dict[str, dict[str, str]] = {}

    def m(self, rel: str) -> Masked:
        if rel not in self.masked:
            self.masked[rel] = mask(self.texts[rel], "go")
        return self.masked[rel]

    def lit(self, rel: str, token: str) -> str | None:
        lit = self.m(rel).lit(token.strip())
        return lit.value if lit is not None and not lit.dynamic else None

    def consts(self, pkg: str) -> dict[str, str]:
        if pkg not in self._consts:
            out: dict[str, str] = {}
            for rel in self.by_dir.get(pkg, []):
                for mm in _CONST_ONE.finditer(self.m(rel).text):
                    value = self.lit(rel, mm.group(2))
                    if value is not None:
                        out.setdefault(mm.group(1), value)
            self._consts[pkg] = out
        return self._consts[pkg]

    def imports(self, rel: str) -> dict[str, str]:
        """alias → package dir (inside this module only)."""
        text = self.m(rel).text
        out: dict[str, str] = {}
        if not self.module:
            return out
        spans = [mm.span() for mm in re.finditer(r"(?s)\bimport\s*\((.*?)\)", text)]
        bodies = [text[a:b] for a, b in spans] + re.findall(r"(?m)^import\s+(.*)$", text)
        for body in bodies:
            for line in body.splitlines():
                line = line.replace("import", "").replace("(", "").replace(")", "").strip()
                mm = _IMPORT_LINE.match(line)
                if not mm:
                    continue
                path = self.lit(rel, mm.group(2))
                if not path or not (path == self.module or path.startswith(self.module + "/")):
                    continue
                pkg = path[len(self.module) :].lstrip("/")
                alias = mm.group(1) or pkg.rsplit("/", 1)[-1]
                out[alias] = pkg
        return out

    def value(self, pkg: str, rel: str, expr: str) -> str | None:
        """A string expression: literal, constant, `pkg.Const`, `a + b`."""
        parts = [p.strip() for p in expr.split("+")]
        out = []
        for p in parts:
            v = self.lit(rel, p)
            if v is None and re.fullmatch(r"[A-Za-z_]\w*", p):
                v = self.consts(pkg).get(p)
            if v is None:
                qm = re.fullmatch(r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)", p)
                if qm and qm.group(1) in self.imports(rel):
                    v = self.consts(self.imports(rel)[qm.group(1)]).get(qm.group(2))
            if v is None:
                return None
            out.append(v)
        return "".join(out)

    def ifaces(self, pkg: str) -> dict[str, tuple[set[str], set[str]]]:
        """interface → (method names, embedded interface names) in ``pkg``."""
        out: dict[str, tuple[set[str], set[str]]] = {}
        for rel in self.by_dir.get(pkg, []):
            text = self.m(rel).text
            for mm in _IFACE.finditer(text):
                body = text[mm.end() : match_close(text, mm.end() - 1)]
                methods = set(re.findall(r"(?m)^\s*([A-Z]\w*)\s*\(", body))
                embedded = set(re.findall(r"(?m)^\s*([A-Z]\w*)\s*$", body))
                out[mm.group(1)] = (methods, embedded)
        return out

    def method_return(self, pkg: str, type_name: str, method: str) -> str | None:
        rx = re.compile(
            rf"func\s*\(\s*\w*\s*\*?\s*{re.escape(type_name)}\s*\)\s*{re.escape(method)}\s*\(\s*\)\s*string\s*\{{"
        )
        for rel in self.by_dir.get(pkg, []):
            text = self.m(rel).text
            mm = rx.search(text)
            if not mm:
                continue
            body = text[mm.end() : match_close(text, mm.end() - 1)].strip()
            rm = re.fullmatch(r"return\s+(.+?)\s*;?", body, re.S)
            if rm:
                return self.value(pkg, rel, rm.group(1))
        return None

    def ctor_type(self, pkg: str, ctor: str) -> str | None:
        rx = re.compile(rf"(?m)^func\s+{re.escape(ctor)}\s*\(")
        for rel in self.by_dir.get(pkg, []):
            text = self.m(rel).text
            mm = rx.search(text)
            if not mm:
                continue
            close = match_close(text, mm.end() - 1)
            ret = text[close + 1 : text.find("{", close)].strip().lstrip("(").strip()
            tm = re.match(r"\*?\s*([A-Z]\w*)\b", ret)
            return tm.group(1) if tm else None
        return None


def _params(sig: str) -> list[tuple[str, str]]:
    """`e *echo.Echo, a, b X, job J` → [(e, *echo.Echo), (a, X), (b, X), (job, J)]."""
    out: list[tuple[str, str]] = []
    pending: list[str] = []
    for part in split_top(sig):
        bits = part.strip().split(None, 1)
        if not bits:
            continue
        if len(bits) == 1:
            pending.append(bits[0])
            continue
        for name in pending:
            out.append((name, bits[1].strip()))
        pending = []
        out.append((bits[0], bits[1].strip()))
    return out


def _find_helpers(pk: _Pkgs) -> list[Helper]:
    out: list[Helper] = []
    for rel in sorted(pk.texts):
        if rel.endswith("_test.go"):
            continue
        text = pk.m(rel).text
        pkg = posixpath.dirname(rel)
        for fm in _FUNC.finditer(text):
            close = match_close(text, fm.end() - 1)
            params = _params(text[fm.end() : close])
            ob = text.find("{", close)
            if ob < 0:
                continue
            body = text[ob : match_close(text, ob) + 1]
            for rm in _ROUTE.finditer(body):
                args = split_top(body[rm.end() : match_close(body, rm.end() - 1)])
                if not args:
                    continue
                path_expr = args[0].strip()
                for idx, (pname, ptype) in enumerate(params):
                    pm = re.search(rf"(?<![\w.]){re.escape(pname)}\s*\.\s*([A-Z]\w*)\s*\(\s*\)", path_expr)
                    if not pm or "." in ptype.lstrip("*"):
                        continue  # only interfaces of this package
                    iface = ptype.lstrip("*")
                    ifaces = pk.ifaces(pkg)
                    if iface not in ifaces:
                        continue
                    before, after = path_expr[: pm.start()], path_expr[pm.end() :]
                    prefix = pk.value(pkg, rel, before.rstrip().rstrip("+")) if before.strip() else ""
                    suffix = pk.value(pkg, rel, after.lstrip().lstrip("+")) if after.strip() else ""
                    if prefix is None or suffix is None:
                        continue
                    methods = set(ifaces[iface][0])
                    for ms, embedded in ifaces.values():
                        if iface in embedded:
                            methods |= ms  # `type WithParams interface { Job; RunWithParams(…) }`
                    methods.discard(pm.group(1))
                    out.append(Helper(pkg, fm.group(1), idx, pname, iface, pm.group(1),
                                      rm.group(1).upper() if rm.group(1) not in ("Any", "Handle") else "ANY",
                                      prefix, suffix, sorted(methods)))
    return out


def _concrete(pk: _Pkgs, rel: str, caller_pkg: str, expr: str, body: str, at: int) -> tuple[str, str] | None:
    """(package dir, type) an argument expression builds."""
    e = expr.strip()
    imports = pk.imports(rel)
    cm = re.match(r"^&?\s*(?:([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)\s*([({])", e)
    if cm:
        alias, name, kind = cm.group(1), cm.group(2), cm.group(3)
        pkg = imports.get(alias) if alias else caller_pkg
        if pkg is None:
            return None
        if kind == "{":
            return pkg, name
        tname = pk.ctor_type(pkg, name)
        return (pkg, tname) if tname else None
    if re.fullmatch(r"[A-Za-z_]\w*", e):
        # a variable assigned from a constructor earlier in the same function
        last = None
        for am in re.finditer(rf"(?<![\w.]){re.escape(e)}\s*:?=\s*", body[:at]):
            last = am
        if last is not None:
            rhs = body[last.end() :]
            end = rhs.find("\n")
            return _concrete(pk, rel, caller_pkg, rhs[: end if end > 0 else len(rhs)], "", 0)
    return None


def scan_helpers(texts: dict[str, str], module: str | None) -> list[HelperRoute]:
    pk = _Pkgs(texts, module)
    helpers = _find_helpers(pk)
    if not helpers:
        return []
    out: list[HelperRoute] = []
    for rel in sorted(pk.texts):
        if rel.endswith("_test.go"):
            continue
        text = pk.m(rel).text
        caller_pkg = posixpath.dirname(rel)
        imports = pk.imports(rel)
        for h in helpers:
            alias = next((a for a, p in imports.items() if p == h.pkg), None)
            if caller_pkg == h.pkg:
                rx = re.compile(rf"(?<![\w.]){re.escape(h.name)}\s*\(")
            elif alias:
                rx = re.compile(rf"(?<![\w.]){re.escape(alias)}\s*\.\s*{re.escape(h.name)}\s*\(")
            else:
                continue
            for cm in rx.finditer(text):
                if text[: cm.start()].rstrip().endswith("func"):
                    continue  # the declaration itself
                args = split_top(text[cm.end() : match_close(text, cm.end() - 1)])
                if h.param_index >= len(args):
                    continue
                fstart = max(text.rfind("\nfunc ", 0, cm.start()), 0)
                concrete = _concrete(pk, rel, caller_pkg, args[h.param_index], text[fstart:], cm.start() - fstart)
                if concrete is None:
                    continue
                tpkg, tname = concrete
                value = pk.method_return(tpkg, tname, h.method)
                if value is None:
                    continue
                line = text.count("\n", 0, cm.start()) + 1
                out.append(HelperRoute(h.verb, h.prefix + value + h.suffix, rel, line, f"{h.pkg}.{h.name}",
                                       tpkg, tname, h.handlers))
    return out
