import subprocess
from pathlib import Path

import pytest

from kicad_project_manager.git import Git, GitIdentity


@pytest.fixture
def git() -> Git:
    return Git(GitIdentity(name="Test User", email="test@example.com"))


@pytest.fixture
def make_bare(tmp_path: Path):
    """Create an empty bare repository that stands in for a Gitea repo."""

    def make(name: str) -> str:
        path = tmp_path / "server" / f"{name}.git"
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
        return str(path)

    return make
