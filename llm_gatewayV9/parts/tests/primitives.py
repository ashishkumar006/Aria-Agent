"""Mesh fixtures built by hand, so the tests do not depend on the code.

Every winding in here is verified against an outward normal by
`test_geometry.py::test_box_volume_matches_the_analytic_value`; if a face were
inside-out that test fails before anything downstream depends on it.
"""
from __future__ import annotations

import numpy as np

# A unit cube, corner-indexed so the faces below read like the geometry does.
#   0=(0,0,0) 1=(1,0,0) 2=(1,1,0) 3=(0,1,0)
#   4=(0,0,1) 5=(1,0,1) 6=(1,1,1) 7=(0,1,1)
CUBE_VERTICES = np.array(
    [
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [1.0, 1.0, 1.0], [0.0, 1.0, 1.0],
    ],
    dtype=np.float64,
)

CUBE_FACES = np.array(
    [
        [0, 3, 2], [0, 2, 1],  # bottom, -z
        [4, 5, 6], [4, 6, 7],  # top, +z
        [0, 1, 5], [0, 5, 4],  # front, -y
        [3, 7, 6], [3, 6, 2],  # back, +y
        [0, 4, 7], [0, 7, 3],  # left, -x
        [1, 2, 6], [1, 6, 5],  # right, +x
    ],
    dtype=np.int64,
)


def cube(scale: float = 1.0, offset=(0.0, 0.0, 0.0)):
    """A closed, outward-wound box. Volume = scale**3, area = 6 * scale**2."""
    v = CUBE_VERTICES * float(scale) + np.asarray(offset, dtype=np.float64)
    return v, CUBE_FACES.copy()


def tetrahedron():
    """Smallest closed solid: 4 faces, 4 vertices, chi = 2."""
    v = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    f = np.array(
        [
            [0, 2, 1],  # bottom, -z
            [0, 1, 3],  # y = 0, -y
            [0, 3, 2],  # x = 0, -x
            [1, 2, 3],  # x + y + z = 1, outward
        ],
        dtype=np.int64,
    )
    return v, f


def open_cube():
    """A cube with its top face removed: closed except for 4 boundary edges.

    The top, not the bottom: the bottom triangles are (0,3,2) and (0,2,1), both
    of which contain vertex 0 at the origin, so they contribute nothing to the
    signed volume. Removing them would leave the volume at 1.0 and quietly make
    the volume assertions test nothing.
    """
    v, f = cube()
    return v, np.delete(f, [2, 3], axis=0)


def two_triangles():
    """A flat sheet. Not watertight, zero volume, no holes to speak of."""
    v = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float64)
    f = np.array([[0, 1, 2]], dtype=np.int64)
    return v, f


def broken_cube():
    """A cube carrying one of each defect the repair passes look for.

    - one vertex nothing references (vertex 8, out at 5,5,5)
    - one zero-area face with a repeated corner
    - one duplicate face: a repeat of an existing triangle's vertex set
    - one inverted face: face 2 reversed *in place*

    The inverted face is a reversal in place rather than an appended
    reversed copy on purpose. Appending [4, 6, 5] next to an existing [4, 5, 6]
    makes the new triangle both inverted *and* a duplicate face, so the two
    repair passes that would catch it become indistinguishable in the fixture
    and the counts stop meaning what the test claims they mean.
    """
    v, f = cube()
    vertices = np.vstack([v, [[5.0, 5.0, 5.0]]])       # vertex 8: unreferenced
    faces = np.vstack([f, [[0, 1, 1]], [[0, 3, 2]]])
    faces[2] = [4, 6, 5]                                # inverted in place
    return vertices, faces


def torus(n_major: int = 12, n_minor: int = 8):
    """A closed genus-1 surface, to pin down the Euler/genus arithmetic."""
    u = np.linspace(0.0, 2.0 * np.pi, n_major, endpoint=False)
    v = np.linspace(0.0, 2.0 * np.pi, n_minor, endpoint=False)
    uu, vv = np.meshgrid(u, v, indexing="ij")
    R, r = 2.0, 0.6
    x = (R + r * np.cos(vv)) * np.cos(uu)
    y = (R + r * np.cos(vv)) * np.sin(uu)
    z = r * np.sin(vv)
    vertices = np.stack([x.ravel(), y.ravel(), z.ravel()], axis=1)
    faces = []
    for i in range(n_major):
        for j in range(n_minor):
            a = i * n_minor + j
            b = ((i + 1) % n_major) * n_minor + j
            c = ((i + 1) % n_major) * n_minor + (j + 1) % n_minor
            d = i * n_minor + (j + 1) % n_minor
            faces.append([a, b, c])
            faces.append([a, c, d])
    return vertices, np.asarray(faces, dtype=np.int64)


def binary_stl(vertices, faces) -> bytes:
    """Serialise by hand, independently of `parts.export`, as a cross-check."""
    import struct

    tri = vertices[faces]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    header = b"unit test".ljust(80, b"\0")
    out = [header, struct.pack("<I", len(faces))]
    for normal, corners in zip(normals, tri):
        out.append(struct.pack("<3f", *normal))
        out.append(struct.pack("<9f", *corners.ravel()))
        out.append(struct.pack("<H", 0))
    return b"".join(out)


def ascii_stl(vertices, faces) -> bytes:
    tri = vertices[faces]
    lines = ["solid test"]
    for corners in tri:
        lines.append("  facet normal 0 0 0")
        lines.append("    outer loop")
        for corner in corners:
            # float() first: under numpy 2 a scalar reprs as np.float64(0.0),
            # which is not valid ASCII STL and not a parseable float.
            x, y, z = (float(c) for c in corner)
            lines.append(f"      vertex {x!r} {y!r} {z!r}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid test")
    return ("\n".join(lines) + "\n").encode("ascii")


def obj(vertices, faces) -> bytes:
    lines = ["# unit test"]
    lines += [
        f"v {float(x)!r} {float(y)!r} {float(z)!r}" for x, y, z in vertices
    ]
    for face in faces:
        lines.append("f " + " ".join(str(i + 1) for i in face))
    return ("\n".join(lines) + "\n").encode("ascii")