"""Group a service's entrypoints into domains (the chapters of the HTML book).

HTTP routes are grouped by the first *useful* path segment: generic segments
(``api``, ``v1``…, ``rest``, ``public``, ``internal``), path parameters, the
prefix shared by every route of the service, and a first segment shared by
most of them (``DOMINANT_SHARE``) are skipped. A group that holds
more than ``SPLIT_SHARE`` of the routes (and more than ``SPLIT_MIN`` routes)
(or more than ``BIG_GROUP`` routes) is split by the next segment, up to
``MAX_DEPTH`` segments: ``/projects``, ``/projects/tasks``… read as sections. Groups smaller than
``MIN_GROUP`` go to ``other``. Non-HTTP entrypoints (jobs, workers, tasks…)
are grouped by kind.

``bizmap.yaml`` can fix the groups (applied first; the rest is automatic)::

    export:
      domains_by: path            # or `folder`: by the handler's directory
      domains:
        api:                      # service name (`"*"`: every service)
          projects: [/projects, /v2/projects]
          billing: [/invoices, "folder:internal/billing/"]
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from typing import Any

GENERIC = frozenset({"api", "apis", "rest", "public", "internal", "private", "graphql", "rpc"})
_VERSION = re.compile(r"^v\d+(\.\d+)?$", re.I)
SPLIT_SHARE = 0.35
SPLIT_MIN = 30
DOMINANT_SHARE = 0.7
BIG_GROUP = 80  # a group this large is always split when its routes have a next segment
MAX_DEPTH = 3
MIN_GROUP = 3
SMALL_SERVICE = 12  # at most this many routes: one domain, no `other`
OTHER = "other"
ROUTES = "routes"
JOB_KINDS = {
    "scheduled_job": "jobs",
    "celery_task": "tasks",
    "cloud_task": "tasks",
    "worker": "workers",
    "pubsub": "events",
    "firestore": "events",
    "firebase_http": "functions",
    "firebase_call": "functions",
}


class DomainConfigError(ValueError):
    pass


def path_segments(path: str) -> list[str]:
    out = []
    for part in (path or "").split("?", 1)[0].split("/"):
        if not part or part.startswith((":", "{", "<", "*", "$", "[")):
            continue
        low = part.lower()
        if low in GENERIC or _VERSION.match(low):
            continue
        out.append(low)
    return out


def parse_domain_rules(raw: Any, source: str = "bizmap.yaml") -> dict[str, list[tuple[str, str, str]]]:
    """`export.domains` -> {service: [(kind 'path'|'folder', prefix, domain)]}, longest prefix first."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DomainConfigError(f"{source}: `export.domains` must map a service to {{domain: [prefixes]}}")
    out: dict[str, list[tuple[str, str, str]]] = {}
    for svc, domains in raw.items():
        if not isinstance(domains, dict):
            raise DomainConfigError(f"{source}: `export.domains.{svc}` must map a domain name to path prefixes")
        rules = []
        for name, prefixes in domains.items():
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            if not isinstance(prefixes, list) or not all(isinstance(p, str) and p.strip() for p in prefixes):
                raise DomainConfigError(
                    f"{source}: `export.domains.{svc}.{name}` must be a path prefix or a list of them"
                )
            for p in prefixes:
                p = p.strip()
                if p.startswith("folder:"):
                    rules.append(("folder", p[len("folder:"):].removeprefix("./"), str(name)))
                else:
                    rules.append(("path", "/" + p.strip("/").lower() if p.strip("/") else "/", str(name)))
        rules.sort(key=lambda r: (-len(r[1]), r[0], r[1], r[2]))
        out[str(svc)] = rules
    return out


def _path_match(path: str, prefix: str) -> bool:
    low = (path or "").lower().rstrip("/")
    return prefix == "/" or low == prefix or low.startswith(prefix + "/")


def _auto_groups(keys: dict[int, list[str]]) -> dict[int, str]:
    """Route index -> domain name, from each route's useful segments."""
    total = len(keys)
    if not total:
        return {}
    # skip the prefix every route shares (e.g. all under /admin)
    common = 0
    seqs = list(keys.values())
    while all(len(s) > common + 1 for s in seqs) and len({s[common] for s in seqs}) == 1:
        common += 1
    # a first segment most routes share (`/svc/...` on 80% of them) is skipped
    # too, for the routes that have it
    skip = {r: common for r in keys}
    firsts: dict[str, int] = defaultdict(int)
    for s in seqs:
        if len(s) > common + 1:
            firsts[s[common]] += 1
    if total >= SPLIT_MIN and firsts:
        seg, n = max(firsts.items(), key=lambda kv: (kv[1], kv[0]))
        if n >= DOMINANT_SHARE * total:
            for r, s in keys.items():
                if len(s) > common + 1 and s[common] == seg:
                    skip[r] = common + 1
    depth = {r: 1 for r in keys}

    def key(r: int) -> tuple[str, ...]:
        s = keys[r][skip[r]:]
        return tuple(s[: depth[r]])

    for _ in range(MAX_DEPTH):
        groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for r in keys:
            groups[key(r)].append(r)
        split = False
        for k, rs in groups.items():
            if (len(rs) > max(SPLIT_MIN, SPLIT_SHARE * total) or len(rs) > BIG_GROUP) and len(k) < MAX_DEPTH:
                deeper = {tuple(keys[r][skip[r]:][: len(k) + 1]) for r in rs}
                if len(deeper) > 1:
                    for r in rs:
                        depth[r] = len(k) + 1
                    split = True
        if not split:
            break
    if total <= SMALL_SERVICE:
        return {r: ROUTES for r in keys}
    # a section too small to stand alone goes back to its parent (`/projects/count`
    # stays in `/projects`); only top-level groups that stay small go to `other`
    final = {r: key(r) for r in keys}
    changed = True
    while changed:
        changed = False
        sizes: dict[tuple[str, ...], int] = defaultdict(int)
        for k in final.values():
            sizes[k] += 1
        for r, k in final.items():
            if len(k) > 1 and sizes[k] < MIN_GROUP:
                final[r] = k[:-1]
                changed = True
    groups = defaultdict(list)
    for r, k in final.items():
        groups[k].append(r)
    out = {}
    for k, rs in groups.items():
        name = "/" + "/".join(k) if k and len(rs) >= MIN_GROUP else OTHER
        for r in rs:
            out[r] = name
    return out


def group_entrypoints(
    routes: list[tuple[int, dict[str, Any], str | None]],
    service: str,
    rules: dict[str, list[tuple[str, str, str]]] | None = None,
    by: str = "path",
    tags: dict[int, str] | None = None,
) -> dict[int, tuple[str, str]]:
    """[(node index, node, handler file)] -> {index: (domain name, rule)}.

    rule is `yaml`, `tag`, `path`, `folder`, `kind` or `other`. `tags` (route
    index -> first OpenAPI/swag tag) wins over automatic grouping.
    """
    mine = (rules or {}).get(service, []) + (rules or {}).get("*", [])
    out: dict[int, tuple[str, str]] = {}
    auto_http: dict[int, list[str]] = {}
    tagged: dict[str, list[int]] = defaultdict(list)
    for idx, node, handler_file in routes:
        hit = None
        for kind, prefix, name in mine:
            if kind == "path" and node.get("kind") == "http" and _path_match(str(node.get("path") or ""), prefix):
                hit = name
            elif kind == "folder" and handler_file and handler_file.startswith(prefix):
                hit = name
            if hit:
                break
        if hit:
            out[idx] = (hit, "yaml")
            continue
        if tags and tags.get(idx):
            tagged[tags[idx]].append(idx)  # the API's own grouping (swag @Tags / OpenAPI tags)
            continue
        if node.get("kind") != "http":
            out[idx] = (JOB_KINDS.get(str(node.get("kind")), str(node.get("kind") or "jobs")), "kind")
            continue
        if by == "folder" and handler_file:
            auto_http[idx] = [p for p in posixpath.dirname(handler_file).lower().split("/") if p]
        else:
            auto_http[idx] = path_segments(str(node.get("path") or ""))
    rule = "folder" if by == "folder" else "path"
    for idx, name in _auto_groups(auto_http).items():
        out[idx] = (name, OTHER if name == OTHER else rule)
    # a path group named like a tag joins it (`/projects` routes without docs go with tag `projects`)
    tag_names = {t.lower(): t for t in tagged}
    for idx, (name, rule_) in list(out.items()):
        if rule_ in ("path", "folder", OTHER) and idx in auto_http:
            segs = auto_http[idx]
            head = name.strip("/").split("/")[0].lower() if rule_ != OTHER else (segs[0] if segs else "")
            if head in tag_names:
                tagged[tag_names[head]].append(idx)
                del out[idx]
    by_idx = {idx: node for idx, node, _f in routes}
    for tag, idxs in tagged.items():
        if len(idxs) <= BIG_GROUP:
            for idx in idxs:
                out[idx] = (tag, "tag")
            continue
        # a very large tag reads better in sections: the tag, then its path groups
        sub = _auto_groups({idx: path_segments(str(by_idx[idx].get("path") or "")) for idx in idxs})
        for idx in idxs:
            name = sub[idx]
            out[idx] = (tag if name in (OTHER, ROUTES) else f"{tag} · {name.lstrip('/')}", "tag")
    return out
