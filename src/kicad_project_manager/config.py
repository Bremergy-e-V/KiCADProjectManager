"""The shared configuration: folder structure and project list.

It lives as ``kicad-projects.json`` in a dedicated "config repository" on
the git server, next to a ``previews/`` folder holding one PNG per project. Every change
is made through :meth:`ConfigRepo.mutate`, which pulls the newest version,
applies the change, commits and pushes - retrying if someone else pushed in
between. Because changes are re-applied on fresh data, two people editing the
folder structure at the same time never overwrite each other.
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .git import Git, GitError

FORMAT_VERSION = 1
CONFIG_FILE = "kicad-projects.json"
PREVIEW_DIR = "previews"
TEMPLATES_DIR = "templates"
LEGACY_TEMPLATE_DIR = "template"  # single team template used by older versions
LEGACY_TEMPLATE_NAME = "Team template"

FolderPath = tuple[str, ...]


class ConfigError(Exception):
    """A user-facing problem with the shared configuration."""


def format_path(path: FolderPath) -> str:
    return "/".join(path) if path else "(top level)"


def _check_name(name: str, what: str) -> str:
    name = name.strip()
    if not name:
        raise ConfigError(f"The {what} name must not be empty.")
    if any(c in name for c in "/\\"):
        raise ConfigError(f"The {what} name must not contain '/' or '\\'.")
    return name


def check_template_name(name: str) -> str:
    """Template names become folder names, so they must be valid on every OS."""
    name = _check_name(name, "template")
    if any(c in name for c in '<>:"|?*') or name.endswith(".") or name.startswith("."):
        raise ConfigError("The template name must not contain any of < > : \" | ? * or start or end with '.'.")
    return name


@dataclass
class Project:
    name: str
    repo: str  # "owner/name" on the git server (owner may contain "/" on GitLab)
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def owner(self) -> str:
        return self.repo.rpartition("/")[0]

    @property
    def repo_name(self) -> str:
        return self.repo.rpartition("/")[2]

    def to_dict(self) -> dict:
        d = {"id": self.id, "name": self.name, "repo": self.repo}
        if self.description:
            d["description"] = self.description
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Project:
        return cls(name=d["name"], repo=d["repo"], description=d.get("description", ""), id=d.get("id") or uuid.uuid4().hex)


@dataclass
class Folder:
    name: str
    folders: list[Folder] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)

    def subfolder(self, name: str) -> Folder | None:
        return next((f for f in self.folders if f.name.casefold() == name.casefold()), None)

    def project_count(self) -> int:
        return len(self.projects) + sum(f.project_count() for f in self.folders)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "folders": [f.to_dict() for f in sorted(self.folders, key=lambda f: f.name.casefold())],
            "projects": [p.to_dict() for p in sorted(self.projects, key=lambda p: p.name.casefold())],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Folder:
        return cls(
            name=d.get("name", ""),
            folders=[Folder.from_dict(f) for f in d.get("folders", [])],
            projects=[Project.from_dict(p) for p in d.get("projects", [])],
        )


@dataclass
class Config:
    name: str
    server_url: str = ""
    default_owner: str = ""
    default_private: bool = True
    root: Folder = field(default_factory=lambda: Folder(""))
    templates: dict[str, str] = field(default_factory=dict)  # template name -> repo of the project it is made from

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": FORMAT_VERSION,
            "name": self.name,
            "server_url": self.server_url,
            "default_owner": self.default_owner,
            "default_private": self.default_private,
            "folders": self.root.to_dict()["folders"],
            "projects": self.root.to_dict()["projects"],
            "templates": dict(sorted(self.templates.items(), key=lambda item: item[0].casefold())),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Config:
        if d.get("format", 1) > FORMAT_VERSION:
            raise ConfigError("The project list was written by a newer version of KiCad Project Manager. Please update.")
        return cls(
            name=d.get("name", "Projects"),
            server_url=d.get("server_url") or d.get("gitea_url", ""),
            default_owner=d.get("default_owner", ""),
            default_private=d.get("default_private", True),
            root=Folder.from_dict({"name": "", "folders": d.get("folders", []), "projects": d.get("projects", [])}),
            templates=dict(d.get("templates", {})),
        )

    # -- lookup --------------------------------------------------------------

    def folder(self, path: FolderPath) -> Folder:
        folder = self.root
        for part in path:
            sub = folder.subfolder(part)
            if sub is None:
                raise ConfigError(f"The folder '{format_path(path)}' no longer exists. Someone may have moved or deleted it.")
            folder = sub
        return folder

    def has_folder(self, path: FolderPath) -> bool:
        try:
            self.folder(path)
            return True
        except ConfigError:
            return False

    def iter_projects(self, path: FolderPath = ()) -> Iterator[tuple[FolderPath, Project]]:
        folder = self.folder(path)
        for project in folder.projects:
            yield path, project
        for sub in folder.folders:
            yield from self.iter_projects((*path, sub.name))

    def find_project(self, project_id: str) -> tuple[FolderPath, Project]:
        for path, project in self.iter_projects():
            if project.id == project_id:
                return path, project
        raise ConfigError("This project no longer exists in the project list. Someone may have removed it.")

    def find_repo(self, repo: str) -> tuple[FolderPath, Project] | None:
        return next(((p, pr) for p, pr in self.iter_projects() if pr.repo.casefold() == repo.casefold()), None)

    # -- folder operations ---------------------------------------------------

    def add_folder(self, parent: FolderPath, name: str) -> FolderPath:
        name = _check_name(name, "folder")
        folder = self.folder(parent)
        if folder.subfolder(name):
            raise ConfigError(f"A folder named '{name}' already exists in '{format_path(parent)}'.")
        folder.folders.append(Folder(name))
        return (*parent, name)

    def rename_folder(self, path: FolderPath, new_name: str) -> FolderPath:
        if not path:
            raise ConfigError("The top level cannot be renamed here.")
        new_name = _check_name(new_name, "folder")
        parent = self.folder(path[:-1])
        folder = self.folder(path)
        other = parent.subfolder(new_name)
        if other is not None and other is not folder:
            raise ConfigError(f"A folder named '{new_name}' already exists.")
        folder.name = new_name
        return (*path[:-1], new_name)

    def delete_folder(self, path: FolderPath) -> None:
        if not path:
            raise ConfigError("The top level cannot be deleted.")
        folder = self.folder(path)
        if folder.folders or folder.projects:
            raise ConfigError(f"The folder '{format_path(path)}' is not empty. Move or remove its contents first.")
        self.folder(path[:-1]).folders.remove(folder)

    def move_folder(self, path: FolderPath, new_parent: FolderPath) -> FolderPath:
        if not path:
            raise ConfigError("The top level cannot be moved.")
        if new_parent[: len(path)] == path:
            raise ConfigError("A folder cannot be moved into itself.")
        if new_parent == path[:-1]:
            return path
        folder = self.folder(path)
        target = self.folder(new_parent)
        if target.subfolder(folder.name):
            raise ConfigError(f"A folder named '{folder.name}' already exists in '{format_path(new_parent)}'.")
        self.folder(path[:-1]).folders.remove(folder)
        target.folders.append(folder)
        return (*new_parent, folder.name)

    # -- project operations --------------------------------------------------

    def add_project(self, parent: FolderPath, project: Project) -> None:
        _check_name(project.name, "project")
        existing = self.find_repo(project.repo)
        if existing:
            raise ConfigError(f"The repository '{project.repo}' is already in the list (folder '{format_path(existing[0])}').")
        self.folder(parent).projects.append(project)

    def move_project(self, project_id: str, new_parent: FolderPath) -> None:
        path, project = self.find_project(project_id)
        target = self.folder(new_parent)
        self.folder(path).projects.remove(project)
        target.projects.append(project)

    def rename_project(self, project_id: str, new_name: str) -> None:
        self.find_project(project_id)[1].name = _check_name(new_name, "project")

    def remove_project(self, project_id: str) -> Project:
        path, project = self.find_project(project_id)
        self.folder(path).projects.remove(project)
        return project

    # -- templates -----------------------------------------------------------
    #
    # A template is a normal project. New projects start from its newest server version,
    # so it is edited like any other project.

    def find_template(self, name: str) -> str | None:
        """The name of the template called ``name``, ignoring case."""
        return next((n for n in self.templates if n.casefold() == name.strip().casefold()), None)

    def template_of(self, repo: str) -> str | None:
        """The name of the template made from the project in ``repo``, if any."""
        return next((n for n, r in self.templates.items() if r.casefold() == repo.casefold()), None)

    def set_template(self, name: str, repo: str) -> str:
        """Offer the project in ``repo`` as template ``name``, replacing a template of that name."""
        name = check_template_name(name)
        for other in [n for n, r in self.templates.items() if n.casefold() == name.casefold() or r.casefold() == repo.casefold()]:
            del self.templates[other]
        self.templates[name] = repo
        return name

    def remove_template(self, name: str) -> None:
        self.templates.pop(name, None)


README_TEXT = """# {name}

This repository is managed by **KiCad Project Manager**.

* `{config}` - folder structure, project list and templates
* `{previews}/` - preview images (one PNG per project id)
* `{templates}/<name>/` - optional: copies of KiCad projects offered as templates for new projects

Please edit it through the app rather than by hand.
"""


class ConfigRepo:
    """Local clone of the config repository. All methods are thread safe."""

    def __init__(self, git: Git, path: Path, url: str):
        self.git = git
        self.path = path
        self.url = url
        self.branch: str | None = None
        self.lock = threading.RLock()

    @property
    def config_file(self) -> Path:
        return self.path / CONFIG_FILE

    @property
    def templates_dir(self) -> Path:
        return self.path / TEMPLATES_DIR

    def template_folders(self) -> dict[str, Path]:
        """Templates stored as a copy in the config repository, by name, sorted. Each one is a folder holding a KiCad project.

        Older versions made templates this way; now they are normally projects (see :attr:`Config.templates`).
        """
        with self.lock:
            found: dict[str, Path] = {}
            if self.templates_dir.is_dir():
                for d in self.templates_dir.iterdir():
                    if d.is_dir() and any(d.glob("*.kicad_pro")):
                        found[d.name] = d
            legacy = self.path / LEGACY_TEMPLATE_DIR
            if any(legacy.glob("*.kicad_pro")) and LEGACY_TEMPLATE_NAME.casefold() not in {n.casefold() for n in found}:
                found[LEGACY_TEMPLATE_NAME] = legacy
            return dict(sorted(found.items(), key=lambda item: item[0].casefold()))

    def find_template_folder(self, name: str) -> str | None:
        """The name of the template folder called ``name``, ignoring case."""
        return next((n for n in self.template_folders() if n.casefold() == name.strip().casefold()), None)

    def remove_template_folder(self, name: str) -> None:
        """Delete the template folder called ``name`` (ignoring case), if any. Use inside :meth:`mutate`."""
        existing = self.find_template_folder(name)
        if existing is not None:
            shutil.rmtree(self.template_folders()[existing])

    def preview_file(self, project_id: str) -> Path:
        return self.path / PREVIEW_DIR / f"{project_id}.png"

    def _git(self, *args: str) -> str:
        return self.git.run(*args, cwd=self.path)

    def _remote_branch(self) -> str | None:
        out = self._git("ls-remote", "--symref", "origin", "HEAD")
        for line in out.splitlines():
            if line.startswith("ref:"):
                return line.split()[1].removeprefix("refs/heads/")
        heads = [line.split("refs/heads/", 1)[1] for line in self._git("ls-remote", "--heads", "origin").splitlines()]
        for preferred in ("main", "master"):
            if preferred in heads:
                return preferred
        return heads[0] if heads else None

    def open(self) -> bool:
        """Clone or update the local copy. Returns whether a config exists."""
        with self.lock:
            if not (self.path / ".git").exists():
                if self.path.exists():
                    shutil.rmtree(self.path)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.git.run("clone", "-q", self.url, str(self.path))
            else:
                self._git("remote", "set-url", "origin", self.url)
            self.sync()
            return self.config_file.exists()

    def sync(self) -> None:
        """Make the local copy identical to the server."""
        with self.lock:
            self._git("fetch", "-q", "--prune", "origin")
            branch = self._remote_branch()
            if branch is None:  # empty repository
                return
            self.branch = branch
            self._git("checkout", "-q", "-B", branch, f"origin/{branch}")
            self._git("reset", "-q", "--hard", f"origin/{branch}")
            self._git("clean", "-q", "-fd")

    def load(self) -> Config:
        with self.lock:
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise ConfigError("The config repository contains no project list.") from None
            except json.JSONDecodeError as e:
                raise ConfigError(f"The project list in the config repository is damaged: {e}") from None
            return Config.from_dict(data)

    def _write(self, config: Config) -> None:
        text = json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n"
        self.config_file.write_text(text, encoding="utf-8", newline="\n")

    def initialize(self, config: Config) -> Config:
        """Write a fresh config into an empty (or non-config) repository."""
        with self.lock:
            self.sync()
            if self.config_file.exists():
                raise ConfigError("This repository already contains a project list.")
            if self.branch is None:
                self.branch = "main"
                self._git("symbolic-ref", "HEAD", "refs/heads/main")
            self._write(config)
            (self.path / PREVIEW_DIR).mkdir(exist_ok=True)
            (self.path / PREVIEW_DIR / ".gitkeep").touch()
            readme = self.path / "README.md"
            if not readme.exists():
                readme.write_text(
                    README_TEXT.format(name=config.name, config=CONFIG_FILE, previews=PREVIEW_DIR, templates=TEMPLATES_DIR),
                    encoding="utf-8",
                    newline="\n",
                )
            self._git("add", "-A")
            self._git("commit", "-q", "-m", f"Set up KiCad Project Manager: {config.name}")
            self._git("push", "-q", "-u", "origin", f"HEAD:{self.branch}")
            return self.load()

    def mutate(self, change: Callable[[Config, ConfigRepo], object], message: str, attempts: int = 4) -> Config:
        """Pull, apply ``change``, commit and push. Retries if the push races someone else."""
        with self.lock:
            for attempt in range(attempts):
                self.sync()
                config = self.load()
                change(config, self)
                self._write(config)
                self._git("add", "-A")
                if self.git.ok("diff", "--cached", "--quiet", cwd=self.path):
                    return config
                self._git("commit", "-q", "-m", message)
                try:
                    self._git("push", "-q", "origin", f"HEAD:{self.branch}")
                    return config
                except GitError:
                    self._git("fetch", "-q", "origin")
                    newer = int(self._git("rev-list", "--count", f"HEAD..origin/{self.branch}") or 0)
                    if not newer or attempt == attempts - 1:
                        raise
            raise ConfigError("Could not save the project list.")  # pragma: no cover
