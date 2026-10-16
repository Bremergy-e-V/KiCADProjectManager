"""Per-user local settings (not shared with the team)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import APP_ID

_KEYRING_SERVICE = APP_ID


def _base_dir(kind: str) -> Path:
    if sys.platform == "win32":
        var = "APPDATA" if kind == "config" else "LOCALAPPDATA"
        return Path(os.environ.get(var) or Path.home() / "AppData" / ("Roaming" if kind == "config" else "Local"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if kind == "config":
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def config_dir() -> Path:
    return _base_dir("config") / APP_ID


def data_dir() -> Path:
    return _base_dir("data") / APP_ID


@dataclass
class Settings:
    forge: str = "gitea"  # "gitea", "github" or "gitlab" - see forge.FORGES
    server_url: str = ""
    username: str = ""
    config_repo: str = ""  # "owner/name" of the shared config repository
    workspace: str = str(Path.home() / "KiCadProjects")
    clone_protocol: str = "https"  # "https" (uses the token) or "ssh"
    kicad_path: str = ""
    kicad_cli_path: str = ""
    check_updates: bool = True
    color_scheme: str = "system"  # "system", "light" or "dark"
    skipped_build: int = 0  # release build the user chose to skip

    @staticmethod
    def file() -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(cls.file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if "gitea_url" in data and "server_url" not in data:  # settings from 0.1.0
            data["server_url"] = data["gitea_url"]
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        self.file().parent.mkdir(parents=True, exist_ok=True)
        self.file().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# -- token storage -----------------------------------------------------------
# The OS keychain is preferred (Windows Credential Manager, macOS Keychain,
# Secret Service on Linux). If none is available we fall back to a file that
# only the current user can read.


def _token_file() -> Path:
    return config_dir() / "token.json"


def _token_key(server_url: str, username: str) -> str:
    return f"{server_url.rstrip('/')}|{username}"


def get_token(server_url: str, username: str) -> str:
    if not server_url or not username:
        return ""
    try:
        import keyring

        token = keyring.get_password(_KEYRING_SERVICE, _token_key(server_url, username))
        if token:
            return token
    except Exception:
        pass
    try:
        return json.loads(_token_file().read_text(encoding="utf-8")).get(_token_key(server_url, username), "")
    except (OSError, ValueError):
        return ""


def set_token(server_url: str, username: str, token: str) -> None:
    key = _token_key(server_url, username)
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, key, token)
        if keyring.get_password(_KEYRING_SERVICE, key) == token:
            return
    except Exception:
        pass
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[key] = token
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
