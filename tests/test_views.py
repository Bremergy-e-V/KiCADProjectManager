import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QTreeWidgetItem  # noqa: E402

from kicad_project_manager.ui.views import ROLE_KEY, FolderTree, ProjectGrid, _make_mime  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _send_drag(widget, pos, mime):
    """Simulate hovering a drag over ``pos`` and dropping it there."""
    pos = QPointF(pos)
    actions = Qt.DropAction.MoveAction
    buttons, mods = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    enter = QDragEnterEvent(pos.toPoint(), actions, mime, buttons, mods)
    QApplication.sendEvent(widget, enter)
    move = QDragMoveEvent(pos.toPoint(), actions, mime, buttons, mods)
    QApplication.sendEvent(widget, move)
    accepted = move.isAccepted()
    drop = QDropEvent(pos, actions, mime, buttons, mods)
    QApplication.sendEvent(widget, drop)
    return accepted


def test_project_dropped_on_folder_card(app):
    grid = ProjectGrid()
    grid.resize(800, 600)
    grid.show()
    grid.add_card("folder", ("Customers",), "Customers")
    grid.add_card("project", "p1", "Board")
    app.processEvents()

    # Regression: setMovement(Static) disables drops on the viewport.
    assert grid.viewport().acceptDrops()

    dropped = []
    grid.items_dropped.connect(lambda items, target: dropped.append((items, target)))
    folder_center = grid.visualItemRect(grid.item(0)).center()
    assert _send_drag(grid.viewport(), folder_center, _make_mime([("project", "p1")]))
    assert dropped == [([("project", "p1")], ("Customers",))]
    assert grid.drop_target_row == -1


def test_drop_on_project_card_is_rejected(app):
    grid = ProjectGrid()
    grid.resize(800, 600)
    grid.show()
    grid.add_card("project", "p1", "Board")
    grid.add_card("project", "p2", "Other")
    app.processEvents()
    dropped = []
    grid.items_dropped.connect(lambda *a: dropped.append(a))
    center = grid.visualItemRect(grid.item(1)).center()
    assert not _send_drag(grid.viewport(), center, _make_mime([("project", "p1")]))
    assert dropped == []


def test_drop_on_tree_does_not_navigate(app):
    tree = FolderTree()
    tree.resize(300, 400)
    tree.show()
    root = QTreeWidgetItem(["All"])
    root.setData(0, ROLE_KEY, [])
    child = QTreeWidgetItem(["Internal"])
    child.setData(0, ROLE_KEY, ["Internal"])
    tree.addTopLevelItem(root)
    root.addChild(child)
    root.setExpanded(True)
    tree.setCurrentItem(root)
    app.processEvents()

    changes, dropped = [], []
    tree.currentItemChanged.connect(lambda *a: changes.append(a))
    tree.items_dropped.connect(lambda items, target: dropped.append(target))
    assert tree.viewport().acceptDrops()
    assert _send_drag(tree.viewport(), tree.visualItemRect(child).center(), _make_mime([("project", "p1")]))
    assert dropped == [("Internal",)]
    assert changes == []  # hovering must not switch folders
