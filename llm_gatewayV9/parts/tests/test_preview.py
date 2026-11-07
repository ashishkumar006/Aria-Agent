"""Fast viewer LOD: large meshes must not wait on quadric decimation."""
from __future__ import annotations

import numpy as np

from parts.mesh import Mesh
from parts.preview import fast_preview


def _grid(n: int) -> Mesh:
    xs, ys = np.meshgrid(np.linspace(0, 1, n + 1), np.linspace(0, 1, n + 1))
    vertices = np.stack([xs.ravel(), ys.ravel(), np.zeros((n + 1) ** 2)], axis=1)
    grid = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
    a, b, c, d = (grid[:-1, :-1].ravel(), grid[:-1, 1:].ravel(),
                  grid[1:, 1:].ravel(), grid[1:, :-1].ravel())
    faces = np.stack([np.stack([a, b, c], axis=1),
                      np.stack([a, c, d], axis=1)], axis=1).reshape(-1, 3)
    return Mesh.create(vertices, faces, source_format="test")


def test_a_large_mesh_is_reduced_to_the_preview_budget():
    mesh = _grid(300)          # 180,000 faces
    preview, changed = fast_preview(mesh, 18_000)
    assert changed is True
    assert 0 < preview.face_count <= 18_000 * 1.12
    assert preview.vertex_count < preview.face_count * 3
    assert mesh.face_count == 180_000  # source untouched


def test_a_small_mesh_is_returned_unchanged():
    mesh = _grid(10)           # 200 faces
    preview, changed = fast_preview(mesh, 18_000)
    assert changed is False
    assert preview is mesh
