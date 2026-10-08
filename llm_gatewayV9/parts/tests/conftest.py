"""Path bootstrap and shared fixtures.

The gateway's own `tests/conftest.py` is not loaded for these files - it lives
under `tests/` and patches httpx so every client presents `X-Gateway-Token`.
Nothing here makes HTTP calls, so that machinery is not wanted. The
`sys.path` insert mirrors what `tests/test_documents_chunker.py` does: make
`parts` importable however pytest was invoked.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
GATEWAY_ROOT = HERE.parent.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from parts.mesh import Mesh  # noqa: E402
from parts.tests import primitives as prim  # noqa: E402


@pytest.fixture
def cube_mesh() -> Mesh:
    v, f = prim.cube()
    return Mesh.create(v, f, source_format="unit-test")


@pytest.fixture
def broken_mesh() -> Mesh:
    v, f = prim.broken_cube()
    return Mesh.create(v, f, source_format="unit-test")


@pytest.fixture
def open_mesh() -> Mesh:
    v, f = prim.open_cube()
    return Mesh.create(v, f, source_format="unit-test")


@pytest.fixture
def tetra_mesh() -> Mesh:
    v, f = prim.tetrahedron()
    return Mesh.create(v, f, source_format="unit-test")


@pytest.fixture
def torus_mesh() -> Mesh:
    v, f = prim.torus()
    return Mesh.create(v, f, source_format="unit-test")


@pytest.fixture
def no_optional_backends(monkeypatch):
    """Force both optional backends absent, whatever the venv happens to have.

    Lets the degradation tests run in an environment where trimesh *is*
    installed, so they keep testing the claim they are named for.
    """
    from parts import backends as backends_mod

    monkeypatch.setattr(
        backends_mod, "_CACHE",
        backends_mod.Backends(trimesh=None, manifold3d=None,
                              errors={"trimesh": "forced", "manifold3d": "forced"}),
    )
    return backends_mod.Backends(trimesh=None, manifold3d=None,
                                 errors={"trimesh": "forced", "manifold3d": "forced"})