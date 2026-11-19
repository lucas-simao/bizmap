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
