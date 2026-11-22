from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "shop"
CLINIC = ROOT / "examples" / "clinic"
PYPLATFORM = ROOT / "examples" / "pyplatform"


@pytest.fixture
def example_root() -> Path:
    return EXAMPLE


@pytest.fixture
def clinic_root() -> Path:
    return CLINIC


@pytest.fixture
def pyplatform_root() -> Path:
    return PYPLATFORM


@pytest.fixture(autouse=True)
def _private_cache_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Index caches and hint markers go to a temp folder, never ~/.cache."""
    monkeypatch.setenv("BIZMAP_CACHE_DIR", str(tmp_path_factory.mktemp("bizmap-cache") / "index"))
    monkeypatch.delenv("BIZMAP_NO_CACHE", raising=False)
    monkeypatch.delenv("BIZMAP_NO_GITIGNORE_HINT", raising=False)
