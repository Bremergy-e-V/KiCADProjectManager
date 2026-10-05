"""Application entry point."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_ID, APP_NAME, __version__
from .settings import Settings


def main() -> None:
    logging.basicConfig(level=logging.DEBUG if os.environ.get("KPM_DEBUG") else logging.WARNING)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ID)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("kicad-project-manager")
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "resources" / "kicad-project-manager.svg")))

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
