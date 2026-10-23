"""
tests/test_migration.py
The version 2 migration: people from config.json into the database, and
Unknown records without a guessed person ID. Backed up first, safe to run
twice, and restorable.

Uses the test database (face_attendance_test) and a temporary config file.
Never touches the real config.json.

Run: python tests/test_migration.py
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from bson import json_util  # noqa: E402

from core.model_loader import DEFAULT_CONFIG, load_config, save_config  # noqa: E402
from db.mongo_client import MongoAttendanceDB  # noqa: E402
from people.migration import MARKER, migrate, pending, restore  # noqa: E402
from people.registry import PersonRegistry  # noqa: E402

TEST_DB = "face_attendance_test"
GOOD_URI = "mongodb://localhost:27017"
BAD_URI = "mongodb://localhost:29999"

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


def file_hash(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


REAL_CONFIG = os.path.join(PROJECT_ROOT, "config.json")
_cfg_hash_before = file_hash(REAL_CONFIG)

tmp_dir = tempfile.mkdtemp(prefix="face_web_migration_")
cfg_path = os.path.join(tmp_dir, "config.json")
backup_root = os.path.join(tmp_dir, "instance", "backups")
cfg = dict(DEFAULT_CONFIG)
cfg["mongodb_uri"] = GOOD_URI
cfg["mongodb_db"] = TEST_DB
cfg["names"] = {"1": "Asha Verma", "2": "Rohan Mehta", "3": "Neha Kapoor"}
save_config(cfg, cfg_path)

db = MongoAttendanceDB(GOOD_URI, TEST_DB)
if not db.is_connected:
    print("  [SKIP] MongoDB is not running on localhost:27017")
    print(f"\n{'=' * 60}\n  RESULTS: 0 passed, 0 failed, 0 total\n{'=' * 60}")
    sys.exit(0)

database = db.database()
for name in ("attendance_logs", "persons", "app_meta"):
    database[name].delete_many({})

names_seen = []
registry = PersonRegistry(lambda: db, lambda: load_config(cfg_path),
                          lambda current: save_config(current, cfg_path),
                          on_change=lambda names: names_seen.append(dict(names)))

# Rohan is already in the database under a newer name; the config copy is older
db.insert_person({"person_id": 2, "name": "Rohan M.", "consent_at": "2026-10-01T09:00:00", "created": "2026-10-01T09:00:00"})
database["attendance_logs"].insert_many([
    {"person_id": 1, "name": "Asha Verma", "date": "2026-10-20", "time_in": "09:00:00", "status": "Present"},
    {"person_id": 7, "name": "Unknown", "date": "2026-10-20", "time_in": "09:05:00", "status": "Unknown"},
    {"person_id": -1, "name": "Unknown", "date": "2026-10-21", "time_in": "10:00:00", "status": "Unknown"},
    {"person_id": None, "name": "Unknown", "date": "2026-10-22", "time_in": "11:00:00", "status": "Unknown"},
])
logs_before = list(database["attendance_logs"].find({}))

# ──────────────────────────────────────────────────────────────────────────
section("1. What needs doing")

todo = pending(db, registry)
check("People 1 and 3 are only in the config", todo["people"] == [1, 3], str(todo))
check("Two Unknown records still carry an ID", todo["unknown_records"] == 2, str(todo))

# ──────────────────────────────────────────────────────────────────────────
section("2. First run")

clock = lambda: datetime(2026, 10, 23, 8, 0, 0)  # noqa: E731
report = migrate(db, registry, backup_root, clock)
check("It ran", report["ran"] is True, str(report))
check("Two people imported", report["people_imported"] == 2)
check("Two Unknown records fixed", report["unknown_records_fixed"] == 2)

folder = report["backup"]
check("Backup folder exists", os.path.isdir(folder))
with open(os.path.join(folder, "attendance_logs.json"), encoding="utf-8") as f:
    backed_logs = json_util.loads(f.read())
with open(os.path.join(folder, "persons.json"), encoding="utf-8") as f:
    backed_persons = json_util.loads(f.read())
check("Backup holds the records exactly as they were before", backed_logs == logs_before,
      str([d.get("person_id") for d in backed_logs]))
check("Backup holds the people as they were before",
      [d["person_id"] for d in backed_persons] == [2],
      str(backed_persons))

persons = db.list_persons()
check("Three people in the database, no duplicates", [p["person_id"] for p in persons] == [1, 2, 3])
check("The database name wins over the older config copy",
      next(p for p in persons if p["person_id"] == 2)["name"] == "Rohan M.")
check("Imported people have no consent recorded",
      all(p.get("consent_at") is None for p in persons if p["person_id"] != 2))
check("Existing consent is kept",
      next(p for p in persons if p["person_id"] == 2)["consent_at"] == "2026-10-01T09:00:00")

unknown = list(database["attendance_logs"].find({"status": "Unknown"}).sort("time_in", 1))
check("Unknown records have no person_id", all(r["person_id"] is None for r in unknown))
check("The guess is kept apart", [r.get("guessed_person_id") for r in unknown] == [7, -1, None],
      str([r.get("guessed_person_id") for r in unknown]))
known = database["attendance_logs"].find_one({"status": "Present"})
check("Known records are untouched", known["person_id"] == 1 and "guessed_person_id" not in known)
check("No record was lost", database["attendance_logs"].count_documents({}) == 4)

with open(cfg_path, encoding="utf-8") as f:
    cached = json.load(f)["names"]
check("Config copy now matches the database", cached == {"1": "Asha Verma", "2": "Rohan M.", "3": "Neha Kapoor"}, str(cached))
check("The recogniser was told the new names", names_seen and names_seen[-1] == cached)
check("Marker stored", (db.get_meta(MARKER) or {}).get("backup") == folder)

# ──────────────────────────────────────────────────────────────────────────
section("3. Second run changes nothing")

backups_before = sorted(os.listdir(backup_root))
report2 = migrate(db, registry, backup_root, clock)
check("Second run reports nothing to do", report2 == {"ran": False, "reason": "nothing to do"}, str(report2))
check("No new backup", sorted(os.listdir(backup_root)) == backups_before)
check("Still three people", len(db.list_persons()) == 3)
check("Still four records", database["attendance_logs"].count_documents({}) == 4)

# ──────────────────────────────────────────────────────────────────────────
section("4. Command line")

script = os.path.join(PROJECT_ROOT, "tools", "migrate_v2.py")
env = dict(os.environ, FACE_WEB_CONFIG=cfg_path, PYTHONIOENCODING="utf-8")
out = subprocess.run([sys.executable, script, "--dry-run"], capture_output=True, text=True, env=env, timeout=120)
check("Dry run exits 0", out.returncode == 0, out.stderr[-300:])
check("Dry run reports nothing pending", "People to move into the database: 0" in out.stdout
      and "guessed ID: 0" in out.stdout, out.stdout)

# A name added to config.json by hand is picked up by the next run
cfg_now = load_config(cfg_path)
cfg_now["names"]["4"] = "Arjun Nair"
save_config(cfg_now, cfg_path)
out = subprocess.run([sys.executable, script, "--dry-run"], capture_output=True, text=True, env=env, timeout=120)
check("Dry run sees the hand-added name", "People to move into the database: 1" in out.stdout, out.stdout)
check("Dry run changed nothing", len(db.list_persons()) == 3)
out = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env, timeout=120)
check("Real run imports it", out.returncode == 0 and "People imported: 1" in out.stdout, out.stdout + out.stderr[-300:])
check("Four people now", [p["person_id"] for p in db.list_persons()] == [1, 2, 3, 4])

# ──────────────────────────────────────────────────────────────────────────
section("5. Restore")

restore(database, folder)
check("Restore brings back the old people", [p["person_id"] for p in db.list_persons()] == [2])
check("Restore brings back the old guessed IDs",
      sorted(str(r["person_id"]) for r in database["attendance_logs"].find({"status": "Unknown"})) == ["-1", "7", "None"])
check("Restore keeps the record count", database["attendance_logs"].count_documents({}) == 4)

# ──────────────────────────────────────────────────────────────────────────
section("6. Database offline")

offline = MongoAttendanceDB(BAD_URI, TEST_DB)
off_registry = PersonRegistry(lambda: offline, lambda: load_config(cfg_path), lambda c: save_config(c, cfg_path))
report = migrate(offline, off_registry, backup_root)
check("Offline: does not run", report == {"ran": False, "reason": "database offline"}, str(report))
check("Offline: pending is None", pending(offline, off_registry) is None)
offline.close()

# ──────────────────────────────────────────────────────────────────────────
section("7. Cleanup")

for name in ("attendance_logs", "persons", "app_meta"):
    database[name].delete_many({})
db.close()
shutil.rmtree(tmp_dir, ignore_errors=True)
check("config.json hash unchanged", file_hash(REAL_CONFIG) == _cfg_hash_before)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
