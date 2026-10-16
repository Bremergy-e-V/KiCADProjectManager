"""Folder tree, project card grid and drag & drop between them."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDrag, QFont, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
)

ROLE_KIND = Qt.ItemDataRole.UserRole + 1  # "folder" | "project"
ROLE_KEY = Qt.ItemDataRole.UserRole + 2  # folder path (list) or project id
MIME_TYPE = "application/x-kicad-project-manager-items"

# A dragged/dropped item: ("folder", ("A", "B")) or ("project", "<id>")
DragItem = tuple[str, object]


def _make_mime(items: list[DragItem]) -> QMimeData | None:
    if not items:
        return None
    mime = QMimeData()
    payload = [{"kind": kind, "key": list(key) if kind == "folder" else key} for kind, key in items]
    mime.setData(MIME_TYPE, json.dumps(payload).encode())
    return mime


def decode_mime(mime: QMimeData) -> list[DragItem]:
    if not mime.hasFormat(MIME_TYPE):
        return []
    payload = json.loads(bytes(mime.data(MIME_TYPE)).decode())
    return [(d["kind"], tuple(d["key"]) if d["kind"] == "folder" else d["key"]) for d in payload]


def _start_drag(view: QAbstractItemView, items: list[DragItem], pixmap: QPixmap | None = None) -> None:
    mime = _make_mime(items)
    if mime is None:
        return
    drag = QDrag(view)
    drag.setMimeData(mime)
    if pixmap is not None and not pixmap.isNull():
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
    # We apply moves ourselves (by editing the config), so the view never removes items.
    drag.exec(Qt.DropAction.MoveAction)


class FolderTree(QTreeWidget):
    """Left-hand folder tree. Folders can be dragged onto other folders."""

    items_dropped = Signal(list, tuple)  # list[DragItem], target folder path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setIconSize(QSize(18, 18))
        self.setIndentation(16)
        self.setAnimated(True)
        self._drop_item: QTreeWidgetItem | None = None

    @staticmethod
    def path_of(item: QTreeWidgetItem) -> tuple[str, ...]:
        return tuple(item.data(0, ROLE_KEY) or ())

    def startDrag(self, actions) -> None:  # noqa: N802
        items = [("folder", self.path_of(i)) for i in self.selectedItems() if self.path_of(i)]
        _start_drag(self, items)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _set_drop_item(self, item: QTreeWidgetItem | None) -> None:
        """Highlight the folder under the cursor while dragging."""
        if item is self._drop_item:
            return
        if self._drop_item is not None:
            self._drop_item.setBackground(0, QBrush())
        self._drop_item = item
        if item is not None:
            color = self.palette().color(QPalette.ColorRole.Highlight)
            color.setAlpha(90)
            item.setBackground(0, QBrush(color))

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if item is not None and event.mimeData().hasFormat(MIME_TYPE):
            self._set_drop_item(item)
            event.acceptProposedAction()
        else:
            self._set_drop_item(None)
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drop_item(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_drop_item(None)
        item = self.itemAt(event.position().toPoint())
        items = decode_mime(event.mimeData())
        if item is None or not items:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.items_dropped.emit(items, self.path_of(item))


@dataclass
class CardInfo:
    title: str
    subtitle: str = ""
    status: str = ""
    status_kind: str = "muted"  # ok | info | warn | error | muted | busy
    preview: QPixmap | None = None
    tooltip: str = ""


_STATUS_COLORS = {
    "ok": ("#2e7d32", "#81c784"),
    "info": ("#1565c0", "#64b5f6"),
    "warn": ("#b26a00", "#ffb74d"),
    "error": ("#c62828", "#e57373"),
    "busy": ("#6a1b9a", "#ce93d8"),
}


class CardDelegate(QStyledItemDelegate):
    """Paints folders and projects as cards with a preview image."""

    WIDTH, HEIGHT, PREVIEW_HEIGHT, RADIUS = 236, 222, 138, 8

    def __init__(self, info: Callable[[str, object], CardInfo], folder_pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.info = info
        self.folder_pixmap = folder_pixmap

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(self.WIDTH, self.HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        kind = index.data(ROLE_KIND)
        key = index.data(ROLE_KEY)
        info = self.info(kind, tuple(key) if kind == "folder" else key)
        pal: QPalette = option.palette
        dark = pal.color(QPalette.ColorRole.Window).lightness() < 128
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        drop_target = getattr(option.widget, "drop_target_row", -1) == index.row()
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(5, 5, -5, -5)
        card = QPainterPath()
        card.addRoundedRect(rect, self.RADIUS, self.RADIUS)
        painter.fillPath(card, pal.color(QPalette.ColorRole.Base))

        # preview area
        preview_rect = QRect(rect.x(), rect.y(), rect.width(), self.PREVIEW_HEIGHT)
        painter.save()
        painter.setClipPath(card)
        painter.fillRect(preview_rect, pal.color(QPalette.ColorRole.AlternateBase))
        painter.restore()
        if kind == "folder":
            pm = self.folder_pixmap
            painter.drawPixmap(preview_rect.center().x() - pm.width() // 2, preview_rect.center().y() - pm.height() // 2, pm)
        elif info.preview is not None and not info.preview.isNull():
            area = preview_rect.adjusted(8, 8, -8, -8)
            pm = info.preview  # pre-scaled to preview_area() by the caller
            size = pm.deviceIndependentSize().toSize()
            if size.width() > area.width() or size.height() > area.height():
                size = size.scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
                pm = pm.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(area.x() + (area.width() - size.width()) // 2, area.y() + (area.height() - size.height()) // 2, pm)
        else:
            painter.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(preview_rect, Qt.AlignmentFlag.AlignCenter, "No preview yet")

        # text
        x, width = rect.x() + 10, rect.width() - 20
        y = preview_rect.bottom() + 8
        title_font = QFont(option.font)
        title_font.setBold(True)
        title_font.setPointSizeF(option.font.pointSizeF() * 1.05)
        painter.setFont(title_font)
        painter.setPen(pal.color(QPalette.ColorRole.Text))
        fm = painter.fontMetrics()
        painter.drawText(
            QRect(x, y, width, fm.height()), Qt.AlignmentFlag.AlignLeft, fm.elidedText(info.title, Qt.TextElideMode.ElideRight, width)
        )
        y += fm.height() + 3

        painter.setFont(option.font)
        fm = painter.fontMetrics()
        if info.subtitle:
            painter.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(
                QRect(x, y, width, fm.height()),
                Qt.AlignmentFlag.AlignLeft,
                fm.elidedText(info.subtitle, Qt.TextElideMode.ElideRight, width),
            )
        y += fm.height() + 3
        if info.status:
            colors = _STATUS_COLORS.get(info.status_kind)
            color = QColor(colors[1] if dark else colors[0]) if colors else pal.color(QPalette.ColorRole.PlaceholderText)
            painter.setPen(color)
            painter.setBrush(color)
            painter.drawEllipse(QRect(x, y + fm.height() // 2 - 3, 7, 7))
            painter.drawText(
                QRect(x + 13, y, width - 13, fm.height()),
                Qt.AlignmentFlag.AlignLeft,
                fm.elidedText(info.status, Qt.TextElideMode.ElideRight, width - 13),
            )

        # border
        if drop_target:
            highlight = pal.color(QPalette.ColorRole.Highlight)
            fill = QColor(highlight)
            fill.setAlpha(40)
            painter.fillPath(card, fill)
            painter.setPen(QPen(highlight, 3, Qt.PenStyle.DashLine))
        elif selected:
            painter.setPen(QPen(pal.color(QPalette.ColorRole.Highlight), 2))
        elif hovered:
            painter.setPen(QPen(pal.color(QPalette.ColorRole.Highlight).lighter(130) if dark else pal.color(QPalette.ColorRole.Mid), 1.5))
        elif dark:
            painter.setPen(QPen(QColor(255, 255, 255, 40), 1))  # Mid is barely visible on dark backgrounds
        else:
            painter.setPen(QPen(pal.color(QPalette.ColorRole.Mid), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(card)
        painter.restore()

    @classmethod
    def preview_area(cls) -> QSize:
        """Size (device independent) available for a preview image inside a card."""
        return QSize(cls.WIDTH - 10 - 16, cls.PREVIEW_HEIGHT - 16)


class ProjectGrid(QListWidget):
    """Right-hand grid of folder and project cards."""

    items_dropped = Signal(list, tuple)  # list[DragItem], target folder path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSpacing(4)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        # setMovement(Static) turns off drops on the viewport; we handle drops ourselves.
        self.viewport().setAcceptDrops(True)
        self.drop_target_row = -1
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    @staticmethod
    def item_key(item: QListWidgetItem) -> DragItem:
        kind = item.data(ROLE_KIND)
        key = item.data(ROLE_KEY)
        return kind, tuple(key) if kind == "folder" else key

    def add_card(self, kind: str, key: object, text: str, tooltip: str = "") -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setData(ROLE_KIND, kind)
        item.setData(ROLE_KEY, list(key) if kind == "folder" else key)
        if tooltip:
            item.setToolTip(tooltip)
        self.addItem(item)
        return item

    def startDrag(self, actions) -> None:  # noqa: N802
        items = [self.item_key(i) for i in self.selectedItems()]
        pixmap = None
        if len(self.selectedItems()) == 1:
            pixmap = self.viewport().grab(self.visualItemRect(self.selectedItems()[0]))
            pixmap = pixmap.scaled(pixmap.size() * 0.7, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        _start_drag(self, items, pixmap)

    def _target(self, event) -> QListWidgetItem | None:
        item = self.itemAt(event.position().toPoint())
        if item is None or item.data(ROLE_KIND) != "folder" or item.isSelected():
            return None
        return item

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _set_drop_target(self, item: QListWidgetItem | None) -> None:
        row = self.row(item) if item is not None else -1
        if row != self.drop_target_row:
            self.drop_target_row = row
            self.viewport().update()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        target = self._target(event)
        if target is not None and event.mimeData().hasFormat(MIME_TYPE):
            self._set_drop_target(target)
            event.acceptProposedAction()
        else:
            self._set_drop_target(None)
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drop_target(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_drop_target(None)
        target = self._target(event)
        items = decode_mime(event.mimeData())
        if target is None or not items:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.items_dropped.emit(items, self.item_key(target)[1])
