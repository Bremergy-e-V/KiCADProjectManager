"""The main window: folder tree on the left, project cards on the right."""

from __future__ import annotations

import html
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QSplitter,
    QStyle,
    QToolBar,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_ID, APP_NAME, updater
from ..config import Config, ConfigError, ConfigRepo, FolderPath, Project, check_template_name, format_path
from ..forge import Forge, ForgeError, Repo, User, make_forge
from ..git import Git, GitError, GitIdentity
from ..preview import render_preview
from ..settings import Settings, data_dir, get_token, set_token
from ..workspace import LocalState, Workspace, WorkspaceError, find_kicad_tool, find_project_file, launch_kicad
from .dialogs import (
    AddExistingDialog,
    ConfigRepoDialog,
    ConnectDialog,
    ImportProjectDialog,
    InitConfigDialog,
    NewProjectDialog,
    SaveDialog,
    SettingsDialog,
    _WaitCursor,
)
from .tasks import TaskRunner
from .views import ROLE_KEY, CardDelegate, CardInfo, DragItem, FolderTree, ProjectGrid

REFRESH_INTERVAL_MS = 3 * 60 * 1000


@dataclass
class ProjectMeta:
    loaded: bool = False
    last_change: datetime | None = None
    last_author: str = ""
    state: LocalState | None = None
    error: str = ""
    busy: str = ""


def relative_time(when: datetime) -> str:
    now = datetime.now(when.tzinfo)
    seconds = (now - when).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return when.astimezone().strftime("%d %b %Y")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.runner = TaskRunner(self)
        self.forge: Forge | None = None
        self.git: Git | None = None
        self.workspace: Workspace | None = None
        self.config_repo: ConfigRepo | None = None
        self.config: Config | None = None
        self.current_path: FolderPath = ()
        self.meta: dict[str, ProjectMeta] = {}
        self.repo_info: dict[str, Repo] = {}
        self._projects: dict[str, tuple[FolderPath, Project]] = {}
        self._previews: dict[str, tuple[float, QPixmap]] = {}
        self._refreshing = False
        self._notice = ""

        self.setWindowTitle(APP_NAME)
        self.resize(1200, 760)
        self._build_ui()
        self.refresh_timer = QTimer(self, interval=REFRESH_INTERVAL_MS)
        self.refresh_timer.timeout.connect(self.refresh)

        qs = QSettings(APP_ID, "window")
        if geometry := qs.value("geometry"):
            self.restoreGeometry(geometry)
        if splitter := qs.value("splitter"):
            self.splitter.restoreState(splitter)

    # ------------------------------------------------------------------ UI --

    def _icon(self, theme: str, fallback: QStyle.StandardPixmap) -> QIcon:
        return QIcon.fromTheme(theme, self.style().standardIcon(fallback))

    def _build_ui(self) -> None:
        sp = QStyle.StandardPixmap
        self.folder_icon = self._icon("folder", sp.SP_DirIcon)
        self.root_icon = self._icon("user-home", sp.SP_DirHomeIcon)

        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        def action(text, icon, slot, shortcut=None, tip=""):
            a = QAction(icon, text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            a.setToolTip(tip or text)
            return a

        self.act_new_project = action(
            "New Project",
            self._icon("document-new", sp.SP_FileIcon),
            lambda: self.new_project(),
            QKeySequence.StandardKey.New,
            "Create a new KiCad project and repository",
        )
        self.act_add_existing = action(
            "Add Existing",
            self._icon("list-add", sp.SP_FileDialogNewFolder),
            lambda: self.add_existing(),
            tip="Add existing repositories from the server",
        )
        self.act_import = action(
            "Import Local",
            self._icon("computer", sp.SP_ComputerIcon),
            lambda: self.import_project(),
            tip="Upload a KiCad project from your computer to the server",
        )
        self.act_new_folder = action(
            "New Folder", self._icon("folder-new", sp.SP_FileDialogNewFolder), lambda: self.new_folder(), "Ctrl+Shift+N"
        )
        self.act_refresh = action(
            "Refresh", self._icon("view-refresh", sp.SP_BrowserReload), lambda: self.refresh(visible=True), QKeySequence.StandardKey.Refresh
        )
        settings_icon = QIcon.fromTheme("preferences-system", self._icon("document-properties", sp.SP_FileDialogDetailedView))
        self.act_settings = action("Settings", settings_icon, lambda: self.open_settings())
        for a in (self.act_add_existing, self.act_new_project, self.act_import, self.act_new_folder):
            toolbar.addAction(a)
        toolbar.addSeparator()
        toolbar.addAction(self.act_refresh)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all projects…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(260)
        self.search.textChanged.connect(lambda *_: self._rebuild_grid())
        toolbar.addWidget(self.search)
        toolbar.addAction(self.act_settings)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.search.setFocus)

        self.tree = FolderTree()
        self.tree.currentItemChanged.connect(self._tree_selection_changed)
        self.tree.items_dropped.connect(self._move_items)
        self.tree.customContextMenuRequested.connect(self._tree_menu)

        self.grid = ProjectGrid()
        self.grid.setItemDelegate(CardDelegate(self._card_info, self._folder_pixmap(), self.grid))
        self.grid.itemActivated.connect(self._grid_activated)
        self.grid.items_dropped.connect(self._move_items)
        self.grid.customContextMenuRequested.connect(self._grid_menu)
        QShortcut(QKeySequence(Qt.Key.Key_Backspace), self.grid, activated=self.go_up)
        QShortcut(QKeySequence("Alt+Up"), self, activated=self.go_up)
        QShortcut(QKeySequence(Qt.Key.Key_F2), self.grid, activated=self._rename_selected)
        QShortcut(QKeySequence.StandardKey.Delete, self.grid, activated=self._delete_selected)

        self.breadcrumb = QLabel()
        self.breadcrumb.setTextFormat(Qt.TextFormat.RichText)
        self.breadcrumb.linkActivated.connect(self._breadcrumb_clicked)
        font = self.breadcrumb.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        self.breadcrumb.setFont(font)
        self.empty_hint = QLabel()
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setStyleSheet("color: palette(placeholder-text); padding: 40px;")
        self.empty_hint.hide()

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 8, 4, 0)
        right_layout.addWidget(self.breadcrumb)
        right_layout.addWidget(self.empty_hint)
        right_layout.addWidget(self.grid, 1)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([260, 940])
        self.setCentralWidget(self.splitter)

        self.status_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(140)
        self.progress.setMaximumHeight(14)
        self.progress.hide()
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self.runner.busy_changed.connect(self._busy_changed)
        self._set_enabled(False)
        # Follow the system when it switches between light and dark mode while the app runs.
        QApplication.styleHints().colorSchemeChanged.connect(lambda *_: QTimer.singleShot(0, self._color_scheme_changed))

    def _folder_pixmap(self) -> QPixmap:
        return self.folder_icon.pixmap(QSize(72, 72), self.devicePixelRatioF())

    def _color_scheme_changed(self) -> None:
        """Icons are rendered for the current colours, so render them again."""
        self.grid.itemDelegate().folder_pixmap = self._folder_pixmap()
        if self.config is not None:
            self._rebuild_tree()
        self.grid.viewport().update()

    def _set_enabled(self, enabled: bool) -> None:
        for a in (self.act_new_project, self.act_add_existing, self.act_import, self.act_new_folder, self.act_refresh):
            a.setEnabled(enabled)

    def _busy_changed(self, label: str) -> None:
        self.progress.setVisible(bool(label))
        self.status_label.setText(label or self._notice)

    def notify(self, text: str) -> None:
        self._notice = text
        self.status_label.setText(text)

    def show_error(self, error: BaseException, title: str = "Something went wrong") -> None:
        if isinstance(error, GitError):
            text = f"A git operation failed:\n\n{error.output or error}"
        else:
            text = str(error)
        QMessageBox.warning(self, title, text)

    def closeEvent(self, event) -> None:  # noqa: N802
        qs = QSettings(APP_ID, "window")
        qs.setValue("geometry", self.saveGeometry())
        qs.setValue("splitter", self.splitter.saveState())
        super().closeEvent(event)

    # --------------------------------------------------------------- setup --

    def start(self) -> None:
        if not self._ensure_account():
            self.close()
            return
        if not self.settings.config_repo and not self._choose_config_repo():
            self.close()
            return
        self._open_config()
        if self.settings.check_updates:
            # Not right away: the project list should be up before anything else asks for attention.
            QTimer.singleShot(4000, lambda: self.check_for_updates(user_asked=False))

    def _ensure_account(self, force_dialog: bool = False) -> bool:
        message = ""
        kind, url, user = self.settings.forge, self.settings.server_url, self.settings.username
        token = get_token(url, user)
        if token and not force_dialog:
            try:
                forge = make_forge(kind, url, token, timeout=10)
                with _WaitCursor():
                    forge.current_user()
                self._set_account(forge)
                return True
            except ForgeError as e:
                message = str(e) if e.status == 0 else "Your saved access token is no longer valid. Please sign in again."
        dialog = ConnectDialog(kind, url, message, self)
        if not dialog.exec():
            return False
        set_token(dialog.server_url, dialog.user.login, dialog.token_value)
        self.settings.forge, self.settings.server_url, self.settings.username = dialog.forge_kind, dialog.server_url, dialog.user.login
        self.settings.save()
        forge = make_forge(dialog.forge_kind, dialog.server_url, dialog.token_value)
        forge._user = dialog.user
        self._set_account(forge)
        return True

    def _set_account(self, forge: Forge) -> None:
        user: User = forge.current_user()
        forge.timeout = 20
        self.forge = forge
        self.git = Git(
            GitIdentity(
                host_url=forge.git_host_url,
                username=forge.git_username(user),
                token=forge.token,
                name=user.name or user.login,
                email=user.email or f"{user.login}@users.noreply",
            )
        )
        self.workspace = Workspace(self.git, Path(self.settings.workspace).expanduser())
        self.repo_info.clear()

    def _choose_config_repo(self) -> bool:
        dialog = ConfigRepoDialog(self.forge, self.settings.config_repo, self)
        if not dialog.exec():
            return False
        self.settings.config_repo = dialog.repo_full_name
        self.settings.save()
        return True

    def clone_url(self, repo: str) -> str:
        """Clone URL for ``owner/name`` (blocking - call from a worker)."""
        info = self.repo_info.get(repo.casefold())
        if info is None:
            info = self.forge.get_repo(repo)
            self.repo_info[repo.casefold()] = info
        return info.ssh_url if self.settings.clone_protocol == "ssh" else info.clone_url

    def _open_config(self) -> None:
        repo = self.settings.config_repo
        host = urlparse(self.settings.server_url).hostname or "server"
        path = data_dir() / "config" / f"{host}__{repo.replace('/', '__')}"
        self.refresh_timer.stop()
        self._set_enabled(False)
        self.config = None
        self.config_repo = None
        self.tree.clear()
        self.grid.clear()

        def work():
            config_repo = ConfigRepo(self.git, path, self.clone_url(repo))
            exists = config_repo.open()
            return config_repo, config_repo.load() if exists else None

        def done(result):
            self.config_repo, config = result
            if config is None:
                self._initialize_config()
            else:
                self._config_ready(config)

        def failed(error):
            box = QMessageBox(
                QMessageBox.Icon.Warning,
                "Could not open project list",
                f"Could not open the config repository {repo}:\n\n{error}",
                parent=self,
            )
            retry = box.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
            other = box.addButton("Choose another…", QMessageBox.ButtonRole.ActionRole)
            box.addButton("Quit", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is retry:
                self._open_config()
            elif box.clickedButton() is other and self._choose_config_repo():
                self._open_config()
            else:
                self.close()

        self.runner.run(work, done, failed, label="Loading project list…")

    def _initialize_config(self) -> None:
        try:
            with _WaitCursor():
                owners = self.forge.owners()
        except ForgeError as e:
            self.show_error(e)
            owners = [self.settings.username]
        dialog = InitConfigDialog(self.settings.config_repo, owners, self)
        if not dialog.exec():
            if self._choose_config_repo():
                self._open_config()
            else:
                self.close()
            return
        config = Config(
            name=dialog.name.text().strip() or "PCB Projects",
            server_url=self.settings.server_url,
            default_owner=dialog.owner.currentData(),
            default_private=dialog.private.isChecked(),
        )
        self.runner.run(
            partial(self.config_repo.initialize, config),
            self._config_ready,
            lambda e: (self.show_error(e, "Could not create project list"), self.close()),
            label="Creating project list…",
        )

    def _config_ready(self, config: Config) -> None:
        self._set_enabled(True)
        self.current_path = ()
        self._apply_config(config)
        self._refresh_meta()
        self.refresh_timer.start()
        self.notify(f"{_plural(len(self._projects), 'project')} in {config.name}")

    # ------------------------------------------------------- config display --

    def _apply_config(self, config: Config) -> None:
        self.config = config
        self._projects = {p.id: (path, p) for path, p in config.iter_projects()}
        while self.current_path and not config.has_folder(self.current_path):
            self.current_path = self.current_path[:-1]
        self.setWindowTitle(f"{config.name} - {APP_NAME}")
        self._rebuild_tree()
        self._rebuild_grid()

    def _expanded_paths(self) -> set[FolderPath]:
        result = set()

        def walk(item: QTreeWidgetItem):
            if item.isExpanded():
                result.add(FolderTree.path_of(item))
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return result

    def _rebuild_tree(self) -> None:
        expanded = self._expanded_paths()
        self.tree.blockSignals(True)
        self.tree.clear()
        root = QTreeWidgetItem([self.config.name])
        root.setData(0, ROLE_KEY, [])
        root.setIcon(0, self.root_icon)
        bold = QFont(root.font(0))
        bold.setBold(True)
        root.setFont(0, bold)
        self.tree.addTopLevelItem(root)

        def add(parent_item, folder, path):
            for sub in sorted(folder.folders, key=lambda f: f.name.casefold()):
                sub_path = (*path, sub.name)
                item = QTreeWidgetItem([sub.name])
                item.setData(0, ROLE_KEY, list(sub_path))
                item.setIcon(0, self.folder_icon)
                item.setToolTip(0, _plural(sub.project_count(), "project"))
                parent_item.addChild(item)
                add(item, sub, sub_path)
                item.setExpanded(sub_path in expanded)

        add(root, self.config.root, ())
        root.setExpanded(True)
        self._select_in_tree(self.current_path)
        self.tree.blockSignals(False)

    def _tree_item(self, path: FolderPath) -> QTreeWidgetItem | None:
        item = self.tree.topLevelItem(0)
        for part in path:
            if item is None:
                return None
            item = next((item.child(i) for i in range(item.childCount()) if item.child(i).text(0) == part), None)
        return item

    def _select_in_tree(self, path: FolderPath) -> None:
        item = self._tree_item(path)
        if item is None:
            return
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self.tree.setCurrentItem(item)

    def _rebuild_grid(self) -> None:
        if self.config is None:
            return
        selected = {self.grid.item_key(i) for i in self.grid.selectedItems()}
        self.grid.clear()
        query = self.search.text().strip().casefold()
        if query:
            for path, p in self.config.iter_projects():
                if query in f"{p.name} {p.repo} {p.description}".casefold():
                    self.grid.add_card("project", p.id, p.name, f"{p.repo}\nFolder: {format_path(path)}")
            self.breadcrumb.setText(f"Search results for <b>{html.escape(self.search.text())}</b>")
            hint = "No projects match your search."
        else:
            folder = self.config.folder(self.current_path)
            for sub in sorted(folder.folders, key=lambda f: f.name.casefold()):
                self.grid.add_card("folder", (*self.current_path, sub.name), sub.name)
            for p in sorted(folder.projects, key=lambda p: p.name.casefold()):
                tooltip = p.repo + (f"\n{p.description}" if p.description else "")
                self.grid.add_card("project", p.id, p.name, tooltip)
            self._update_breadcrumb()
            hint = "This folder is empty.\n\nCreate a new project or folder, add existing projects, or drag projects here."
        for i in range(self.grid.count()):
            if self.grid.item_key(self.grid.item(i)) in selected:
                self.grid.item(i).setSelected(True)
        self.empty_hint.setText(hint)
        self.empty_hint.setVisible(self.grid.count() == 0)
        self.grid.setVisible(self.grid.count() > 0)

    def _update_breadcrumb(self) -> None:
        names = [self.config.name, *self.current_path]
        parts = []
        for i, name in enumerate(names):
            if i == len(names) - 1:
                parts.append(f"<b>{html.escape(name)}</b>")
            else:
                parts.append(f"<a href='{i}'>{html.escape(name)}</a>")
        self.breadcrumb.setText("<style>a { text-decoration: none; }</style>" + " &nbsp;›&nbsp; ".join(parts))

    def _breadcrumb_clicked(self, link: str) -> None:
        self.navigate(self.current_path[: int(link)])

    def navigate(self, path: FolderPath) -> None:
        if self.search.text():
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self.current_path = path
        self.tree.blockSignals(True)
        self._select_in_tree(path)
        self.tree.blockSignals(False)
        self._rebuild_grid()

    def go_up(self) -> None:
        if self.current_path:
            self.navigate(self.current_path[:-1])

    def _tree_selection_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is not None:
            self.navigate(FolderTree.path_of(current))

    # -------------------------------------------------------------- cards --

    def _card_info(self, kind: str, key) -> CardInfo:
        if self.config is None:
            return CardInfo(title="")
        if kind == "folder":
            try:
                folder = self.config.folder(key)
            except ConfigError:
                return CardInfo(title=key[-1])
            subtitle = _plural(folder.project_count(), "project")
            if folder.folders:
                subtitle += f", {_plural(len(folder.folders), 'folder')}"
            return CardInfo(title=folder.name, subtitle=subtitle)

        entry = self._projects.get(key)
        if entry is None:
            return CardInfo(title="")
        project = entry[1]
        meta = self.meta.get(key) or ProjectMeta()
        info = CardInfo(title=project.name, preview=self._preview(project.id))
        if meta.last_change:
            info.subtitle = f"Changed {relative_time(meta.last_change)}" + (f" by {meta.last_author}" if meta.last_author else "")
        elif meta.loaded and not meta.error:
            info.subtitle = "No changes yet"
        else:
            info.subtitle = project.repo
        info.status, info.status_kind = self._status_text(meta)
        return info

    @staticmethod
    def _status_text(meta: ProjectMeta) -> tuple[str, str]:
        if meta.busy:
            return meta.busy, "busy"
        if meta.error:
            return meta.error, "error"
        state = meta.state
        if state is None:
            return "", "muted"
        if not state.cloned:
            return "Not downloaded", "muted"
        if state.changed_files or state.ahead:
            text = "Changes not on server yet"
            if state.behind:
                text += f", {state.behind} new on server"
            return text, "warn"
        if state.behind:
            return f"{_plural(state.behind, 'new change')} on server", "info"
        if state.offline:
            return "Offline", "muted"
        return "Up to date", "ok"

    def _preview(self, project_id: str) -> QPixmap | None:
        if self.config_repo is None:
            return None
        path = self.config_repo.preview_file(project_id)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._previews.get(project_id)
        if cached and cached[0] == mtime:
            return cached[1]
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return None
        ratio = self.devicePixelRatioF()
        pixmap = pixmap.scaled(
            CardDelegate.preview_area() * ratio, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        pixmap.setDevicePixelRatio(ratio)
        self._previews[project_id] = (mtime, pixmap)
        return pixmap

    def _repaint_cards(self) -> None:
        self.grid.viewport().update()

    # ------------------------------------------------------------ refresh --

    def refresh(self, visible: bool = False) -> None:
        if self.config_repo is None or self._refreshing:
            return
        self._refreshing = True

        def work():
            self.config_repo.sync()
            return self.config_repo.load()

        def done(config: Config):
            self._refreshing = False
            if self.config is None or config.to_dict() != self.config.to_dict():
                self._apply_config(config)
            self._previews.clear()
            self._refresh_meta()

        def failed(error):
            self._refreshing = False
            self.notify(f"Could not refresh: {error}")

        self.runner.run(work, done, failed, label="Refreshing…" if visible else None)

    def _refresh_meta(self, ids: list[str] | None = None, fetch: bool = True) -> None:
        for pid in ids if ids is not None else list(self._projects):
            entry = self._projects.get(pid)
            if entry is None:
                continue
            self.runner.run(partial(self._load_meta, entry[1], fetch), partial(self._meta_loaded, pid))

    def _load_meta(self, project: Project, fetch: bool) -> ProjectMeta:
        meta = ProjectMeta(loaded=True)
        try:
            last = self.forge.last_commit(project.repo)
            if last:
                meta.last_change, meta.last_author = last
        except ForgeError as e:
            if e.status in (403, 404):
                meta.error = "Repository not found or no access"
            elif e.status == 0:
                meta.error = "Server not reachable"
            else:
                meta.error = str(e)
        meta.state = self.workspace.state(project, fetch=fetch and not meta.error)
        return meta

    def _meta_loaded(self, pid: str, meta: ProjectMeta) -> None:
        old = self.meta.get(pid)
        if old is not None:
            meta.busy = old.busy
        self.meta[pid] = meta
        self._repaint_cards()

    # ----------------------------------------------------- config changes --

    def _mutate(
        self, change: Callable[[Config, ConfigRepo | None], object], message: str, on_done: Callable[[Config], None] | None = None
    ) -> None:
        """Apply a change to the shared config: locally right away, then on the server."""
        preview = Config.from_dict(self.config.to_dict())
        try:
            change(preview, None)
        except ConfigError as e:
            self.show_error(e, "Not possible")
            return
        self._apply_config(preview)

        def done(config: Config):
            self._apply_config(config)
            if on_done:
                on_done(config)

        def failed(error):
            self.show_error(error, "Could not save the project list")
            self.refresh()

        self.runner.run(partial(self.config_repo.mutate, change, message), done, failed, label="Saving project list…")

    def new_folder(self, parent: FolderPath | None = None) -> None:
        parent = self.current_path if parent is None else parent
        name, ok = QInputDialog.getText(self, "New folder", f"Name of the new folder in '{format_path(parent)}':")
        if ok and name.strip():
            self._mutate(lambda c, _r: c.add_folder(parent, name), f"Create folder {format_path((*parent, name.strip()))}")

    def rename_folder(self, path: FolderPath) -> None:
        name, ok = QInputDialog.getText(self, "Rename folder", "New name:", text=path[-1])
        if not ok or not name.strip() or name.strip() == path[-1]:
            return
        new_path = (*path[:-1], name.strip())

        def change(c, _r):
            c.rename_folder(path, name)

        if self.current_path[: len(path)] == path:
            self.current_path = new_path + self.current_path[len(path) :]
        self._mutate(change, f"Rename folder {format_path(path)} to {name.strip()}")

    def delete_folder(self, path: FolderPath) -> None:
        if QMessageBox.question(self, "Delete folder", f"Delete the empty folder '{format_path(path)}'?") != QMessageBox.StandardButton.Yes:
            return
        self._mutate(lambda c, _r: c.delete_folder(path), f"Delete folder {format_path(path)}")

    def _move_items(self, items: list[DragItem], target: FolderPath) -> None:
        moves = []
        for kind, key in items:
            if kind == "folder" and key[:-1] != target and key != target:
                moves.append((kind, key))
            elif kind == "project" and key in self._projects and self._projects[key][0] != target:
                moves.append((kind, key))
        if not moves:
            return

        def change(c: Config, _r):
            for kind, key in moves:
                if kind == "folder":
                    c.move_folder(key, target)
                else:
                    c.move_project(key, target)

        names = [key[-1] if kind == "folder" else self._projects[key][1].name for kind, key in moves]
        what = names[0] if len(names) == 1 else f"{len(names)} items"
        self._mutate(change, f"Move {what} to {format_path(target)}")

    def rename_project(self, pid: str) -> None:
        project = self._projects[pid][1]
        name, ok = QInputDialog.getText(self, "Rename project", "Display name (the repository keeps its name):", text=project.name)
        if ok and name.strip() and name.strip() != project.name:
            self._mutate(lambda c, _r: c.rename_project(pid, name), f"Rename project {project.name} to {name.strip()}")

    def remove_project(self, pid: str) -> None:
        project = self._projects[pid][1]
        answer = QMessageBox.question(
            self,
            "Remove from list",
            f"Remove '{project.name}' from the project list?\n\n"
            "The repository on the server and your local copy are not deleted. "
            "You can add it again with 'Add Existing'.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def change(c: Config, repo: ConfigRepo | None):
            c.remove_project(pid)
            if repo is not None:
                repo.preview_file(pid).unlink(missing_ok=True)

        self._mutate(change, f"Remove project {project.name}")

    def _rename_selected(self) -> None:
        items = self.grid.selectedItems()
        if len(items) == 1:
            kind, key = self.grid.item_key(items[0])
            self.rename_folder(key) if kind == "folder" else self.rename_project(key)

    def _delete_selected(self) -> None:
        items = self.grid.selectedItems()
        if len(items) == 1:
            kind, key = self.grid.item_key(items[0])
            self.delete_folder(key) if kind == "folder" else self.remove_project(key)

    # ----------------------------------------------------------- previews --

    def _render(self, project: Project) -> Path | None:
        """Render a preview into a temp dir (blocking). Caller removes ``result.parent``."""
        cli = self.settings.kicad_cli_path or find_kicad_tool("kicad-cli")
        if not cli:
            return None
        tmp = Path(tempfile.mkdtemp(prefix="kpm-preview-"))
        source = self.workspace.path_for(project)
        if not self.workspace.is_cloned(project) or self.workspace.changed_files(project):
            source = tmp / "src"
            try:
                self.git.run("clone", "-q", "--depth", "1", self.clone_url(project.repo), str(source))
            except GitError:
                shutil.rmtree(tmp, ignore_errors=True)
                return None
        out = tmp / "preview.png"
        if render_preview(cli, source, out, project.repo_name):
            return out
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    def _publish_previews(self, images: list[tuple[Project, Path]]) -> Config | None:
        """Commit rendered previews to the config repository (blocking)."""
        if not images:
            return None

        def change(c: Config, repo: ConfigRepo | None):
            for project, image in images:
                if repo is not None and any(p.id == project.id for _, p in c.iter_projects()):
                    target = repo.preview_file(project.id)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(image, target)

        message = f"Update preview of {images[0][0].name}" if len(images) == 1 else f"Update {len(images)} previews"
        try:
            return self.config_repo.mutate(change, message)
        finally:
            for _, image in images:
                shutil.rmtree(image.parent, ignore_errors=True)

    def _render_and_publish(self, projects: list[Project]) -> Config | None:
        images = [(p, img) for p in projects if (img := self._render(p)) is not None]
        return self._publish_previews(images)

    def update_preview(self, pid: str) -> None:
        project = self._projects[pid][1]

        def work():
            config = self._render_and_publish([project])
            if config is None:
                raise WorkspaceError(
                    "Could not render a preview. Check that kicad-cli is set in Settings "
                    "and that the project contains a board or schematic."
                )
            return config

        self._project_task(pid, "Rendering preview…", work, self._apply_config)

    def _generate_previews(self, projects: list[Project]) -> None:
        if projects:
            self.runner.run(
                partial(self._render_and_publish, projects),
                lambda config: config and self._apply_config(config),
                lambda e: self.notify(f"Could not create previews: {e}"),
                label=f"Rendering {_plural(len(projects), 'preview')}…",
            )

    # ------------------------------------------------- project operations --

    def _project_task(
        self,
        pid: str,
        busy: str,
        work: Callable[[], object],
        on_done: Callable[[object], None] | None = None,
        error_title: str = "Something went wrong",
    ) -> None:
        meta = self.meta.setdefault(pid, ProjectMeta())
        if meta.busy:
            self.notify(f"Please wait - {meta.busy.lower()}")
            return
        meta.busy = busy
        self._repaint_cards()
        name = self._projects[pid][1].name

        def finish():
            if pid in self.meta:
                self.meta[pid].busy = ""
            self._repaint_cards()
            self._refresh_meta([pid], fetch=False)

        def done(result):
            finish()
            if on_done:
                on_done(result)

        def failed(error):
            finish()
            self.show_error(error, error_title)

        self.runner.run(work, done, failed, label=f"{busy[:-1] if busy.endswith('…') else busy} {name}…")

    def _kicad(self) -> str | None:
        kicad = self.settings.kicad_path or find_kicad_tool("kicad")
        if not kicad:
            QMessageBox.warning(self, "KiCad not found", "KiCad could not be found. Please set its location in Settings.")
        return kicad

    def open_project(self, pid: str) -> None:
        project = self._projects[pid][1]
        kicad = self._kicad()
        if not kicad:
            return
        already_open = bool(self.workspace.open_lock_files(project))
        cloned = self.workspace.is_cloned(project)

        def work():
            url = self.clone_url(project.repo)
            if not self.workspace.is_cloned(project):
                self.workspace.download(project, url)
                return ""
            self.workspace.set_url(project, url)
            if already_open:
                return ""
            try:
                self.workspace.get_latest(project, self.settings.username)
            except WorkspaceError as e:
                return str(e)
            return ""

        def done(warning: str):
            pro = find_project_file(self.workspace.path_for(project), project.repo_name)
            if pro is None:
                QMessageBox.information(self, project.name, "This repository does not contain a KiCad project (.kicad_pro) file.")
                return
            launch_kicad(kicad, pro)
            self.notify(f"Opened {project.name} in KiCad")
            if warning and "Could not reach" not in warning:
                QMessageBox.information(self, project.name, warning)

        self._project_task(pid, "Getting latest…" if cloned else "Downloading…", work, done)

    def get_latest(self, pid: str) -> None:
        project = self._projects[pid][1]
        if not self.workspace.is_cloned(project):
            self._project_task(
                pid,
                "Downloading…",
                lambda: self.workspace.download(project, self.clone_url(project.repo)),
                lambda _: self.notify(f"Downloaded {project.name}"),
            )
            return
        if self.workspace.open_lock_files(project):
            answer = QMessageBox.question(
                self,
                "Project is open in KiCad",
                f"'{project.name}' seems to be open in KiCad. Please close it before getting the latest version, "
                "otherwise KiCad may overwrite the new files.\n\nContinue anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        def work():
            self.workspace.set_url(project, self.clone_url(project.repo))
            return self.workspace.get_latest(project, self.settings.username)

        self._project_task(pid, "Getting latest…", work, lambda msg: self.notify(f"{project.name}: {msg}"))

    def save_project(self, pid: str) -> None:
        project = self._projects[pid][1]
        if not self.workspace.is_cloned(project):
            QMessageBox.information(self, project.name, "This project is not downloaded yet, so there is nothing to save.")
            return
        changed = self.workspace.changed_files(project)
        meta = self.meta.get(pid)
        if not changed and not (meta and meta.state and meta.state.ahead):
            QMessageBox.information(self, project.name, "There are no changes to save.\n\nDid you save your work in KiCad (Ctrl+S)?")
            return
        dialog = SaveDialog(project.name, changed, self)
        if not dialog.exec():
            return
        message = dialog.text() or f"Update {project.name}"

        def work():
            self.workspace.set_url(project, self.clone_url(project.repo))
            result = self.workspace.save(project, message, self.settings.username)
            config = None if result.startswith("Nothing") else self._render_and_publish([project])
            return result, config

        def done(result):
            text, config = result
            self.notify(f"{project.name}: {text}")
            if config is not None:
                self._apply_config(config)

        self._project_task(pid, "Saving…", work, done, error_title="Could not save to server")

    def _owners(self) -> list[str] | None:
        try:
            with _WaitCursor():
                return self.forge.owners()
        except ForgeError as e:
            self.show_error(e)
            return None

    def new_project(self) -> None:
        parent = self.current_path
        owners = self._owners()
        if owners is None:
            return
        templates = list(self.config_repo.templates())
        dialog = NewProjectDialog(owners, self.config.default_owner, self.config.default_private, format_path(parent), templates, self)
        if not dialog.exec():
            return
        title = dialog.name.text().strip()
        template_name = dialog.template.currentData()

        def setup(project: Project, url: str) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                template = None
                with self.config_repo.lock:
                    source = self.config_repo.templates().get(template_name) if template_name else None
                    if source is not None:
                        template = Path(tmp) / "template"
                        shutil.copytree(source, template)
                if template_name and template is None:
                    raise WorkspaceError(f"The template '{template_name}' no longer exists. Someone may have removed it.")
                self.workspace.create(project, url, title, template)

        self._create_project(dialog, parent, setup, "Created", "Could not create project")

    def import_project(self) -> None:
        parent = self.current_path
        owners = self._owners()
        if owners is None:
            return
        dialog = ImportProjectDialog(owners, self.config.default_owner, self.config.default_private, format_path(parent), self)
        if not dialog.exec():
            return
        title, source = dialog.name.text().strip(), dialog.source_path()

        def setup(project: Project, url: str) -> None:
            self.workspace.import_local(project, url, title, source)

        self._create_project(dialog, parent, setup, "Imported", "Could not import project")

    def _create_project(
        self,
        dialog: NewProjectDialog,
        parent: FolderPath,
        setup: Callable[[Project, str], None],
        verb: str,
        error_title: str,
    ) -> None:
        """Create the repository, fill it with ``setup`` and add it to the project list."""
        title = dialog.name.text().strip()
        owner, repo_name = dialog.owner.currentText(), dialog.repo.text().strip()
        project = Project(name=title, repo=f"{owner}/{repo_name}", description=dialog.description.text().strip())
        private, open_after = dialog.private.isChecked(), dialog.open_after.isChecked()

        def work():
            info = self.forge.create_repo(owner, repo_name, project.description, private)
            self.repo_info[project.repo.casefold()] = info
            setup(project, self.clone_url(project.repo))
            config = self.config_repo.mutate(lambda c, _r: c.add_project(parent, project), f"Add project {title}")
            return self._render_and_publish([project]) or config

        def done(config: Config):
            self._apply_config(config)
            self._refresh_meta([project.id], fetch=False)
            self.notify(f"{verb} {title}")
            if open_after:
                self.open_project(project.id)

        def failed(error):
            extra = ""
            if project.repo.casefold() in self.repo_info:
                extra = f"\n\nThe repository {project.repo} was created on the server. You can add it with 'Add Existing'."
            QMessageBox.warning(self, error_title, f"{error}{extra}")
            self.refresh()

        self.runner.run(work, done, failed, label=f"{'Creating' if verb == 'Created' else 'Importing'} {title}…")

    def revert_project(self, pid: str) -> None:
        project = self._projects[pid][1]
        if not self.workspace.is_cloned(project):
            QMessageBox.information(self, project.name, "This project is not downloaded yet, so there is nothing to revert.")
            return
        changed = self.workspace.changed_files(project)
        meta = self.meta.get(pid)
        ahead = meta.state.ahead if meta and meta.state else 0
        if not changed and not ahead:
            QMessageBox.information(self, project.name, "There are no local changes - your copy already matches the server.")
            return
        details = []
        if changed:
            shown = "\n".join(f"  • {f}" for f in changed[:15])
            more = f"\n  … and {len(changed) - 15} more" if len(changed) > 15 else ""
            details.append(f"{_plural(len(changed), 'changed file')}:\n{shown}{more}")
        if ahead:
            details.append(f"{_plural(ahead, 'saved change')} that never reached the server.")
        if self.workspace.open_lock_files(project):
            details.append("The project seems to be open in KiCad. Close it first, or KiCad may write your changes back.")
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Revert to server state",
            f"Throw away all local changes to '{project.name}' and go back to the version on the server?\n\n"
            + "\n\n".join(details)
            + "\n\nThis cannot be undone.",
            parent=self,
        )
        revert = box.addButton("Discard My Changes", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not revert:
            return

        def work():
            self.workspace.set_url(project, self.clone_url(project.repo))
            return self.workspace.revert(project)

        self._project_task(pid, "Reverting…", work, lambda msg: self.notify(f"{project.name}: {msg}"), "Could not revert")

    def convert_to_template(self, pid: str) -> None:
        project = self._projects[pid][1]
        name, ok = QInputDialog.getText(
            self,
            "Convert to template",
            "Name of the new template.\nIt is made from the version on the server and offered under 'New Project':",
            text=project.name,
        )
        if not ok or not name.strip():
            return
        try:
            name = check_template_name(name)
        except ConfigError as e:
            self.show_error(e, "Not possible")
            return

        def work():
            with tempfile.TemporaryDirectory(prefix="kpm-template-") as tmp:
                source = Path(tmp) / "src"
                self.git.run("clone", "-q", "--depth", "1", self.clone_url(project.repo), str(source))
                return self.config_repo.mutate(lambda _c, r: r.add_template(name, source), f"Add template {name}")

        def done(config: Config):
            self._apply_config(config)
            self.notify(f"Template '{name}' created from {project.name}")

        self._project_task(pid, "Creating template…", work, done, "Could not create template")

    def add_existing(self) -> None:
        parent = self.current_path
        already = {p.repo for p in (entry[1] for entry in self._projects.values())}
        dialog = AddExistingDialog(self.forge, already, format_path(parent), self)
        if not dialog.exec():
            return
        repos = dialog.picker.selected()
        if not repos:
            return
        for r in repos:
            self.repo_info[r.full_name.casefold()] = r
        projects = [Project(name=r.name, repo=r.full_name, description=r.description) for r in repos]

        def change(c: Config, _r):
            for p in projects:
                if c.find_repo(p.repo) is None:
                    c.add_project(parent, p)

        def done(config: Config):
            self._refresh_meta([p.id for p in projects])
            self._generate_previews(projects)

        message = f"Add project {projects[0].name}" if len(projects) == 1 else f"Add {len(projects)} projects"
        self._mutate(change, message, done)

    def show_files(self, pid: str) -> None:
        project = self._projects[pid][1]
        if not self.workspace.is_cloned(project):
            QMessageBox.information(self, project.name, "This project is not downloaded yet. Double-click it to download and open it.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.workspace.path_for(project))))

    def open_on_server(self, pid: str) -> None:
        project = self._projects[pid][1]
        info = self.repo_info.get(project.repo.casefold())
        QDesktopServices.openUrl(QUrl(info.html_url if info and info.html_url else self.forge.repo_web_url(project.repo)))

    # ---------------------------------------------------------- menus etc --

    def _grid_activated(self, item) -> None:
        kind, key = self.grid.item_key(item)
        if kind == "folder":
            self.navigate(key)
        else:
            self.open_project(key)

    def _grid_menu(self, pos) -> None:
        item = self.grid.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            menu.addAction(self.act_add_existing)
            menu.addAction(self.act_new_project)
            menu.addAction(self.act_import)
            menu.addAction(self.act_new_folder)
        else:
            kind, key = self.grid.item_key(item)
            if kind == "folder":
                menu.addAction("Open", lambda: self.navigate(key))
                menu.addAction("New Folder Inside…", lambda: self.new_folder(key))
                menu.addSeparator()
                menu.addAction("Rename…", lambda: self.rename_folder(key))
                menu.addAction("Delete", lambda: self.delete_folder(key))
            else:
                open_action = menu.addAction("Open in KiCad", lambda: self.open_project(key))
                font = open_action.font()
                font.setBold(True)
                open_action.setFont(font)
                menu.addAction("Get Latest", lambda: self.get_latest(key))
                menu.addAction("Save to Server…", lambda: self.save_project(key))
                menu.addAction("Revert to Server State…", lambda: self.revert_project(key))
                menu.addSeparator()
                menu.addAction("Update Preview", lambda: self.update_preview(key))
                menu.addAction("Show Files", lambda: self.show_files(key))
                menu.addAction(f"Open on {self.forge.label.split(' ')[0]}", lambda: self.open_on_server(key))
                menu.addAction("Convert to Template…", lambda: self.convert_to_template(key))
                menu.addSeparator()
                menu.addAction("Rename…", lambda: self.rename_project(key))
                menu.addAction("Remove from List…", lambda: self.remove_project(key))
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None or self.config is None:
            return
        path = FolderTree.path_of(item)
        menu = QMenu(self)
        menu.addAction("New Folder Inside…", lambda: self.new_folder(path))
        if path:
            menu.addAction("Rename…", lambda: self.rename_folder(path))
            menu.addAction("Delete", lambda: self.delete_folder(path))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def open_settings(self) -> None:
        old = (self.settings.workspace, self.settings.clone_protocol)
        organizations, default_owner = [], ""
        if self.config is not None:
            default_owner = self.config.default_owner
            try:
                with _WaitCursor():
                    organizations = self.forge.owners()[1:]
            except ForgeError:
                pass  # offline - the project config section is simply not shown
        dialog = SettingsDialog(self.settings, organizations, default_owner, self)
        result = dialog.exec()
        if result == SettingsDialog.CHANGE_ACCOUNT:
            if self._ensure_account(force_dialog=True):
                self._open_config()
            return
        if result == SettingsDialog.CHANGE_CONFIG:
            if self._choose_config_repo():
                self._open_config()
            return
        if result:
            self.settings.save()
            apply_color_scheme(self.settings.color_scheme)
            new_owner = dialog.default_owner.currentData() if dialog.default_owner is not None else default_owner
            if self.config is not None and new_owner != default_owner:
                self._mutate(
                    lambda c, _r: setattr(c, "default_owner", new_owner),
                    f"Set default organization to {new_owner}" if new_owner else "Clear default organization",
                )
            if (self.settings.workspace, self.settings.clone_protocol) != old:
                self.workspace = Workspace(self.git, Path(self.settings.workspace).expanduser())
                self._refresh_meta(fetch=False)
        if result == SettingsDialog.CHECK_UPDATES:
            self.check_for_updates(user_asked=True)

    # ------------------------------------------------------------- updates --

    def check_for_updates(self, user_asked: bool) -> None:
        if not updater.updates_supported():
            if user_asked:
                QMessageBox.information(
                    self,
                    "Updates",
                    f"This is version {updater.version_text()}.\n\nIt was not installed from a release, so it cannot "
                    "update itself. Download the newest version from the releases page instead.",
                )
            return

        def done(release: updater.Release):
            if not updater.is_newer(release):
                if user_asked:
                    QMessageBox.information(self, "Updates", f"You have the newest version, {updater.version_text()}.")
                return
            if not user_asked and self.settings.skipped_build == release.build:
                return
            self._offer_update(release, user_asked)

        def failed(error):
            if user_asked:
                self.show_error(error, "Could not check for updates")

        self.runner.run(updater.fetch_release, done, failed, label="Checking for updates…" if user_asked else None)

    def _offer_update(self, release: updater.Release, user_asked: bool) -> None:
        kind = updater.install_kind()
        asset = release.asset_for(kind)
        if asset is None:
            if user_asked:
                QMessageBox.information(self, "Updates", "A newer version exists, but not for this platform.")
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Update available")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<b>A new version of {APP_NAME} is available.</b>")
        how = (
            "The app closes, installs the update and starts again."
            if kind is updater.InstallKind.WINDOWS_INSTALLER
            else "You will be asked for your password to install it."
        )
        link = f" <a href='{release.release_url}'>What's new?</a>" if release.release_url else ""
        box.setInformativeText(
            f"You have build {updater.current_build()}, the new one is build {release.build} "
            f"(about {asset.size / 1_048_576:.1f} MB). {how}{link}"
        )
        update = box.addButton("Update Now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not Now", QMessageBox.ButtonRole.RejectRole)
        skip = None if user_asked else box.addButton("Skip This Version", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(update)
        box.exec()
        if skip is not None and box.clickedButton() is skip:
            self.settings.skipped_build = release.build
            self.settings.save()
        elif box.clickedButton() is update:
            self._install_update(asset, kind)

    def _install_update(self, asset: updater.Asset, kind: updater.InstallKind) -> None:
        def work():
            path = updater.download(asset)
            updater.install(path, kind)

        def done(_):
            if kind is updater.InstallKind.WINDOWS_INSTALLER:
                QApplication.quit()  # the installer waits for us, then restarts the app
                return
            command = updater.restart_command()
            if (
                command
                and QMessageBox.question(self, "Update installed", "The update is installed. Restart now?")
                == QMessageBox.StandardButton.Yes
            ):
                subprocess.Popen(command, start_new_session=True)
                QApplication.quit()
            elif not command:
                QMessageBox.information(self, "Update installed", "The update is installed. Please restart the app.")

        self.runner.run(work, done, lambda e: self.show_error(e, "Update failed"), label="Downloading update…")


def apply_color_scheme(name: str) -> None:
    """Light or dark mode as chosen in the settings; "system" follows the operating system."""
    hints = QApplication.styleHints()
    if not hasattr(hints, "setColorScheme"):  # Qt < 6.8: always follow the system
        return
    schemes = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}
    hints.setColorScheme(schemes.get(name, Qt.ColorScheme.Unknown))


def run_app(settings: Settings) -> MainWindow:
    apply_color_scheme(settings.color_scheme)
    window = MainWindow(settings)
    window.show()
    QTimer.singleShot(0, window.start)
    return window
