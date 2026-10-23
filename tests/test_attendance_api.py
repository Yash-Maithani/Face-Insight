"""
tests/test_attendance_api.py
Attendance and stats routes.
Uses Flask test_client and a separate test database.
"""

import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

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

from openpyxl import load_workbook
import server
from db.mongo_client import MongoAttendanceDB
from attendance.engine import AttendanceEngine
import cv2
import numpy as np

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


# Switch DB to face_attendance_test
test_db = MongoAttendanceDB("mongodb://localhost:27017", "face_attendance_test")
server.mongo_db = test_db
server.engine._db = test_db

if test_db.is_connected:
    test_db._collection.delete_many({})

from helpers import signed_in_client  # noqa: E402
client = signed_in_client(server)

# ── 1. GET /api/attendance ──────────────────────────────────────────
section("1. GET /api/attendance")

# Invalid date
res = client.get("/api/attendance?date=invalid-date")
check("Invalid date returns 400", res.status_code == 400)

# Valid date (empty)
today = datetime.now().strftime("%Y-%m-%d")
res = client.get(f"/api/attendance?date={today}")
check("Valid date returns 200", res.status_code == 200)
check("Returns list", isinstance(res.get_json(), list))

# Default date (omitted)
res_def = client.get("/api/attendance")
check("Default date returns 200", res_def.status_code == 200)

# ── 2. POST /api/attendance/manual ──────────────────────────────────
section("2. POST /api/attendance/manual")

# Invalid status
res = client.post("/api/attendance/manual", json={"person_id": 1, "name": "Alice", "status": "Sick"})
check("Invalid status returns 400", res.status_code == 400)

# Missing name
res = client.post("/api/attendance/manual", json={"person_id": 1, "status": "Present"})
check("Missing name returns 400", res.status_code == 400)

# Empty name
res = client.post("/api/attendance/manual", json={"person_id": 1, "name": "   ", "status": "Present"})
check("Empty name returns 400", res.status_code == 400)

# Non-integer person_id (string)
res = client.post("/api/attendance/manual", json={"person_id": "abc", "name": "Alice", "status": "Present"})
check("Non-int person_id (str) returns 400", res.status_code == 400)

# Non-integer person_id (bool)
res = client.post("/api/attendance/manual", json={"person_id": True, "name": "Alice", "status": "Present"})
check("Non-int person_id (bool) returns 400", res.status_code == 400)

# Manual marks are for registered people only
from helpers import register_people  # noqa: E402
register_people(server, {101: "Alice Cooper", 102: "Bob Marley"})
res = client.post("/api/attendance/manual", json={"person_id": 999, "name": "Nobody", "status": "Present"})
check("Unregistered ID returns 404", res.status_code == 404)
res = client.post("/api/attendance/manual", json={"person_id": 101, "name": "Bob Marley", "status": "Present"})
check("Name that does not match the ID returns 400", res.status_code == 400)
res = client.post("/api/attendance/manual", json={"person_id": 101, "name": "Alice Cooper", "status": "Unknown"})
check("Manual Unknown returns 400", res.status_code == 400)

# Valid manual mark
res = client.post("/api/attendance/manual", json={"person_id": 101, "name": "Alice Cooper", "status": "Present"})
check("Valid manual mark returns 200", res.status_code == 200)
data = res.get_json()
check("Response ok is True", data.get("ok") is True)
rec = data.get("record", {})
check("Record has id", bool(rec.get("id")))
check("Record manual is True", rec.get("manual") is True)
check("Record has_snapshot is False", rec.get("has_snapshot") is False)
check("Record status is Present", rec.get("status") == "Present")
check("Record created_at is ISO string", isinstance(rec.get("created_at"), str))

# Second manual mark: Late
res2 = client.post("/api/attendance/manual", json={"person_id": 102, "name": "Bob Marley", "status": "Late"})
check("Second manual mark returns 200", res2.status_code == 200)

# Third record: an Unknown face seen by the camera (manual marks cannot be Unknown)
rec3 = server.engine.process_detection(-1, False, "", 0.0)
check("Unknown face recorded", rec3 is not None)
check("Unknown record has no person_id", rec3 is not None and rec3.get("person_id") is None)

# Now check /api/attendance has 3 records
res_list = client.get("/api/attendance")
logs = res_list.get_json()
check("List now has 3 records", len(logs) == 3, f"got {len(logs)}")

# Check get_today_stats with mixed records (Alice Present, Bob Late, Unknown Person Unknown)
today_stats = server.engine.get_today_stats()
check("get_today_stats present is 1", today_stats["present"] == 1)
check("get_today_stats late is 1", today_stats["late"] == 1)
check("get_today_stats total is 3 (all records including unknown)", today_stats["total"] == 3)
check("get_today_stats present + late is 2 (excludes unknown)", today_stats["present"] + today_stats["late"] == 2)

# Check emotion stats when today has only manual records
res_manual_e = client.get("/api/stats/emotions")
check("Manual-only records produce empty emotion stats", res_manual_e.get_json() == {})

# ── 3. Snapshot support & detection ──────────────────────────────────
section("3. Snapshot support")

# Invalid snapshot ID (path traversal attempt)
res = client.get("/api/attendance/../secret/snapshot")
check("Path traversal ID returns 400 or 404", res.status_code in (400, 404))

res = client.get("/api/attendance/abc!@$/snapshot")
check("Special chars in ID returns 400", res.status_code == 400)

# Non-existent snapshot
res = client.get("/api/attendance/1234567890abcdef/snapshot")
check("Non-existent snapshot returns 404", res.status_code == 404)

# Test real detection with face_crop
fake_crop = np.zeros((100, 100), dtype=np.uint8)
det_rec = server.engine.process_detection(103, True, "Happy", 45.2, face_crop=fake_crop)
check("process_detection with face_crop succeeds", det_rec is not None)
check("det_rec has_snapshot is True", det_rec.get("has_snapshot") is True)
snap_id = det_rec.get("id")
check("det_rec has id", bool(snap_id))

# Fetch the snapshot via API
res_snap = client.get(f"/api/attendance/{snap_id}/snapshot")
check("Snapshot GET returns 200", res_snap.status_code == 200)
check("Content-Type is image/jpeg", res_snap.headers.get("Content-Type") == "image/jpeg")
res_snap.close()

# Clean up snapshot file
snap_file = os.path.join(PROJECT_ROOT, "snapshots", f"{snap_id}.jpg")
if os.path.exists(snap_file):
    os.remove(snap_file)

# ── 4. Exports (Excel & CSV) ─────────────────────────────────────────
section("4. Export Excel and CSV")

# CSV export
res_csv = client.get(f"/api/export/csv?date={today}")
check("CSV export returns 200", res_csv.status_code == 200)
check("CSV Content-Type is text/csv", "text/csv" in res_csv.headers.get("Content-Type", ""))
check("Content-Disposition has filename", f"attendance_{today}.csv" in res_csv.headers.get("Content-Disposition", ""))

csv_content = res_csv.data.decode("utf-8")
csv_reader = list(csv.reader(io.StringIO(csv_content)))
check("CSV has header + 4 data rows", len(csv_reader) == 5, f"got {len(csv_reader)}")
expected_cols = ["Name", "ID", "Time In", "Status", "Emotion", "Confidence", "Manual"]
check("CSV header columns match expected", csv_reader[0] == expected_cols, str(csv_reader[0]))

# Excel export
res_xlsx = client.get(f"/api/export/excel?date={today}")
check("Excel export returns 200", res_xlsx.status_code == 200)
check("Excel Content-Disposition has filename", f"attendance_{today}.xlsx" in res_xlsx.headers.get("Content-Disposition", ""))

wb = load_workbook(io.BytesIO(res_xlsx.data))
ws = wb.active
xlsx_header = [cell.value for cell in ws[1]]
check("Excel header columns match expected", xlsx_header == expected_cols, str(xlsx_header))
check("Excel has 5 rows (header + 4 data)", ws.max_row == 5, f"got {ws.max_row}")

# ── 5. Stats (weekly, emotions, persons) ──────────────────────────────
section("5. Stats APIs")

res_w = client.get("/api/stats/weekly")
check("Weekly stats returns 200", res_w.status_code == 200)
w_data = res_w.get_json()
check("Weekly stats returns 7 entries", len(w_data) == 7, f"got {len(w_data)}")

res_e = client.get("/api/stats/emotions")
check("Emotions stats returns 200", res_e.status_code == 200)
e_data = res_e.get_json()
check("Emotions stats returns dict", isinstance(e_data, dict))
check("Emotions stats contains Happy", "Happy" in e_data)

res_p = client.get("/api/stats/persons")
check("Persons stats returns 200", res_p.status_code == 200)
p_data = res_p.get_json()
check("Persons stats returns list", isinstance(p_data, list))
check("Persons stats has entries", len(p_data) > 0)
check("First person entry has person_id, name, count",
      all(k in p_data[0] for k in ("person_id", "name", "count")))

# ── 6. Disconnected DB Resilience ────────────────────────────────────
section("6. Disconnected DB Resilience")

# Create a disconnected DB instance
offline_db = MongoAttendanceDB("mongodb://localhost:29999", "fake_db")
server.mongo_db = offline_db

res_off_att = client.get("/api/attendance")
check("Disconnected Mongo /api/attendance returns 200 (not 500)", res_off_att.status_code == 200)
check("Disconnected Mongo returns empty list", res_off_att.get_json() == [])

res_off_w = client.get("/api/stats/weekly")
check("Disconnected Mongo /api/stats/weekly returns 200 (not 500)", res_off_w.status_code == 200)
check("Weekly summary returns 7 zero-filled entries", len(res_off_w.get_json()) == 7)

res_off_e = client.get("/api/stats/emotions")
check("Disconnected Mongo /api/stats/emotions returns 200", res_off_e.status_code == 200)
check("Emotions returns empty dict", res_off_e.get_json() == {})

res_off_p = client.get("/api/stats/persons")
check("Disconnected Mongo /api/stats/persons returns 200", res_off_p.status_code == 200)
check("Persons returns empty list", res_off_p.get_json() == [])

# Restore test_db and drop it
if test_db.is_connected:
    test_db._client.drop_database("face_attendance_test")

shutil.rmtree(tmp_dir, ignore_errors=True)

# ── 7. Real config.json integrity ────────────────────────────────────
section("7. Real config.json integrity")
_cfg_hash_after = get_real_cfg_hash()
check("Real config.json unchanged during test run", _cfg_hash_before == _cfg_hash_after)

# ── Summary ──────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
