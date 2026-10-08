"""
tests/test_dashboard_period.py
Dashboard period (?days=), per-person
distinct-day counts, and acknowledgements for the live settings events.
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
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)

cfg_copy["mongodb_db"] = "face_attendance_test"
cfg_copy["confidence_threshold"] = 60
cfg_copy.setdefault("attendance", {})["auto_attend"] = True
with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)

os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

import server
from core.model_loader import load_config
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


test_db = MongoAttendanceDB("mongodb://localhost:27017", "face_attendance_test")
server.mongo_db = test_db
server.engine._db = test_db
client = server.app.test_client()
mongo_available = test_db.is_connected

TEST_NAME = "P7 Range Tester"
TEST_ID = 9707

# ──────────────────────────────────────────────────────────────────────────
section("1. ?days= on /api/stats/weekly")

res = client.get("/api/stats/weekly")
check("No days param returns 200", res.status_code == 200)
check("No days param returns 7 entries", len(res.get_json()) == 7, str(len(res.get_json())))

for n in (7, 14, 30):
    res = client.get(f"/api/stats/weekly?days={n}")
    data = res.get_json()
    check(f"days={n} returns 200", res.status_code == 200)
    check(f"days={n} returns {n} entries", len(data) == n, str(len(data)))

res = client.get("/api/stats/weekly?days=30")
data = res.get_json()
today = datetime.now().date()
check("Entries are oldest first and end today",
      data[0]["date"] == (today - timedelta(days=29)).isoformat() and data[-1]["date"] == today.isoformat(),
      f"{data[0]['date']} . {data[-1]['date']}")
check("Every entry has the three counts",
      all(set(d) == {"date", "present_count", "late_count", "unknown_count"} for d in data))

for bad in ("0", "8", "365", "abc", "-7", "7.5", ""):
    res = client.get(f"/api/stats/weekly?days={bad}")
    check(f"days={bad!r} on weekly is rejected with 400", res.status_code == 400, str(res.status_code))

# ──────────────────────────────────────────────────────────────────────────
section("2. ?days= and distinct days on /api/stats/persons")

for bad in ("0", "abc", "15"):
    res = client.get(f"/api/stats/persons?days={bad}")
    check(f"days={bad!r} on persons is rejected with 400", res.status_code == 400, str(res.status_code))

if mongo_available:
    coll = test_db._collection
    coll.delete_many({"name": TEST_NAME})

    def add(days_ago, time_in):
        d = (today - timedelta(days=days_ago)).isoformat()
        coll.insert_one({
            "person_id": TEST_ID, "name": TEST_NAME, "date": d, "time_in": time_in,
            "status": "Present", "emotion": "Neutral", "confidence": 40.0,
            "manual": False, "has_snapshot": False,
        })

    # 3 records over 2 distinct days inside 7 days, 1 more on a day only inside 14
    add(0, "09:00:00")
    add(0, "13:00:00")
    add(2, "09:05:00")
    add(10, "09:10:00")

    def mine(days=None):
        url = "/api/stats/persons" + (f"?days={days}" if days else "")
        rows = [p for p in client.get(url).get_json() if p.get("name") == TEST_NAME]
        return rows[0] if rows else None

    row = mine()
    check("Person appears with default period", row is not None)
    if row:
        check("Default period: count is records (3)", row.get("count") == 3, str(row))
        check("Default period: days is distinct dates (2)", row.get("days") == 2, str(row))
        check("Row keeps person_id and name", row.get("person_id") == TEST_ID and row.get("name") == TEST_NAME)

    row = mine(14)
    check("14 days: count is 4", bool(row) and row.get("count") == 4, str(row))
    check("14 days: days is 3", bool(row) and row.get("days") == 3, str(row))

    row = mine(30)
    check("30 days: days is 3", bool(row) and row.get("days") == 3, str(row))

    weekly = client.get("/api/stats/weekly?days=14").get_json()
    by_date = {d["date"]: d for d in weekly}
    ten_ago = (today - timedelta(days=10)).isoformat()
    check("14-day trend includes the record from 10 days ago",
          by_date.get(ten_ago, {}).get("present_count", 0) >= 1, str(by_date.get(ten_ago)))
    seven = {d["date"] for d in client.get("/api/stats/weekly?days=7").get_json()}
    check("7-day trend does not reach back 10 days", ten_ago not in seven)

    coll.delete_many({"name": TEST_NAME})
    check("Test records removed", coll.count_documents({"name": TEST_NAME}) == 0)
else:
    print("  [SKIP] MongoDB is not running on localhost:27017")

# ──────────────────────────────────────────────────────────────────────────
section("3. Live settings events acknowledge the save")

sio = server.socketio.test_client(server.app)

ack = sio.emit("settings:threshold", {"value": 72}, callback=True)
check("Valid threshold is acknowledged ok", isinstance(ack, dict) and ack.get("ok") is True, str(ack))
check("Ack echoes the value", isinstance(ack, dict) and ack.get("value") == 72, str(ack))
check("Threshold persisted to the temp config", load_config().get("confidence_threshold") == 72)

for bad in ("abc", None, True, 0, 9999, -5):
    ack = sio.emit("settings:threshold", {"value": bad}, callback=True)
    check(f"Threshold {bad!r} is refused", isinstance(ack, dict) and ack.get("ok") is False, str(ack))
check("Refused values did not change the config", load_config().get("confidence_threshold") == 72)

ack = sio.emit("settings:threshold", {}, callback=True)
check("Missing threshold value is refused", isinstance(ack, dict) and ack.get("ok") is False, str(ack))

ack = sio.emit("settings:auto_attend", {"enabled": False}, callback=True)
check("auto_attend False is acknowledged ok", isinstance(ack, dict) and ack.get("ok") is True, str(ack))
check("auto_attend persisted False", load_config()["attendance"]["auto_attend"] is False)

ack = sio.emit("settings:auto_attend", {"enabled": "yes"}, callback=True)
check("auto_attend with a non-boolean is refused", isinstance(ack, dict) and ack.get("ok") is False, str(ack))
check("Refused value did not change the config", load_config()["attendance"]["auto_attend"] is False)

ack = sio.emit("settings:auto_attend", {"enabled": True}, callback=True)
check("auto_attend True is acknowledged ok", isinstance(ack, dict) and ack.get("ok") is True, str(ack))
check("auto_attend persisted True", load_config()["attendance"]["auto_attend"] is True)

sio.disconnect()

# ──────────────────────────────────────────────────────────────────────────
section("4. Real config.json untouched")

check("config.json hash unchanged", get_real_cfg_hash() == _cfg_hash_before)

test_db.close()
shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
