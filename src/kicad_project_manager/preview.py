"""Preview images rendered with kicad-cli.

The 3D render of the board is used when the board has footprints; otherwise
(early in a project) the root schematic sheet is used instead.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .workspace import find_project_file

PREVIEW_SIZE = (640, 400)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str], timeout: float = 180) -> bool:
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _contains(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def render_preview(kicad_cli: str, project_dir: Path, out: Path, preferred_name: str = "") -> bool:
    """Render a PNG preview of the project in ``project_dir`` to ``out``."""
    pro = find_project_file(project_dir, preferred_name)
    if pro is None:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)

    pcb = pro.with_suffix(".kicad_pcb")
    if _contains(pcb, "(footprint "):
        w, h = PREVIEW_SIZE
        cmd = [
            kicad_cli,
            "pcb",
            "render",
            "-w",
            str(w),
            "-h",
            str(h),
            "--side",
            "top",
            "--quality",
            "basic",
            "--zoom",
            "0.9",
            "-o",
            str(out),
            str(pcb),
        ]
        if _run(cmd) and out.exists():
            return True

    sch = pro.with_suffix(".kicad_sch")
    # "(lib_id" only appears in placed symbols - an empty sheet makes a useless preview.
    if _contains(sch, "(lib_id") or _contains(sch, "(sheet\n") or _contains(sch, "(sheet "):
        with tempfile.TemporaryDirectory() as tmp:
            if _run([kicad_cli, "sch", "export", "svg", "-e", "--pages", "1", "-o", tmp, str(sch)]):
                svgs = sorted(Path(tmp).glob("*.svg"))
                if svgs and _svg_to_png(svgs[0], out):
                    return True
    return False


def _svg_to_png(svg: Path, out: Path) -> bool:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        return False
    w, h = PREVIEW_SIZE
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    size = renderer.defaultSize().scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio)
    target = QRectF((w - size.width()) / 2, (h - size.height()) / 2, size.width(), size.height())
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, target)
    painter.end()
    return image.save(str(out), "PNG")
