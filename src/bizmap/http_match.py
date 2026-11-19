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
    specific = max((literal_segment_count(c) for c in candidates), default=0) >= 2
    hits: list[str] = []
    for eid, rmethod, rpath in routes:
        if rmethod.upper() != method_u:
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
