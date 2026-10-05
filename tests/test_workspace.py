from pathlib import Path

import pytest

from kicad_project_manager.config import Project
from kicad_project_manager.workspace import SaveConflict, Workspace, WorkspaceError, copy_template

PROJECT = Project("Motor Controller", "team/motor", id="m")


@pytest.fixture
def setup(git, make_bare, tmp_path):
    url = make_bare("motor")
    alice = Workspace(git, tmp_path / "alice")
    bob = Workspace(git, tmp_path / "bob")
    alice.create(PROJECT, url, "Motor Controller")
    bob.download(PROJECT, url)
    return alice, bob


def edit(ws: Workspace, name: str, text: str) -> None:
    (ws.path_for(PROJECT) / name).write_text(text)


def test_create_writes_kicad_project(setup):
    alice, bob = setup
    files = {p.name for p in bob.path_for(PROJECT).iterdir()}
    assert {"motor.kicad_pro", "motor.kicad_sch", ".gitignore", ".gitattributes", "README.md"} <= files
    assert bob.state(PROJECT).cloned


def test_save_and_get_latest(setup):
    alice, bob = setup
    edit(alice, "notes.txt", "hello")
    assert "Saved" in alice.save(PROJECT, "add notes")
    assert bob.state(PROJECT).behind == 1
    assert "1 new change" in bob.get_latest(PROJECT)
    assert (bob.path_for(PROJECT) / "notes.txt").read_text() == "hello"
    assert alice.save(PROJECT, "nothing").startswith("Nothing")


def test_save_combines_independent_changes(setup):
    alice, bob = setup
    edit(alice, "a.txt", "alice")
    alice.save(PROJECT, "alice")
    edit(bob, "b.txt", "bob")
    assert "combined" in bob.save(PROJECT, "bob")
    alice.get_latest(PROJECT)
    assert (alice.path_for(PROJECT) / "b.txt").read_text() == "bob"


def test_get_latest_refuses_with_local_changes_and_remote_changes(setup):
    alice, bob = setup
    edit(alice, "a.txt", "alice")
    alice.save(PROJECT, "alice")
    edit(bob, "b.txt", "unsaved")
    with pytest.raises(WorkspaceError):
        bob.get_latest(PROJECT)


def test_conflict_goes_to_rescue_branch(setup, git):
    alice, bob = setup
    edit(alice, "same.txt", "alice version")
    alice.save(PROJECT, "alice")
    edit(bob, "same.txt", "bob version")
    with pytest.raises(SaveConflict) as info:
        bob.save(PROJECT, "bob", username="bob")
    branch = info.value.branch
    assert branch.startswith("conflict/bob-")
    # Bob's work is on the server, his local copy matches the server's main branch.
    remote_heads = git.run("ls-remote", "--heads", "origin", cwd=bob.path_for(PROJECT))
    assert f"refs/heads/{branch}" in remote_heads
    assert (bob.path_for(PROJECT) / "same.txt").read_text() == "alice version"
    state = bob.state(PROJECT)
    assert (state.ahead, state.behind, state.changed_files) == (0, 0, [])


def test_copy_template_renames_project_files(tmp_path: Path):
    template = tmp_path / "template"
    (template / "lib.pretty").mkdir(parents=True)
    (template / "Template.kicad_pro").write_text("{}")
    (template / "Template.kicad_sch").write_text("()")
    (template / "Power.kicad_sch").write_text("()")
    (template / "lib.pretty" / "fp.kicad_mod").write_text("()")
    dest = tmp_path / "dest"
    dest.mkdir()
    copy_template(template, dest, "Motor")
    assert {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()} == {
        "Motor.kicad_pro",
        "Motor.kicad_sch",
        "Power.kicad_sch",
        "lib.pretty/fp.kicad_mod",
    }


def test_lock_files_detected(setup):
    alice, _ = setup
    assert alice.open_lock_files(PROJECT) == []
    edit(alice, "~motor.kicad_sch.lck", "{}")
    assert len(alice.open_lock_files(PROJECT)) == 1
