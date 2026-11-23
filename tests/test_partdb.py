import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from kicad_project_manager.partdb import (
    NOT_FOUND_COMMENT,
    BomRow,
    PartDB,
    PartDBError,
    candidate_part_ids,
    normalize_url,
    parse_bom_csv,
    resolve,
    sync_bom,
)


class FakePartDB:
    """An in-memory Part-DB API: tokens, parts, projects and BOM entries."""

    def __init__(self, level: int = 3, page_size: int = 2):
        self.level = level
        self.page_size = page_size
        self.parts = {1704: {"name": "LED", "manufacturer_product_number": "15435385A9050"}, 1036: {"name": "885012209073", "manufacturer_product_number": "885012209073"}}
        self.projects = {47: {"name": "BreMo27", "parent": None}, 53: {"name": "LV", "parent": 47}}
        self.entries: dict[int, dict] = {}
        self.next_id = 1000
        self.requests: list[tuple[str, str]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload=None):
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/ld+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append((method, url.path))
                if self.headers.get("Authorization") != "Bearer tok":
                    return self._send(401, {"detail": "Invalid token"})
                status, payload = fake.route(method, url.path, query, body)
                self._send(status, payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_DELETE(self):
                self._handle("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _page(self, path: str, items: list, query: dict):
        page = int(query.get("page", 1))
        chunk = items[(page - 1) * self.page_size : page * self.page_size]
        view = {}
        if page * self.page_size < len(items):
            view["hydra:next"] = f"{path}?page={page + 1}"
        return 200, {"hydra:member": chunk, "hydra:totalItems": len(items), "hydra:view": view}

    def _project(self, pid):
        p = self.projects[pid]
        return {"id": pid, "name": p["name"], "parent": f"/api/projects/{p['parent']}" if p["parent"] else None, "full_path": p["name"]}

    def _entry(self, eid):
        e = self.entries[eid]
        d = {"id": eid, "project": {"@id": f"/api/projects/{e['project']}", "id": e["project"]}, "quantity": e["quantity"], "mountnames": e["mountnames"], "comment": e.get("comment", "")}
        if e.get("part"):
            d["part"] = {"@id": f"/api/parts/{e['part']}", "id": e["part"]}
        else:
            d["name"] = e.get("name", "")
        return d

    def route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if path == "/api/tokens/current":
            return 200, {"level": self.level, "user": {"name": "marvin"}}
        if parts[:2] == ["api", "parts"] and len(parts) == 3:
            pid = int(parts[2])
            return (200, {"id": pid, **self.parts[pid]}) if pid in self.parts else (404, {"detail": "Not Found"})
        if path == "/api/parts":
            mpn = query.get("manufacturer_product_number")
            name = query.get("name")
            found = [{"id": k, **v} for k, v in self.parts.items() if (mpn and mpn.lower() in v["manufacturer_product_number"].lower()) or (name and name.lower() in v["name"].lower())]
            return self._page(path, found, query)
        if path == "/api/projects" and method == "GET":
            return self._page(path, [self._project(k) for k in self.projects], query)
        if path == "/api/projects" and method == "POST":
            pid = self.next_id = self.next_id + 1
            self.projects[pid] = {"name": body["name"], "parent": int(body["parent"].rpartition("/")[2]) if body.get("parent") else None}
            return 201, self._project(pid)
        if parts[:2] == ["api", "projects"] and len(parts) == 3:
            pid = int(parts[2])
            return (200, self._project(pid)) if pid in self.projects else (404, {"detail": "Not Found"})
        if parts[:2] == ["api", "projects"] and parts[3:] == ["children"]:
            pid = int(parts[2])
            return self._page(path, [self._project(k) for k, v in self.projects.items() if v["parent"] == pid], query)
        if path == "/api/project_bom_entries" and method == "GET":
            return self._page(path, [self._entry(k) for k in sorted(self.entries)], query)
        if path == "/api/project_bom_entries" and method == "POST":
            eid = self.next_id = self.next_id + 1
            self.entries[eid] = {
                "project": int(body["project"].rpartition("/")[2]),
                "part": int(body["part"].rpartition("/")[2]) if body.get("part") else None,
                "name": body.get("name", ""),
                "quantity": body["quantity"],
                "mountnames": body["mountnames"],
                "comment": body.get("comment", ""),
            }
            return 201, self._entry(eid)
        if parts[:2] == ["api", "project_bom_entries"] and len(parts) == 3:
            eid = int(parts[2])
            if method == "PATCH":
                self.entries[eid].update(body)
                return 200, self._entry(eid)
            if method == "DELETE":
                del self.entries[eid]
                return 204, None
        return 404, {"detail": "Not Found"}

    def bom(self, project_id: int) -> dict:
        return {(e.get("part") or e.get("name")): (e["quantity"], e["mountnames"]) for e in self.entries.values() if e["project"] == project_id}


@pytest.fixture
def fake():
    f = FakePartDB()
    yield f
    f.httpd.shutdown()


def test_normalize_url():
    assert normalize_url("partdb.example.com/") == "https://partdb.example.com"
    assert normalize_url("https://partdb.example.com/en/kicad-api/") == "https://partdb.example.com"
    assert normalize_url("https://x.org/partdb/api/projects") == "https://x.org/partdb"


def test_check_token(fake):
    assert PartDB(fake.url, "tok").check_token().level == 3
    with pytest.raises(PartDBError, match="not accepted"):
        PartDB(fake.url, "wrong").check_token()
    fake.level = 1
    with pytest.raises(PartDBError, match="can only read"):
        PartDB(fake.url, "tok").check_token()
    fake.level = 2
    PartDB(fake.url, "tok").check_token()


def test_projects_are_paged(fake):
    for i in range(5):
        fake.projects[100 + i] = {"name": f"P{i}", "parent": 53}
    names = [p.name for p in PartDB(fake.url, "tok").projects()]
    assert len(names) == 7
    assert {p.id for p in PartDB(fake.url, "tok").children(53)} == set(range(100, 105))


CSV = '''"Reference","Value","Footprint","DNP","Part-DB ID","ID","PARTDB","Part-DB URL","MPN","manf#","NAME"
"#PWR01","GND","","","","","","","","",""
"D1","LED","LED:3535","","1704","","","http://partdb/en/part/1704/info","15435385A9050","",""
"D2","LED","LED:3535","","1704","","","","","",""
"C1","10u","C:1210","","","","","","","885012209073",""
"C2","10u","C:1210","","","1036","https://partdb/en/part/1036","","","",""
"R1","1k","R:0603","","","","","","","",""
"R10","1k","R:0603","","","","","","","",""
"R2","1k","R:0603","","","","","","","",""
"J1","Conn","J:XT60","","","","","","XT60-OLD","",""
'''


def test_parse_bom_csv():
    rows = parse_bom_csv(CSV)
    assert [r.reference for r in rows][:2] == ["D1", "D2"]  # power symbols left out
    assert rows[0].fields["Part-DB ID"] == "1704"


def test_candidate_part_ids():
    assert candidate_part_ids(BomRow("X", fields={"Part-DB ID": "5", "Part-DB URL": "http://h/en/part/6/info"})) == [5, 6]
    assert candidate_part_ids(BomRow("X", fields={"ID": "7", "PARTDB": "https://h/en/part/7"})) == [7]
    assert candidate_part_ids(BomRow("X", fields={"ID": "7"})) == []  # a bare ID field is not trusted


def test_resolve(fake):
    entries = resolve(parse_bom_csv(CSV), PartDB(fake.url, "tok"))
    by_key = {e.part_id or e.name: (e.quantity, e.mountnames) for e in entries}
    assert by_key == {
        1704: (2, "D1, D2"),
        1036: (2, "C1, C2"),  # by manf# and by the old ID/PARTDB fields
        "1k 0603": (3, "R1, R2, R10"),
        "XT60-OLD": (1, "J1"),
    }


def test_sync_creates_and_updates_subproject(fake):
    partdb = PartDB(fake.url, "tok")
    fake.entries[1] = {"project": 26, "part": 1704, "quantity": 9, "mountnames": "X1"}  # another project's entry
    result = sync_bom(partdb, 53, None, "27_EL_LV_Test", resolve(parse_bom_csv(CSV), partdb))
    sub = result.subproject_id
    assert fake.projects[sub] == {"name": "27_EL_LV_Test", "parent": 53}
    assert (result.added, result.updated, result.removed) == (4, 0, 0)
    assert sorted(result.unmatched) == ["1k 0603", "XT60-OLD"]
    assert fake.bom(sub)[1704] == (2, "D1, D2")
    assert next(e for e in fake.entries.values() if e.get("name") == "XT60-OLD")["comment"] == NOT_FOUND_COMMENT

    # A comment written by hand is kept; changed and removed lines are updated.
    led = next(k for k, e in fake.entries.items() if e["part"] == 1704 and e["project"] == sub)
    fake.entries[led]["comment"] = "use the tray"
    changed = CSV.replace('"D2","LED"', '"D3","LED"').replace('"J1","Conn","J:XT60","","","","","","XT60-OLD","",""\n', "")
    again = sync_bom(partdb, 53, None, "27_EL_LV_Test", resolve(parse_bom_csv(changed), partdb))
    assert again.subproject_id == sub  # found again by name, no duplicate
    assert (again.added, again.updated, again.removed) == (0, 1, 1)
    assert fake.bom(sub)[1704] == (2, "D1, D3")
    assert fake.entries[led]["comment"] == "use the tray"
    assert "XT60-OLD" not in fake.bom(sub)
    assert fake.entries[1]["quantity"] == 9  # other projects untouched

    unchanged = sync_bom(partdb, 53, sub, "renamed PCB", resolve(parse_bom_csv(changed), partdb))
    assert (unchanged.subproject_id, unchanged.added, unchanged.updated, unchanged.removed) == (sub, 0, 0, 0)


def test_sync_with_missing_parent(fake):
    with pytest.raises(PartDBError, match="no longer exists"):
        sync_bom(PartDB(fake.url, "tok"), 999, None, "X", [])
