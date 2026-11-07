"""Folder metadata contracts.

Folders must never require a re-index: a document's chunks are keyed by its
stable `doc_id`, so a move is one registry write. Deleting a folder is
deliberately not recursive: losing indexed chunks behind one tree click is
not acceptable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from documents import registry as R  # noqa: E402


@pytest.fixture()
def reg(tmp_path):
    return R.Registry(tmp_path / "docs")


def _doc(reg, name="notes.txt", folder_id=""):
    doc = reg.add(filename=name, doc_type="txt", size_bytes=5,
                  folder_id=folder_id)
    reg.write_source(doc.id, b"hello")
    return doc


def test_nested_folders_and_paths(reg):
    root = reg.create_folder("Research")
    sub = reg.create_folder("Batteries", root.id)
    grand = reg.create_folder("Sodium ion", sub.id)
    assert reg.folder_path(grand.id) == "Research / Batteries / Sodium ion"
    assert reg.folder_subtree(root.id) == {root.id, sub.id, grand.id}
    assert reg.folder_subtree(sub.id) == {sub.id, grand.id}


def test_sibling_names_are_case_insensitive_but_other_parents_are_not(reg):
    root = reg.create_folder("Research")
    with pytest.raises(ValueError):
        reg.create_folder("research")
    sub = reg.create_folder("research", root.id)  # different parent: allowed
    assert sub.parent_id == root.id


def test_folder_cycle_impossible_and_depth_capped(reg):
    parent = ""
    ids = []
    for i in range(R.FOLDER_MAX_DEPTH):
        f = reg.create_folder(f"L{i}", parent)
        ids.append(f.id)
        parent = f.id
    with pytest.raises(ValueError):
        reg.create_folder("too deep", parent)


def test_move_is_metadata_only_and_scope_uses_descendants(reg):
    root = reg.create_folder("Research")
    sub = reg.create_folder("Batteries", root.id)
    other = reg.create_folder("Other")
    d1 = _doc(reg, "a.txt", sub.id)
    d2 = _doc(reg, "b.txt", other.id)
    # A move keeps the same id and source; no re-index is required.
    moved = reg.move_document(d1.id, other.id)
    assert moved and moved.id == d1.id and moved.folder_id == other.id
    assert reg.read_source(d1.id) == b"hello"
    assert reg.document_ids_in_folders({sub.id}, enabled_only=False) == set()
    assert reg.document_ids_in_folders(
        {root.id}, enabled_only=False) == set()
    assert reg.document_ids_in_folders(
        {other.id}, enabled_only=False) == {d1.id, d2.id}


def test_delete_refuses_non_empty_folders(reg):
    root = reg.create_folder("Research")
    sub = reg.create_folder("Batteries", root.id)
    _doc(reg, "a.txt", root.id)
    with pytest.raises(ValueError):
        reg.delete_folder(root.id)  # has a subfolder
    reg.delete_folder(sub.id)
    with pytest.raises(ValueError):
        reg.delete_folder(root.id)  # now contains a document
    doc = reg.list()[0]
    reg.update(doc.id, folder_id="")
    assert reg.delete_folder(root.id)


def test_legacy_document_without_folder_loads_at_root(reg):
    doc = _doc(reg)
    assert doc.folder_id == ""
    assert reg.document_ids_in_folders(set(), enabled_only=False) == set()
    moved = reg.move_document(doc.id, "")
    assert moved.folder_id == ""
