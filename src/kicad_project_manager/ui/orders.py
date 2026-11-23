"""The Orders window: every PCB order of every project, sortable and filterable."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from PySide6.QtCore import QModelIndex, QSettings, QSortFilterProxyModel, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import APP_ID
from ..config import Config, Order

ROLE_ORDER_ID = Qt.ItemDataRole.UserRole + 1
ROLE_SORT = Qt.ItemDataRole.UserRole + 2
ROLE_PROJECT = Qt.ItemDataRole.UserRole + 3

COLUMNS = ["Project", "Ordered on", "Ordered from", "Qty", "Ordered by", "Version", "Tag", "Notes"]
COL_PROJECT, COL_DATE, COL_MANUFACTURER, COL_QTY, COL_BY, COL_VERSION, COL_TAG, COL_NOTES = range(len(COLUMNS))


def format_date(iso: str) -> str:
    """``2026-11-23`` → ``23 Nov 2026``; anything unparsable is returned as is."""
    try:
        return date.fromisoformat(iso).strftime("%d %b %Y")
    except ValueError:
        return iso


def format_order(order: Order) -> str:
    """Short one-line description, e.g. for menus: ``23 Nov 2026 – JLCPCB, 5 pcs``."""
    return f"{format_date(order.ordered_on)} – {order.manufacturer}, {order.quantity} pcs"


def _version_text(order: Order) -> str:
    text = order.commit[:7]
    if order.commit_date:
        try:
            text += f" ({datetime.fromisoformat(order.commit_date).astimezone():%d %b %Y %H:%M})"
        except ValueError:
            pass
    return text


class _OrderFilter(QSortFilterProxyModel):
    """Matches the search text in any column, and optionally only one project."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project_id = ""
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(-1)
        self.setSortRole(ROLE_SORT)

    def set_project(self, project_id: str) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+, where invalidateFilter() is deprecated
            self.beginFilterChange()
            self.project_id = project_id
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.project_id = project_id
            self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        if self.project_id:
            index = self.sourceModel().index(row, COL_PROJECT, parent)
            if index.data(ROLE_PROJECT) != self.project_id:
                return False
        return super().filterAcceptsRow(row, parent)


class OrdersWindow(QWidget):
    """A non-modal window listing all orders. Changes go through the callbacks, so it holds no server logic."""

    def __init__(
        self,
        on_open: Callable[[Order], None],
        on_edit: Callable[[Order], None],
        on_delete: Callable[[Order], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("PCB Orders")
        self.resize(1000, 520)
        self.on_open, self.on_edit, self.on_delete = on_open, on_edit, on_delete
        self.config: Config | None = None
        self._orders: dict[str, Order] = {}

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search orders…")
        self.search.setClearButtonEnabled(True)
        self.project_filter = QComboBox()
        self.project_filter.setMinimumWidth(220)
        self.count_label = QLabel()

        self.model = QStandardItemModel(0, len(COLUMNS), self)
        self.model.setHorizontalHeaderLabels(COLUMNS)
        self.proxy = _OrderFilter(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(lambda index: self._call(self.on_open, index))
        QShortcut(QKeySequence.StandardKey.Delete, self.table, activated=lambda: self._call(self.on_delete))
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.search.setFocus)

        self.search.textChanged.connect(self._search_changed)
        self.project_filter.currentIndexChanged.connect(lambda *_: self._project_changed())

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(QLabel("Project:"))
        top.addWidget(self.project_filter)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.count_label)

        qs = QSettings(APP_ID, "orders")
        if geometry := qs.value("geometry"):
            self.restoreGeometry(geometry)
        header_state = qs.value("header")
        if header_state:
            header.restoreState(header_state)
        else:
            for col, width in ((COL_PROJECT, 200), (COL_DATE, 100), (COL_MANUFACTURER, 120), (COL_QTY, 50), (COL_BY, 130),
                               (COL_VERSION, 170), (COL_TAG, 200)):  # fmt: skip
                self.table.setColumnWidth(col, width)
            self.table.sortByColumn(COL_DATE, Qt.SortOrder.DescendingOrder)

    # -- data -------------------------------------------------------------------

    def set_config(self, config: Config) -> None:
        """Show the orders of ``config``, keeping filters, sorting and the selection."""
        selected = self.selected_order()
        self.config = config
        projects = {p.id: p.name for _path, p in config.iter_projects()}
        self._orders = {o.id: o for o in config.orders}

        current_project = self.project_filter.currentData() or ""
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("All projects", "")
        names: dict[str, str] = {}
        for o in config.orders:
            names[o.project_id] = projects.get(o.project_id) or f"{o.project_name} (removed)"
        for pid, name in sorted(names.items(), key=lambda item: item[1].casefold()):
            self.project_filter.addItem(name, pid)
        self.project_filter.setCurrentIndex(max(0, self.project_filter.findData(current_project)))
        self.project_filter.blockSignals(False)
        self.proxy.set_project(self.project_filter.currentData() or "")

        sorting = self.table.horizontalHeader().sortIndicatorSection(), self.table.horizontalHeader().sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self.model.removeRows(0, self.model.rowCount())
        for o in config.orders:
            self.model.appendRow(self._row(o, names[o.project_id]))
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(*sorting)
        if selected is not None:
            self.select_order(selected.id)
        self._update_count()

    @staticmethod
    def _row(order: Order, project_name: str) -> list[QStandardItem]:
        def item(text: str, sort=None) -> QStandardItem:
            it = QStandardItem(text)
            it.setData(order.id, ROLE_ORDER_ID)
            it.setData(text.casefold() if sort is None else sort, ROLE_SORT)
            return it

        project = item(project_name)
        project.setData(order.project_id, ROLE_PROJECT)
        project.setToolTip(order.repo)
        qty = item(str(order.quantity), order.quantity)
        qty.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        version = item(_version_text(order), order.commit_date or order.commit)
        version.setToolTip(order.commit)
        notes = item(order.notes.replace("\n", " "))
        notes.setToolTip(order.notes)
        return [
            project,
            item(format_date(order.ordered_on), f"{order.ordered_on}|{order.id}"),
            item(order.manufacturer),
            qty,
            item(order.ordered_by),
            version,
            item(order.tag or "(none)", order.tag),
            notes,
        ]

    def _update_count(self) -> None:
        shown, total = self.proxy.rowCount(), self.model.rowCount()
        boards = sum(self._orders[self.proxy.index(r, 0).data(ROLE_ORDER_ID)].quantity for r in range(shown))
        text = f"{shown} order{'' if shown == 1 else 's'}, {boards} board{'' if boards == 1 else 's'}"
        if shown != total:
            text += f" (of {total} orders)"
        self.count_label.setText(text)

    # -- filters and selection --------------------------------------------------

    def _search_changed(self, text: str) -> None:
        self.proxy.setFilterFixedString(text)
        self._update_count()

    def _project_changed(self) -> None:
        self.proxy.set_project(self.project_filter.currentData() or "")
        self._update_count()

    def show_project(self, project_id: str = "", order_id: str = "") -> None:
        """Bring the window up, filtered to one project (or all), with an order selected."""
        self.search.clear()
        index = self.project_filter.findData(project_id)
        self.project_filter.setCurrentIndex(max(0, index))
        if order_id:
            self.select_order(order_id)
        self.show()
        self.raise_()
        self.activateWindow()

    def select_order(self, order_id: str) -> None:
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if index.data(ROLE_ORDER_ID) == order_id:
                self.table.selectRow(row)
                self.table.scrollTo(index)
                return

    def selected_order(self) -> Order | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self._orders.get(rows[0].data(ROLE_ORDER_ID)) if rows else None

    def _order_at(self, index: QModelIndex | None) -> Order | None:
        if index is not None and index.isValid():
            return self._orders.get(index.data(ROLE_ORDER_ID))
        return self.selected_order()

    def _call(self, callback: Callable[[Order], None], index: QModelIndex | None = None) -> None:
        order = self._order_at(index)
        if order is not None:
            callback(order)

    def _menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        order = self._order_at(index) if index.isValid() else None
        if order is None:
            return
        menu = QMenu(self)
        open_action = menu.addAction("Open Version on Server", lambda: self.on_open(order))
        font = open_action.font()
        font.setBold(True)
        open_action.setFont(font)
        menu.addAction("Copy Commit ID", lambda: QGuiApplication.clipboard().setText(order.commit))
        menu.addSeparator()
        menu.addAction("Edit…", lambda: self.on_edit(order))
        menu.addAction("Delete…", lambda: self.on_delete(order))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def closeEvent(self, event) -> None:  # noqa: N802
        qs = QSettings(APP_ID, "orders")
        qs.setValue("geometry", self.saveGeometry())
        qs.setValue("header", self.table.horizontalHeader().saveState())
        super().closeEvent(event)
