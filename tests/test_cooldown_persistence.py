"""
tests/test_cooldown_persistence.py
Cooldowns survive a restart: they are rebuilt from today's records, so the
same person is not logged twice inside their cooldown. Different unknown
faces get their own cooldowns.

Uses the test database (face_attendance_test) directly, with a fixed clock.

Run: python tests/test_cooldown_persistence.py
"""

import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from attendance.engine import AttendanceEngine  # noqa: E402
from db.mongo_client import MongoAttendanceDB  # noqa: E402

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


class FakeFirebase:
    def push_attendance(self, _record):
        pass


db = MongoAttendanceDB("mongodb://localhost:27017", "face_attendance_test")
if not db.is_connected:
    print("  [SKIP] MongoDB is not running on localhost:27017")
    print(f"\n{'=' * 60}\n  RESULTS: 0 passed, 0 failed, 0 total\n{'=' * 60}")
    sys.exit(0)
db.database()["attendance_logs"].delete_many({})

CFG = {"names": {1: "Asha", 2: "Rohan"},
       "attendance": {"cooldown_minutes": 60, "late_after": "09:30", "working_hours_end": "17:30",
                      "auto_attend": True, "review_margin": 0}}
clock = [datetime(2026, 10, 19, 9, 0, 0)]  # a Monday


def new_engine():
    """What the server does on start: a fresh engine, cooldowns rebuilt from the records."""
    e = AttendanceEngine(CFG, db, FakeFirebase(), clock=lambda: clock[0])
    e.set_threshold(60)
    return e


# ──────────────────────────────────────────────────────────────────────────
section("1. A restart inside the cooldown")

engine = new_engine()
check("Asha logged at 09:00", engine.process_detection(1, True, "", 30.0) is not None)
clock[0] = datetime(2026, 10, 19, 9, 20)
check("Seen again at 09:20: blocked", engine.process_detection(1, True, "", 30.0) is None)

engine = new_engine()  # the server restarts
check("Fresh engine knows nothing before restoring", engine.process_detection(2, True, "", 30.0) is not None)
engine = new_engine()
restored = engine.restore_cooldowns()
check("Restored both of today's cooldowns", restored == 2, str(restored))
clock[0] = datetime(2026, 10, 19, 9, 30)
check("After the restart, Asha at 09:30 is still blocked", engine.process_detection(1, True, "", 30.0) is None)
clock[0] = datetime(2026, 10, 19, 10, 1)
check("Once the hour is up she is logged again", engine.process_detection(1, True, "", 30.0) is not None)
check("Only two records for Asha today", db.database()["attendance_logs"].count_documents({"person_id": 1}) == 2)

# ──────────────────────────────────────────────────────────────────────────
section("2. What does not start a cooldown")

db.insert_log({"person_id": 2, "name": "Rohan", "date": "2026-10-19", "time_in": "10:05:00",
               "status": "Present", "manual": True})
db.insert_log({"person_id": 1, "name": "Asha", "date": "2026-10-18", "time_in": "23:59:00",
               "status": "Present", "manual": False})
db.insert_log({"person_id": 2, "name": "Rohan", "date": "2026-10-19", "time_in": "23:00:00",
               "status": "Present", "manual": False})
clock[0] = datetime(2026, 10, 19, 10, 10)
engine = new_engine()
engine.restore_cooldowns()
check("A manual mark does not block the camera", engine._last_logged.get(2) == datetime(2026, 10, 19, 9, 20),
      str(engine._last_logged.get(2)))
check("A record dated later today than the clock is ignored", engine._last_logged[2] < clock[0])
check("Yesterday's records are ignored", engine._last_logged[1].date() == clock[0].date())

# ──────────────────────────────────────────────────────────────────────────
section("3. Unknown faces")

db.database()["attendance_logs"].delete_many({})
clock[0] = datetime(2026, 10, 19, 11, 0)
engine = new_engine()
check("Stranger closest to person 1: logged", engine.process_detection(1, False, "", 90.0) is not None)
check("Stranger closest to person 2: logged too (own cooldown)", engine.process_detection(2, False, "", 90.0) is not None)
check("The first stranger again: blocked", engine.process_detection(1, False, "", 90.0) is None)
check("No model (-1): logged once", engine.process_detection(-1, False, "", 0.0) is not None)
check("No model again (-2 means the same, no guess): blocked", engine.process_detection(-2, False, "", 0.0) is None)

engine = new_engine()
engine.restore_cooldowns()
check("Unknown cooldowns come back after a restart", engine.process_detection(2, False, "", 90.0) is None
      and engine.process_detection(-1, False, "", 0.0) is None)
check("A stranger closest to someone new is still logged", engine.process_detection(3, False, "", 90.0) is not None)

rec = db.find_logs("2026-10-19", "2026-10-19")
first = next(r for r in rec if r.get("guessed_person_id") == 1)
engine.forget_record(first)
check("Deleting a stranger's record frees only that stranger", engine.process_detection(1, False, "", 90.0) is not None
      and engine.process_detection(2, False, "", 90.0) is None)

# ──────────────────────────────────────────────────────────────────────────
section("4. Cleanup")

db.database()["attendance_logs"].delete_many({})
db.close()
check("Done", True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
