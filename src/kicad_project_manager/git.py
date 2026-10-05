"""Thin wrapper around the git command line.

Every git call made by the app goes through :class:`Git`, which injects the
user's access token and identity through environment variables. That way team
members never have to configure git, credentials or SSH keys themselves, and
the token never shows up in a remote URL or on the command line.
"""

from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(Exception):
    def __init__(self, args: list[str], returncode: int, output: str):
        self.git_args = list(args)
        self.returncode = returncode
        self.output = output.strip()
        super().__init__(f"git {' '.join(args)} failed ({returncode}):\n{self.output}")


@dataclass
class GitIdentity:
    """Credentials and author info used for every git call."""

    host_url: str = ""  # e.g. "https://github.com/" - the token is only sent there
    username: str = ""
    token: str = ""
    name: str = ""
    email: str = ""


class Git:
    def __init__(self, identity: GitIdentity | None = None):
        self.identity = identity or GitIdentity()

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")

        ident = self.identity
        extra: list[tuple[str, str]] = []
        if ident.token and ident.username and ident.host_url:
            cred = base64.b64encode(f"{ident.username}:{ident.token}".encode()).decode()
            extra.append((f"http.{ident.host_url}.extraHeader", f"Authorization: Basic {cred}"))
        count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        for key, value in extra:
            env[f"GIT_CONFIG_KEY_{count}"] = key
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)

        if ident.name:
            env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = ident.name
        if ident.email:
            env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = ident.email
        return env

    def _exec(self, args: tuple[str, ...], cwd: Path | None, timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self._env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )

    def run(self, *args: str, cwd: Path | None = None, timeout: float = 600) -> str:
        """Run git and return stdout; raise :class:`GitError` on failure."""
        proc = self._exec(args, cwd, timeout)
        if proc.returncode != 0:
            raise GitError(list(args), proc.returncode, (proc.stderr or "") + (proc.stdout or ""))
        return proc.stdout.strip()

    def ok(self, *args: str, cwd: Path | None = None, timeout: float = 600) -> bool:
        """Run git and return whether it succeeded."""
        return self._exec(args, cwd, timeout).returncode == 0
