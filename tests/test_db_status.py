"""
tests/test_db_status.py
Database status reporting and reconnect.
Uses Flask test_client, an isolated temp config and the test database.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile

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


GOOD_URI = "mongodb://localhost:27017"
BAD_URI = "mongodb://localhost:29999"

test_db = MongoAttendanceDB(GOOD_URI, "face_attendance_test")
server.mongo_db = test_db
server.engine._db = test_db
from helpers import signed_in_client  # noqa: E402
client = signed_in_client(server)
mongo_available = test_db.is_connected

# ──────────────────────────────────────────────────────────────────────────
section("1. Status is reported while the database is reachable")

if mongo_available:
    res = client.get("/api/status")
    check("/api/status returns 200", res.status_code == 200)
    check("/api/status db_connected is True", res.get_json().get("db_connected") is True, str(res.get_json()))

    res = client.get("/api/stats/today")
    data = res.get_json()
    check("/api/stats/today returns 200", res.status_code == 200)
    check("/api/stats/today db_connected is True", data.get("db_connected") is True, str(data))
    check("/api/stats/today still has total/present/late",
          all(k in data for k in ("total", "present", "late")), str(data))
    check("/api/stats/today still has fps/faces/known",
          all(k in data for k in ("fps", "faces", "known")), str(data))
else:
    print("  [SKIP] MongoDB is not running on localhost:27017")

# ──────────────────────────────────────────────────────────────────────────
section("2. Status is reported while the database is unreachable")

offline_db = MongoAttendanceDB(BAD_URI, "face_attendance_test")
server.mongo_db = offline_db
server.engine._db = offline_db

res = client.get("/api/status")
check("/api/status returns 200 when offline", res.status_code == 200)
check("/api/status db_connected is False", res.get_json().get("db_connected") is False, str(res.get_json()))

res = client.get("/api/stats/today")
data = res.get_json()
check("/api/stats/today returns 200 when offline", res.status_code == 200)
check("/api/stats/today db_connected is False", data.get("db_connected") is False, str(data))
check("/api/stats/today counts are zero when offline",
      (data.get("total"), data.get("present"), data.get("late")) == (0, 0, 0), str(data))

# ──────────────────────────────────────────────────────────────────────────
section("3. Reconnect endpoint")

res = client.post("/api/db/reconnect")
check("Reconnect to unreachable DB returns 200 (not 500)", res.status_code == 200)
check("Reconnect to unreachable DB reports ok False",
      res.get_json() == {"ok": False, "db_connected": False}, str(res.get_json()))

res = client.get("/api/db/reconnect")
check("GET /api/db/reconnect is not allowed", res.status_code == 405, str(res.status_code))

if mongo_available:
    # Same object, reachable address again: reconnect must recover it
    offline_db._uri = GOOD_URI
    res = client.post("/api/db/reconnect")
    check("Reconnect to reachable DB returns 200", res.status_code == 200)
    check("Reconnect to reachable DB reports ok True",
          res.get_json() == {"ok": True, "db_connected": True}, str(res.get_json()))
    check("/api/status follows the reconnect",
          client.get("/api/status").get_json().get("db_connected") is True)
else:
    print("  [SKIP] MongoDB is not running on localhost:27017")

# ──────────────────────────────────────────────────────────────────────────
section("4. Real config.json untouched")

check("config.json hash unchanged", get_real_cfg_hash() == _cfg_hash_before)

offline_db.close()
test_db.close()
shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
