import sys

from kicad_project_manager import app


def test_appimage_added_to_application_menu(monkeypatch, tmp_path):
    appdir = tmp_path / "mount"
    appdir.mkdir()
    (appdir / "kicad-project-manager.desktop").write_text(
        "[Desktop Entry]\nName=KPM\nExec=kicad-project-manager\nIcon=kicad-project-manager\n"
    )
    (appdir / "kicad-project-manager.svg").write_text("<svg/>")
    data = tmp_path / "data"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setenv("APPIMAGE", '/home/me/My Apps/100% "KPM".AppImage')
    monkeypatch.setenv("XDG_DATA_HOME", str(data))

    app.integrate_appimage()

    entry = (data / "applications" / "kicad-project-manager.desktop").read_text()
    assert 'Exec="/home/me/My Apps/100%% \\\\"KPM\\\\".AppImage"' in entry.splitlines()
    assert "Name=KPM" in entry
    assert (data / "icons" / "hicolor" / "scalable" / "apps" / "kicad-project-manager.svg").read_text() == "<svg/>"


def test_appimage_integration_only_when_frozen(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("APPDIR", str(tmp_path))
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "KPM.AppImage"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    app.integrate_appimage()
    assert not (tmp_path / "data").exists()


def test_child_processes_get_the_system_libraries(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/app/_internal:/opt/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/lib")
    app.restore_system_environment()
    assert app.os.environ["LD_LIBRARY_PATH"] == "/opt/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in app.os.environ

    monkeypatch.setenv("LD_LIBRARY_PATH", "/app/_internal")
    app.restore_system_environment()
    assert "LD_LIBRARY_PATH" not in app.os.environ
