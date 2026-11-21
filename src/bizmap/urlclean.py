"""Stable URL labels for HTTP calls in every language.

`clean_url` keeps `{name}` / `{settings.API_URL}` placeholders, turns any other
`{…}` (the source of an expression: `{x.y()}`, `{...config, a: () => 1}`,
`{1 << 20}`) into `{dynamic}`, and drops the query string, which is never part
of the route a call goes to (`{API}/daily?{q.Encode()}` → `{API}/daily`).
"""

from __future__ import annotations

import re

_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(\.[A-Za-z_$][A-Za-z0-9_$]*)*$")


def clean_placeholders(url: str) -> str:
    if "{" not in url:
        return url
    out: list[str] = []
    i = 0
    while i < len(url):
        ch = url[i]
        if ch != "{":
            out.append(ch)
            i += 1
            continue
        depth, j = 0, i
        while j < len(url):
            if url[j] == "{":
                depth += 1
            elif url[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(url):  # unbalanced: the rest is code
            out.append("{dynamic}")
            break
        inner = url[i + 1 : j].strip()
        out.append("{" + inner + "}" if _NAME.match(inner) else "{dynamic}")
        i = j + 1
    return "".join(out)


def strip_query(url: str) -> str:
    """`/daily?{q}` → `/daily`; `/?x=1` → `/`. A `?` inside a placeholder is kept."""
    depth = 0
    for i, ch in enumerate(url):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch in "?#" and depth == 0:
            return url[:i]
    return url


def clean_url(url: str) -> str:
    return strip_query(clean_placeholders(url or "")) if url else url


def clean_method(method: str) -> str:
    """An HTTP method label: a known verb (upper-cased) or `{dynamic}`."""
    m = (method or "").strip()
    if re.fullmatch(r"[A-Za-z]+", m):
        return m.upper()
    return "{dynamic}" if m else m
