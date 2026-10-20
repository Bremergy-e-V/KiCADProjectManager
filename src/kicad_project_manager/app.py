"""Application entry point."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_ID, APP_NAME, __version__
from .settings import Settings

DESKTOP_ID = "kicad-project-manager"  # name of the desktop entry and icon


def restore_system_environment() -> None:
    """Let child processes (git, KiCad, the browser) use the system's libraries.

    A PyInstaller build on Linux points ``LD_LIBRARY_PATH`` at its bundled libraries. That is needed only
    while this process starts, but child processes would inherit it and load e.g. its OpenSSL instead of the
    system's. PyInstaller keeps the original value in ``LD_LIBRARY_PATH_ORIG``.
    """
    if not (getattr(sys, "frozen", False) and sys.platform.startswith("linux")):
        return
    original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if original:
        os.environ["LD_LIBRARY_PATH"] = original
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


def integrate_appimage() -> None:
    """Add the AppImage to the application menu, pointing at wherever the file is now.

    The AppImage runtime sets ``$APPIMAGE`` (the file) and ``$APPDIR`` (its mounted contents).
    """
    appimage, appdir = os.environ.get("APPIMAGE"), os.environ.get("APPDIR")
    if not (getattr(sys, "frozen", False) and appimage and appdir):
        return
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    # Desktop entry quoting: escape for a quoted Exec argument, then for a string value.
    arg = re.sub(r'([\\"`$])', r"\\\1", appimage).replace("\\", "\\\\").replace("%", "%%")
    try:
        entry = (Path(appdir) / f"{DESKTOP_ID}.desktop").read_text(encoding="utf-8")
        entry = re.sub(r"^Exec=.*$", lambda _: f'Exec="{arg}"', entry, flags=re.MULTILINE)
        icon = data / "icons" / "hicolor" / "scalable" / "apps" / f"{DESKTOP_ID}.svg"
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(appdir) / f"{DESKTOP_ID}.svg", icon)
        target = data / "applications" / f"{DESKTOP_ID}.desktop"
        if not target.exists() or target.read_text(encoding="utf-8") != entry:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry, encoding="utf-8")
    except OSError as e:
        logging.warning("Could not add the app to the application menu: %s", e)


def main() -> None:
    restore_system_environment()
    logging.basicConfig(level=logging.DEBUG if os.environ.get("KPM_DEBUG") else logging.WARNING)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ID)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(DESKTOP_ID)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "resources" / f"{DESKTOP_ID}.svg")))
    integrate_appimage()

    if shutil.which("git") is None:
        QMessageBox.critical(
            None,
            APP_NAME,
            "Git is not installed.\n\nPlease install it from https://git-scm.com/downloads and start the app again.",
        )
        sys.exit(1)

    from .ui.main_window import run_app

    window = run_app(Settings.load())  # noqa: F841 - keep a reference
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
