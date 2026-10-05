"""Render the SVG app icon to a multi-size .ico for the Windows build.

Usage: python make_icon.py <output.ico>
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

SVG = Path(__file__).resolve().parents[2] / "src" / "kicad_project_manager" / "resources" / "kicad-project-manager.svg"


def main() -> None:
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication([])  # noqa: F841 - QSvgRenderer needs an application
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(SVG)).render(painter)
    painter.end()
    png = out.with_suffix(".png")
    image.save(str(png))
    Image.open(png).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
