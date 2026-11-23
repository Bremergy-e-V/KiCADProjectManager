"""Part-DB integration: push the BOM of a KiCad project into a Part-DB project.

Each PCB gets one Part-DB project (a "subproject" below a parent the team picks, e.g.
``BreMo27 → LV → <PCB>``). Its BOM entries are kept identical to the KiCad BOM:
:func:`sync_bom` adds, updates and removes entries, so ids and hand-written comments
of entries that stay are kept.

Parts are matched with the fields Part-DB's KiCad library writes into the symbols
(``Part-DB ID``, or ``ID``/``PARTDB`` in older libraries), then by manufacturer part
number. Lines without a match are added as free-text entries.

Only the standard library is used.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .workspace import find_project_file

MIN_WRITE_LEVEL = 2  # Part-DB token levels: 1 read only, 2 edit, 3 admin, 4 full
SUBPROJECT_DESCRIPTION = "BOM synced from KiCad by KiCad Project Manager"
NOT_FOUND_COMMENT = "Not found in Part-DB"

BOM_FIELDS = ["Reference", "Value", "Footprint", "${DNP}", "Part-DB ID", "ID", "PARTDB", "Part-DB URL", "MPN", "manf#", "NAME"]
_MPN_FIELDS = ("MPN", "manf#", "NAME")
_PART_URL_RE = re.compile(r"/part/(\d+)")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class PartDBError(Exception):
    def __init__(self, message: str, status: int = 0):
        self.status = status
        super().__init__(message)


@dataclass
class TokenInfo:
    level: int
    user: str = ""
    valid_until: str = ""


@dataclass
class PartDBProject:
    id: int
    name: str
    parent_id: int | None = None
    full_path: str = ""


@dataclass
class BomEntry:
    id: int
    part_id: int | None
    name: str
    quantity: float
    mountnames: str
    comment: str = ""


def _iri_id(value) -> int | None:
    """The numeric id of an IRI (``/api/parts/12``) or an embedded object (``{"id": 12}``)."""
    if isinstance(value, dict):
        value = value.get("id", value.get("@id"))
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        tail = value.rstrip("/").rpartition("/")[2]
        if tail.isdigit():
            return int(tail)
    return None


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    # People often paste a page of the web interface; only the server is needed.
    parsed = urlparse(url)
    path = parsed.path
    for marker in ("/api", "/en/", "/de/", "/kicad-api"):
        if marker in path + "/":
            path = path[: (path + "/").index(marker)]
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/") if parsed.netloc else url


class PartDB:
    """A small client for the Part-DB REST API (``/api``)."""

    page_size = 200

    def __init__(self, url: str, token: str, timeout: float = 20):
        self.url = normalize_url(url)
        self.token = token
        self.timeout = timeout
        self._part_cache: dict[int, bool] = {}
        self._mpn_cache: dict[str, int | None] = {}

    # -- HTTP ----------------------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None, content_type: str = ""):
        url = path if path.startswith("http") else f"{self.url}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/ld+json", "User-Agent": "KiCadProjectManager", "Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = content_type or "application/ld+json"
        try:
            with urlopen(Request(url, data=data, method=method, headers=headers), timeout=self.timeout) as resp:
                raw = resp.read()
        except HTTPError as e:
            message = e.reason
            try:
                payload = json.loads(e.read())
                message = payload.get("hydra:description") or payload.get("detail") or payload.get("message") or message
            except Exception:
                pass
            if e.code == 401:
                message = "The Part-DB token was not accepted. It may be wrong or expired."
            raise PartDBError(f"Part-DB: {message} ({e.code})", e.code) from None
        except (URLError, TimeoutError, OSError) as e:
            raise PartDBError(f"Cannot reach Part-DB at {self.url}: {getattr(e, 'reason', e)}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise PartDBError(f"{self.url} does not look like a Part-DB server.") from None

    def _collection(self, path: str, params: dict | None = None) -> list[dict]:
        """All items of a collection, following the pagination links."""
        items: list[dict] = []
        next_url: str | None = path
        query = {**(params or {}), "itemsPerPage": self.page_size}
        for _ in range(1000):
            if next_url is None:
                break
            page = self._request("GET", next_url, query)
            query = None  # the "next" link already carries the query
            if isinstance(page, list):  # plain JSON
                items.extend(page)
                break
            page = page or {}
            items.extend(page.get("hydra:member", page.get("member", [])))
            view = page.get("hydra:view", page.get("view")) or {}
            nxt = view.get("hydra:next", view.get("next"))
            next_url = nxt if nxt and nxt != next_url else None
        return items

    # -- API -----------------------------------------------------------------

    def check_token(self) -> TokenInfo:
        """Make sure the token works and may write. Raises :class:`PartDBError` otherwise."""
        data = self._request("GET", "/api/tokens/current")
        if not isinstance(data, dict) or "level" not in data:
            raise PartDBError(f"{self.url} does not look like a Part-DB server.")
        user = data.get("user") or {}
        info = TokenInfo(level=int(data["level"]), user=user.get("name", "") if isinstance(user, dict) else "", valid_until=data.get("valid_until") or "")
        if info.level < MIN_WRITE_LEVEL:
            raise PartDBError("This Part-DB token can only read. Please create a token with \"Edit\" access or higher.")
        return info

    @staticmethod
    def _project(d: dict) -> PartDBProject:
        return PartDBProject(id=int(d["id"]), name=d.get("name", ""), parent_id=_iri_id(d.get("parent")), full_path=d.get("full_path") or d.get("name", ""))

    def projects(self) -> list[PartDBProject]:
        return sorted((self._project(d) for d in self._collection("/api/projects")), key=lambda p: p.full_path.casefold())

    def get_project(self, project_id: int) -> PartDBProject | None:
        try:
            return self._project(self._request("GET", f"/api/projects/{project_id}"))
        except PartDBError as e:
            if e.status == 404:
                return None
            raise

    def children(self, project_id: int) -> list[PartDBProject]:
        return [self._project(d) for d in self._collection(f"/api/projects/{project_id}/children")]

    def create_project(self, name: str, parent_id: int | None, description: str = "") -> PartDBProject:
        body = {"name": name, "description": description, "parent": f"/api/projects/{parent_id}" if parent_id else None}
        return self._project(self._request("POST", "/api/projects", body=body))

    def bom_entries(self, project_id: int) -> list[BomEntry]:
        # The API cannot filter BOM entries by project, so all of them are read.
        result = []
        for d in self._collection("/api/project_bom_entries"):
            if _iri_id(d.get("project")) != project_id:
                continue
            result.append(
                BomEntry(
                    id=int(d["id"]),
                    part_id=_iri_id(d.get("part")),
                    name=d.get("name") or "",
                    quantity=float(d.get("quantity") or 0),
                    mountnames=d.get("mountnames") or "",
                    comment=d.get("comment") or "",
                )
            )
        return result

    def add_bom_entry(self, project_id: int, part_id: int | None, name: str, quantity: float, mountnames: str, comment: str = "") -> None:
        body: dict = {"project": f"/api/projects/{project_id}", "quantity": quantity, "mountnames": mountnames, "comment": comment}
        if part_id is not None:
            body["part"] = f"/api/parts/{part_id}"
        else:
            body["name"] = name
        self._request("POST", "/api/project_bom_entries", body=body)

    def update_bom_entry(self, entry_id: int, **fields) -> None:
        self._request("PATCH", f"/api/project_bom_entries/{entry_id}", body=fields, content_type="application/merge-patch+json")

    def delete_bom_entry(self, entry_id: int) -> None:
        self._request("DELETE", f"/api/project_bom_entries/{entry_id}")

    def part_exists(self, part_id: int) -> bool:
        if part_id not in self._part_cache:
            try:
                self._request("GET", f"/api/parts/{part_id}")
                self._part_cache[part_id] = True
            except PartDBError as e:
                if e.status != 404:
                    raise
                self._part_cache[part_id] = False
        return self._part_cache[part_id]

    def find_part_by_mpn(self, mpn: str) -> int | None:
        key = mpn.strip().casefold()
        if not key:
            return None
        if key not in self._mpn_cache:
            found = None
            for d in self._collection("/api/parts", {"manufacturer_product_number": mpn.strip()}):
                if (d.get("manufacturer_product_number") or "").strip().casefold() == key:
                    found = int(d["id"])
                    break
            if found is None:  # older parts may carry the MPN only as their name
                for d in self._collection("/api/parts", {"name": mpn.strip()}):
                    if (d.get("name") or "").strip().casefold() == key:
                        found = int(d["id"])
                        break
            self._mpn_cache[key] = found
        return self._mpn_cache[key]


# -- BOM -----------------------------------------------------------------------


@dataclass
class BomRow:
    """One placed symbol of the KiCad BOM."""

    reference: str
    value: str = ""
    footprint: str = ""
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class DesiredEntry:
    part_id: int | None
    name: str
    refs: list[str] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return len(self.refs)

    @property
    def mountnames(self) -> str:
        return ", ".join(sorted(self.refs, key=_natural_key))


def _natural_key(ref: str) -> list:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", ref)]


def parse_bom_csv(text: str) -> list[BomRow]:
    """Parse the CSV written by ``kicad-cli sch export bom`` with :data:`BOM_FIELDS` (one row per symbol)."""
    rows: list[BomRow] = []
    seen: set[str] = set()
    for record in csv.DictReader(io.StringIO(text)):
        ref = (record.get("Reference") or "").strip()
        if not ref or ref.startswith("#") or ref in seen:
            continue
        seen.add(ref)
        rows.append(
            BomRow(
                reference=ref,
                value=(record.get("Value") or "").strip(),
                footprint=(record.get("Footprint") or "").strip(),
                fields={k: (v or "").strip() for k, v in record.items() if k not in ("Reference", "Value", "Footprint") and k},
            )
        )
    return rows


def export_bom(kicad_cli: str, project_dir: Path, preferred_name: str = "", timeout: float = 180) -> list[BomRow]:
    """The BOM of the KiCad project in ``project_dir``, one row per placed symbol (DNP parts left out)."""
    pro = find_project_file(project_dir, preferred_name)
    if pro is None:
        raise PartDBError("The project contains no KiCad project file.")
    sch = pro.with_suffix(".kicad_sch")
    if not sch.exists():
        raise PartDBError("The project contains no schematic, so there is no BOM.")
    with tempfile.TemporaryDirectory(prefix="kpm-bom-") as tmp:
        out = Path(tmp) / "bom.csv"
        cmd = [kicad_cli, "sch", "export", "bom", "--fields", ",".join(BOM_FIELDS), "--group-by", "", "--exclude-dnp", "-o", str(out), str(sch)]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PartDBError(f"kicad-cli could not export the BOM: {e}") from None
        if proc.returncode != 0 or not out.exists():
            detail = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
            raise PartDBError(f"kicad-cli could not export the BOM.{(' ' + detail) if detail else ''}")
        return parse_bom_csv(out.read_text(encoding="utf-8-sig", errors="replace"))


def _int(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def candidate_part_ids(row: BomRow) -> list[int]:
    """Part-DB ids named by the symbol, best first."""
    f = row.fields
    ids = [_int(f.get("Part-DB ID", ""))]
    if f.get("PARTDB"):
        ids.append(_int(f.get("ID", "")))
    for key in ("Part-DB URL", "PARTDB"):
        m = _PART_URL_RE.search(f.get(key, ""))
        if m:
            ids.append(int(m.group(1)))
    result: list[int] = []
    for i in ids:
        if i is not None and i not in result:
            result.append(i)
    return result


def mpn_of(row: BomRow) -> str:
    return next((row.fields[k] for k in _MPN_FIELDS if row.fields.get(k, "").strip() and row.fields[k].strip() != "~"), "")


def _free_text_name(row: BomRow) -> str:
    mpn = mpn_of(row)
    if mpn:
        return mpn
    footprint = row.footprint.rpartition(":")[2]
    return " ".join(x for x in (row.value, footprint) if x) or row.reference


def resolve(rows: Iterable[BomRow], partdb: PartDB) -> list[DesiredEntry]:
    """Match the BOM rows to Part-DB parts and group them into BOM entries."""
    entries: dict[tuple, DesiredEntry] = {}
    for row in rows:
        part_id = next((i for i in candidate_part_ids(row) if partdb.part_exists(i)), None)
        if part_id is None:
            # Older Part-DB symbols carry the manufacturer part number only as their value.
            for mpn in dict.fromkeys(x for x in (mpn_of(row), row.value) if x and x != "~"):
                part_id = partdb.find_part_by_mpn(mpn)
                if part_id is not None:
                    break
        if part_id is not None:
            key: tuple = ("part", part_id)
            name = ""
        else:
            name = _free_text_name(row)
            key = ("name", name.casefold())
        entry = entries.setdefault(key, DesiredEntry(part_id=part_id, name=name))
        entry.refs.append(row.reference)
    return sorted(entries.values(), key=lambda e: _natural_key(e.mountnames))


@dataclass
class SyncResult:
    subproject_id: int
    subproject_path: str = ""
    added: int = 0
    updated: int = 0
    removed: int = 0
    unmatched: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.added} added", f"{self.updated} updated", f"{self.removed} removed"]
        text = f"BOM synced to Part-DB ({', '.join(parts)})"
        if self.unmatched:
            text += f" - {len(self.unmatched)} not found in Part-DB: {', '.join(self.unmatched[:5])}{' …' if len(self.unmatched) > 5 else ''}"
        return text


def find_or_create_subproject(partdb: PartDB, parent_id: int, subproject_id: int | None, name: str) -> PartDBProject:
    if subproject_id:
        existing = partdb.get_project(subproject_id)
        if existing is not None and existing.parent_id == parent_id:
            return existing
    if partdb.get_project(parent_id) is None:
        raise PartDBError("The Part-DB project assigned to this PCB no longer exists. Please choose another one.")
    match = next((p for p in partdb.children(parent_id) if p.name.strip().casefold() == name.strip().casefold()), None)
    return match or partdb.create_project(name, parent_id, SUBPROJECT_DESCRIPTION)


def sync_bom(partdb: PartDB, parent_id: int, subproject_id: int | None, pcb_name: str, desired: list[DesiredEntry]) -> SyncResult:
    """Make the BOM of the PCB's subproject below ``parent_id`` match ``desired``."""
    project = find_or_create_subproject(partdb, parent_id, subproject_id, pcb_name)
    result = SyncResult(subproject_id=project.id, subproject_path=project.full_path)

    existing: dict[tuple, list[BomEntry]] = {}
    for entry in partdb.bom_entries(project.id):
        key = ("part", entry.part_id) if entry.part_id is not None else ("name", entry.name.strip().casefold())
        existing.setdefault(key, []).append(entry)

    for want in desired:
        key = ("part", want.part_id) if want.part_id is not None else ("name", want.name.casefold())
        if want.part_id is None:
            result.unmatched.append(want.name)
        matches = existing.pop(key, [])
        if matches:
            have, extra = matches[0], matches[1:]
            if have.quantity != want.quantity or have.mountnames != want.mountnames:
                partdb.update_bom_entry(have.id, quantity=want.quantity, mountnames=want.mountnames)
                result.updated += 1
            for e in extra:
                partdb.delete_bom_entry(e.id)
                result.removed += 1
        else:
            comment = "" if want.part_id is not None else NOT_FOUND_COMMENT
            partdb.add_bom_entry(project.id, want.part_id, want.name, want.quantity, want.mountnames, comment)
            result.added += 1

    for leftovers in existing.values():
        for e in leftovers:
            partdb.delete_bom_entry(e.id)
            result.removed += 1
    return result


def sync_project(kicad_cli: str, project_dir: Path, preferred_name: str, partdb: PartDB, parent_id: int, subproject_id: int | None, pcb_name: str) -> SyncResult:
    """Export the BOM of ``project_dir`` and sync it into Part-DB (blocking)."""
    rows = export_bom(kicad_cli, project_dir, preferred_name)
    return sync_bom(partdb, parent_id, subproject_id, pcb_name, resolve(rows, partdb))
