"""Match HTTP client URLs to scanned route templates."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_ENV_GET = re.compile(
    r"""(?:os\.)?(?:environ(?:\.get)?|getenv)\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""
)
_ENV_SUB = re.compile(
    r"""(?:os\.)?environ\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""
)
_SETTINGS = re.compile(r"""(?:settings|config|conf)\.([A-Za-z_][A-Za-z0-9_]*)""")


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


def _service_fields(name: str, spec: Any) -> tuple[str, list[str], list[str]]:
    """Return (url, env names, hosts) for a services: entry."""
    envs: list[str] = []
    hosts: list[str] = []
    url = ""
    if isinstance(spec, dict):
        url = str(spec.get("url") or spec.get("base") or spec.get("host") or "")
        env = spec.get("env") or spec.get("env_var") or spec.get("environ")
        if isinstance(env, list):
            envs.extend(str(x) for x in env if x)
        elif env:
            envs.append(str(env))
        host = spec.get("host") or spec.get("hostname")
        if host:
            hosts.append(str(host))
    elif spec is not None:
        url = str(spec)
    if url and "://" in url:
        parsed = urlparse(url)
        if parsed.netloc:
            hosts.append(parsed.netloc.split("@")[-1])
    elif url and "." in url and "/" not in url and url.isidentifier() is False:
        hosts.append(url)
    envs.append(name)
    if name.isidentifier() and name.upper() != name:
        envs.append(name.upper())
        envs.append(name.upper() + "_URL" if not name.upper().endswith("_URL") else name.upper())
    snake = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    if snake:
        envs.append(snake)
        envs.append(snake.upper())
        if not snake.upper().endswith("_URL"):
            envs.append(snake.upper() + "_URL")
            envs.append(snake + "_url")
    # unique, preserve order
    seen: set[str] = set()
    env_out: list[str] = []
    for e in envs:
        if e and e not in seen:
            seen.add(e)
            env_out.append(e)
    host_out: list[str] = []
    seen_h: set[str] = set()
    for h in hosts:
        key = h.lower()
        if h and key not in seen_h:
            seen_h.add(key)
            host_out.append(h)
    return url, env_out, host_out


def expand_service_prefixes(services: dict[str, Any] | None) -> list[str]:
    """Prefixes to strip from a client URL, including env/settings placeholders."""
    prefixes: list[str] = []
    for name, spec in (services or {}).items():
        url, envs, hosts = _service_fields(str(name), spec)
        if url:
            prefixes.append(url)
            prefixes.append(url.rstrip("/"))
        prefixes.append("{" + str(name) + "}")
        for env in envs:
            prefixes.append("{" + env + "}")
            prefixes.append("{os.environ[\"" + env + "\"]}")
            prefixes.append("{os.environ['" + env + "']}")
            prefixes.append("{os.environ.get(\"" + env + "\")}")
            prefixes.append("{os.environ.get('" + env + "')}")
            prefixes.append("{os.getenv(\"" + env + "\")}")
            prefixes.append("{os.getenv('" + env + "')}")
            prefixes.append("{settings." + env + "}")
            prefixes.append("{settings." + env.lower() + "}")
            prefixes.append("{config." + env.lower() + "}")
        for host in hosts:
            prefixes.append("http://" + host)
            prefixes.append("https://" + host)
            prefixes.append(host)
    prefixes.sort(key=len, reverse=True)
    return prefixes


def strip_service_base(url: str, services: dict[str, Any] | None) -> str:
    """Remove a configured service base URL or `{ENV}` / `{name}` prefix."""
    if not url:
        return url
    for prefix in expand_service_prefixes(services):
        if not prefix:
            continue
        if url.lower().startswith(str(prefix).lower()):
            rest = url[len(prefix) :]
            return rest if rest else "/"
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return parsed.path or "/"
    return url


def path_without_placeholders(path: str) -> str:
    """Drop leading `{ENV}` segments so `/ingest` remains from `{WORKER_URL}/ingest`."""
    raw = normalize_path(path)
    parts = [p for p in raw.split("/") if p]
    kept: list[str] = []
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            continue
        kept.append(part)
    if not kept:
        return raw
    return "/" + "/".join(kept)


def drop_leading_placeholders(path: str) -> str:
    """Strip only leading `{ENV}` / `:param` segments; keep later literals."""
    raw = normalize_path(path)
    parts = [p for p in raw.split("/") if p]
    while parts and _is_param(parts[0]):
        parts.pop(0)
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def has_meaningful_literal(path: str) -> bool:
    """True when the path has a non-parameter segment (a real prefix)."""
    parts = [p for p in normalize_path(path).split("/") if p]
    return any(not _is_param(p) for p in parts)


def literal_segment_count(path: str) -> int:
    """Count non-parameter path segments (`internal/x` → 2)."""
    return sum(1 for p in normalize_path(path).split("/") if p and not _is_param(p))


def _service_prefix_hit(url: str, services: dict[str, Any] | None) -> bool:
    if not url or not services:
        return False
    raw = url.lower()
    for prefix in expand_service_prefixes(services):
        if prefix and raw.startswith(str(prefix).lower()):
            return True
    return False


def path_template_match(template: str, actual: str) -> bool:
    """Match a client path against a route template.

    Route params (`{id}`, `:id`) match one concrete client segment. Client
    placeholders are **not** wildcards. At least one literal segment must
    agree, so an unresolved client URL never hits every `{param}` route.
    """
    t_parts = normalize_path(template).split("/")
    a_parts = normalize_path(actual).split("/")
    if len(t_parts) != len(a_parts):
        return False
    literal_hits = 0
    for ts, actual_seg in zip(t_parts, a_parts, strict=True):
        if not ts and not actual_seg:
            continue
        if _is_param(ts) and _is_param(actual_seg):
            # Dynamic call-site segments (`{id}`, PathEscape) match route params
            # when surrounding literals still agree.
            continue
        if _is_param(ts):
            continue
        if _is_param(actual_seg):
            return False
        if ts != actual_seg:
            return False
        literal_hits += 1
    return literal_hits >= 1


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


def is_relative_client_url(url: str | None) -> bool:
    """True for same-repo paths like ``/projects`` (no host, env, or service origin)."""
    raw = (url or "").strip()
    if not raw or "://" in raw:
        return False
    if raw.startswith("{"):
        return False
    return raw.startswith("/")


_LEADING_HOLE = re.compile(r"^\{([^{}]+)\}")


def service_for_url(url: str, services: dict[str, Any] | None) -> str | None:
    """Name of the ``services:`` entry a client URL points at, if any.

    Matched by env placeholder (``{BILLING_URL}/x``), host, or URL prefix.
    The entry's ``repo:`` wins over its key, so ``services: {pay: {repo:
    billing}}`` names the scanned repo ``billing``.
    """
    if not url or not services:
        return None
    raw = url.strip()
    m = _LEADING_HOLE.match(raw)
    hole = m.group(1).strip() if m else ""
    hole_leaf = hole.split(".")[-1] if hole else ""
    host = urlparse(raw).netloc.split("@")[-1].lower() if "://" in raw else ""
    for name, spec in services.items():
        svc_url, envs, hosts = _service_fields(str(name), spec)
        target = str(spec.get("repo") or name) if isinstance(spec, dict) else str(name)
        if hole and (hole in envs or hole_leaf in envs):
            return target
        if host and host in {h.lower() for h in hosts}:
            return target
        if svc_url and "://" in svc_url and raw.lower().startswith(svc_url.lower().rstrip("/")):
            return target
    return None


def base_is_known(url: str) -> bool:
    """True when the client URL carries a concrete origin: an absolute URL or
    an env-var placeholder (``{VITE_API_URL}/x``). Lower-case holes such as
    ``{base}`` / ``{client}`` and bare paths are unknown bases."""
    raw = (url or "").strip()
    if "://" in raw:
        return True
    m = _LEADING_HOLE.match(raw)
    if not m:
        return False
    leaf = m.group(1).strip().split(".")[-1]
    return bool(_ENV_LIKE_NAME.match(leaf))


def match_http(
    method: str,
    url: str,
    routes: list[tuple[str, str, str]],
    services: dict[str, Any] | None = None,
    *,
    caller_repo: str | None = None,
    route_repos: dict[str, str] | None = None,
) -> list[str]:
    """Return entrypoint ids whose method+path match this client call."""
    method_u = (method or "GET").upper()
    if method_u in {"HTTP_METHOD.POST", "HTTPMETHOD.POST"}:
        method_u = "POST"
    stripped = strip_service_base(url, services or {})
    path = normalize_path(stripped)
    leading = drop_leading_placeholders(path)
    candidates = [path]
    if leading != path:
        candidates.append(leading)
    if not any(has_meaningful_literal(c) for c in candidates):
        return []
    relative = is_relative_client_url(url)
    service_hit = _service_prefix_hit(url, services or {})
    # When the base names a declared service and the graph spans several
    # repos, only that service's routes may match. A service that was not
    # scanned matches nothing (the call stays `target_not_scanned`) instead of
    # falling through to a same-path route in another repo.
    target_service: str | None = None
    if route_repos and caller_repo and any(r and r != caller_repo for r in route_repos.values()):
        target_service = service_for_url(url, services or {})
    specific = max((literal_segment_count(c) for c in candidates), default=0) >= 2
    hits: list[str] = []
    for eid, rmethod, rpath in routes:
        if rmethod.upper() != method_u:
            continue
        if target_service is not None and route_repos is not None:
            if (route_repos.get(eid) or "") != target_service:
                continue
        if caller_repo and route_repos:
            rrepo = route_repos.get(eid)
            if rrepo and rrepo != caller_repo:
                # Bare `fetch('/x')` stays same-repo. Constructor/config bases
                # (`{base}/internal/x`) may match across repos when the path is
                # specific (2+ literal segments) or `services:` maps the origin.
                if relative or not (service_hit or specific):
                    continue
        for cand in candidates:
            if not has_meaningful_literal(cand):
                continue
            if path_template_match(rpath, cand):
                hits.append(eid)
                break
    return hits


UNRESOLVED_HTTP_REASONS = (
    "target_not_scanned",
    "base_unknown",
    "path_dynamic",
    "client_unknown",
)
_BASE_PLACEHOLDERS = {
    "base",
    "baseurl",
    "base_url",
    "prefixurl",
    "prefix",
    "origin",
    "host",
    "client",
    "http",
    "api",
    "transport",
    "session",
    "instance",
}
_ENV_LIKE_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")


def classify_unresolved_http(
    url: str,
    *,
    client_unknown: bool = False,
) -> str:
    """Pick a ``reason`` for an unmatched outbound HTTP call."""
    if client_unknown:
        return "client_unknown"
    raw = url or ""
    placeholders = [m.group(1).strip() for m in _PLACEHOLDER.finditer(raw)]
    base_holes = [
        p
        for p in placeholders
        if p.lower() in _BASE_PLACEHOLDERS
        or p.lower().split(".")[-1] in _BASE_PLACEHOLDERS
    ]
    env_holes = [p for p in placeholders if _ENV_LIKE_NAME.match(p.split(".")[-1] or p)]
    other_holes = [p for p in placeholders if p not in base_holes and p not in env_holes]
    leading = drop_leading_placeholders(raw)
    literal = has_meaningful_literal(leading)
    if other_holes and not literal:
        return "path_dynamic"
    if base_holes and not env_holes and "://" not in raw:
        return "base_unknown"
    if not literal:
        return "path_dynamic"
    return "target_not_scanned"


def unresolved_http_keys(url: str) -> list[str]:
    """Env var / settings / hostname keys visible in an unmatched client URL."""
    keys: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        item = (item or "").strip()
        if not item or item in seen:
            return
        seen.add(item)
        keys.append(item)

    for m in _PLACEHOLDER.finditer(url or ""):
        inner = m.group(1).strip()
        env = _ENV_SUB.search(inner) or _ENV_GET.search(inner)
        if env:
            add(env.group(1))
            continue
        settings = _SETTINGS.search(inner)
        if settings:
            add(settings.group(1))
            continue
        # os.environ["X"] written without wrapping parse
        if inner.startswith("os.environ") or inner.startswith("os.getenv"):
            boxed = _ENV_SUB.search(inner) or _ENV_GET.search(inner)
            if boxed:
                add(boxed.group(1))
                continue
        add(inner.split(".")[-1] if "." in inner and " " not in inner else inner)
    for m in _ENV_SUB.finditer(url or ""):
        add(m.group(1))
    for m in _ENV_GET.finditer(url or ""):
        add(m.group(1))
    parsed = urlparse(url)
    if parsed.netloc and not parsed.netloc.startswith("{"):
        add(parsed.netloc.split("@")[-1])
    return keys


def _segments(path: str) -> list[str]:
    return [p for p in normalize_path(path).split("/") if p]


def suffix_template_match(template: str, actual: str) -> bool:
    """True when ``actual`` matches the *tail* of a longer route template.

    ``/orders/:id`` is a suffix of ``/api/v1/orders/:id`` (the client base
    carried ``/api/v1``). Same-length paths are not suffix matches.
    """
    t_parts = _segments(template)
    a_parts = _segments(actual)
    if not a_parts or len(t_parts) <= len(a_parts):
        return False
    tail = "/" + "/".join(t_parts[-len(a_parts) :])
    return path_template_match(tail, "/" + "/".join(a_parts))


def route_literal_hits(
    path: str,
    method: str | None,
    routes: list[tuple[str, str, str]],
) -> list[str]:
    """Routes that an API-looking string literal plausibly targets.

    Precision rules (a literal carries no client/base information):

    * same-length template match: any literal with a non-param segment;
    * suffix match (unknown base prefix): needs 2+ literal segments, or a
      method hint from the surrounding call (``.get(...)``, ``method: 'POST'``);
    * the method hint, when present, must equal the route method;
    * callers link only when exactly one route remains (ambiguous → nothing).
    """
    if literal_segment_count(path) < 1:
        return []
    method_u = method.upper() if method else None
    full: list[str] = []
    suffix: list[str] = []
    allow_suffix = literal_segment_count(path) >= 2 or method_u is not None
    for eid, rmethod, rpath in routes:
        if method_u and rmethod.upper() not in {method_u, "ANY", "ALL"}:
            continue
        if path_template_match(rpath, path):
            full.append(eid)
        elif allow_suffix and suffix_template_match(rpath, path):
            suffix.append(eid)
    return full or suffix
