"""Firebase Functions names and callers in JavaScript / TypeScript (a text pass).

- **Deployed names.** Firebase deploys what the functions entry file exports,
  grouping nested exports with ``-``::

      // index.ts
      exports.users = require('./users');      // group `users`
      // users/index.ts
      exports.archiveUser = mgmt.archiveUser;  // deployed as `users-archiveUser`

  :func:`deployed_names` follows ``exports.x = require(…)``, ``exports.x =
  mod.y``, ``export { a as b } from …``, ``export * from …`` and local
  ``export const`` / ``exports.x =`` definitions from the entry file of every
  ``package.json`` that depends on ``firebase-functions``, and maps each
  defining (file, export name) to the deployed name.

- **Callable callers.** ``httpsCallable(functions, 'users-archiveUser')``
  (generic form included) and ``httpsCallableFromURL(functions, url)`` name
  the function they call; :func:`callable_sites` lists them with the calling
  function, so the scan can link them to the function's entrypoint.
"""

from __future__ import annotations

import bisect
import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from bizmap.srctext import Masked, mask, match_close, split_top

_SUFFIXES = ("", ".ts", ".tsx", ".js", ".mjs", ".cjs", "/index.ts", "/index.js", "/index.mjs")
_ID = r"[A-Za-z_$][\w$]*"
_LIT = r"\x00\d+\x00"
_REQ_ALIAS = re.compile(rf"(?<![\w$.])(?:const|let|var)\s+({_ID})\s*=\s*require\s*\(\s*({_LIT})\s*\)\s*(?:;|\n)")
_IMPORT_NS = re.compile(rf"(?<![\w$.])import\s+(?:\*\s+as\s+)?({_ID})\s+from\s+({_LIT})")
_IMPORT_NAMED = re.compile(rf"(?<![\w$.])import\s+(?:type\s+)?\{{([^{{}}]*)\}}\s*from\s+({_LIT})")
_EXPORTS_ASSIGN = re.compile(rf"(?<![\w$.])(?:module\.)?exports\.({_ID})\s*=\s*")
_EXPORT_DECL = re.compile(rf"(?<![\w$.])export\s+(?:async\s+)?(?:const|let|var|function\*?|class)\s+({_ID})")
_EXPORT_FROM = re.compile(rf"(?<![\w$.])export\s+(?:type\s+)?\{{([^{{}}]*)\}}\s*from\s+({_LIT})")
_EXPORT_LOCAL = re.compile(r"(?<![\w$.])export\s+(?:type\s+)?\{([^{}]*)\}\s*(?!\s*from)(?:;|\n|$)")
_EXPORT_STAR = re.compile(rf"(?<![\w$.])export\s+\*\s+(?:as\s+({_ID})\s+)?from\s+({_LIT})")
_CALLABLE = re.compile(r"(?<![\w$.])httpsCallable(FromURL)?\b")
_MODULE_CONST = re.compile(rf"^[ \t]*(?:export\s+)?(?:const|let|var)\s+({_ID})\s*(?::[^=\n]+)?=\s*$")


@dataclass
class CallableSite:
    symbol: str  # calling function (or `<module>.<const>` / `<module>.module`)
    name: str  # deployed function name
    file: str
    line: int


def _module(rel: str) -> str:
    return rel.replace("/", ".").rsplit(".", 1)[0]


def _resolve(texts: dict[str, str], base_file: str, spec: str) -> str | None:
    if not spec.startswith("."):
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base_file), spec))
    stem = re.sub(r"\.(?:js|mjs|cjs)$", "", joined)
    for cand in (joined, stem):
        for suffix in _SUFFIXES:
            if cand + suffix in texts:
                return cand + suffix
    return None


def _pairs(spec_list: str) -> list[tuple[str, str]]:
    """`a, b as c, type d` → [(a, a), (b, c)]."""
    out = []
    for part in spec_list.split(","):
        part = part.strip()
        if not part or part.startswith("type "):
            continue
        m = re.fullmatch(rf"({_ID})(?:\s+as\s+({_ID}))?", part)
        if m:
            out.append((m.group(1), m.group(2) or m.group(1)))
    return out


class _Exports:
    def __init__(self, texts: dict[str, str]):
        self.texts = texts
        self.masked: dict[str, Masked] = {}

    def m(self, rel: str) -> Masked:
        if rel not in self.masked:
            self.masked[rel] = mask(self.texts[rel], "javascript")
        return self.masked[rel]

    def lit(self, rel: str, token: str) -> str | None:
        lit = self.m(rel).lit(token)
        return lit.value if lit is not None and not lit.dynamic else None

    def aliases(self, rel: str) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
        """(namespace alias → module file, named import → (module file, name))."""
        text = self.m(rel).text
        ns: dict[str, str] = {}
        named: dict[str, tuple[str, str]] = {}
        for rx in (_REQ_ALIAS, _IMPORT_NS):
            for mm in rx.finditer(text):
                spec = self.lit(rel, mm.group(2))
                target = _resolve(self.texts, rel, spec) if spec else None
                if target:
                    ns[mm.group(1)] = target
        for mm in _IMPORT_NAMED.finditer(text):
            spec = self.lit(rel, mm.group(2))
            target = _resolve(self.texts, rel, spec) if spec else None
            if target:
                for orig, local in _pairs(mm.group(1)):
                    named[local] = (target, orig)
        return ns, named

    def leaf(self, rel: str, name: str, depth: int = 0) -> tuple[str, str]:
        """The (file, name) that defines export ``name`` of ``rel``, following
        re-exports (`exports.x = mod.x`, `export { x } from './m'`)."""
        if depth > 12:
            return rel, name
        text = self.m(rel).text
        ns, named = self.aliases(rel)
        for mm in _EXPORTS_ASSIGN.finditer(text):
            if mm.group(1) != name:
                continue
            rhs = text[mm.end() : mm.end() + 200]
            am = re.match(rf"({_ID})\s*\.\s*({_ID})\s*(?:;|\n|$)", rhs)
            if am and am.group(1) in ns:
                return self.leaf(ns[am.group(1)], am.group(2), depth + 1)
            rm = re.match(rf"require\s*\(\s*({_LIT})\s*\)\s*\.\s*({_ID})", rhs)
            if rm:
                spec = self.lit(rel, rm.group(1))
                target = _resolve(self.texts, rel, spec) if spec else None
                if target:
                    return self.leaf(target, rm.group(2), depth + 1)
            im = re.match(rf"({_ID})\s*(?:;|\n|$)", rhs)
            if im and im.group(1) in named:
                return self.leaf(*named[im.group(1)], depth + 1)
            return rel, name
        for mm in _EXPORT_FROM.finditer(text):
            for orig, public in _pairs(mm.group(1)):
                if public == name:
                    spec = self.lit(rel, mm.group(2))
                    target = _resolve(self.texts, rel, spec) if spec else None
                    if target:
                        return self.leaf(target, orig, depth + 1)
        for mm in _EXPORT_LOCAL.finditer(text):
            for orig, public in _pairs(mm.group(1)):
                if public == name and orig in named:
                    return self.leaf(*named[orig], depth + 1)
        return rel, name

    def walk(self, rel: str, prefix: str, out: dict[tuple[str, str], str], seen: set[tuple[str, str]]) -> None:
        if (rel, prefix) in seen or len(seen) > 5000:
            return
        seen.add((rel, prefix))
        text = self.m(rel).text
        ns, named = self.aliases(rel)

        def put(public: str, file: str, local: str) -> None:
            defining = self.leaf(file, local)
            out.setdefault(defining, prefix + public)
            out.setdefault((file, local), prefix + public)

        for mm in _EXPORTS_ASSIGN.finditer(text):
            public = mm.group(1)
            rhs = text[mm.end() : mm.end() + 200]
            rm = re.match(rf"require\s*\(\s*({_LIT})\s*\)\s*(\.\s*({_ID}))?", rhs)
            if rm:
                spec = self.lit(rel, rm.group(1))
                target = _resolve(self.texts, rel, spec) if spec else None
                if target and rm.group(3):
                    put(public, target, rm.group(3))
                elif target:
                    self.walk(target, f"{prefix}{public}-", out, seen)  # a group
                continue
            gm = re.match(rf"({_ID})\s*(?:;|\n|$)", rhs)
            if gm and gm.group(1) in ns:
                self.walk(ns[gm.group(1)], f"{prefix}{public}-", out, seen)  # `exports.g = mod`
                continue
            put(public, rel, public)
        for mm in _EXPORT_DECL.finditer(text):
            put(mm.group(1), rel, mm.group(1))
        for mm in _EXPORT_FROM.finditer(text):
            spec = self.lit(rel, mm.group(2))
            target = _resolve(self.texts, rel, spec) if spec else None
            if target:
                for orig, public in _pairs(mm.group(1)):
                    put(public, target, orig)
        for mm in _EXPORT_LOCAL.finditer(text):
            for orig, public in _pairs(mm.group(1)):
                if orig in named:
                    put(public, *named[orig])
                elif orig in ns:
                    self.walk(ns[orig], f"{prefix}{public}-", out, seen)
                else:
                    put(public, rel, orig)
        for mm in _EXPORT_STAR.finditer(text):
            spec = self.lit(rel, mm.group(2))
            target = _resolve(self.texts, rel, spec) if spec else None
            if target:
                self.walk(target, f"{prefix}{mm.group(1)}-" if mm.group(1) else prefix, out, seen)


def _entry_files(root: Path, texts: dict[str, str]) -> list[str]:
    """Functions entry files: for each package.json (next to a scanned file or
    above it) depending on firebase-functions, its `main` mapped back to the
    source (`lib/index.js` → `src/index.ts`), else `src/index.ts` / `index.ts`."""
    dirs: set[str] = set()
    for rel in texts:
        d = posixpath.dirname(rel)
        while True:
            dirs.add(d)
            if not d:
                break
            d = posixpath.dirname(d)
    out: list[str] = []
    for d in sorted(dirs):
        pkg = root / d / "package.json" if d else root / "package.json"
        if not pkg.is_file():
            continue
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if "firebase-functions" not in deps:
            continue
        main = str(data.get("main") or "")
        cands = []
        if main:
            m = posixpath.normpath(main)
            stem = re.sub(r"\.(?:js|mjs|cjs)$", "", m)
            cands += [m, stem, re.sub(r"^(?:lib|dist|build|out)/", "src/", stem)]
        cands += ["src/index", "index"]
        for cand in cands:
            path = posixpath.join(d, cand) if d else cand
            hit = next((path + s for s in _SUFFIXES if path + s in texts), None)
            if hit:
                out.append(hit)
                break
    return out


def deployed_names(root: Path, texts: dict[str, str]) -> dict[tuple[str, str], str]:
    """(defining file, export name) → deployed function name, for every
    functions codebase under ``root``."""
    ex = _Exports(texts)
    out: dict[tuple[str, str], str] = {}
    for entry in _entry_files(root, texts):
        ex.walk(entry, "", out, set())
    return out


def _fn_at(spans: list[tuple[int, int, str]], line: int) -> str | None:
    best = None
    for start, end, qual in spans:
        if start <= line <= end and (best is None or end - start < best[1] - best[0]):
            best = (start, end, qual)
    return best[2] if best else None


def _url_name(url: str) -> str | None:
    """`https://region-proj.cloudfunctions.net/users-archiveUser` → `users-archiveUser`."""
    m = re.match(r"https?://[^/]+\.cloudfunctions\.net/([\w-]+)", url)
    return m.group(1) if m else None


def callable_sites(
    texts: dict[str, str],
    functions: dict[str, tuple[str, int]],
    function_ends: dict[str, int],
    consts: dict[str, str] | None = None,
) -> list[CallableSite]:
    """`httpsCallable(fns, '<name>')` calls with the function making them."""
    spans_by_file: dict[str, list[tuple[int, int, str]]] = {}
    for qual, (file, line) in functions.items():
        spans_by_file.setdefault(file, []).append((line, function_ends.get(qual) or line, qual))
    out: list[CallableSite] = []
    for rel, src in texts.items():
        if "httpsCallable" not in src:
            continue
        m = mask(src, "javascript")
        text = m.text
        lines_at = [0] + [x.end() for x in re.finditer("\n", text)]

        for mm in _CALLABLE.finditer(text):
            i = mm.end()
            while i < len(text) and text[i] in " \t\n":
                i += 1
            if i < len(text) and text[i] == "<":  # generic arguments
                depth = 0
                while i < len(text):
                    if text[i] == "<":
                        depth += 1
                    elif text[i] == ">" and text[i - 1] != "=":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
                while i < len(text) and text[i] in " \t\n":
                    i += 1
            if i >= len(text) or text[i] != "(":
                continue
            args = split_top(text[i + 1 : match_close(text, i)])
            if len(args) < 2:
                continue
            a1 = args[1].strip()
            lit = m.lit(a1)
            value = lit.value if lit is not None and not lit.dynamic else (consts or {}).get(a1)
            if not value:
                continue
            name = _url_name(value) if mm.group(1) else value
            if not name or not re.fullmatch(r"[\w-]+", name):
                continue
            line = bisect.bisect_right(lines_at, mm.start())
            symbol = _fn_at(spans_by_file.get(rel, []), line)
            if symbol is None:
                stmt = text[text.rfind("\n", 0, mm.start()) + 1 : mm.start()]
                cm = _MODULE_CONST.match(stmt)
                symbol = f"{_module(rel)}.{cm.group(1)}" if cm else f"{_module(rel)}.module"
            out.append(CallableSite(symbol, name, rel, line))
    return out
