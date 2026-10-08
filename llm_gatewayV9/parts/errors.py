"""Typed failures for the parts pipeline.

Everything the pipeline rejects raises a `PartError` carrying a stable
`code`. The HTTP layer maps `code` -> status code and nothing else needs to
know about mesh internals, and a caller can branch on `code` without
string-matching a message. Malformed input fails here rather than producing
an empty report: a report about zero triangles is indistinguishable from a
report about a mesh that legitimately has none, and only one of those is
useful.
"""
from __future__ import annotations


class PartError(Exception):
    """Base class for every rejection this package raises."""

    code = "part_error"

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, object] = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class UnsupportedFormatError(PartError):
    """No reader for this container, and the one that could read it is absent."""

    code = "unsupported_format"


class MeshTooLargeError(PartError):
    """Input exceeds a declared resource budget (bytes, faces or vertices)."""

    code = "mesh_too_large"


class MeshIntegrityError(PartError):
    """Input parsed but violates a mesh invariant (shape, NaN, index range)."""

    code = "mesh_integrity"


class EmptyMeshError(PartError):
    """Input carries no surface at all."""

    code = "empty_mesh"


class RepairFailedError(PartError):
    """A repair pass destroyed the mesh instead of fixing it."""

    code = "repair_failed"


class MissingBackendError(PartError):
    """An operation was requested that needs an optional dependency."""

    code = "missing_backend"


__all__ = [
    "PartError",
    "UnsupportedFormatError",
    "MeshTooLargeError",
    "MeshIntegrityError",
    "EmptyMeshError",
    "RepairFailedError",
    "MissingBackendError",
]