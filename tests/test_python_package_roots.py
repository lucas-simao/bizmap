"""Python module names follow package roots (src/ layouts, monorepos)."""

from __future__ import annotations

from pathlib import Path

from bizmap.graph import function_id
from bizmap.scan import scan_path


def _write(root: Path, rel: str, source: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _reach(graph, frm: str) -> set[str]:
    return {
        e.to_id.split(".")[0].replace("column:", "table:")
        for e in graph.edges.values()
        if e.type in {"reads", "writes"} and e.from_id == frm
    }


ASSET = """
from sqlalchemy import text


def bind_id(conn, asset_id):
    conn.execute(text("UPDATE assets SET bound = true WHERE id = :id"), {"id": asset_id})
"""

API = """
from fastapi import FastAPI

from reports.layers import asset

app = FastAPI()


@app.post("/assets/{asset_id}/bind")
def bind(asset_id: str, conn=None):
    return asset.bind_id(conn, asset_id)
"""


def test_py_src_layout_module_qualified(tmp_path: Path) -> None:
    """The evaluation repro: package in services/reports/src/reports, scanned from the repo root."""
    _write(tmp_path, "schema.sql", "CREATE TABLE assets (id text, bound boolean);\n")
    svc = "services/reports"
    _write(tmp_path, f"{svc}/pyproject.toml", '[project]\nname = "reports"\n')
    _write(tmp_path, f"{svc}/src/reports/__init__.py")
    _write(tmp_path, f"{svc}/src/reports/layers/__init__.py")
    _write(tmp_path, f"{svc}/src/reports/layers/asset.py", ASSET)
    _write(tmp_path, f"{svc}/src/reports/api.py", API)
    graph = scan_path(tmp_path)
    assert function_id("reports.layers.asset.bind_id") in graph.nodes
    assert "table:assets" in _reach(graph, "entrypoint:http:POST:/assets/{asset_id}/bind")
    node = graph.get(function_id("reports.api.bind"))
    assert "reports.layers.asset.bind_id" not in (node.attrs.get("unresolved_calls") or [])


def test_namespace_package_under_declared_src_root(tmp_path: Path) -> None:
    """No __init__.py: `src/` next to pyproject.toml is the root."""
    _write(tmp_path, "schema.sql", "CREATE TABLE assets (id text, bound boolean);\n")
    _write(tmp_path, "svc/pyproject.toml", '[tool.setuptools.packages.find]\nwhere = ["src"]\n')
    _write(tmp_path, "svc/src/reports/layers/asset.py", ASSET)
    _write(tmp_path, "svc/src/reports/api.py", API)
    graph = scan_path(tmp_path)
    assert "table:assets" in _reach(graph, "entrypoint:http:POST:/assets/{asset_id}/bind")


def test_colliding_package_names_fall_back_to_paths(tmp_path: Path) -> None:
    """Two services both ship package `common`: ids must stay distinct."""
    for svc in ("a", "b"):
        _write(tmp_path, f"services/{svc}/pyproject.toml", "[project]\n")
        _write(tmp_path, f"services/{svc}/src/common/__init__.py")
        _write(tmp_path, f"services/{svc}/src/common/util.py", "def helper():\n    return 1\n")
    graph = scan_path(tmp_path)
    ids = {n.id for n in graph.nodes.values() if n.id.endswith("common.util.helper")}
    assert ids == {
        function_id("services.a.src.common.util.helper"),
        function_id("services.b.src.common.util.helper"),
    }, ids


def test_nested_functions_with_same_name_get_distinct_ids(tmp_path: Path) -> None:
    _write(tmp_path, "schema.sql", "CREATE TABLE a (x int);\nCREATE TABLE b (y int);\n")
    _write(
        tmp_path,
        "panels.py",
        """
from sqlalchemy import text


def sales(conn):
    def compute():
        return conn.execute(text("SELECT x FROM a"))

    return compute()


def stock(conn):
    def compute():
        return conn.execute(text("SELECT y FROM b"))

    return compute()
""",
    )
    graph = scan_path(tmp_path)
    assert function_id("panels.sales.compute") in graph.nodes
    assert function_id("panels.stock.compute") in graph.nodes
    assert "table:a" in _reach(graph, function_id("panels.sales.compute"))
    assert "table:b" not in _reach(graph, function_id("panels.sales.compute"))
    assert "table:b" in _reach(graph, function_id("panels.stock.compute"))


def test_py_namespace_package_partial_init(tmp_path: Path) -> None:
    """Evaluation repro: src/pipeline/ has NO __init__.py (PEP 420), but
    src/pipeline/render/ has one. `from pipeline.render._utils import
    build_body` must resolve (no pyproject needed: `src/` is a source root)."""
    _write(tmp_path, "schema.sql", "CREATE TABLE bodies (id text, body text);\n")
    _write(tmp_path, "services/render/src/pipeline/render/__init__.py")
    _write(
        tmp_path,
        "services/render/src/pipeline/render/_utils.py",
        """
from sqlalchemy import text


def build_body(conn, body_id):
    return conn.execute(text("SELECT id, body FROM bodies WHERE id = :i"), {"i": body_id})
""",
    )
    _write(
        tmp_path,
        "services/render/src/pipeline/api.py",
        """
from fastapi import FastAPI

from pipeline.render._utils import build_body

app = FastAPI()


@app.get("/bodies/{body_id}")
def get_body(body_id: str, conn=None):
    return build_body(conn, body_id)
""",
    )
    graph = scan_path(tmp_path)
    assert function_id("pipeline.render._utils.build_body") in graph.nodes
    assert "table:bodies" in _reach(graph, "entrypoint:http:GET:/bodies/{body_id}")
    node = graph.get(function_id("pipeline.api.get_body"))
    assert not (node.attrs.get("unresolved_calls") or []), node.attrs.get("unresolved_calls")


def test_import_prefix_outside_scan_root_resolves_by_unique_suffix(tmp_path: Path) -> None:
    """Scanning `backend/` while the code imports `backend.app.repo`."""
    root = tmp_path / "backend"
    _write(root, "schema.sql", "CREATE TABLE notes (id text);\n")
    _write(
        root,
        "app/repo.py",
        """
from sqlalchemy import text


def load_notes(conn):
    return conn.execute(text("SELECT id FROM notes"))
""",
    )
    _write(
        root,
        "app/api.py",
        """
from fastapi import FastAPI

from backend.app import repo
from backend.app.repo import load_notes

app = FastAPI()


@app.get("/notes")
def notes(conn=None):
    return repo.load_notes(conn)


@app.get("/notes2")
def notes2(conn=None):
    return load_notes(conn)
""",
    )
    graph = scan_path(root)
    assert "table:notes" in _reach(graph, "entrypoint:http:GET:/notes")
    assert "table:notes" in _reach(graph, "entrypoint:http:GET:/notes2")


def test_py_barrel_reexport_call(tmp_path: Path) -> None:
    """`pkg/media_utils/__init__.py` re-exports from `.encode`; callers import the
    name from the package. Also `import *` re-exports, two-level barrels and
    `import media_utils; media_utils.fn()`."""
    _write(tmp_path, "schema.sql", "CREATE TABLE thumbs (id text);\n")
    _write(
        tmp_path,
        "pkg/media_utils/__init__.py",
        "from .encode import to_data_uri, normalize_bytes\nfrom .resize import *\n",
    )
    _write(
        tmp_path,
        "pkg/media_utils/encode.py",
        """
from sqlalchemy import text


def to_data_uri(conn, img):
    conn.execute(text("INSERT INTO thumbs (id) VALUES ('x')"))


def normalize_bytes(b):
    return b
""",
    )
    _write(tmp_path, "pkg/media_utils/resize.py", "def shrink(img):\n    return img\n")
    _write(tmp_path, "pkg/media/__init__.py", "from media_utils import to_data_uri\n")
    _write(
        tmp_path,
        "pkg/api.py",
        """
import media_utils
from media_utils import to_data_uri, shrink
from media import to_data_uri as encode2


def upload(conn, img):
    to_data_uri(conn, img)
    shrink(img)
    encode2(conn, img)
    return media_utils.normalize_bytes(img)
""",
    )
    graph = scan_path(tmp_path)
    callees = {
        e.to_id for e in graph.edges.values() if e.type == "calls" and e.from_id == function_id("pkg.api.upload")
    }
    assert {
        function_id("media_utils.encode.to_data_uri"),
        function_id("media_utils.resize.shrink"),
        function_id("media_utils.encode.normalize_bytes"),
    } <= callees, callees
    node = graph.get(function_id("pkg.api.upload"))
    assert not (node.attrs.get("unresolved_calls") or []), node.attrs.get("unresolved_calls")
    assert "table:thumbs" in _reach(graph, function_id("pkg.api.upload"))
