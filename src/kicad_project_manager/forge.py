"""Clients for the git hosting services ("forges") the app can work with.

Everything the app needs from a server goes through the small :class:`Forge`
interface, implemented for Gitea/Forgejo, GitHub (including GitHub Enterprise)
and GitLab. Only the standard library is used.

Repositories are always named by their full path, ``owner/name``. On GitLab the
owner may itself contain slashes (``group/subgroup/name``).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


class ForgeError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


@dataclass
class User:
    login: str
    name: str = ""
    email: str = ""


@dataclass
class Repo:
    full_name: str  # "owner/name"
    name: str
    description: str = ""
    clone_url: str = ""  # HTTPS
    ssh_url: str = ""
    html_url: str = ""


def split_repo(full_name: str) -> tuple[str, str]:
    owner, _, name = full_name.rpartition("/")
    return owner, name


class Forge(ABC):
    kind: str = ""
    label: str = ""
    default_url: str = ""
    page_size = 50

    def __init__(self, url: str, token: str, timeout: float = 20):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._user: User | None = None

    # -- HTTP --------------------------------------------------------------------

    @property
    @abstractmethod
    def api_url(self) -> str: ...

    @abstractmethod
    def _auth_headers(self) -> dict[str, str]: ...

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": "KiCadProjectManager", **self._auth_headers()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(url, data=data, method=method, headers=headers), timeout=self.timeout) as resp:
                raw = resp.read()
        except HTTPError as e:
            message = e.reason
            try:
                payload = json.loads(e.read())
                message = payload.get("message") or payload.get("error") or message
                if isinstance(message, (dict, list)):
                    message = json.dumps(message)
            except Exception:
                pass
            raise ForgeError(e.code, f"{self.label}: {message} ({e.code})") from None
        except (URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ForgeError(0, f"Cannot reach {self.url}: {reason}") from None
        return json.loads(raw) if raw else None

    def _paged(self, path: str, params: dict, max_results: int = 1000) -> list:
        items: list = []
        page = 1
        while len(items) < max_results:
            batch = self._request("GET", path, {**params, self._page_size_param: self.page_size, "page": page}) or []
            if isinstance(batch, dict):  # Gitea's search wraps results
                batch = batch.get("data") or []
            items.extend(batch)
            if len(batch) < self.page_size:
                break
            page += 1
        return items

    _page_size_param = "per_page"

    # -- git access ----------------------------------------------------------------

    @property
    def git_host_url(self) -> str:
        """URL prefix of the git remotes; the token is sent to git only for these."""
        return self.url + "/"

    def git_username(self, user: User) -> str:
        return user.login

    # -- API -----------------------------------------------------------------------

    def current_user(self) -> User:
        if self._user is None:
            self._user = self._fetch_user()
        return self._user

    @abstractmethod
    def _fetch_user(self) -> User: ...

    @abstractmethod
    def owners(self) -> list[str]:
        """The current user plus every organization/group they can create repositories in."""

    @abstractmethod
    def list_repos(self) -> list[Repo]:
        """All repositories the user is a member of."""

    @abstractmethod
    def get_repo(self, full_name: str) -> Repo: ...

    @abstractmethod
    def create_repo(self, owner: str, name: str, description: str = "", private: bool = True) -> Repo: ...

    @abstractmethod
    def last_commit(self, full_name: str) -> tuple[datetime, str] | None:
        """Date and author name of the newest commit on the default branch (None if empty)."""

    def repo_web_url(self, full_name: str) -> str:
        return f"{self.url}/{full_name}"

    @classmethod
    def token_help(cls, url: str) -> str:
        return ""


# -- Gitea / Forgejo -------------------------------------------------------------------


class GiteaForge(Forge):
    kind = "gitea"
    label = "Gitea / Forgejo"
    default_url = ""
    _page_size_param = "limit"

    @property
    def api_url(self) -> str:
        return f"{self.url}/api/v1"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"}

    @staticmethod
    def _repo(d: dict) -> Repo:
        return Repo(
            d["full_name"], d["name"], d.get("description") or "", d.get("clone_url", ""), d.get("ssh_url", ""), d.get("html_url", "")
        )

    def _fetch_user(self) -> User:
        d = self._request("GET", "/user")
        return User(d["login"], d.get("full_name") or "", d.get("email") or "")

    def owners(self) -> list[str]:
        orgs = self._paged("/user/orgs", {})
        return [self.current_user().login, *sorted((o["username"] for o in orgs), key=str.casefold)]

    def list_repos(self) -> list[Repo]:
        return [self._repo(d) for d in self._paged("/repos/search", {"sort": "alpha"})]

    def get_repo(self, full_name: str) -> Repo:
        owner, name = split_repo(full_name)
        return self._repo(self._request("GET", f"/repos/{quote(owner)}/{quote(name)}"))

    def create_repo(self, owner: str, name: str, description: str = "", private: bool = True) -> Repo:
        body = {"name": name, "description": description, "private": private, "default_branch": "main", "auto_init": False}
        if owner.casefold() == self.current_user().login.casefold():
            return self._repo(self._request("POST", "/user/repos", body=body))
        return self._repo(self._request("POST", f"/orgs/{quote(owner)}/repos", body=body))

    def last_commit(self, full_name: str) -> tuple[datetime, str] | None:
        owner, name = split_repo(full_name)
        params = {"limit": 1, "stat": "false", "verification": "false", "files": "false"}
        try:
            commits = self._request("GET", f"/repos/{quote(owner)}/{quote(name)}/commits", params)
        except ForgeError as e:
            if e.status == 409:  # empty repository
                return None
            raise
        return _github_style_commit(commits)

    @classmethod
    def token_help(cls, url: str) -> str:
        link = f"<a href='{url}/user/settings/applications'>Settings → Applications</a>" if url else "Settings → Applications"
        return (
            f"Create a token under {link} → <i>Generate New Token</i>. Give it <b>Read and Write</b> "
            "access to <i>repository</i> and <i>organization</i>, and <b>Read</b> access to <i>user</i>."
        )


def _github_style_commit(commits: list | None) -> tuple[datetime, str] | None:
    if not commits:
        return None
    commit = commits[0]["commit"]
    author = commit.get("author") or {}
    date = (commit.get("committer") or author)["date"]
    return datetime.fromisoformat(date.replace("Z", "+00:00")), author.get("name") or ""


# -- GitHub ----------------------------------------------------------------------------


class GitHubForge(Forge):
    kind = "github"
    label = "GitHub"
    default_url = "https://github.com"
    page_size = 100

    @property
    def api_url(self) -> str:
        host = urlparse(self.url).hostname or ""
        return "https://api.github.com" if host in ("github.com", "www.github.com") else f"{self.url}/api/v3"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    @staticmethod
    def _repo(d: dict) -> Repo:
        return Repo(
            d["full_name"], d["name"], d.get("description") or "", d.get("clone_url", ""), d.get("ssh_url", ""), d.get("html_url", "")
        )

    def _fetch_user(self) -> User:
        d = self._request("GET", "/user")
        email = d.get("email") or f"{d['id']}+{d['login']}@users.noreply.github.com"
        return User(d["login"], d.get("name") or "", email)

    def owners(self) -> list[str]:
        orgs = self._paged("/user/orgs", {})
        return [self.current_user().login, *sorted((o["login"] for o in orgs), key=str.casefold)]

    def list_repos(self) -> list[Repo]:
        params = {"affiliation": "owner,collaborator,organization_member", "sort": "full_name"}
        return [self._repo(d) for d in self._paged("/user/repos", params)]

    def get_repo(self, full_name: str) -> Repo:
        owner, name = split_repo(full_name)
        return self._repo(self._request("GET", f"/repos/{quote(owner)}/{quote(name)}"))

    def create_repo(self, owner: str, name: str, description: str = "", private: bool = True) -> Repo:
        body = {"name": name, "description": description, "private": private, "auto_init": False}
        if owner.casefold() == self.current_user().login.casefold():
            return self._repo(self._request("POST", "/user/repos", body=body))
        return self._repo(self._request("POST", f"/orgs/{quote(owner)}/repos", body=body))

    def last_commit(self, full_name: str) -> tuple[datetime, str] | None:
        owner, name = split_repo(full_name)
        try:
            commits = self._request("GET", f"/repos/{quote(owner)}/{quote(name)}/commits", {"per_page": 1})
        except ForgeError as e:
            if e.status == 409:  # empty repository
                return None
            raise
        return _github_style_commit(commits)

    @classmethod
    def token_help(cls, url: str) -> str:
        base = url or cls.default_url
        return (
            f"Create a token under <a href='{base}/settings/tokens'>Settings → Developer settings → Personal access tokens</a>. "
            "A classic token needs the <b>repo</b> and <b>read:org</b> scopes."
        )


# -- GitLab ----------------------------------------------------------------------------


class GitLabForge(Forge):
    kind = "gitlab"
    label = "GitLab"
    default_url = "https://gitlab.com"
    page_size = 100

    @property
    def api_url(self) -> str:
        return f"{self.url}/api/v4"

    def _auth_headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    def git_username(self, user: User) -> str:
        return "oauth2"  # GitLab accepts any personal access token with this user name

    @staticmethod
    def _repo(d: dict) -> Repo:
        return Repo(
            d["path_with_namespace"], d.get("path") or d["name"], d.get("description") or "",
            d.get("http_url_to_repo", ""), d.get("ssh_url_to_repo", ""), d.get("web_url", ""),
        )  # fmt: skip

    @staticmethod
    def _id(full_name: str) -> str:
        return quote(full_name, safe="")

    def _fetch_user(self) -> User:
        d = self._request("GET", "/user")
        return User(d["username"], d.get("name") or "", d.get("commit_email") or d.get("email") or d.get("public_email") or "")

    def owners(self) -> list[str]:
        groups = self._paged("/groups", {"min_access_level": 30})
        return [self.current_user().login, *sorted((g["full_path"] for g in groups), key=str.casefold)]

    def list_repos(self) -> list[Repo]:
        params = {"membership": "true", "simple": "true", "order_by": "path", "sort": "asc"}
        return [self._repo(d) for d in self._paged("/projects", params)]

    def get_repo(self, full_name: str) -> Repo:
        return self._repo(self._request("GET", f"/projects/{self._id(full_name)}"))

    def create_repo(self, owner: str, name: str, description: str = "", private: bool = True) -> Repo:
        body: dict = {"name": name, "path": name, "description": description, "visibility": "private" if private else "public"}
        if owner.casefold() != self.current_user().login.casefold():
            body["namespace_id"] = self._request("GET", f"/namespaces/{self._id(owner)}")["id"]
        return self._repo(self._request("POST", "/projects", body=body))

    def last_commit(self, full_name: str) -> tuple[datetime, str] | None:
        try:
            commits = self._request("GET", f"/projects/{self._id(full_name)}/repository/commits", {"per_page": 1})
        except ForgeError as e:
            if e.status == 404:
                self.get_repo(full_name)  # raises if the project itself is missing
                return None  # project exists but has no commits yet
            raise
        if not commits:
            return None
        return datetime.fromisoformat(commits[0]["committed_date"].replace("Z", "+00:00")), commits[0].get("author_name") or ""

    @classmethod
    def token_help(cls, url: str) -> str:
        base = url or cls.default_url
        return (
            f"Create a token under <a href='{base}/-/user_settings/personal_access_tokens'>Preferences → Access tokens</a> "
            "with the <b>api</b> scope."
        )


FORGES: dict[str, type[Forge]] = {f.kind: f for f in (GiteaForge, GitHubForge, GitLabForge)}


def make_forge(kind: str, url: str, token: str, timeout: float = 20) -> Forge:
    try:
        return FORGES[kind](url, token, timeout)
    except KeyError:
        raise ForgeError(0, f"Unknown server type '{kind}'.") from None


def guess_kind(url: str) -> str | None:
    """Best guess of the server type from its URL, or None if it can't be told."""
    host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").casefold()
    if host == "github.com" or host.startswith("github."):
        return "github"
    if host == "gitlab.com" or host.startswith("gitlab."):
        return "gitlab"
    if host.startswith(("gitea.", "forgejo.", "codeberg.")) or host == "codeberg.org":
        return "gitea"
    return None
