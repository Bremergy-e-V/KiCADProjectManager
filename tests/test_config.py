import json

import pytest

from kicad_project_manager.config import CONFIG_FILE, Config, ConfigError, ConfigRepo, Project


def sample() -> Config:
    c = Config(name="PCBs", default_owner="PCB_Engineering")
    c.add_folder((), "Customers")
    c.add_folder(("Customers",), "ACME")
    c.add_folder((), "Internal")
    c.add_project(("Customers", "ACME"), Project("Motor Controller", "PCB_Engineering/motor", id="m"))
    c.add_project((), Project("Test Jig", "PCB_Engineering/jig", id="j"))
    return c


def test_round_trip():
    c = sample()
    again = Config.from_dict(json.loads(json.dumps(c.to_dict())))
    assert again.to_dict() == c.to_dict()
    assert again.find_project("m") == (("Customers", "ACME"), again.folder(("Customers", "ACME")).projects[0])


def test_folder_names_validated():
    c = sample()
    for bad in ("", "  ", "a/b", "a\\b"):
        with pytest.raises(ConfigError):
            c.add_folder((), bad)
    with pytest.raises(ConfigError):
        c.add_folder((), "customers")  # case-insensitive duplicate


def test_move_folder():
    c = sample()
    assert c.move_folder(("Customers", "ACME"), ("Internal",)) == ("Internal", "ACME")
    assert c.find_project("m")[0] == ("Internal", "ACME")
    with pytest.raises(ConfigError):
        c.move_folder(("Internal",), ("Internal", "ACME"))  # into own child
    with pytest.raises(ConfigError):
        c.move_folder(("Internal",), ("Internal",))


def test_move_and_remove_project():
    c = sample()
    c.move_project("j", ("Internal",))
    assert c.find_project("j")[0] == ("Internal",)
    c.remove_project("j")
    with pytest.raises(ConfigError):
        c.find_project("j")


def test_delete_only_empty_folder():
    c = sample()
    with pytest.raises(ConfigError):
        c.delete_folder(("Customers",))
    c.delete_folder(("Internal",))
    assert not c.has_folder(("Internal",))


def test_rename_folder_case_only():
    c = sample()
    assert c.rename_folder(("Internal",), "internal") == ("internal",)
    with pytest.raises(ConfigError):
        c.rename_folder(("internal",), "Customers")


def test_duplicate_repo_rejected():
    c = sample()
    with pytest.raises(ConfigError):
        c.add_project(("Internal",), Project("Other name", "pcb_engineering/MOTOR"))


def test_config_repo_initialize_and_concurrent_mutations(git, make_bare, tmp_path):
    url = make_bare("config")
    alice = ConfigRepo(git, tmp_path / "alice", url)
    bob = ConfigRepo(git, tmp_path / "bob", url)

    assert alice.open() is False  # empty repository
    alice.initialize(Config(name="PCBs"))
    assert (tmp_path / "alice" / CONFIG_FILE).exists()
    with pytest.raises(ConfigError):
        alice.initialize(Config(name="again"))

    assert bob.open() is True
    alice.mutate(lambda c, _r: c.add_folder((), "From Alice"), "alice")
    # Bob's clone is now stale; his change must be applied on top of Alice's, not overwrite it.
    result = bob.mutate(lambda c, _r: c.add_folder((), "From Bob"), "bob")
    assert {f.name for f in result.root.folders} == {"From Alice", "From Bob"}
    alice.sync()
    assert {f.name for f in alice.load().root.folders} == {"From Alice", "From Bob"}


def test_mutate_without_change_makes_no_commit(git, make_bare, tmp_path):
    repo = ConfigRepo(git, tmp_path / "c", make_bare("config"))
    repo.open()
    repo.initialize(Config(name="PCBs"))
    before = git.run("rev-parse", "HEAD", cwd=repo.path)
    repo.mutate(lambda c, _r: None, "noop")
    assert git.run("rev-parse", "HEAD", cwd=repo.path) == before


def test_preview_files_committed(git, make_bare, tmp_path):
    url = make_bare("config")
    repo = ConfigRepo(git, tmp_path / "c", url)
    repo.open()
    repo.initialize(Config(name="PCBs"))
    repo.mutate(lambda c, r: r.preview_file("abc").write_bytes(b"png"), "preview")
    other = ConfigRepo(git, tmp_path / "other", url)
    other.open()
    assert other.preview_file("abc").read_bytes() == b"png"


def test_project_templates():
    c = sample()
    assert c.set_template(" 2-Layer ", "PCB_Engineering/motor") == "2-Layer"
    assert c.template_of("pcb_engineering/MOTOR") == "2-Layer"
    assert c.find_template("2-layer") == "2-Layer"
    c.set_template("Motor base", "PCB_Engineering/motor")  # renames: a project is at most one template
    assert c.templates == {"Motor base": "PCB_Engineering/motor"}
    c.set_template("motor BASE", "PCB_Engineering/jig")  # replaces the template of that name
    assert c.templates == {"motor BASE": "PCB_Engineering/jig"}
    assert c.template_of("PCB_Engineering/motor") is None
    for bad in ("a:b", ".hidden", ""):
        with pytest.raises(ConfigError):
            c.set_template(bad, "PCB_Engineering/motor")
    again = Config.from_dict(json.loads(json.dumps(c.to_dict())))
    assert again.templates == c.templates
    c.remove_template("motor BASE")
    assert c.templates == {}


def test_template_folders_listed_and_replaced(git, make_bare, tmp_path):
    url = make_bare("config")
    repo = ConfigRepo(git, tmp_path / "c", url)
    repo.open()
    repo.initialize(Config(name="PCBs"))

    def add_folder(_c, r: ConfigRepo):
        (r.templates_dir / "4-Layer").mkdir(parents=True)
        (r.templates_dir / "4-Layer" / "Board.kicad_pro").write_text("{}")

    repo.mutate(add_folder, "template folder")
    other = ConfigRepo(git, tmp_path / "other", url)
    other.open()
    assert list(other.template_folders()) == ["4-Layer"]
    assert other.find_template_folder("4-layer") == "4-Layer"

    def replace(c: Config, r: ConfigRepo):
        c.set_template("4-layer", "PCB_Engineering/four-layer")
        r.remove_template_folder("4-layer")

    other.mutate(replace, "replace")
    repo.sync()
    assert repo.template_folders() == {}
    assert repo.load().templates == {"4-layer": "PCB_Engineering/four-layer"}


def test_legacy_template_folder_still_offered(git, make_bare, tmp_path):
    repo = ConfigRepo(git, tmp_path / "c", make_bare("config"))
    repo.open()
    repo.initialize(Config(name="PCBs"))
    (repo.path / "template").mkdir()
    (repo.path / "template" / "T.kicad_pro").write_text("{}")
    assert list(repo.template_folders()) == ["Team template"]
