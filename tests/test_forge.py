import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from kicad_project_manager.forge import ForgeError, GitHubForge, guess_kind, make_forge


class FakeServer:
    """Serves canned JSON for (method, path) and records every request."""

    def __init__(self):
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self, method):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                server.requests.append(
                    {
                        "method": method,
                        "path": url.path,
                        "query": parse_qs(url.query),
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                status, payload = server.routes.get((method, url.path), (404, {"message": "not found"}))
                if callable(payload):
                    payload = payload(parse_qs(url.query))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def last(self, method: str, path: str) -> dict:
        return next(r for r in reversed(self.requests) if r["method"] == method and r["path"] == path)


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.httpd.shutdown()


def test_guess_kind():
    assert guess_kind("https://github.com") == "github"
    assert guess_kind("gitlab.com") == "gitlab"
    assert guess_kind("https://gitlab.example.org") == "gitlab"
    assert guess_kind("https://gitea.bremergy.de") == "gitea"
    assert guess_kind("https://git.example.com") is None


def test_github_api_url():
    assert GitHubForge("https://github.com", "t").api_url == "https://api.github.com"
    assert GitHubForge("https://ghe.corp", "t").api_url == "https://ghe.corp/api/v3"


def test_gitea(server):
    base = "/api/v1"
    server.routes[("GET", f"{base}/user")] = (200, {"login": "marvin", "full_name": "Marvin", "email": "m@x"})
    server.routes[("GET", f"{base}/user/orgs")] = (200, [{"username": "PCB"}])
    server.routes[("GET", f"{base}/repos/search")] = (
        200,
        {"data": [{"full_name": "PCB/a", "name": "a", "clone_url": "c", "ssh_url": "s", "html_url": "h"}]},
    )
    server.routes[("POST", f"{base}/orgs/PCB/repos")] = (201, {"full_name": "PCB/new", "name": "new"})
    server.routes[("GET", f"{base}/repos/PCB/empty/commits")] = (409, {"message": "empty"})
    server.routes[("GET", f"{base}/repos/PCB/a/commits")] = (
        200,
        [{"commit": {"author": {"name": "Anna", "date": "2026-10-01T10:00:00+02:00"}, "committer": {"date": "2026-10-01T11:00:00+02:00"}}}],
    )
    forge = make_forge("gitea", server.url, "tok")
    assert forge.current_user().login == "marvin"
    assert server.last("GET", f"{base}/user")["headers"]["authorization"] == "token tok"
    assert forge.owners() == ["marvin", "PCB"]
    assert [r.full_name for r in forge.list_repos()] == ["PCB/a"]
    assert forge.create_repo("PCB", "new").full_name == "PCB/new"
    assert server.last("POST", f"{base}/orgs/PCB/repos")["body"]["private"] is True
    assert forge.last_commit("PCB/empty") is None
    date, author = forge.last_commit("PCB/a")
    assert (date.hour, author) == (11, "Anna")
    assert forge.git_username(forge.current_user()) == "marvin"


def test_github_enterprise(server):
    base = "/api/v3"
    server.routes[("GET", f"{base}/user")] = (200, {"login": "keks", "id": 7, "name": None, "email": None})
    server.routes[("GET", f"{base}/user/orgs")] = (200, [{"login": "Bremergy-e-V"}])
    server.routes[("GET", f"{base}/user/repos")] = (200, lambda q: [] if q["page"] != ["1"] else [{"full_name": "keks/a", "name": "a"}])
    server.routes[("POST", f"{base}/user/repos")] = (201, {"full_name": "keks/new", "name": "new"})
    server.routes[("GET", f"{base}/repos/keks/a/commits")] = (200, [{"commit": {"author": {"name": "K", "date": "2026-10-01T09:00:00Z"}}}])
    forge = make_forge("github", server.url, "tok")
    user = forge.current_user()
    assert user.email == "7+keks@users.noreply.github.com"
    assert server.last("GET", f"{base}/user")["headers"]["authorization"] == "Bearer tok"
    assert forge.owners() == ["keks", "Bremergy-e-V"]
    assert [r.full_name for r in forge.list_repos()] == ["keks/a"]
    assert forge.create_repo("KEKS", "new").full_name == "keks/new"  # own account, case-insensitive
    date, author = forge.last_commit("keks/a")
    assert (date.utcoffset().total_seconds(), author) == (0, "K")


def test_gitlab(server):
    base = "/api/v4"
    project = {
        "path_with_namespace": "hw/boards/motor",
        "path": "motor",
        "name": "Motor",
        "http_url_to_repo": "https://gl/hw/boards/motor.git",
        "ssh_url_to_repo": "git@gl:hw/boards/motor.git",
        "web_url": "https://gl/hw/boards/motor",
    }
    server.routes[("GET", f"{base}/user")] = (200, {"username": "marvin", "name": "Marvin", "commit_email": "c@x"})
    server.routes[("GET", f"{base}/groups")] = (200, [{"full_path": "hw/boards"}, {"full_path": "hw"}])
    server.routes[("GET", f"{base}/projects")] = (200, [project])
    server.routes[("GET", f"{base}/namespaces/hw%2Fboards")] = (200, {"id": 42})
    server.routes[("POST", f"{base}/projects")] = (201, project)
    server.routes[("GET", f"{base}/projects/hw%2Fboards%2Fmotor")] = (200, project)
    server.routes[("GET", f"{base}/projects/hw%2Fboards%2Fmotor/repository/commits")] = (
        200,
        [{"committed_date": "2026-10-02T08:00:00.000+02:00", "author_name": "Bob"}],
    )
    server.routes[("GET", f"{base}/projects/hw%2Fempty")] = (200, {**project, "path_with_namespace": "hw/empty"})
    forge = make_forge("gitlab", server.url, "tok")
    assert forge.current_user().email == "c@x"
    assert server.last("GET", f"{base}/user")["headers"]["private-token"] == "tok"
    assert forge.git_username(forge.current_user()) == "oauth2"
    assert forge.owners() == ["marvin", "hw", "hw/boards"]
    repo = forge.list_repos()[0]
    assert (repo.full_name, repo.name, repo.clone_url) == ("hw/boards/motor", "motor", "https://gl/hw/boards/motor.git")
    forge.create_repo("hw/boards", "motor", private=False)
    body = server.last("POST", f"{base}/projects")["body"]
    assert (body["namespace_id"], body["visibility"], body["path"]) == (42, "public", "motor")
    assert forge.last_commit("hw/boards/motor")[1] == "Bob"
    assert forge.last_commit("hw/empty") is None  # commits 404, but the project exists
    with pytest.raises(ForgeError):
        forge.last_commit("hw/missing")


def test_unreachable_server():
    forge = make_forge("gitea", "http://127.0.0.1:9", "tok", timeout=2)
    with pytest.raises(ForgeError) as info:
        forge.current_user()
    assert info.value.status == 0
