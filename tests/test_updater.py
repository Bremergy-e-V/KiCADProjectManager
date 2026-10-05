import functools
import hashlib
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kicad_project_manager import updater
from kicad_project_manager.updater import Asset, InstallKind, UpdateError

SHA = "a" * 64


def manifest(**overrides) -> bytes:
    d = {
        "version": "0.2.0",
        "build": 12,
        "commit": "abc",
        "releaseUrl": "https://example/releases/tag/latest",
        "windows": {"name": "setup.exe", "url": "https://example/setup.exe", "sha256": SHA, "size": 10},
        "archlinux": {"name": "pkg.tar.zst", "url": "https://example/pkg.tar.zst", "sha256": SHA.upper(), "size": 20},
    }
    d.update(overrides)
    return json.dumps(d).encode()


def test_parse_manifest():
    release = updater.parse_manifest(manifest())
    assert release.build == 12
    assert release.asset_for(InstallKind.WINDOWS_INSTALLER).size == 10
    assert release.asset_for(InstallKind.ARCH_PACKAGE).sha256 == SHA  # normalized to lower case
    assert release.asset_for(InstallKind.OTHER) is None


@pytest.mark.parametrize(
    "data",
    [b"<html>not found</html>", manifest(build="x"), manifest(windows={"name": "a", "url": "b", "sha256": "short", "size": 1}), b"[]"],
)
def test_parse_manifest_rejects_junk(data):
    with pytest.raises(UpdateError):
        updater.parse_manifest(data)


def test_is_newer():
    release = updater.parse_manifest(manifest())
    assert updater.is_newer(release, build=11)
    assert not updater.is_newer(release, build=12)
    assert not updater.is_newer(release, build=0)  # development copies never update


def test_development_copy_does_not_update(monkeypatch):
    monkeypatch.setattr(updater, "BUILD", 0)
    assert not updater.updates_supported()


@pytest.fixture
def file_server(tmp_path):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield tmp_path, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_download_verifies_checksum(file_server):
    folder, url = file_server
    payload = b"installer bytes" * 1000
    (folder / "setup.exe").write_bytes(payload)
    good = Asset("setup.exe", f"{url}/setup.exe", hashlib.sha256(payload).hexdigest(), len(payload))
    seen = []
    path = updater.download(good, progress=lambda got, total: seen.append((got, total)))
    assert path.read_bytes() == payload
    assert seen[-1] == (len(payload), len(payload))

    with pytest.raises(UpdateError):
        updater.download(Asset("setup.exe", good.url, "0" * 64, good.size))
    with pytest.raises(UpdateError):
        updater.download(Asset("setup.exe", good.url, good.sha256, good.size + 1))
    with pytest.raises(UpdateError):
        updater.download(Asset("missing.exe", f"{url}/missing.exe", good.sha256, good.size))


def test_fetch_release(file_server):
    folder, url = file_server
    (folder / "version.json").write_bytes(manifest(build=99))
    assert updater.fetch_release(f"{url}/version.json").build == 99
