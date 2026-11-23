import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from kicad_project_manager import APP_ID  # noqa: E402
from kicad_project_manager.config import Config, Order, Project  # noqa: E402
from kicad_project_manager.ui.orders import COL_DATE, COL_PROJECT, COL_QTY, OrdersWindow, format_order  # noqa: E402


@pytest.fixture(scope="module")
def app():
    QSettings(APP_ID, "orders").clear()
    return QApplication.instance() or QApplication([])


def _config() -> Config:
    c = Config(name="PCBs")
    c.add_project((), Project("Motor", "o/motor", id="m"))
    c.add_project((), Project("Jig", "o/jig", id="j"))

    def order(oid, pid, name, when, qty, maker):
        return Order(pid, f"o/{name.lower()}", name, "c" * 40, maker, when, qty, id=oid)

    c.add_order(order("a", "m", "Motor", "2026-03-01", 5, "JLCPCB"))
    c.add_order(order("b", "m", "Motor", "2026-11-01", 20, "Aisler"))
    c.add_order(order("c", "j", "Jig", "2026-07-15", 100, "PCBWay"))
    c.add_order(order("d", "gone", "Old Board", "2025-12-24", 3, "JLCPCB"))
    return c


def _column(window: OrdersWindow, col: int) -> list[str]:
    return [window.proxy.index(r, col).data() for r in range(window.proxy.rowCount())]


def test_orders_window_sorting_and_filters(app):
    window = OrdersWindow(lambda o: None, lambda o: None, lambda o: None)
    window.set_config(_config())
    # newest first by default, chronologically even across years
    assert _column(window, COL_DATE) == ["01 Nov 2026", "15 Jul 2026", "01 Mar 2026", "24 Dec 2025"]
    assert "Old Board (removed)" in _column(window, COL_PROJECT)

    window.table.sortByColumn(COL_QTY, Qt.SortOrder.AscendingOrder)
    assert _column(window, COL_QTY) == ["3", "5", "20", "100"]  # numeric, not "100" < "20"

    window.search.setText("jlc")
    assert sorted(_column(window, COL_PROJECT)) == ["Motor", "Old Board (removed)"]
    assert window.count_label.text() == "2 orders, 8 boards (of 4 orders)"

    window.show_project("m", "b")
    assert window.search.text() == ""
    assert _column(window, COL_PROJECT) == ["Motor", "Motor"]
    assert window.selected_order().id == "b"

    # a refresh keeps the filter and the selection
    window.set_config(_config())
    assert _column(window, COL_PROJECT) == ["Motor", "Motor"]
    assert window.selected_order().id == "b"
    window.hide()


def test_format_order():
    order = Order("m", "o/m", "Motor", "c" * 40, "JLCPCB", "2026-11-23", 5)
    assert format_order(order) == "23 Nov 2026 – JLCPCB, 5 pcs"
