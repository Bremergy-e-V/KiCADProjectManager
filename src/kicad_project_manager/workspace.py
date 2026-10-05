"""Local copies of the KiCad project repositories.

The operations here are deliberately few and named after what the user wants,
not after git commands:

* :meth:`Workspace.download` - clone a project
* :meth:`Workspace.get_latest` - pull new changes from the server
* :meth:`Workspace.save` - commit everything and push it

If two people changed the same project, :meth:`Workspace.save` never leaves the
user in a half-merged state. When git can combine the changes automatically it
does; otherwise the user's version is uploaded to a separate branch, and the
local copy is reset to the server version (see :class:`SaveConflict`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Project
from .git import Git, GitError

GITIGNORE = """\
# KiCad local/temporary files
*.kicad_prl
*.lck
~*.lck
_autosave-*
*-backups/
.history/
fp-info-cache
*.bak

# Generated outputs (remove these lines if you want them in git)
gerber/
production/
*.zip
"""

GITATTRIBUTES = """\
# Keep KiCad files byte-identical across Windows/Linux/macOS
*.kicad_pro   text eol=lf
*.kicad_sch   text eol=lf
*.kicad_pcb   text eol=lf
*.kicad_mod   text eol=lf
*.kicad_sym   text eol=lf
*.kicad_dru   text eol=lf
fp-lib-table  text eol=lf
sym-lib-table text eol=lf
"""

_PRO_TEMPLATE = """\
{{
  "meta": {{
    "filename": "{name}.kicad_pro",
    "version": 1
  }}
}}
"""

_SCH_TEMPLATE = """\
(kicad_sch
\t(version 20231120)
\t(generator "kicad_project_manager")
\t(uuid "{uuid}")
\t(paper "A4")
\t(title_block
\t\t(title "{title}")
\t)
\t(lib_symbols)
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
)
"""


class WorkspaceError(Exception):
    """A user-facing problem with a local project copy."""


class SaveConflict(WorkspaceError):
    def __init__(self, branch: str):
        self.branch = branch
        super().__init__(
            "Someone else changed the same parts of this project at the same time, "
            "so the two versions could not be combined automatically.\n\n"
            f"Nothing is lost: your version was uploaded to the branch '{branch}' on the server, "
            "and your local copy now shows the newest server version. "
            "Please ask someone familiar with git to merge the two versions."
        )


@dataclass
class LocalState:
    cloned: bool = False
    changed_files: list[str] = field(default_factory=list)
    ahead: int = 0  # local commits not on the server yet
    behind: int = 0  # server commits not downloaded yet
    offline: bool = False


class Workspace:
    def __init__(self, git: Git, root: Path):
        self.git = git
        self.root = root

    def path_for(self, project: Project) -> Path:
        return self.root / project.owner / project.repo_name

    def is_cloned(self, project: Project) -> bool:
        return (self.path_for(project) / ".git").exists()

    def _git(self, project: Project, *args: str) -> str:
        return self.git.run(*args, cwd=self.path_for(project))

    def _has_upstream(self, project: Project) -> bool:
        return self.git.ok("rev-parse", "--verify", "-q", "@{u}", cwd=self.path_for(project))

    # -- status ----------------------------------------------------------------

    def changed_files(self, project: Project) -> list[str]:
        out = self.git.run("status", "--porcelain", "-uall", cwd=self.path_for(project))
        return [line[3:] for line in out.splitlines() if line.strip()]

    def state(self, project: Project, fetch: bool = True) -> LocalState:
        if not self.is_cloned(project):
            return LocalState(cloned=False)
        state = LocalState(cloned=True)
        if fetch:
            try:
                self._git(project, "fetch", "-q", "--prune", "origin")
            except GitError:
                state.offline = True
        state.changed_files = self.changed_files(project)
        if self._has_upstream(project):
            ahead, behind = self._git(project, "rev-list", "--left-right", "--count", "HEAD...@{u}").split()
            state.ahead, state.behind = int(ahead), int(behind)
        elif self.git.ok("rev-parse", "--verify", "-q", "HEAD", cwd=self.path_for(project)):
            state.ahead = int(self._git(project, "rev-list", "--count", "HEAD"))
        return state

    def open_lock_files(self, project: Project) -> list[Path]:
        """KiCad lock files - present while the project is open in KiCad."""
        path = self.path_for(project)
        return sorted(path.glob("*.lck")) if path.exists() else []

    # -- operations --------------------------------------------------------------

    def download(self, project: Project, url: str) -> None:
        dest = self.path_for(project)
        if (dest / ".git").exists():
            return
        if dest.exists() and any(dest.iterdir()):
            raise WorkspaceError(f"The folder {dest} already exists and is not a copy of this project.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.git.run("clone", "-q", url, str(dest))

    def set_url(self, project: Project, url: str) -> None:
        if self.is_cloned(project):
            self._git(project, "remote", "set-url", "origin", url)

    def get_latest(self, project: Project, username: str = "") -> str:
        state = self.state(project)
        if state.offline:
            raise WorkspaceError("Could not reach the server.")
        if not state.behind:
            return "Already up to date."
        if state.changed_files:
            raise WorkspaceError(
                f"There are {state.behind} new change(s) on the server, but you also have unsaved local changes.\n\n"
                "Use 'Save to server' - it uploads your work and combines it with the new changes."
            )
        if state.ahead:
            self._combine(project, username)
        else:
            self._git(project, "merge", "-q", "--ff-only", "@{u}")
        return f"Downloaded {state.behind} new change(s)."

    def save(self, project: Project, message: str, username: str = "") -> str:
        path = self.path_for(project)
        self._git(project, "add", "-A")
        if not self.git.ok("diff", "--cached", "--quiet", cwd=path):
            self._git(project, "commit", "-q", "-m", message)
        state = self.state(project)
        if state.offline:
            raise WorkspaceError("Could not reach the server. Your changes are saved locally; try again later.")
        if not state.ahead:
            return "Nothing to save - no changes."
        combined = ""
        if state.behind:
            self._combine(project, username)
            combined = f" (combined with {state.behind} newer change(s) from the server)"
        self._git(project, "push", "-q", "-u", "origin", "HEAD")
        return f"Saved to server{combined}."

    def _combine(self, project: Project, username: str = "") -> None:
        """Rebase local commits onto the server; on conflict upload them to a rescue branch."""
        try:
            self._git(project, "rebase", "-q", "@{u}")
        except GitError:
            self.git.ok("rebase", "--abort", cwd=self.path_for(project))
            who = "".join(c if c.isalnum() or c in "-_." else "-" for c in (username or "user"))
            branch = f"conflict/{who}-{datetime.now():%Y%m%d-%H%M%S}"
            self._git(project, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
            self._git(project, "reset", "-q", "--hard", "@{u}")
            raise SaveConflict(branch) from None

    def create(self, project: Project, url: str, title: str, template: Path | None = None) -> Path:
        """Create a new KiCad project locally and push it to the (empty) remote."""
        dest = self.path_for(project)
        if dest.exists() and any(dest.iterdir()):
            raise WorkspaceError(f"The folder {dest} already exists.")
        dest.mkdir(parents=True, exist_ok=True)
        self.git.run("init", "-q", "-b", "main", str(dest))
        self._git(project, "remote", "add", "origin", url)
        base = project.repo_name
        if template is not None and any(template.glob("*.kicad_pro")):
            copy_template(template, dest, base)
        else:
            (dest / f"{base}.kicad_pro").write_text(_PRO_TEMPLATE.format(name=base), encoding="utf-8", newline="\n")
            (dest / f"{base}.kicad_sch").write_text(
                _SCH_TEMPLATE.format(uuid=uuid.uuid4(), title=title.replace('"', "'")), encoding="utf-8", newline="\n"
            )
        for name, text in ((".gitignore", GITIGNORE), (".gitattributes", GITATTRIBUTES)):
            if not (dest / name).exists():
                (dest / name).write_text(text, encoding="utf-8", newline="\n")
        if not (dest / "README.md").exists():
            text = f"# {title}\n\n{project.description}\n" if project.description else f"# {title}\n"
            (dest / "README.md").write_text(text, encoding="utf-8", newline="\n")
        self._git(project, "add", "-A")
        self._git(project, "commit", "-q", "-m", f"Create project {title}")
        self._git(project, "push", "-q", "-u", "origin", "main")
        return dest


def copy_template(template: Path, dest: Path, base: str) -> None:
    """Copy a template project, renaming files named after the template project."""
    old = next(template.glob("*.kicad_pro")).stem
    for src in template.rglob("*"):
        rel = src.relative_to(template)
        if ".git" in rel.parts:
            continue
        target = dest / rel
        if src.is_file() and src.stem == old and src.suffix.startswith(".kicad_"):
            target = target.with_name(base + src.suffix)
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


# -- KiCad -----------------------------------------------------------------------


def find_kicad_tool(name: str) -> str | None:
    """Locate ``kicad`` or ``kicad-cli`` in the usual install locations."""
    if found := shutil.which(name):
        return found
    if sys.platform == "win32":
        roots = {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", r"C:\Program Files")}
        candidates = [c for root in roots for c in Path(root).glob(f"KiCad/*/bin/{name}.exe")]

        def version_key(p: Path):
            return tuple(int(x) if x.isdigit() else 0 for x in p.parent.parent.name.split("."))

        if candidates:
            return str(max(candidates, key=version_key))
    elif sys.platform == "darwin":
        candidate = Path(f"/Applications/KiCad/KiCad.app/Contents/MacOS/{name}")
        if candidate.exists():
            return str(candidate)
    return None


def find_project_file(path: Path, preferred: str = "") -> Path | None:
    files = sorted(path.glob("*.kicad_pro"))
    if not files:
        return None
    return next((f for f in files if f.stem == preferred), files[0])


def launch_kicad(kicad: str, project_file: Path) -> None:
    kwargs: dict = {"cwd": project_file.parent, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([kicad, str(project_file)], **kwargs)
