"""Every form control of the HTML export shares one look (the `.ctl` class)."""

from __future__ import annotations

import re
from pathlib import Path

EXPORT = Path(__file__).resolve().parents[1] / "src" / "bizmap" / "export"
# small pills with a look of their own: path chips, tabs, checkboxes inside the details menu
EXEMPT = re.compile(r'type="checkbox"|class="(?:via|chip|stab)\b')


def _controls() -> list[tuple[str, str]]:
    out = []
    for f in sorted([*EXPORT.glob("*.js"), EXPORT / "template.html"]):
        for m in re.finditer(r"<(?:input|select|button|textarea)\b[^>]*>", f.read_text(encoding="utf-8")):
            out.append((f.name, m.group(0)))
    return out


def test_every_input_select_and_button_uses_the_shared_class() -> None:
    controls = _controls()
    assert len(controls) >= 15, controls
    missing = [(f, tag) for f, tag in controls if not EXEMPT.search(tag) and not re.search(r'class="ctl\b', tag)]
    assert not missing, missing


def test_shared_control_style_is_inline_and_uniform() -> None:
    css = (EXPORT / "template.html").read_text(encoding="utf-8")
    assert "--ctl-h: 32px" in css and "--ring:" in css
    rule = re.search(r"\n  \.ctl \{([^}]*)\}", css).group(1)
    for decl in ("height: var(--ctl-h)", "border: 1px solid var(--line)", "border-radius: var(--ctl-r)", "font: inherit"):
        assert decl in rule, (decl, rule)
    sel = re.search(r"select\.ctl \{([^}]*)\}", css).group(1)
    assert "appearance: none" in sel and "data:image/svg+xml" in sel, sel
    assert re.search(r"\.ctl:focus[^{]*\{[^}]*box-shadow: var\(--ring\)", css)
    assert '<summary class="ctl">' in css  # "Show details"
    assert '<input id="q" class="ctl"' in css  # top search
    assert "http://" not in re.sub(r"xmlns='http://www\.w3\.org/2000/svg'", "", css.split("<style>")[1].split("</style>")[0])
    home = (EXPORT / "home.js").read_text(encoding="utf-8")
    assert 'class="ctl ctl-lg ask-in"' in home and '<div class="bar mx-bar">' in home
