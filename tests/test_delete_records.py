"""
tests/test_delete_records.py
Deleting one attendance record and
clearing today's records.

What must hold:
- The record, its snapshot file and the cooldown it started are removed.
- Nothing else is: other records, other days, other snapshots, and the
  real database and config are left exactly as they were.
Uses Flask test_client, an isolated temp config and the test database.
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
        real_cfg = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    real_cfg = dict(DEFAULT_CONFIG)

cfg_copy = json.loads(json.dumps(real_cfg))
cfg_copy["mongodb_db"] = "face_attendance_test"
with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)

os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

from pymongo import MongoClient

import server
from db.mongo_client import MongoAttendanceDB

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


SNAP_DIR = os.path.join(PROJECT_ROOT, "snapshots")
os.makedirs(SNAP_DIR, exist_ok=True)
PID = 9901
NAME = "P9 Delete Tester"
TODAY = datetime.now().strftime("%Y-%m-%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def snap_path(record_id):
    return os.path.join(SNAP_DIR, f"{record_id}.jpg")


def make_snapshot(record_id):
    with open(snap_path(record_id), "wb") as f:
        f.write(b"test snapshot")


def real_db_count():
    """Number of records in the real (non-test) database, or None if unreachable."""
    try:
        c = MongoClient(real_cfg.get("mongodb_uri", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2000)
        n = c[real_cfg.get("mongodb_db", "face_attendance")]["attendance_logs"].count_documents({})
        c.close()
        return n
    except Exception:
        return None


_real_count_before = real_db_count()
_snapshots_before = set(os.listdir(SNAP_DIR))

test_db = MongoAttendanceDB("mongodb://localhost:27017", "face_attendance_test")
server.mongo_db = test_db
engine = server.engine
engine._db = test_db
engine._cooldown_minutes = 60
engine._auto_attend = True
engine.set_names({PID: NAME})
engine.reset_cooldowns()
client = server.app.test_client()

if not test_db.is_connected:
    print("  [SKIP] MongoDB is not running on localhost:27017; nothing can be verified")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    sys.exit(1)

coll = test_db._collection
coll.delete_many({"name": NAME})
coll.delete_many({"date": TODAY})


def today_ids():
    return {r["id"] for r in client.get(f"/api/attendance?date={TODAY}").get_json()}


def detect_known():
    return engine.process_detection(PID, True, "Neutral", 40.0)


def detect_unknown():
    return engine.process_detection(PID, False, "Sad", 90.0)


# ──────────────────────────────────────────────────────────────────────────
section("1. Delete one record")

sio = server.socketio.test_client(server.app)
sio.get_received()  # drop the events sent on connect

rec1 = detect_known()
check("Setup: a detection creates a record", bool(rec1) and "id" in rec1, str(rec1))
make_snapshot(rec1["id"])
check("Setup: the cooldown blocks a second detection", detect_known() is None)

manual = engine.manual_mark(PID, NAME, "Present")
check("Setup: a manual mark creates a second record", bool(manual) and manual["id"] != rec1["id"])
make_snapshot(manual["id"])

res = client.delete(f"/api/attendance/{manual['id']}")
check("Deleting the manual record returns 200", res.status_code == 200, str(res.get_json()))
check("Response reports one deletion with the id",
      res.get_json() == {"ok": True, "deleted": 1, "id": manual["id"]}, str(res.get_json()))
check("The manual record is gone", manual["id"] not in today_ids())
check("Its snapshot file is gone", not os.path.exists(snap_path(manual["id"])))
check("The other record is still there", rec1["id"] in today_ids())
check("The other record's snapshot is still there", os.path.exists(snap_path(rec1["id"])))
check("Deleting a manual record leaves the cooldown in force", detect_known() is None)

events = [e for e in sio.get_received() if e["name"] == "attendance:deleted"]
check("attendance:deleted is emitted once", len(events) == 1, str(events))
check("The event carries the id and date",
      bool(events) and events[0]["args"][0] == {"ids": [manual["id"]], "date": TODAY}, str(events))

res = client.delete(f"/api/attendance/{rec1['id']}")
check("Deleting the detected record returns 200", res.status_code == 200, str(res.get_json()))
check("The detected record is gone", rec1["id"] not in today_ids())
check("Its snapshot file is gone", not os.path.exists(snap_path(rec1["id"])))

rec2 = detect_known()
check("The cooldown it started is cleared: the person can be recorded again",
      bool(rec2) and rec2["id"] != rec1["id"], str(rec2))

res = client.delete(f"/api/attendance/{rec1['id']}")
check("Deleting the same record twice returns 404", res.status_code == 404, str(res.status_code))
res = client.delete("/api/attendance/0123456789abcdef01234567")
check("Deleting an unknown id returns 404", res.status_code == 404, str(res.status_code))
res = client.delete("/api/attendance/abc-def")
check("Deleting a malformed id returns 400", res.status_code == 400, str(res.status_code))
check("Failed deletes removed nothing", rec2["id"] in today_ids())

# ──────────────────────────────────────────────────────────────────────────
section("2. An older record does not clear a newer cooldown")

# Pretend a later record for the same person replaced rec2's cooldown
engine._last_logged[PID] = datetime.now() + timedelta(minutes=5)
res = client.delete(f"/api/attendance/{rec2['id']}")
check("Deleting the older record returns 200", res.status_code == 200)
check("The newer cooldown is still in force", detect_known() is None)
engine.reset_cooldowns()

# ──────────────────────────────────────────────────────────────────────────
section("3. Unknown-face records")

unk = detect_unknown()
check("Setup: an unknown detection creates a record", bool(unk) and unk.get("status") == "Unknown", str(unk))
check("Setup: the shared unknown cooldown blocks a second one", detect_unknown() is None)
res = client.delete(f"/api/attendance/{unk['id']}")
check("Deleting the unknown record returns 200", res.status_code == 200)
unk2 = detect_unknown()
check("The unknown cooldown is cleared", bool(unk2), str(unk2))
client.delete(f"/api/attendance/{unk2['id']}")
check("Today is empty again", today_ids() == set(), str(today_ids()))

# ──────────────────────────────────────────────────────────────────────────
section("4. Clear today's records")


def insert(date_str, time_in, status="Present"):
    doc = {
        "person_id": PID, "name": NAME, "date": date_str, "time_in": time_in,
        "status": status, "emotion": "Neutral", "confidence": 40.0,
        "manual": False, "has_snapshot": True,
    }
    return str(coll.insert_one(doc).inserted_id)


t1 = insert(TODAY, "08:00:00")
t2 = insert(TODAY, "08:30:00", "Late")
t3 = insert(TODAY, "09:00:00", "Unknown")
y1 = insert(YESTERDAY, "09:00:00")
for rid in (t1, t2, y1):
    make_snapshot(rid)
live = detect_known()
check("Setup: four records today, one yesterday", today_ids() == {t1, t2, t3, live["id"]}, str(today_ids()))
check("Setup: a cooldown is in force", detect_known() is None)
sio.get_received()

for label, url in (
    ("without a date", "/api/attendance"),
    ("with yesterday's date", f"/api/attendance?date={YESTERDAY}"),
    ("with a malformed date", "/api/attendance?date=today"),
    ("with an empty date", "/api/attendance?date="),
):
    res = client.delete(url)
    check(f"Clearing {label} is refused with 400", res.status_code == 400, str(res.status_code))
check("Refused requests deleted nothing", len(today_ids()) == 4, str(len(today_ids())))
check("Refused requests left yesterday alone",
      y1 in {r["id"] for r in client.get(f"/api/attendance?date={YESTERDAY}").get_json()})
check("Refused requests emitted no event",
      not [e for e in sio.get_received() if e["name"] == "attendance:deleted"])

res = client.delete(f"/api/attendance?date={TODAY}")
check("Clearing today returns 200", res.status_code == 200, str(res.get_json()))
check("Response reports the exact count",
      res.get_json() == {"ok": True, "deleted": 4, "date": TODAY}, str(res.get_json()))
check("Today has no records", today_ids() == set(), str(today_ids()))
check("/api/stats/today total is 0", client.get("/api/stats/today").get_json().get("total") == 0)
check("Today's snapshots are gone", not os.path.exists(snap_path(t1)) and not os.path.exists(snap_path(t2)))
check("Yesterday's record is still there",
      y1 in {r["id"] for r in client.get(f"/api/attendance?date={YESTERDAY}").get_json()})
check("Yesterday's snapshot is still there", os.path.exists(snap_path(y1)))

events = [e for e in sio.get_received() if e["name"] == "attendance:deleted"]
check("attendance:deleted is emitted once", len(events) == 1, str(len(events)))
check("The event lists exactly the four deleted ids",
      bool(events) and set(events[0]["args"][0]["ids"]) == {t1, t2, t3, live["id"]}
      and events[0]["args"][0]["date"] == TODAY, str(events))

after = detect_known()
check("All cooldowns are reset: the person can be recorded straight away", bool(after), str(after))

res = client.delete(f"/api/attendance?date={TODAY}")
check("Clearing again removes the one new record", res.get_json().get("deleted") == 1, str(res.get_json()))
res = client.delete(f"/api/attendance?date={TODAY}")
check("Clearing an empty day returns 200 with deleted 0",
      res.status_code == 200 and res.get_json().get("deleted") == 0, str(res.get_json()))

# Registered people and face images are not part of this route
check("Config names are untouched by clearing", server.load_config().get("names") == cfg_copy.get("names"))

# Cleanup of what this test put outside today
coll.delete_many({"name": NAME})
if os.path.exists(snap_path(y1)):
    os.remove(snap_path(y1))

# ──────────────────────────────────────────────────────────────────────────
section("5. Database offline")

offline_db = MongoAttendanceDB("mongodb://localhost:29999", "face_attendance_test")
server.mongo_db = offline_db
engine._db = offline_db

res = client.delete("/api/attendance/0123456789abcdef01234567")
check("Single delete returns 503 when offline", res.status_code == 503, str(res.status_code))
check("Single delete says nothing was deleted", "Nothing was deleted" in res.get_json().get("error", ""))
res = client.delete(f"/api/attendance?date={TODAY}")
check("Clear today returns 503 when offline", res.status_code == 503, str(res.status_code))

status = client.get("/api/status").get_json()
check("/api/status reports firebase_enabled False", status.get("firebase_enabled") is False, str(status))

server.mongo_db = test_db
engine._db = test_db

# ──────────────────────────────────────────────────────────────────────────
section("6. Nothing real was touched")

check("Snapshot folder holds exactly the files it had before",
      set(os.listdir(SNAP_DIR)) == _snapshots_before,
      str(set(os.listdir(SNAP_DIR)) ^ _snapshots_before))
if _real_count_before is not None:
    check("Real database record count unchanged", real_db_count() == _real_count_before,
          f"{_real_count_before} -> {real_db_count()}")
else:
    print("  [SKIP] Real database not reachable for a count")
check("config.json hash unchanged", get_real_cfg_hash() == _cfg_hash_before)

sio.disconnect()
offline_db.close()
test_db.close()
shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
