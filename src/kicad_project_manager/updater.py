"""Self-update from the rolling GitHub release.

Every push to ``main`` republishes the release tagged ``latest`` (see
``.github/workflows/release.yml``). Next to the downloads it carries a
``version.json`` manifest, so the newest release is always at a URL that never
changes - a plain file download, no API token and no rate limit.

The releases are rolling and the project version barely moves, so "newer" is
decided by the build number (the workflow's run number, which only goes up).
A copy with build 0 was not built by the workflow - a developer checkout - and
never offers to update itself.

Only copies installed by a release can update:

* Windows: installed by the Inno Setup installer (an ``unins*.exe`` next to the
  program). The new installer is run silently and restarts the app.
* Arch Linux: installed as a pacman package. The new package is installed with
  ``pkexec pacman -U``, which asks for the user's password.

Every download is checked against the size and SHA-256 published in the
manifest before it is run.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import __version__
from ._build import BUILD, REPOSITORY

MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/version.json"


class UpdateError(Exception):
    """A user-facing problem while checking for or installing an update."""


class InstallKind(Enum):
    WINDOWS_INSTALLER = "windows"
    ARCH_PACKAGE = "archlinux"
    OTHER = "other"


@dataclass
class Asset:
    name: str
    url: str
    sha256: str
    size: int


@dataclass
class Release:
    build: int
    version: str
    commit: str
    release_url: str
    assets: dict[str, Asset]

    def asset_for(self, kind: InstallKind) -> Asset | None:
        return self.assets.get(kind.value)


def install_kind() -> InstallKind:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        if any(Path(sys.executable).parent.glob("unins*.exe")):
            return InstallKind.WINDOWS_INSTALLER
    elif sys.platform.startswith("linux") and shutil.which("pacman"):
        if Path(__file__).resolve().is_relative_to("/usr/lib"):
            return InstallKind.ARCH_PACKAGE
    return InstallKind.OTHER


def current_build() -> int:
    return BUILD


def version_text() -> str:
    return f"{__version__} (build {BUILD})" if BUILD else f"{__version__} (development version)"


def updates_supported() -> bool:
    return BUILD > 0 and install_kind() is not InstallKind.OTHER


def parse_manifest(data: bytes) -> Release:
    try:
        d = json.loads(data)
        assets = {}
        for key in ("windows", "archlinux"):
            if a := d.get(key):
                asset = Asset(a["name"], a["url"], a["sha256"].lower(), int(a["size"]))
                if len(asset.sha256) != 64 or asset.size <= 0:
                    raise ValueError(f"incomplete {key} entry")
                assets[key] = asset
        return Release(int(d["build"]), str(d.get("version", "")), str(d.get("commit", "")), str(d.get("releaseUrl", "")), assets)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise UpdateError(f"The release information could not be read ({e}).") from None


def _open(url: str, timeout: float):
    return urlopen(Request(url, headers={"User-Agent": f"KiCadProjectManager/{__version__}"}), timeout=timeout)


def fetch_release(url: str = MANIFEST_URL, timeout: float = 15) -> Release:
    try:
        with _open(url, timeout) as resp:
            return parse_manifest(resp.read())
    except (URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"Could not reach GitHub: {getattr(e, 'reason', e)}") from None


def is_newer(release: Release, build: int | None = None) -> bool:
    build = BUILD if build is None else build
    return build > 0 and release.build > build


def download(asset: Asset, progress: Callable[[int, int], None] | None = None, timeout: float = 60) -> Path:
    """Download ``asset`` into a temp folder and verify it. Returns the file path."""
    folder = Path(tempfile.mkdtemp(prefix="kpm-update-"))
    path = folder / Path(asset.name).name
    digest = hashlib.sha256()
    received = 0
    try:
        with _open(asset.url, timeout) as resp, path.open("wb") as out:
            while chunk := resp.read(256 * 1024):
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress:
                    progress(received, asset.size)
    except (URLError, TimeoutError, OSError) as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError(f"The download failed: {getattr(e, 'reason', e)}") from None
    if received != asset.size or digest.hexdigest() != asset.sha256:
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError("The downloaded file does not match the checksum the release publishes. It has been discarded.")
    return path


def install(path: Path, kind: InstallKind) -> None:
    """Install a verified download.

    Windows: starts the installer and returns; the app must quit right away so
    the installer can replace its files (it restarts the app when done).
    Arch Linux: blocks until pacman is done; the app should then restart.
    """
    if kind is InstallKind.WINDOWS_INSTALLER:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(path), "/SILENT", "/NORESTART", "/RELAUNCH=1"], creationflags=flags, close_fds=True)
    elif kind is InstallKind.ARCH_PACKAGE:
        if not shutil.which("pkexec"):
            raise UpdateError(f"pkexec is not available. Install the update in a terminal with:\n\nsudo pacman -U {path}")
        proc = subprocess.run(["pkexec", "pacman", "-U", "--noconfirm", str(path)], capture_output=True, text=True)
        if proc.returncode in (126, 127):
            raise UpdateError("The update was cancelled.")
        if proc.returncode != 0:
            raise UpdateError(f"pacman could not install the update:\n\n{(proc.stderr or proc.stdout).strip()}")
    else:
        raise UpdateError("This copy was not installed from a release, so it cannot update itself.")


def restart_command() -> list[str] | None:
    """Command that starts the freshly installed app (Arch Linux)."""
    exe = shutil.which("kicad-project-manager")
    return [exe] if exe else None
