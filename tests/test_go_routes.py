from __future__ import annotations

from pathlib import Path

from bizmap.detect.go_lang import GoAnalyzer
from bizmap.detect.rules import load_rules
from bizmap.graph import Confidence, column_id, function_id, table_id
from bizmap.scan import scan_path


def test_unresolved_const_path_keeps_placeholder(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module t\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "main.go").write_text(
        """
package main

import (
    "github.com/labstack/echo/v4"
    otherpkg "t/notscanned"
)

func main() {
    e := echo.New()
    e.POST(otherpkg.PendingPath+"/setup", handleSetup)
}

func handleSetup() {}
""",
        encoding="utf-8",
    )
    analysis = GoAnalyzer(load_rules()).analyze([tmp_path / "main.go"], tmp_path)
    assert analysis.routes, analysis.routes
    route = analysis.routes[0]
    assert "{otherpkg.PendingPath}" in route.path
    assert route.path.endswith("/setup")
    assert route.path != "/setup"
    assert route.path_confidence == Confidence.UNKNOWN
    assert route.path_unresolved is True


def test_ambiguous_handler_is_unknown_with_candidates(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module t\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "pkg/a").mkdir(parents=True)
    (tmp_path / "pkg/b").mkdir(parents=True)
    (tmp_path / "pkg/a/h.go").write_text(
        "package a\ntype Handler struct{}\nfunc (h *Handler) Create() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg/b/h.go").write_text(
        "package b\ntype Handler struct{}\nfunc (h *Handler) Create() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        """
package main

import "github.com/labstack/echo/v4"

func main() {
    e := echo.New()
    e.POST("/x", x.Create)
}
""",
        encoding="utf-8",
    )
    files = [tmp_path / "main.go", tmp_path / "pkg/a/h.go", tmp_path / "pkg/b/h.go"]
    analysis = GoAnalyzer(load_rules()).analyze(files, tmp_path)
    assert len(analysis.routes) == 1
    route = analysis.routes[0]
    assert route.handler is None
    assert route.confidence == Confidence.UNKNOWN
    assert any("Create" in c for c in route.candidates)


def test_expand_star_only_at_origin(clinic_root: Path) -> None:
    graph = scan_path(clinic_root, expand_star=True)
    origin = function_id("repositories/users.Repo.List")
    assert any(
        e.from_id == origin and e.to_id == column_id("patients", "email") for e in graph.edges.values()
    )
    handler = function_id("controllers/users.Handler.List")
    assert graph.edges.get(f"reads:{handler}->{table_id('patients')}") is not None
    assert graph.edges.get(f"reads:{handler}->{column_id('patients', 'email')}") is None
