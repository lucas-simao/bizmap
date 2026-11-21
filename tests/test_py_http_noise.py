"""Round 44: Python HTTP calls. Tests are skipped by default, a `post` helper
defined in a test never captures `httpx.post` elsewhere, TestClient /
ASGITransport calls are in-process (not unresolved, no calls_http), labels
never carry source code, and `CONST.format(x)` is a template."""

from __future__ import annotations

import re
from pathlib import Path

from click.testing import CliRunner

from bizmap.cli import main, scan_summary
from bizmap.scan import scan_path

MAIN = '''from fastapi import APIRouter, FastAPI

router = APIRouter(prefix="/internal")


@router.post("/orders/{order_id}/confirm")
def confirm(order_id: str):
    return {}


@router.post("/orders/{order_id}/ship")
def ship(order_id: str):
    return {}


@router.post("/jobs/flush")
def flush():
    return {}


def create_app():
    app = FastAPI()
    app.include_router(router)
    return app
'''

NOTIFY = '''import os

import httpx

HOOKS = os.environ["HOOKS_URL"]


def notify(order_id: str):
    return httpx.post(f"{HOOKS}/hooks/orders/{order_id}", json={})


def flush_self(client: httpx.Client):
    return client.post("/internal/jobs/flush")
'''

SUPPORT = '''import httpx

from app.main import create_app


def build_app():
    app = create_app()
    ctx = {"clock": None}
    return app, ctx


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shop.test")


async def post(app, path, body):
    async with client_for(app) as client:
        return await client.post(path, json=body)
'''

TEST_ORDERS = '''from fastapi.testclient import TestClient

from app.main import create_app
from tests.support import build_app, post

CONFIRM = "/internal/orders/{}/confirm"
api = TestClient(create_app())


def test_ship():
    assert api.post("/internal/orders/o-1/ship").status_code == 200


def test_confirm():
    oid = api.post("/internal/orders/o-1/ship").json()["orderId"]
    assert api.post(CONFIRM.format(oid)).status_code == 200


async def test_flush_in_process():
    app, ctx = build_app()
    r = await post(app, "/internal/jobs/flush", {"a": 1})
    assert r.status_code == 200


def _post(client, order_id):
    return client.post(f"/internal/orders/{order_id}/ship")


def test_verb_named_wrapper():
    assert _post(TestClient(create_app()), "o-3").status_code == 200
'''


def _shop(root: Path, *, support: bool = True, include_tests: bool = False) -> Path:
    files = {"app/__init__.py": "", "app/main.py": MAIN, "app/notify.py": NOTIFY,
             "tests/__init__.py": "", "tests/test_orders.py": TEST_ORDERS}
    if support:
        files["tests/support.py"] = SUPPORT
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    if include_tests:
        (root / "bizmap.yaml").write_text("scan:\n  include_tests: true\n", encoding="utf-8")
    return root


def _unresolved(graph) -> list[tuple[str, str, str]]:
    return sorted((u["method"], u["url"], u["reason"]) for u in graph.attrs.get("unresolved_http") or [])


def _links(graph) -> set[tuple[str, str]]:
    return {(e.from_id, e.to_id) for e in graph.edges.values() if e.type == "calls_http"}


HOOKS = ("POST", "{HOOKS_URL}/hooks/orders/{order_id}", "target_not_scanned")
FLUSH = ("function:app.notify.flush_self", "entrypoint:http:POST:/internal/jobs/flush")


def test_default_skips_tests(tmp_path: Path) -> None:
    graph = scan_path(_shop(tmp_path / "shop"))
    assert _unresolved(graph) == [HOOKS]
    assert _links(graph) == {FLUSH}
    assert not any("tests" in n.id for n in graph.nodes.values())
    assert graph.attrs["skipped"]["py_test_dirs"] == 1
    text = "\n".join(scan_summary(graph))
    assert "1 Python test dirs" in text
    assert "1 unresolved HTTP calls: target_not_scanned=1" in text


def test_a_test_helper_does_not_change_production_labels(tmp_path: Path) -> None:
    with_support = scan_path(_shop(tmp_path / "a" / "shop", include_tests=True))
    without = scan_path(_shop(tmp_path / "b" / "shop", support=False, include_tests=True))
    for graph in (with_support, without):
        notify = [u for u in graph.attrs.get("unresolved_http") or [] if u["from"].endswith("app.notify.notify")]
        assert [(u["method"], u["url"]) for u in notify] == [HOOKS[:2]], notify
        assert FLUSH in _links(graph)


def test_in_process_calls_are_tests_not_services(tmp_path: Path) -> None:
    graph = scan_path(_shop(tmp_path / "shop", include_tests=True))
    assert _unresolved(graph) == [HOOKS], _unresolved(graph)
    assert _links(graph) == {FLUSH}, _links(graph)  # no test → route edges
    assert graph.attrs["in_process_http"] >= 5
    text = "\n".join(scan_summary(graph))
    assert "in-process test calls:" in text


def test_include_tests_flag(tmp_path: Path) -> None:
    root = _shop(tmp_path / "shop")
    out = tmp_path / "g.json"
    res = CliRunner().invoke(main, ["scan", str(root), "--include-tests", "--out", str(out), "-q"])
    assert res.exit_code == 0, res.output
    assert "in-process test calls:" in res.output
    assert "Python test" not in res.output


def test_labels_never_carry_code(tmp_path: Path) -> None:
    root = _shop(tmp_path / "shop", include_tests=True)
    (root / "app" / "odd.py").write_text(
        'import httpx\n\nBASE = "http://x"\n\n'
        "def report(now):\n"
        "    return Report(\n        generated_at=now,\n        rows=[],\n    )\n\n"
        "def send(oid):\n"
        '    httpx.get(f"{BASE}/a/{report(oid)}")\n'
        '    httpx.post(f"{BASE}/b/{1 << 20}/{oid}")\n'
        '    httpx.post(f"{BASE}/c/{(lambda: 1)()}")\n',
        encoding="utf-8",
    )
    graph = scan_path(root)
    urls = [u["url"] for u in graph.attrs.get("unresolved_http") or []]
    for node in graph.nodes.values():
        for call in node.attrs.get("http_calls") or []:
            urls.append(call["url"])
    assert urls
    for url in urls:
        assert not re.search(r"[(),\"'\n<>*+]", url), url


def test_format_template(tmp_path: Path) -> None:
    from bizmap.detect.py_resolve import ImportMap, resolve_expr
    from bizmap.detect.python_lang import _parser

    src = b'CONFIRM = "/internal/orders/{}/confirm"\nx = CONFIRM.format(oid)\ny = "/a/{n}/b".format(n=item.id)\n'
    tree = _parser().parse(src).root_node
    consts = {"CONFIRM": resolve_expr(tree.named_children[0].named_children[0].child_by_field_name("right"), {}, {})}
    x = tree.named_children[1].named_children[0].child_by_field_name("right")
    y = tree.named_children[2].named_children[0].child_by_field_name("right")
    assert resolve_expr(x, {}, consts, ImportMap()).path_text == "/internal/orders/{oid}/confirm"
    assert resolve_expr(y, {}, {}, ImportMap()).path_text == "/a/{id}/b"
