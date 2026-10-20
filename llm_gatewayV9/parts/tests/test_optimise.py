"""The optimiser an agent is allowed to drive.

`parts` v1 stopped at validate/repair by design - topology optimisation and
decimation are named as out of scope in its docstring. That was right for a
validation tool, but an agentic optimiser needs something to DO.

These tests pin the properties that make it safe to hand to an agent:

  - the measurements are correct against meshes with a known answer
  - an inverted or collapsed solid cannot score well
  - the detection passes never modify geometry
  - a change that damages the part is refused, not applied
  - an unknown pass fails loudly rather than silently doing nothing
"""
import numpy as np
import pytest

from parts import optimise as O
from parts.mesh import Mesh


def cube(s=10.0) -> Mesh:
    """A closed axis-aligned cube: volume s^3, surface area 6s^2."""
    h = s / 2.0
    v = np.array([[x, y, z] for x in (-h, h)
                  for y in (-h, h) for z in (-h, h)], dtype=np.float64)
    f = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5],
                  [0, 4, 5], [0, 5, 1], [2, 3, 7], [2, 7, 6],
                  [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], dtype=np.int64)
    return Mesh(vertices=v, faces=f, source_format="test")


def sphere(subdiv=3, r=10.0):
    trimesh = pytest.importorskip("trimesh")
    tm = trimesh.creation.icosphere(subdivisions=subdiv, radius=r)
    return Mesh(vertices=np.asarray(tm.vertices, dtype=np.float64),
                faces=np.asarray(tm.faces, dtype=np.int64),
                source_format="test")


def test_volume_and_area_match_the_analytic_answer():
    m = O.measure(cube(10.0))
    assert m.volume_mm3 == pytest.approx(1000.0, rel=1e-6)
    assert m.surface_area_mm2 == pytest.approx(600.0, rel=1e-6)
    assert m.dimensions_mm == pytest.approx([10.0, 10.0, 10.0])
    assert m.vertices == 8 and m.faces == 12


def test_an_inverted_solid_cannot_score_well():
    """An inside-out mesh reports negative volume and must be penalised hard,
    not quietly optimised."""
    c = cube()
    flipped = c.with_arrays(c.vertices, c.faces[:, ::-1].copy())
    assert O.measure(flipped).volume_mm3 < 0
    assert O.score(O.measure(flipped)) > O.score(O.measure(c)) + 100


def test_an_invalid_solid_loses_even_to_a_wildly_missed_target():
    """The inverted-solid penalty must dominate every normal objective, not
    just a mesh close to its target. A flat 1000 could be swamped by a large
    target_faces miss and let an inside-out solid rank above a valid one."""
    c = cube()
    flipped = c.with_arrays(c.vertices, c.faces[:, ::-1].copy())
    target = 10 ** 9
    assert O.score(O.measure(flipped), target_faces=target) > \
        O.score(O.measure(c), target_faces=target)


def test_the_detection_passes_do_not_modify_geometry():
    """v1 refuses to hole-fill a boundary on the grounds that a through-hole
    and a missing face look identical. That principle applies here too: the
    detectors report, they never alter the mesh."""
    c = cube()
    before = O.measure(c)
    for name in ("detect_overhang", "detect_thin_walls"):
        r = O.run_pass(name, c)
        after = O.measure(r.mesh)
        assert not r.changed, f"{name} claimed to have changed the mesh"
        assert (before.vertices, before.faces) == (after.vertices, after.faces)
        assert r.note, f"{name} produced no explanation for the agent"


def test_decimation_reduces_faces_and_preserves_the_solid():
    s = sphere(3)
    m0 = O.measure(s)
    r = O.run_pass("decimate", s, {"target_faces": 200})
    assert r.changed, r.note
    m1 = O.measure(r.mesh)
    assert m1.faces < m0.faces
    # the decimation must not quietly change what the part IS
    drift = abs(m1.volume_mm3 - m0.volume_mm3) / m0.volume_mm3
    assert drift < 0.10, f"decimation changed the solid by {drift * 100:.1f}%"


def test_decimation_refuses_to_add_geometry():
    s = sphere(3)
    r = O.run_pass("decimate", s, {"target_faces": 10_000_000})
    assert not r.changed
    assert O.measure(r.mesh).faces == O.measure(s).faces


def test_a_change_that_damages_the_part_is_refused():
    """Full-strength Laplacian relaxation collapses a cube to zero volume.
    The pass must report that rather than return the wreckage."""
    r = O.run_pass("smooth", cube(), {"iterations": 1, "strength": 1.0})
    assert not r.changed
    assert "rejected" in r.note
    assert O.measure(r.mesh).volume_mm3 == pytest.approx(1000.0, rel=1e-6)


def test_an_unknown_pass_fails_loudly():
    with pytest.raises(KeyError):
        O.run_pass("make_it_better", cube())


def test_the_score_is_stable_and_directional():
    """`score` is what makes 'was this iteration better?' one comparison.
    It must not move for a no-op, and must fall when a budget is met."""
    c = cube()
    assert O.score(O.measure(c)) == pytest.approx(
        O.score(O.measure(O.run_pass("detect_overhang", c).mesh)))

    s = sphere(3)
    over = O.score(O.measure(s), target_faces=200)
    under = O.score(O.measure(O.run_pass("decimate", s,
                                         {"target_faces": 200}).mesh),
                    target_faces=200)
    assert under < over


def test_every_registered_pass_is_dispatchable():
    """A pass the agent can name but not run is worse than one that is absent."""
    for p in O.PASSES:
        r = O.run_pass(p.name, cube(), p.params or None)
        assert r.note or not r.changed, f"{p.name} said nothing"


def test_the_registry_documents_what_modifies_geometry():
    """An agent has to be able to tell a modifying pass from a measuring one
    before it picks one."""
    for p in O.PASSES:
        assert isinstance(p.modifies, bool)
        assert p.description, f"{p.name} has no description"
def test_volume_is_translation_invariant():
    """An open mesh's origin-based volume is pure coordinate offset.

    `_volume` must measure around the mesh, matching Geometry.signed_volume;
    otherwise a part a million millimetres from the origin looks enormous and
    skips the inverted-solid penalty.
    """
    c = cube()
    shifted = Mesh(vertices=c.vertices + np.array([1e6, 1e6, 1e6]),
                   faces=c.faces, source_format="test")
    assert O.measure(shifted).volume_mm3 == pytest.approx(1000.0, rel=1e-6)
    assert O.score(O.measure(shifted)) == pytest.approx(
        O.score(O.measure(c)), rel=1e-6)


def test_an_open_triangle_far_from_the_origin_has_no_volume():
    v = np.array([[1e6, 1e6, 1e6],
                  [1e6 + 1.0, 1e6, 1e6],
                  [1e6, 1e6 + 1.0, 1e6]])
    m = O.measure(Mesh(vertices=v, faces=np.array([[0, 1, 2]]),
                       source_format="test"))
    assert abs(m.volume_mm3) < 1e-6
