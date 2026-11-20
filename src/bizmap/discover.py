"""Find the projects in a work folder full of clones and worktrees.

`bizmap scan --discover DIR` (and `bizmap discover DIR`, which only lists):

1. find git repositories under ``DIR`` (up to ``depth`` levels, never inside
   dependency/build dirs, never inside another repository);
2. group copies of the same repository: worktrees sharing a git dir, and
   clones with the same normalized ``origin`` URL;
3. pick one representative per group, deterministically:
   preferred (``prefer``) > main checkout (not a worktree) > on the default
   branch > folder named like the repository > most recent commit (``git
   log``; without git, the commit object or the ref file's mtime) > path order;
4. leave out groups with no supported source code.

Git metadata is read from the repository files (`.git`, `config`, `HEAD`,
`commondir`, refs); git itself is only run for commit times.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bizmap.files import GENERATED_DIR_NAMES, SKIP_DIR_NAMES, path_excluded

CODE_SUFFIXES = {".go", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".sql"}
DEFAULT_DEPTH = 2
_NO_DESCEND = SKIP_DIR_NAMES | GENERATED_DIR_NAMES


@dataclass
class RepoInfo:
    path: Path
    rel: str
    kind: str  # "main" (checkout with a .git dir), "worktree", or "folder" (no git, included by hand)
    git_dir: Path | None = None
    common_dir: Path | None = None
    origin: str | None = None  # normalized
    origin_url: str | None = None  # as written in the config
    branch: str | None = None  # None = detached HEAD
    default_branch: str | None = None
    commit_time: int | None = None
    time_source: str | None = None  # "git", "commit-file" or "ref-mtime"
    has_code: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.rel,
            "kind": self.kind,
            "origin": self.origin,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "commit_time": self.commit_time,
            "time_source": self.time_source,
            "has_code": self.has_code,
        }


@dataclass
class RepoGroup:
    name: str
    key: str
    members: list[RepoInfo]
    chosen: RepoInfo | None
    criterion: str  # what decided: only_copy, preferred, main_checkout, default_branch, folder_name, latest_commit, path_order, no_code
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "key": self.key,
            "chosen": self.chosen.rel if self.chosen else None,
            "criterion": self.criterion,
            "reason": self.reason,
            "members": [m.to_dict() for m in self.members],
        }


@dataclass
class Discovery:
    root: Path
    depth: int
    groups: list[RepoGroup] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    warnings: list[str] = field(default_factory=list)

    @property
    def chosen(self) -> list[RepoGroup]:
        return [g for g in self.groups if g.chosen is not None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "depth": self.depth,
            "groups": [g.to_dict() for g in self.groups],
            "skipped": [{"path": p, "reason": r} for p, r in self.skipped],
            "warnings": list(self.warnings),
        }


# ── git metadata (read from files) ───────────────────────────────────────────


def normalize_origin(url: str, base: Path | None = None) -> str:
    """`https://user@GitHub.com/Org/Repo.git/`, `git@github.com:Org/Repo` and
    `ssh://git@github.com:22/Org/Repo.git` all become `github.com/org/repo`;
    a local path becomes `file:<absolute path>`."""
    raw = url.strip()
    m = re.match(r"^(?:[\w+.-]+)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", raw)
    if m and not raw.startswith("file://"):
        host, rest = m.group(1), m.group(2)
    else:
        m = re.match(r"^(?:[^@/]+@)?([^/:]+):(?!/)(.+)$", raw)  # scp-like git@host:owner/repo
        if m and not re.match(r"^[A-Za-z]:[\\/]", raw):
            host, rest = m.group(1), m.group(2)
        else:
            path = raw.removeprefix("file://")
            p = Path(path)
            if not p.is_absolute() and base is not None:
                p = base / p
            try:
                p = p.resolve()
            except OSError:
                pass
            return "file:" + p.as_posix().removesuffix(".git").rstrip("/")
    rest = rest.strip("/").removesuffix(".git").strip("/")
    return f"{host.lower()}/{rest.lower()}"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _git_dirs(repo: Path) -> tuple[Path, Path, str] | None:
    """(git dir, common dir, kind) for a working tree, from its `.git`."""
    dot = repo / ".git"
    if dot.is_dir():
        common_txt = _read(dot / "commondir")
        common = (dot / common_txt.strip()).resolve() if common_txt else dot.resolve()
        return dot.resolve(), common, "main"
    if dot.is_file():
        text = _read(dot) or ""
        m = re.match(r"gitdir:\s*(.+)", text.strip())
        if not m:
            return None
        git_dir = Path(m.group(1).strip())
        if not git_dir.is_absolute():
            git_dir = repo / git_dir
        git_dir = git_dir.resolve()
        common_txt = _read(git_dir / "commondir")
        common = (git_dir / common_txt.strip()).resolve() if common_txt else git_dir
        # a submodule's git dir has no commondir: it is its own repository
        kind = "worktree" if common_txt else "main"
        return git_dir, common, kind
    return None


def _origin_url(common: Path) -> str | None:
    text = _read(common / "config") or ""
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if section and re.match(r'\[remote\s+"origin"\]', section):
            m = re.match(r"url\s*=\s*(.+)", stripped)
            if m:
                return m.group(1).strip()
    return None


def _head_branch(git_dir: Path) -> str | None:
    text = (_read(git_dir / "HEAD") or "").strip()
    if text.startswith("ref:"):
        return text[4:].strip().removeprefix("refs/heads/")
    return None


def _default_branch(common: Path) -> str | None:
    text = (_read(common / "refs" / "remotes" / "origin" / "HEAD") or "").strip()
    if text.startswith("ref:"):
        return text[4:].strip().removeprefix("refs/remotes/origin/")
    return None


def _ref_sha(git_dir: Path, common: Path, branch: str | None) -> str | None:
    """The commit HEAD points at, from the ref files (loose or packed)."""
    if branch is None:
        text = (_read(git_dir / "HEAD") or "").strip()
        return text if re.fullmatch(r"[0-9a-f]{40,64}", text) else None
    loose = (_read(common / "refs" / "heads" / branch) or "").strip()
    if re.fullmatch(r"[0-9a-f]{40,64}", loose):
        return loose
    for line in (_read(common / "packed-refs") or "").splitlines():
        sha, _, ref = line.partition(" ")
        if ref.strip() == f"refs/heads/{branch}" and re.fullmatch(r"[0-9a-f]{40,64}", sha):
            return sha
    return None


def _commit_file_time(common: Path, sha: str) -> int | None:
    """Committer time read from a loose commit object (packed objects: None)."""
    path = common / "objects" / sha[:2] / sha[2:]
    try:
        raw = zlib.decompress(path.read_bytes())
    except (OSError, zlib.error):
        return None
    header, _, body = raw.partition(b"\0")
    if not header.startswith(b"commit "):
        return None
    for line in body.split(b"\n"):
        if not line:
            break
        if line.startswith(b"committer "):
            m = re.search(rb" (\d+) [+-]\d{4}$", line)
            return int(m.group(1)) if m else None
    return None


def _commit_time(repo: Path, git_dir: Path, common: Path, branch: str | None) -> tuple[int | None, str | None]:
    if shutil.which("git"):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
            if out.returncode == 0 and out.stdout.strip().isdigit():
                return int(out.stdout.strip()), "git"
        except (OSError, subprocess.SubprocessError):
            pass
    sha = _ref_sha(git_dir, common, branch)
    if sha:
        when = _commit_file_time(common, sha)
        if when is not None:
            return when, "commit-file"
    candidates = []
    if branch:
        candidates.append(common / "refs" / "heads" / branch)
    # a packed branch ref was last written with packed-refs, not with HEAD
    candidates += [common / "packed-refs", git_dir / "HEAD"] if branch else [git_dir / "HEAD", common / "packed-refs"]
    for path in candidates:
        try:
            return int(path.stat().st_mtime), "ref-mtime"
        except OSError:
            continue
    return None, None


def _has_code(path: Path) -> bool:
    for _dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if d not in _NO_DESCEND)
        if any(Path(f).suffix.lower() in CODE_SUFFIXES for f in filenames):
            return True
    return False


def inspect_repo(path: Path, root: Path) -> RepoInfo:
    rel = path.relative_to(root).as_posix() if path != root else "."
    dirs = _git_dirs(path)
    if dirs is None:
        return RepoInfo(path=path, rel=rel, kind="folder", has_code=_has_code(path))
    git_dir, common, kind = dirs
    url = _origin_url(common)
    branch = _head_branch(git_dir)
    when, source = _commit_time(path, git_dir, common, branch)
    return RepoInfo(
        path=path,
        rel=rel,
        kind=kind,
        git_dir=git_dir,
        common_dir=common,
        origin=normalize_origin(url, base=path) if url else None,
        origin_url=url,
        branch=branch,
        default_branch=_default_branch(common),
        commit_time=when,
        time_source=source,
        has_code=_has_code(path),
    )


# ── discovery ────────────────────────────────────────────────────────────────


def find_repos(
    root: Path, depth: int, include: list[str], exclude: list[str]
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Git working trees under ``root`` (and folders matching ``include``),
    plus what was left out and why."""
    root = root.resolve()
    found: list[Path] = []
    skipped: list[tuple[str, str]] = []
    if (root / ".git").exists():
        return [root], skipped
    plain: list[Path] = []  # folders looked at that are not repositories
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, level = stack.pop()
        try:
            entries = sorted(
                (e for e in os.scandir(cur) if e.is_dir(follow_symlinks=False)), key=lambda e: e.name
            )
        except OSError:
            continue
        for entry in reversed(entries):  # stack: keep alphabetical order
            child = Path(entry.path)
            rel = child.relative_to(root).as_posix()
            if entry.name in _NO_DESCEND or entry.name.startswith("."):
                continue
            if exclude and path_excluded(rel, exclude):
                skipped.append((rel, "excluded"))
                continue
            if (child / ".git").exists():
                found.append(child)
                continue
            if include and path_excluded(rel, include):
                found.append(child)
                continue
            plain.append(child)
            if level + 1 < depth:
                stack.append((child, level + 1))
    # folders with code but no git, so `--list` can say why they are missing:
    # the outermost ones that hold no repository found below them
    holders = {parent for f in found for parent in f.parents}
    for folder in plain:
        if folder in holders or not (folder.parent == root or folder.parent in holders):
            continue
        if _has_code(folder):
            rel = folder.relative_to(root).as_posix()
            skipped.append((rel, f"no git; add with --include {rel}"))
    return sorted(found, key=lambda p: p.relative_to(root).as_posix()), skipped


def _tie_reason(m: RepoInfo) -> str:
    if m.time_source == "ref-mtime":
        return "first by path (tie: without git, commit times could not be told apart)"
    return "first by path (tie)"


def _named_like_repo(m: RepoInfo) -> bool:
    if not m.origin:
        return False
    name = m.origin.removeprefix("file:").rstrip("/").rsplit("/", 1)[-1]
    return m.path.name.lower() == name.lower()


def _choose(members: list[RepoInfo], prefer: list[str]) -> tuple[RepoInfo, str, str]:
    """(chosen, criterion, reason). Members are sorted by `rel` already."""
    if len(members) == 1:
        return members[0], "only_copy", "only copy"
    preferred = [m for m in members if prefer and (m.rel in prefer or path_excluded(m.rel, prefer))]
    if preferred:
        return preferred[0], "preferred", f"preferred ({preferred[0].rel})"

    def default_of(m: RepoInfo) -> set[str]:
        return {m.default_branch} if m.default_branch else {"main", "master"}

    criteria = [
        ("main_checkout", lambda m: 0 if m.kind == "main" else 1, lambda m: "main checkout (others are worktrees)"),
        (
            "default_branch",
            lambda m: 0 if m.branch and m.branch in default_of(m) else 1,
            lambda m: f"on the default branch ({m.branch})",
        ),
        (
            "folder_name",
            lambda m: 0 if _named_like_repo(m) else 1,
            lambda m: f"folder named like the repository ({m.path.name})",
        ),
        (
            "latest_commit",
            lambda m: -(m.commit_time or 0),
            lambda m: f"most recent commit ({m.time_source or 'unknown'})",
        ),
        ("path_order", lambda m: m.rel, _tie_reason),
    ]
    ranked = sorted(members, key=lambda m: tuple(c[1](m) for c in criteria))
    best, second = ranked[0], ranked[1]
    for name, key, explain in criteria:
        if key(best) != key(second):
            return best, name, explain(best)
    return best, "path_order", _tie_reason(best)


def _repo_name(group: list[RepoInfo]) -> str:
    for m in group:
        if m.origin and not m.origin.startswith("file:"):
            return m.origin.rsplit("/", 1)[-1]
    for m in group:
        if m.origin:
            return Path(m.origin.removeprefix("file:")).name
    return group[0].path.name


def discover(
    root: Path,
    *,
    depth: int = DEFAULT_DEPTH,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    prefer: list[str] | None = None,
) -> Discovery:
    root = root.resolve()
    include = list(include or [])
    exclude = list(exclude or [])
    prefer = [p.strip("/") for p in (prefer or [])]
    paths, skipped = find_repos(root, depth, include, exclude)
    infos = [inspect_repo(p, root) for p in paths]
    warnings = [
        f"--prefer {pat!r} matches none of the folders found"
        for pat in prefer
        if not any(i.rel == pat or path_excluded(i.rel, [pat]) for i in infos)
    ]

    # union-find over shared git dirs and origins
    parent = list(range(len(infos)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_key: dict[str, int] = {}
    for i, info in enumerate(infos):
        keys = []
        if info.common_dir is not None:
            keys.append("git:" + info.common_dir.as_posix())
        if info.origin:
            keys.append("origin:" + info.origin)
        for key in keys:
            if key in by_key:
                parent[find(i)] = find(by_key[key])
            else:
                by_key[key] = i
    clusters: dict[int, list[RepoInfo]] = {}
    for i, info in enumerate(infos):
        clusters.setdefault(find(i), []).append(info)

    groups: list[RepoGroup] = []
    for members in clusters.values():
        members.sort(key=lambda m: m.rel)
        key = next((f"origin:{m.origin}" for m in members if m.origin), None) or f"path:{members[0].rel}"
        coded = [m for m in members if m.has_code]
        if not coded:
            groups.append(RepoGroup(_repo_name(members), key, members, None, "no_code", "no supported source code"))
            continue
        chosen, criterion, reason = _choose(coded, prefer)
        groups.append(RepoGroup(_repo_name(members), key, members, chosen, criterion, reason))
    groups.sort(key=lambda g: (g.name, g.key))
    seen: dict[str, int] = {}
    for g in groups:  # unique, deterministic names
        n = seen.get(g.name, 0) + 1
        seen[g.name] = n
        if n > 1:
            g.name = f"{g.name}-{n}"
    return Discovery(root=root, depth=depth, groups=groups, skipped=sorted(skipped), warnings=warnings)


def format_discovery(d: Discovery) -> str:
    lines = [f"{d.root}  (depth {d.depth})", ""]
    if not d.groups:
        lines.append("no git repositories found")
    for g in d.groups:
        head = f"{g.name}: " + (f"{g.chosen.rel}  [{g.criterion}: {g.reason}]" if g.chosen else f"skipped ({g.reason})")
        lines.append(head)
        for m in g.members:
            mark = "*" if g.chosen is m else " "
            branch = "no git" if m.kind == "folder" else (m.branch or "(detached)")
            bits = [m.kind, branch]
            if m.commit_time is not None:
                bits.append(f"t={m.commit_time} ({m.time_source})")
            if not m.has_code:
                bits.append("no code")
            lines.append(f"  {mark} {m.rel:<40} {' · '.join(bits)}")
    if d.skipped:
        lines += ["", "left out:"]
        lines += [f"  {p}  ({r})" for p, r in d.skipped]
    if d.warnings:
        lines += [""] + [f"warning: {w}" for w in d.warnings]
    chosen = d.chosen
    if chosen:
        lines += ["", f"{len(chosen)} project(s) to scan: " + ", ".join(g.chosen.rel for g in chosen if g.chosen)]
    else:
        lines += ["", "nothing to scan"]
    return "\n".join(lines) + "\n"


def load_discover_settings(root: Path, config: Path | None = None) -> dict[str, Any]:
    """The ``discover:`` section of the bizmap.yaml in ``root`` (or ``config``):
    ``depth``, ``include``, ``exclude``, ``prefer``."""
    import yaml

    from bizmap.overrides import find_overrides

    path = find_overrides(root, config=config)
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    section = data.get("discover") if isinstance(data, dict) else None
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"{path}: `discover:` must be a mapping (depth, include, exclude, prefer)")
    out: dict[str, Any] = {}
    if "depth" in section:
        depth = section["depth"]
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
            raise ValueError(f"{path}: discover.depth must be a positive integer, got {depth!r}")
        out["depth"] = depth
    for key in ("include", "exclude", "prefer"):
        value = section.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{path}: discover.{key} must be a list of paths/globs")
        out[key] = value
    unknown = sorted(set(section) - {"depth", "include", "exclude", "prefer"})
    if unknown:
        raise ValueError(f"{path}: unknown discover key(s): {', '.join(unknown)}")
    return out
