"""Dialogs: sign-in, config repository, projects and settings."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..forge import FORGES, Forge, ForgeError, Repo, User, guess_kind, make_forge
from ..settings import Settings
from ..updater import updates_supported, version_text
from ..workspace import find_kicad_tool

REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def slugify(name: str) -> str:
    slug = re.sub(r"\s+", "_", name.strip())
    slug = re.sub(r"[^A-Za-z0-9_.-]", "", slug)
    return slug.strip("._-")


class _WaitCursor:
    def __enter__(self):
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def __exit__(self, *exc):
        QGuiApplication.restoreOverrideCursor()


def _error_label() -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    label.setStyleSheet("color: #d32f2f;")
    label.hide()
    return label


class ConnectDialog(QDialog):
    """Ask for the git server, its type and a personal access token, and verify them."""

    def __init__(self, kind: str = "gitea", url: str = "", message: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Sign in")
        self.setMinimumWidth(560)
        self.user: User | None = None

        self.kind = QComboBox()
        for forge in FORGES.values():
            self.kind.addItem(forge.label, forge.kind)
        self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
        self.url = QLineEdit(url)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Paste your access token")
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setOpenExternalLinks(True)
        self.help.setTextFormat(Qt.TextFormat.RichText)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.url.textEdited.connect(self._url_edited)
        self.url.textChanged.connect(self._update_help)
        self._kind_changed()
        self.error = _error_label()
        if message:
            self.error.setText(message)
            self.error.show()

        form = QFormLayout()
        form.addRow("Server type:", self.kind)
        form.addRow("Server:", self.url)
        form.addRow("Access token:", self.token)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self._connect)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Sign in to the git server where your team keeps its projects</b>"))
        layout.addLayout(form)
        layout.addWidget(self.help)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    def _forge_class(self):
        return FORGES[self.kind.currentData()]

    def _kind_changed(self, *_args) -> None:
        forge = self._forge_class()
        self.url.setPlaceholderText(forge.default_url or "https://git.example.com")
        if not self.url.text().strip() or self.url.text().strip() in {f.default_url for f in FORGES.values() if f.default_url}:
            self.url.setText(forge.default_url)
        self._update_help()

    def _url_edited(self, text: str) -> None:
        kind = guess_kind(text)
        if kind and kind != self.kind.currentData():
            self.kind.blockSignals(True)
            self.kind.setCurrentIndex(self.kind.findData(kind))
            self.kind.blockSignals(False)
            self._update_help()

    def _update_help(self, *_args) -> None:
        self.help.setText(self._forge_class().token_help(self.url.text().strip().rstrip("/")))

    def _connect(self) -> None:
        url = self.url.text().strip().rstrip("/")
        token = self.token.text().strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not url:
            self.error.setText("Please enter the server address.")
        elif not token:
            self.error.setText("Please enter an access token.")
        else:
            try:
                with _WaitCursor():
                    self.user = make_forge(self.kind.currentData(), url, token, timeout=15).current_user()
            except ForgeError as e:
                self.error.setText("The token was not accepted." if e.status in (401, 403) else str(e))
            else:
                self.server_url, self.token_value, self.forge_kind = url, token, self.kind.currentData()
                self.accept()
                return
        self.error.show()


class RepoPicker(QWidget):
    """Searchable list of the repositories the user can see."""

    def __init__(self, forge: Forge, multi: bool = False, disabled: set[str] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.forge = forge
        self.disabled = {d.casefold() for d in (disabled or set())}
        self.repos: list[Repo] = []
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search repositories…")
        self.search.setClearButtonEnabled(True)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection if multi else QListWidget.SelectionMode.SingleSelection)
        self.list.setAlternatingRowColors(True)
        self.error = _error_label()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.search)
        layout.addWidget(self.list)
        layout.addWidget(self.error)
        self.search.textChanged.connect(self._fill)

    def load(self) -> None:
        try:
            with _WaitCursor():
                self.repos = self.forge.list_repos()
        except ForgeError as e:
            self.error.setText(str(e))
            self.error.show()
        self._fill()

    def add_repo(self, repo: Repo) -> None:
        self.repos.append(repo)
        self.search.clear()
        self._fill()
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.ItemDataRole.UserRole).full_name == repo.full_name:
                self.list.setCurrentItem(item)

    def _fill(self) -> None:
        query = self.search.text().strip().casefold()
        self.list.clear()
        for repo in sorted(self.repos, key=lambda r: r.full_name.casefold()):
            text = repo.full_name
            if query and query not in text.casefold() and query not in repo.description.casefold():
                continue
            item = QListWidgetItem(text + ("   (already in the list)" if text.casefold() in self.disabled else ""))
            item.setData(Qt.ItemDataRole.UserRole, repo)
            item.setToolTip(repo.description)
            if text.casefold() in self.disabled:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
            self.list.addItem(item)

    def selected(self) -> list[Repo]:
        return [i.data(Qt.ItemDataRole.UserRole) for i in self.list.selectedItems()]


class CreateRepoDialog(QDialog):
    def __init__(self, forge: Forge, owners: list[str], default_owner: str, default_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create repository")
        self.forge = forge
        self.repo: Repo | None = None
        self.owner = QComboBox()
        self.owner.addItems(owners)
        if default_owner in owners:
            self.owner.setCurrentText(default_owner)
        self.name = QLineEdit(default_name)
        self.private = QCheckBox("Private (only members can see it)")
        self.private.setChecked(True)
        self.error = _error_label()
        form = QFormLayout()
        form.addRow("Owner:", self.owner)
        form.addRow("Name:", self.name)
        form.addRow("", self.private)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    def _create(self) -> None:
        name = self.name.text().strip()
        if not REPO_NAME_RE.match(name):
            self.error.setText("Use only letters, digits, '-', '_' and '.' in the name.")
            self.error.show()
            return
        try:
            with _WaitCursor():
                self.repo = self.forge.create_repo(
                    self.owner.currentText(), name, "KiCad Project Manager configuration", self.private.isChecked()
                )
        except ForgeError as e:
            self.error.setText(str(e))
            self.error.show()
            return
        self.accept()


class ConfigRepoDialog(QDialog):
    """Pick the repository that holds the shared project list (or create one)."""

    def __init__(self, forge: Forge, current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose config repository")
        self.resize(560, 520)
        self.forge = forge
        self.repo_full_name = ""
        intro = QLabel(
            "The <b>config repository</b> stores your team's folder structure, project list and preview images. "
            "Everyone on the team uses the same one.<br><br>"
            "Pick an existing config repository, or an empty repository to start a new project list in."
        )
        intro.setWordWrap(True)
        self.picker = RepoPicker(forge)
        create = QPushButton("Create new repository…")
        create.clicked.connect(self._create)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use selected")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.picker.list.itemDoubleClicked.connect(lambda *_: self._accept())
        bottom = QHBoxLayout()
        bottom.addWidget(create)
        bottom.addStretch()
        bottom.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.picker)
        layout.addLayout(bottom)
        QTimer.singleShot(0, self._load)
        self._current = current

    def _load(self) -> None:
        self.picker.load()
        if self._current:
            self.picker.search.setText(self._current)

    def _create(self) -> None:
        try:
            with _WaitCursor():
                owners = self.forge.owners()
        except ForgeError as e:
            QMessageBox.warning(self, "Create repository", str(e))
            return
        dialog = CreateRepoDialog(self.forge, owners, owners[-1] if len(owners) > 1 else owners[0], "kicad-project-manager-config", self)
        if dialog.exec() and dialog.repo:
            self.picker.add_repo(dialog.repo)

    def _accept(self) -> None:
        selected = self.picker.selected()
        if not selected:
            return
        self.repo_full_name = selected[0].full_name
        self.accept()


class InitConfigDialog(QDialog):
    """Settings for a brand-new shared project list."""

    def __init__(self, repo: str, owners: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set up project list")
        self.name = QLineEdit("PCB Projects")
        self.owner = QComboBox()
        self.owner.addItems(owners)
        if len(owners) > 1:
            self.owner.setCurrentIndex(1)  # first organization rather than the personal account
        self.private = QCheckBox("New projects are private by default")
        self.private.setChecked(True)
        intro = QLabel(f"<b>{repo}</b> does not contain a project list yet. A new one will be created in it.")
        intro.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Name:", self.name)
        form.addRow("Create new projects in:", self.owner)
        form.addRow("", self.private)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create project list")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)


class NewProjectDialog(QDialog):
    def __init__(self, owners: list[str], default_owner: str, default_private: bool, folder: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New KiCad project")
        self.setMinimumWidth(480)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Motor Controller")
        self.repo = QLineEdit()
        self.repo.setPlaceholderText("Motor_Controller")
        self.owner = QComboBox()
        self.owner.addItems(owners)
        if default_owner in owners:
            self.owner.setCurrentText(default_owner)
        self.description = QLineEdit()
        self.private = QCheckBox("Private (only members can see it)")
        self.private.setChecked(default_private)
        self.open_after = QCheckBox("Open in KiCad when created")
        self.open_after.setChecked(True)
        self.error = _error_label()
        self._repo_edited = False
        self.name.textChanged.connect(self._name_changed)
        self.repo.textEdited.connect(lambda *_: setattr(self, "_repo_edited", True))

        form = QFormLayout()
        form.addRow("Folder:", QLabel(folder))
        form.addRow("Project name:", self.name)
        form.addRow("Repository name:", self.repo)
        form.addRow("Create in:", self.owner)
        form.addRow("Description:", self.description)
        form.addRow("", self.private)
        form.addRow("", self.open_after)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    def _name_changed(self, text: str) -> None:
        if not self._repo_edited:
            self.repo.setText(slugify(text))

    def _accept(self) -> None:
        if not self.name.text().strip():
            self.error.setText("Please enter a project name.")
        elif not REPO_NAME_RE.match(self.repo.text().strip()) or self.repo.text().strip().endswith(".git"):
            self.error.setText("The repository name may only contain letters, digits, '-', '_' and '.'.")
        else:
            self.accept()
            return
        self.error.show()


class AddExistingDialog(QDialog):
    def __init__(self, forge: Forge, already: set[str], folder: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add existing projects")
        self.resize(560, 560)
        intro = QLabel(f"Select one or more repositories to add to <b>{folder}</b>. Hold Ctrl or Shift to select several.")
        intro.setWordWrap(True)
        self.picker = RepoPicker(forge, multi=True, disabled=already)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.picker)
        layout.addWidget(buttons)
        QTimer.singleShot(0, self.picker.load)


class SaveDialog(QDialog):
    def __init__(self, project_name: str, changed_files: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Save {project_name} to server")
        self.resize(520, 440)
        intro = QLabel(
            "Save your work in KiCad first (<b>Ctrl+S</b>) - only saved files are uploaded.<br><br>Briefly describe what you changed:"
        )
        intro.setWordWrap(True)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("e.g. Routed the power section, swapped U3 for a smaller package")
        self.message.setMaximumHeight(90)
        files = QListWidget()
        files.addItems(changed_files or ["(no changed files)"])
        files.setEnabled(bool(changed_files))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save to server")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.message)
        layout.addWidget(QLabel(f"Changed files ({len(changed_files)}):"))
        layout.addWidget(files)
        layout.addWidget(buttons)
        self.message.setFocus()

    def text(self) -> str:
        return self.message.toPlainText().strip()


class SettingsDialog(QDialog):
    CHANGE_ACCOUNT, CHANGE_CONFIG, CHECK_UPDATES = 2, 3, 4

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.settings = settings
        self.workspace = QLineEdit(settings.workspace)
        self.kicad = QLineEdit(settings.kicad_path)
        self.kicad.setPlaceholderText(find_kicad_tool("kicad") or "not found - please select")
        self.kicad_cli = QLineEdit(settings.kicad_cli_path)
        self.kicad_cli.setPlaceholderText(find_kicad_tool("kicad-cli") or "not found - please select")
        self.protocol = QComboBox()
        self.protocol.addItem("HTTPS - uses your access token (recommended)", "https")
        self.protocol.addItem("SSH - uses your SSH key", "ssh")
        self.protocol.setCurrentIndex(max(0, self.protocol.findData(settings.clone_protocol)))

        form = QFormLayout()
        form.addRow("Projects folder:", self._with_browse(self.workspace, directory=True))
        form.addRow("KiCad:", self._with_browse(self.kicad))
        form.addRow("kicad-cli:", self._with_browse(self.kicad_cli))
        form.addRow("Download via:", self.protocol)

        account = QLabel(
            f"Signed in to <b>{settings.server_url}</b> as <b>{settings.username}</b><br>"
            f"Config repository: <b>{settings.config_repo or '-'}</b>"
        )
        change_account = QPushButton("Change account…")
        change_account.clicked.connect(lambda: self.done(self.CHANGE_ACCOUNT))
        change_config = QPushButton("Change config repository…")
        change_config.clicked.connect(lambda: self.done(self.CHANGE_CONFIG))
        row = QHBoxLayout()
        row.addWidget(change_account)
        row.addWidget(change_config)
        row.addStretch()

        self.check_updates = QCheckBox("Check for updates when the app starts")
        self.check_updates.setChecked(settings.check_updates)
        self.check_updates.setEnabled(updates_supported())
        check_now = QPushButton("Check for updates now")
        check_now.clicked.connect(lambda: (self._apply(), self.done(self.CHECK_UPDATES)))
        updates = QHBoxLayout()
        updates.addWidget(self.check_updates)
        updates.addStretch()
        updates.addWidget(check_now)
        version = QLabel(f"KiCad Project Manager {version_text()}")
        version.setStyleSheet("color: palette(placeholder-text);")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addSpacing(12)
        layout.addWidget(account)
        layout.addLayout(row)
        layout.addSpacing(12)
        layout.addLayout(updates)
        layout.addWidget(version)
        layout.addWidget(buttons)

    def _with_browse(self, edit: QLineEdit, directory: bool = False) -> QWidget:
        button = QPushButton("Browse…")

        def browse():
            if directory:
                path = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
            else:
                path, _ = QFileDialog.getOpenFileName(self, "Choose program", edit.text())
            if path:
                edit.setText(path)

        button.clicked.connect(browse)
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit)
        row.addWidget(button)
        return widget

    def _save(self) -> None:
        self._apply()
        self.accept()

    def _apply(self) -> None:
        self.settings.workspace = self.workspace.text().strip() or self.settings.workspace
        self.settings.kicad_path = self.kicad.text().strip()
        self.settings.kicad_cli_path = self.kicad_cli.text().strip()
        self.settings.clone_protocol = self.protocol.currentData()
        self.settings.check_updates = self.check_updates.isChecked()
