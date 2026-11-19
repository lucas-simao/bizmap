"""Match HTTP client URLs to scanned route templates."""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return "/"
    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.path or "/"
    if "?" in raw:
        raw = raw.split("?", 1)[0]
    if not raw.startswith("/") and not raw.startswith("{"):
        raw = "/" + raw
    if len(raw) > 1:
        raw = raw.rstrip("/")
    return raw or "/"


def strip_service_base(url: str, services: dict[str, str]) -> str:
    """Remove a configured service base URL or `{ENV}` / `{name}` prefix."""
    if not url:
        return url
    candidates: list[tuple[str, str]] = []
    for name, base in (services or {}).items():
        if not base:
            continue
        candidates.append((name, str(base)))
        candidates.append((name, "{" + name + "}"))
        if str(base).isidentifier():
            candidates.append((name, "{" + str(base) + "}"))
            candidates.append((name, str(base)))
    # longest prefix first
    candidates.sort(key=lambda kv: len(kv[1]), reverse=True)
    for _name, prefix in candidates:
        if not prefix:
            continue
        if url.lower().startswith(str(prefix).lower()):
            rest = url[len(prefix) :]
            return rest if rest else "/"
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return parsed.path or "/"
    return url


def path_template_match(template: str, actual: str) -> bool:
    t_parts = normalize_path(template).split("/")
    a_parts = normalize_path(actual).split("/")
    if len(t_parts) != len(a_parts):
        return False
    for ts, actual_seg in zip(t_parts, a_parts, strict=True):
        if _is_param(ts) or _is_param(actual_seg):
            continue
        if ts != actual_seg:
            return False
    return True


def _is_param(seg: str) -> bool:
    if not seg:
        return False
    if seg.startswith("{") and seg.endswith("}"):
        return True
    if seg.startswith(":"):
        return True
    if seg.startswith("<") and seg.endswith(">"):
        return True
    return False


def match_http(
    method: str,
    url: str,
    routes: list[tuple[str, str, str]],
    services: dict[str, str] | None = None,
) -> list[str]:
    """Return entrypoint ids whose method+path match this client call."""
    method_u = (method or "GET").upper()
    if method_u in {"HTTP_METHOD.POST", "HTTPMETHOD.POST"}:
        method_u = "POST"
    path = normalize_path(strip_service_base(url, services or {}))
    hits: list[str] = []
    for eid, rmethod, rpath in routes:
        if rmethod.upper() != method_u:
            continue
        if path_template_match(rpath, path) or path_template_match(path, rpath):
            hits.append(eid)
    return hits
