from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_RULES_DIR = Path(__file__).resolve().parent.parent / "rules"


@dataclass
class Rule:
    id: str
    language: str
    framework: str | None = None
    description: str = ""
    status: str = "active"  # active | stub
    when: dict[str, Any] = field(default_factory=dict)
    detect: dict[str, Any] = field(default_factory=dict)
    produce: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_stub(self) -> bool:
        return self.status.lower() in {"stub", "example", "disabled"}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def load_rule_file(path: Path) -> list[Rule]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(data, list):
        items = data
    elif "rules" in data:
        items = data["rules"]
    else:
        items = [data]
    rules: list[Rule] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or path.stem)
        rules.append(
            Rule(
                id=rid,
                language=str(item.get("language") or "").lower(),
                framework=item.get("framework"),
                description=str(item.get("description") or ""),
                status=str(item.get("status") or "active"),
                when=item.get("when") or {},
                detect=item.get("detect") or {},
                produce=item.get("produce") or {},
                options=item.get("options") or {},
                raw=item,
            )
        )
    return rules


def load_rules(extra_dirs: list[Path] | None = None) -> list[Rule]:
    paths: list[Path] = []
    if PACKAGE_RULES_DIR.is_dir():
        paths.extend(sorted(PACKAGE_RULES_DIR.glob("*.yaml")))
        paths.extend(sorted(PACKAGE_RULES_DIR.glob("*.yml")))
    for directory in extra_dirs or []:
        if directory.is_dir():
            paths.extend(sorted(directory.glob("*.yaml")))
            paths.extend(sorted(directory.glob("*.yml")))
        elif directory.is_file():
            paths.append(directory)
    seen: set[str] = set()
    rules: list[Rule] = []
    for path in paths:
        for rule in load_rule_file(path):
            if rule.id in seen:
                # later files (user --rules) override bundled ones
                rules = [r for r in rules if r.id != rule.id]
            seen.add(rule.id)
            rules.append(rule)
    return rules


def method_matches(name: str, spec: dict[str, Any] | None) -> bool:
    if not spec:
        return True
    name_u = name
    allowed = [str(x) for x in _as_list(spec.get("in"))]
    if allowed and name_u not in allowed and name_u.lower() not in [a.lower() for a in allowed]:
        return False
    regex = spec.get("regex")
    if regex:
        import re

        if not re.search(regex, name_u):
            return False
    return True


def import_matches(imports: list[str], when: dict[str, Any]) -> bool:
    spec = when.get("imports") if when else None
    if not spec:
        return True
    needles = [str(x) for x in _as_list(spec.get("contains") or spec.get("contains_any"))]
    if not needles:
        return True
    matched = any(any(n in imp for imp in imports) for n in needles)
    if spec.get("optional"):
        return True
    return matched
