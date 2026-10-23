"""
tests/test_audit_log.py
The audit log: what gets recorded, the offline fallback file, filtering, and
GET /api/audit.

Uses an isolated temp config and the test database (face_attendance_test).
Never touches the real config.json or instance/.

Run: python tests/test_audit_log.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def get_real_cfg_hash():
    p = os.path.join(PROJECT_ROOT, "config.json")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


_cfg_hash_before = get_real_cfg_hash()

# Setup isolated temp config
tmp_dir = tempfile.mkdtemp(prefix="face_web_test_")
tmp_cfg_path = os.path.join(tmp_dir, "config.json")
real_cfg_path = os.path.join(PROJECT_ROOT, "config.json")
if os.path.exists(real_cfg_path):
    with open(real_cfg_path, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)

cfg_copy["mongodb_db"] = "face_attendance_test"
with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)

os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

import server
from auth.audit import ACTIONS, AuditLog
from db.mongo_client import MongoAttendanceDB
from helpers import TEST_USER, sign_in, signed_in_client, socket_client

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}  -- {detail}")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


GOOD_URI = "mongodb://localhost:27017"
BAD_URI = "mongodb://localhost:29999"
TODAY = datetime.now().date().isoformat()


class FakeDB:
    """Stands in for MongoAttendanceDB: keeps entries in a list, can go offline or fail."""

    def __init__(self):
        self.rows = []
        self.online = True
        self.explode = False

    def insert_audit(self, entry):
        if self.explode:
            raise RuntimeError("boom")
        if not self.online:
            return False
        self.rows.append(entry)
        return True

    def find_audit(self, query, limit=None):
        if not self.online:
            return None
        rows = [r for r in self.rows if all(self._match(r, k, v) for k, v in query.items())]
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return [dict(r) for r in (rows[:limit] if limit else rows)]

    def count_audit(self, query):
        if not self.online:
            return None
        return sum(1 for r in self.rows if all(self._match(r, k, v) for k, v in query.items()))

    @staticmethod
    def _match(row, key, cond):
        value = row.get(key)
        if isinstance(cond, dict):
            if "$gte" in cond and not value >= cond["$gte"]:
                return False
            if "$lte" in cond and not value <= cond["$lte"]:
                return False
            return True
        return value == cond


# ──────────────────────────────────────────────────────────────────────────
section("1. AuditLog: writing entries")

unit_dir = tempfile.mkdtemp(prefix="face_web_audit_")
clock = [datetime(2026, 3, 10, 9, 0, 0)]
fake = FakeDB()
log = AuditLog(lambda: fake, unit_dir, clock=lambda: clock[0])
offline_path = os.path.join(unit_dir, "audit-offline.jsonl")

entry = log.record("auth.login", user="ana", address="10.0.0.5")
check("Entry has the time", entry["time"] == "2026-03-10T09:00:00", entry["time"])
check("Entry keeps who and from where", entry["user"] == "ana" and entry["address"] == "10.0.0.5")
check("Target is None when not given", entry["target"] is None)
check("Details default to an empty object", entry["details"] == {})
check("Written to the database", len(fake.rows) == 1)
check("No offline file while the database is up", not os.path.exists(offline_path))

clock[0] += timedelta(minutes=1)
log.record("person.deleted", user="ana", target=7, details={"name": "Bo"})
check("Numeric target is stored as text", fake.rows[-1]["target"] == "7")

fake.online = False
clock[0] += timedelta(minutes=1)
log.record("camera.started", user="ben", target="camera 0")
check("Database down: entry goes to the offline file", os.path.exists(offline_path))
with open(offline_path, encoding="utf-8") as f:
    lines = [json.loads(line) for line in f if line.strip()]
check("Offline file holds one JSON line", len(lines) == 1 and lines[0]["action"] == "camera.started")

fake.online = True
fake.explode = True
clock[0] += timedelta(minutes=1)
try:
    log.record("camera.stopped", user="ben")
    check("A failing database never breaks the action", True)
except Exception as exc:  # noqa: BLE001
    check("A failing database never breaks the action", False, repr(exc))
fake.explode = False
with open(offline_path, encoding="utf-8") as f:
    check("Failed write falls back to the file", sum(1 for line in f if line.strip()) == 2)

log.record("not.a.real.action", user="ben")
check("Unknown action is still recorded", fake.rows[-1]["action"] == "not.a.real.action")

# ──────────────────────────────────────────────────────────────────────────
section("2. AuditLog: reading and filtering")

clock[0] = datetime(2026, 3, 12, 15, 30, 0)
log.record("settings.saved", user="ana", details={"changed": ["late_after"]})

entries, total, db_ok = log.query()
check("Reads merge database and offline file", total == 6, str(total))
check("Database reported available", db_ok is True)
check("Newest first", [e["time"] for e in entries] == sorted((e["time"] for e in entries), reverse=True))
check("No _id leaks out", all("_id" not in e for e in entries))

entries, total, _ = log.query(user="ben")
check("Filter by user (includes offline entries)", total == 3 and all(e["user"] == "ben" for e in entries))
entries, total, _ = log.query(action="camera.started")
check("Filter by action", total == 1 and entries[0]["target"] == "camera 0")
entries, total, _ = log.query(date_from="2026-03-11")
check("From date is inclusive", total == 1 and entries[0]["action"] == "settings.saved")
entries, total, _ = log.query(date_to="2026-03-10")
check("To date is inclusive", total == 5, str(total))
entries, total, _ = log.query(date_from="2026-03-12", date_to="2026-03-12")
check("Single-day range", total == 1)
entries, total, _ = log.query(limit=2)
check("Limit cuts the list but total counts everything", len(entries) == 2 and total == 6)
check("Limit keeps the newest", entries[0]["action"] == "settings.saved")

fake.online = False
entries, total, db_ok = log.query()
check("Database down: offline entries still shown", total == 2 and db_ok is False)

with open(offline_path, "a", encoding="utf-8") as f:
    f.write("not json\n\n")
entries, total, _ = log.query()
check("A damaged line in the offline file is skipped", total == 2)
shutil.rmtree(unit_dir, ignore_errors=True)

# ──────────────────────────────────────────────────────────────────────────
section("3. Actions recorded by the app")

test_db = MongoAttendanceDB(GOOD_URI, "face_attendance_test")
mongo_available = test_db.is_connected
if mongo_available:
    test_db._client["face_attendance_test"]["audit_log"].delete_many({})
server.mongo_db = test_db
server.engine._db = test_db

client = signed_in_client(server)


def audit(**params):
    res = client.get("/api/audit", query_string=params)
    return res.status_code, res.get_json()


status, data = audit(action="auth.login")
check("Sign-in is recorded", status == 200 and data["total"] >= 1, str(data)[:200])
if data and data.get("entries"):
    e = data["entries"][0]
    check("Sign-in entry names the user", e["user"] == TEST_USER)
    check("Sign-in entry has the address", e["address"] == "127.0.0.1")

bad = server.app.test_client()
sign_in(bad, TEST_USER, "wrong-password")
server.app.extensions["face_web_throttle"].succeeded(TEST_USER, "127.0.0.1")
status, data = audit(action="auth.login_failed")
check("Failed sign-in is recorded with the username tried",
      data["total"] == 1 and data["entries"][0]["target"] == TEST_USER and data["entries"][0]["user"] is None,
      str(data["entries"][:1]))
check("Failed sign-in never stores the password", "wrong-password" not in json.dumps(data))

if mongo_available:
    from helpers import register_people  # noqa: E402
    register_people(server, {901: "Audit Person"})
    res = client.post("/api/attendance/manual", json={"person_id": 901, "name": "Audit Person", "status": "Present"})
    record_id = res.get_json()["record"]["id"]
    status, data = audit(action="attendance.manual_mark")
    e = data["entries"][0] if data["entries"] else {}
    check("Manual mark is recorded", e.get("target") == record_id, str(e))
    check("Manual mark details", e.get("details") == {"person_id": 901, "name": "Audit Person", "status": "Present"},
          str(e.get("details")))

    client.delete(f"/api/attendance/{record_id}")
    status, data = audit(action="attendance.deleted")
    check("Record delete is recorded", data["total"] == 1 and data["entries"][0]["target"] == record_id)
else:
    print("  [SKIP] MongoDB is not running on localhost:27017")

res = client.post("/api/config", json={"cooldown_minutes": int(server.cfg.get("cooldown_minutes", 5)) + 1})
status, data = audit(action="settings.saved")
check("Settings save is recorded", res.status_code == 200 and data["total"] == 1, str(res.get_json())[:200])
if data["entries"]:
    check("Settings entry lists the changed key",
          data["entries"][0]["details"].get("changed") == ["cooldown_minutes"], str(data["entries"][0]))

sio = socket_client(server, client)
old_threshold = server.cfg.get("confidence_threshold", 60)
sio.emit("settings:threshold", {"value": 77}, callback=True)
sio.emit("settings:threshold", {"value": old_threshold}, callback=True)
sio.disconnect()
status, data = audit(action="settings.threshold")
check("Threshold change over the socket is recorded", data["total"] == 2, str(data["total"]))
if data["entries"]:
    # Both changes land in the same second, so match by content, not order
    details = [e["details"] for e in data["entries"]]
    check("Threshold entries have from and to",
          {"from": old_threshold, "to": 77} in details and {"from": 77, "to": old_threshold} in details,
          str(details))

client.post("/api/db/reconnect")
status, data = audit(action="db.reconnect")
check("Database retry is recorded", data["total"] == 1)

client.delete("/api/persons/99999")
status, data = audit(action="person.deleted")
check("A refused delete (404) records nothing", data["total"] == 0)

out = signed_in_client(server)
out.post("/logout")
status, data = audit(action="auth.logout")
check("Sign-out is recorded", data["total"] == 1 and data["entries"][0]["user"] == TEST_USER)

# ──────────────────────────────────────────────────────────────────────────
section("4. GET /api/audit")

status, data = audit()
check("Returns entries, total and availability",
      status == 200 and {"entries", "total", "db_available", "actions", "users"} <= set(data))
check("Lists every action with its label", data["actions"] == ACTIONS)
check("Lists the admins for the user filter", data["users"] == [TEST_USER])
check("Database availability matches", data["db_available"] is mongo_available)

status, data = audit(user="nobody")
check("Unknown user filter -> empty list", status == 200 and data["total"] == 0 and data["entries"] == [])
status, data = audit(**{"from": TODAY, "to": TODAY})
check("Today's range includes today's entries", data["total"] > 0)
status, data = audit(**{"to": "2000-01-01"})
check("A range before any entry is empty", data["total"] == 0)
status, data = audit(limit=1)
check("limit=1 returns one entry", len(data["entries"]) == 1 and data["total"] > 1)

status, data = audit(limit="abc")
check("Non-numeric limit -> 400", status == 400)
status, data = audit(**{"from": "yesterday"})
check("Bad from date -> 400", status == 400 and "from" in data["error"])
status, data = audit(**{"to": "2026-1-5"})
check("Bad to date -> 400", status == 400)
status, data = audit(limit=0)
check("limit below 1 is raised to 1", status == 200 and len(data["entries"]) <= 1)

# ──────────────────────────────────────────────────────────────────────────
section("5. Database outage")

offline_db = MongoAttendanceDB(BAD_URI, "face_attendance_test")
server.mongo_db = offline_db
server.engine._db = offline_db
client.post("/api/camera/stop")
status, data = audit(action="camera.stopped")
check("Entry made during the outage is shown", data["total"] == 1)
check("Reported as offline", data["db_available"] is False)
check("Offline file is in the temporary instance folder",
      os.path.exists(os.path.join(server.INSTANCE_DIR, "audit-offline.jsonl")))

if mongo_available:
    server.mongo_db = test_db
    server.engine._db = test_db
    status, data = audit()
    actions = {e["action"] for e in data["entries"]}
    check("Back online: outage entries and database entries are merged",
          "camera.stopped" in actions and "auth.login" in actions and data["db_available"] is True)
    test_db._client["face_attendance_test"]["audit_log"].delete_many({})

# ──────────────────────────────────────────────────────────────────────────
section("6. Real config.json untouched")

check("config.json hash unchanged", get_real_cfg_hash() == _cfg_hash_before)

offline_db.close()
test_db.close()
shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
