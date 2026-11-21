"""Plain-text helpers over source code, without a parser.

Used by the data-flow pass (``dataflow.py``) and database identity
(``dbident.py``), which work on the text of function bodies the detectors
already located. ``mask`` replaces every string literal with a placeholder
token (``\x00<n>\x00``) and blanks comments, keeping line breaks, so regexes
over the masked text never match inside strings or comments and line numbers
stay the same.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

LIT_RE = re.compile(r"\x00(\d+)\x00")
OPEN = "([{"
CLOSE = ")]}"
PAIR = {"(": ")", "[": "]", "{": "}"}


@dataclass
class Lit:
    value: str
    dynamic: bool = False  # template/f-string with interpolation


@dataclass
class Masked:
    text: str
    lits: list[Lit] = field(default_factory=list)

    def lit(self, token: str) -> Lit | None:
        m = LIT_RE.fullmatch(token.strip())
        if not m:
            return None
        return self.lits[int(m.group(1))]


def language_of(path: str) -> str:
    low = path.lower()
    if low.endswith(".go"):
        return "go"
    if low.endswith(".py"):
        return "python"
    return "javascript"


def mask(src: str, lang: str) -> Masked:
    """Replace string literals by ``\\x00n\\x00`` and blank comments."""
    out: list[str] = []
    lits: list[Lit] = []
    i = 0
    n = len(src)

    def push(value: str, raw: str, dynamic: bool = False) -> None:
        lits.append(Lit(value=value, dynamic=dynamic))
        out.append(f"\x00{len(lits) - 1}\x00")
        out.append("\n" * raw.count("\n"))

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if lang != "python" and c == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            i = j
            continue
        if lang != "python" and c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, j))
            i = j
            continue
        if lang == "python" and c == "#":
            j = src.find("\n", i)
            j = n if j < 0 else j
            i = j
            continue
        if lang == "python" and c in "'\"":
            # prefix (f, r, b, rb, ...) is already in `out`; detect f-strings
            k = i
            while k > 0 and i - k < 2 and src[k - 1].lower() in "frbu":
                k -= 1
            prefix = src[k:i].lower() if (k == 0 or not (src[k - 1].isalnum() or src[k - 1] == "_")) else ""
            fstr = "f" in prefix
            if src.startswith(c * 3, i):
                j = src.find(c * 3, i + 3)
                j = n if j < 0 else j
                raw = src[i : j + 3]
                body = src[i + 3 : j]
                push(body, raw, dynamic=fstr and "{" in body)
                i = j + 3
                continue
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                if src[j] == "\\":
                    j += 1
                j += 1
            body = src[i + 1 : j]
            push(_unescape(body), src[i : j + 1], dynamic=fstr and "{" in body)
            i = j + 1
            continue
        if lang != "python" and c in "'\"":
            if lang == "go" and c == "'":
                # rune literal
                j = src.find("'", i + 1 + (1 if nxt == "\\" else 0) + 1)
                j = n if j < 0 else j
                out.append(" ")
                i = j + 1
                continue
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                if src[j] == "\\":
                    j += 1
                j += 1
            push(_unescape(src[i + 1 : j]), src[i : j + 1])
            i = j + 1
            continue
        if c == "`":
            j = i + 1
            depth = 0
            dynamic = False
            while j < n:
                if lang == "javascript" and src[j] == "\\":
                    j += 2
                    continue
                if lang == "javascript" and src.startswith("${", j):
                    dynamic = True
                    depth += 1
                    j += 2
                    continue
                if depth and src[j] == "}":
                    depth -= 1
                elif not depth and src[j] == "`":
                    break
                j += 1
            push(src[i + 1 : j], src[i : j + 1], dynamic=dynamic)
            i = j + 1
            continue
        out.append(c)
        i += 1
    return Masked(text="".join(out), lits=lits)


def _unescape(body: str) -> str:
    return body.replace('\\"', '"').replace("\\'", "'").replace("\\n", "\n").replace("\\t", "\t")


def match_close(text: str, i: int) -> int:
    """Index of the bracket closing ``text[i]`` (or len(text) when unbalanced)."""
    stack = [PAIR[text[i]]]
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c in OPEN:
            stack.append(PAIR[c])
        elif c in CLOSE:
            if not stack or c != stack[-1]:
                return n
            stack.pop()
            if not stack:
                return j
        j += 1
    return n


def split_top(text: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` at bracket depth 0 (masked text: no strings)."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in text:
        if c in OPEN:
            depth += 1
        elif c in CLOSE:
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    tail = "".join(cur)
    if tail.strip() or parts:
        parts.append(tail)
    return [p.strip() for p in parts]


def line_of(text: str, pos: int) -> int:
    """1-based line of ``pos`` relative to the start of ``text``."""
    return text.count("\n", 0, pos) + 1


def body_slice(lines: list[str], start: int, end: int) -> str:
    """Lines ``start..end`` (1-based, inclusive) joined."""
    return "\n".join(lines[max(start - 1, 0) : max(end, 0)])


def source_texts(root: Path, files: list[Path]) -> dict[str, str]:
    """Text of each file, by path relative to ``root`` (unreadable files skipped)."""
    out: dict[str, str] = {}
    for p in files:
        try:
            out[os.path.relpath(p, root).replace(os.sep, "/")] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out
