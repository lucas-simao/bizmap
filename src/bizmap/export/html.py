"""`bizmap export --format html`: one self-contained, offline HTML "book".

The page reads the system like a book — services, then domains (routes
grouped by path), then a route's call tree, then the data it reads and
writes — and opens each level only when clicked. Data is embedded as one
small index (read when the page opens) and one JSON ``<script>`` per chunk
(a domain, a front end's screens, a batch of tables…) read on first use; see
:mod:`bizmap.export.book`. Cytoscape.js (MIT) is inlined for the service map.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from bizmap.export.book import build_book

LANGS = ("en", "pt-BR")


def _asset(name: str) -> str:
    return resources.files("bizmap.export").joinpath(name).read_text(encoding="utf-8")


def _json_safe(blob: str) -> str:
    """JSON that cannot close the <script> element it sits in (`<` only
    appears inside strings, where `\\u003c` is the same character)."""
    return blob.replace("<", "\\u003c")


def _js_safe(name: str, code: str) -> str:
    low = code.lower()
    if "</script" in low or "<!--" in low:
        raise ValueError(f"{name} cannot be inlined in a <script> element")
    return code


def _dump(obj: Any) -> str:
    return _json_safe(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_html(
    data: dict[str, Any],
    svc_of: dict[str, str | None],
    lang: str = "en",
    title: str | None = None,
    *,
    domain_rules: dict[str, list[tuple[str, str, str]]] | None = None,
    domains_by: str = "path",
    specs: list[tuple[str | None, dict[str, Any]]] | None = None,
) -> str:
    lang = lang if lang in LANGS else "en"
    index, chunks = build_book(
        data, svc_of, domain_rules=domain_rules, domains_by=domains_by, lang=lang, specs=specs
    )
    services = index["services"]
    if title:
        name = title
    elif len(services) == 1:
        name = services[0]["name"]
    elif services:
        name = f"{len(services)} {'serviços' if lang == 'pt-BR' else 'services'}"
    else:
        name = data.get("repo") or "graph"
    blocks = [f'<script id="bm-index" type="application/json">{_dump(index)}</script>']
    for cid in index["chunks"] + ["names"]:
        blocks.append(f'<script id="bm-{cid}" type="application/json">{_dump(chunks[cid])}</script>')
    page = _asset("template.html")
    parts = {
        "__TITLE__": _html_escape(f"BizMap — {name}"),
        "__LANG__": lang,
        "<!--__DATA__-->": "\n".join(blocks),
        "/*__CYTOSCAPE__*/": _js_safe("cytoscape", _asset("vendor/cytoscape.min.js")),
        # Mermaid 11.16.1 (MIT): the architecture, queue-flow and map diagrams, the
        # same renderer GitHub uses for the PR comment; vendored with `<!--` written
        # `<\!--` (inside strings and one regex, where it reads the same) so it can sit in <script>
        "/*__MERMAID__*/": _js_safe("mermaid", _asset("vendor/mermaid.min.js")),
        "/*__QUERY__*/": _js_safe("query.js", _asset("query.js")),
        "/*__APP__*/": _js_safe("book.js", _asset("book.js").replace("/*__HOME__*/", _asset("home.js") + _asset("views.js"), 1)),
    }
    out: list[str] = []
    pos = 0
    while True:  # one pass, so a placeholder-looking string inside the data is never expanded
        hits = [(page.find(k, pos), k) for k in parts]
        hits = [(i, k) for i, k in hits if i >= 0]
        if not hits:
            out.append(page[pos:])
            break
        i, k = min(hits)
        out.append(page[pos:i])
        out.append(parts[k])
        pos = i + len(k)
    return "".join(out)
