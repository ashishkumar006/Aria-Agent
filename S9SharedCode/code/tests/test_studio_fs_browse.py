"""The folder picker's read: folder names only, machine-wide.

`GET /api/studio/fs/list` is what replaced "type the absolute path from
memory". It must list folders everywhere (the user navigates to their own
projects) while refusing the one thing the add route refuses: picking the
agent's state directory, an ancestor of it, or the drive root. Listing is
deliberately not gated by the workspace confinement — a picker that could
not reach `C:\\Users` could not pick `Downloads` either — but it never reads
file contents and never lists hidden entries.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import studio_workspace as sw  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import agent_server  # noqa: E402

client = TestClient(agent_server.app)


def _state(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


def test_the_top_of_the_picker_lists_drives(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "state_root", lambda: _state(tmp_path))
    r = client.get("/api/studio/fs/list")
    assert r.status_code == 200
    d = r.json()
    assert d["path"] == "" and d["selectable"] is False
    assert isinstance(d["dirs"], list) and len(d["dirs"]) >= 1
    # Windows drives cannot be picked; the button would refuse them all.
    assert all(x["selectable"] is False for x in d["dirs"])


def test_a_folder_listing_gives_folders_without_files_or_hidden_entries(tmp_path, monkeypatch):
    sd = _state(tmp_path)
    monkeypatch.setattr(sw, "state_root", lambda: sd)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "sub").mkdir()
    (plain / "file.txt").write_text("x", encoding="utf-8")
    (plain / ".hidden").mkdir()
    r = client.get("/api/studio/fs/list", params={"path": str(plain)})
    assert r.status_code == 200
    d = r.json()
    names = [x["name"] for x in d["dirs"]]
    assert "sub" in names
    assert "file.txt" not in names
    assert ".hidden" not in names
    assert d["selectable"] is True
    assert d["selectable"] == (not d["reason"])


def test_the_state_dir_itself_is_not_selectable(tmp_path, monkeypatch):
    sd = _state(tmp_path)
    (sd / "code-studio").mkdir()
    monkeypatch.setattr(sw, "state_root", lambda: sd)
    r = client.get("/api/studio/fs/list", params={"path": str(sd)})
    assert r.status_code == 200
    d = r.json()
    assert d["selectable"] is False
    assert "state directory" in d["reason"]
    # …but walking into it lists it, so the user can see what is inside.
    assert "code-studio" in [x["name"] for x in d["dirs"]]


def test_an_ancestor_of_the_state_dir_is_not_selectable_but_navigable(tmp_path, monkeypatch):
    sd = _state(tmp_path)
    monkeypatch.setattr(sw, "state_root", lambda: sd)
    (tmp_path / "project-x").mkdir()
    r = client.get("/api/studio/fs/list", params={"path": str(tmp_path)})
    assert r.status_code == 200
    d = r.json()
    assert d["selectable"] is False
    assert "holds the agent's state directory" in d["reason"]
    names = [x["name"] for x in d["dirs"]]
    # The folder holding the state dir shows up as a row, greyed, walkable.
    assert "state" in names


def test_a_scratch_playground_is_selectable(tmp_path, monkeypatch):
    sd = _state(tmp_path)
    monkeypatch.setattr(sw, "state_root", lambda: sd)
    pg = sd / "code-studio" / "playgrounds" / "st-test"
    pg.mkdir(parents=True)
    r = client.get("/api/studio/fs/list", params={"path": str(pg)})
    assert r.status_code == 200
    assert r.json()["selectable"] is True


def test_a_missing_path_is_an_error_not_a_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "state_root", lambda: _state(tmp_path))
    r = client.get("/api/studio/fs/list", params={"path": str(tmp_path / "nope")})
    assert r.status_code == 404
    assert "does not exist" in r.json()["error"]


def test_a_file_cannot_be_listed_as_a_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "state_root", lambda: _state(tmp_path))
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    r = client.get("/api/studio/fs/list", params={"path": str(f)})
    assert r.status_code == 400
    assert "not a folder" in r.json()["error"]
