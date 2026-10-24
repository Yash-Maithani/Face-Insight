"""
tests/test_schedules.py
Groups and schedules: Present, Late, Left early and Absent judged against
each person's group, with a fixed test clock, including non-working days.
Also the schedules API, moving people between groups, and the absence list.

Uses an isolated temp config and the test database. Never touches the real
config.json or instance/.

Run: python tests/test_schedules.py
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

from attendance.engine import AttendanceEngine  # noqa: E402
from attendance.schedules import (  # noqa: E402
    DEFAULT_GROUP, ScheduleError, apply_schedules, check_schedule, left_early, schedules_of, status_at,
)
from attendance.summary import ABSENT, LATE, LEFT_EARLY, NOT_YET, PRESENT, daily_summary, person_totals  # noqa: E402

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


def raises(fn, field=None):
    try:
        fn()
    except ScheduleError as exc:
        return field is None or exc.field == field
    return False


DEFAULT = {"working_days": [0, 1, 2, 3, 4], "start": "09:00", "late_after": "09:30", "end": "17:30"}
MORNING = {"working_days": [0, 1, 2, 3, 4, 5], "start": "07:30", "late_after": "08:00", "end": "12:00"}
MON = datetime(2026, 10, 19)  # a Monday

# ──────────────────────────────────────────────────────────────────────────
section("1. Reading and checking schedules")

old_cfg = {"attendance": {"late_after": "09:15", "working_hours_end": "16:00"}}
s = schedules_of(old_cfg)
check("An older config gets a Default group", list(s) == [DEFAULT_GROUP])
check("Default comes from the attendance settings",
      s[DEFAULT_GROUP] == {"working_days": [0, 1, 2, 3, 4], "start": "09:15", "late_after": "09:15", "end": "16:00"},
      str(s[DEFAULT_GROUP]))
cfg = {"attendance": {}, "schedules": {"Morning": MORNING}}
check("Other groups come from schedules", list(schedules_of(cfg)) == [DEFAULT_GROUP, "Morning"])

check("A good schedule passes", check_schedule(MORNING) == MORNING)
check("No working days refused", raises(lambda: check_schedule(dict(MORNING, working_days=[])), "working_days"))
check("Day 7 refused", raises(lambda: check_schedule(dict(MORNING, working_days=[7])), "working_days"))
check("A time like 9:30 refused", raises(lambda: check_schedule(dict(MORNING, late_after="9:30")), "late_after"))
check("A time like 24:00 refused", raises(lambda: check_schedule(dict(MORNING, end="24:00")), "end"))
check("Late after before start refused", raises(lambda: check_schedule(dict(MORNING, late_after="07:00"))))
check("End before late after refused", raises(lambda: check_schedule(dict(MORNING, end="08:00"))))
check("Duplicate days are merged and sorted", check_schedule(dict(MORNING, working_days=[4, 0, 4]))["working_days"] == [0, 4])

cfg = {"attendance": {"cooldown_minutes": 30}}
apply_schedules(cfg, {DEFAULT_GROUP: DEFAULT, "  Morning   shift ": MORNING})
check("Default is written into the attendance settings",
      cfg["attendance"]["late_after"] == "09:30" and cfg["attendance"]["working_hours_end"] == "17:30"
      and cfg["attendance"]["start"] == "09:00" and cfg["attendance"]["cooldown_minutes"] == 30)
check("Group names are tidied", list(cfg["schedules"]) == ["Morning shift"])
check("Default cannot be removed", raises(lambda: apply_schedules({}, {"Other": MORNING})))
check("Two groups with one name refused", raises(lambda: apply_schedules({}, {DEFAULT_GROUP: DEFAULT, "A": MORNING, "A ": MORNING})))
check("An empty group name refused", raises(lambda: apply_schedules({}, {DEFAULT_GROUP: DEFAULT, "  ": MORNING}), "name"))

# ──────────────────────────────────────────────────────────────────────────
section("2. Present and Late, compared as times of day")

check("09:29:59 on a Monday is Present", status_at(DEFAULT, MON.replace(hour=9, minute=29, second=59)) == PRESENT)
check("09:30:00 on a Monday is Late", status_at(DEFAULT, MON.replace(hour=9, minute=30)) == LATE)
check("10:00 on a Monday is Late", status_at(DEFAULT, MON.replace(hour=10)) == LATE)
sunday = MON + timedelta(days=6)
check("Sunday is not a working day: never Late", status_at(DEFAULT, sunday.replace(hour=11)) == PRESENT)
saturday = MON + timedelta(days=5)
check("Saturday counts for a group that works it", status_at(MORNING, saturday.replace(hour=8, minute=10)) == LATE)
check("Morning group is Late at 08:00", status_at(MORNING, MON.replace(hour=8)) == LATE)

# ──────────────────────────────────────────────────────────────────────────
section("3. Left early")

check("Two sightings, last before the end: left early", left_early(DEFAULT, MON, "15:00:00", 2) is True)
check("One sighting tells nothing", left_early(DEFAULT, MON, "09:00:00", 1) is False)
check("Last sighting at the end is not early", left_early(DEFAULT, MON, "17:30:00", 2) is False)
check("Non-working day: never left early", left_early(DEFAULT, sunday, "10:00:00", 3) is False)

# ──────────────────────────────────────────────────────────────────────────
section("4. A week for two groups, with a fixed clock")

people = [
    {"person_id": 1, "name": "Asha Verma", "group": DEFAULT_GROUP, "created": None},
    {"person_id": 2, "name": "Rohan Mehta", "group": "Morning", "created": None},
]
scheds = {DEFAULT_GROUP: DEFAULT, "Morning": MORNING}


def rec(pid, day, t, status, manual=False, review=None):
    r = {"person_id": pid, "date": (MON + timedelta(days=day)).date().isoformat(), "time_in": t,
         "status": status, "manual": manual}
    if review:
        r["review"] = review
    return r


records = [
    rec(1, 0, "09:10:00", PRESENT), rec(1, 0, "17:45:00", PRESENT),   # Mon: present, stayed
    rec(1, 1, "09:40:00", LATE),                                      # Tue: late, one sighting
    rec(1, 2, "09:05:00", PRESENT), rec(1, 2, "14:00:00", PRESENT),   # Wed: left early
    rec(1, 5, "10:00:00", PRESENT),                                   # Sat: not a working day for Default
    rec(2, 0, "07:50:00", PRESENT), rec(2, 0, "11:00:00", PRESENT),   # Mon: Morning left before 12:00
    rec(2, 1, "08:20:00", LATE), rec(2, 1, "12:30:00", LATE),         # Tue: late, stayed to the end
    rec(2, 2, "08:30:00", LATE, review="pending"),                     # Wed: waiting for review, not counted
    rec(2, 3, "07:40:00", PRESENT, review="rejected"),                 # Thu: rejected, not counted
    rec(2, 4, "07:45:00", PRESENT, review="confirmed"),                # Fri: confirmed, counts
]
after_week = (MON + timedelta(days=6)).replace(hour=20)
rows = daily_summary(records, people, MON.date().isoformat(), (MON + timedelta(days=6)).date().isoformat(),
                     after_week, schedule_of=lambda p: scheds[p["group"]])
a = {r["date"][-2:]: r for r in rows if r["person_id"] == 1}
b = {r["date"][-2:]: r for r in rows if r["person_id"] == 2}

check("Asha Mon: Present, not left early", a["19"]["status"] == PRESENT and not a["19"]["left_early"])
check("Asha Tue: Late, one sighting so no left early", a["20"]["status"] == LATE and not a["20"]["left_early"])
check("Asha Wed: Present and left early", a["21"]["status"] == PRESENT and a["21"]["left_early"])
check("Asha Thu and Fri: Absent", a["22"]["status"] == ABSENT and a["23"]["status"] == ABSENT)
check("Asha Sat: record on a non-working day still shows, never left early",
      a["24"]["status"] == PRESENT and not a["24"]["left_early"])
check("Asha Sun: not a working day, no row", "25" not in a)
check("Rohan Mon: left before the Morning end", b["19"]["status"] == PRESENT and b["19"]["left_early"])
check("Rohan Tue: Late, stayed", b["20"]["status"] == LATE and not b["20"]["left_early"])
check("Rohan Wed: the only record waits for review, so Absent", b["21"]["status"] == ABSENT)
check("Rohan Thu: rejected record does not count, Absent", b["22"]["status"] == ABSENT)
check("Rohan Fri: confirmed record counts", b["23"]["status"] == PRESENT)
check("Rohan Sat: a working day for Morning, Absent", b["24"]["status"] == ABSENT)
check("Rohan Sun: no row", "25" not in b)

totals = {t["person_id"]: t for t in person_totals(rows, people)}
check("Asha totals", (totals[1][PRESENT], totals[1][LATE], totals[1][ABSENT], totals[1][LEFT_EARLY]) == (3, 1, 2, 1),
      str(totals[1]))
check("Rohan totals", (totals[2][PRESENT], totals[2][LATE], totals[2][ABSENT], totals[2][LEFT_EARLY]) == (2, 1, 3, 1),
      str(totals[2]))

midday_wed = (MON + timedelta(days=2)).replace(hour=13)
rows = daily_summary([rec(1, 2, "09:05:00", PRESENT), rec(1, 2, "12:00:00", PRESENT)], people,
                     (MON + timedelta(days=2)).date().isoformat(), (MON + timedelta(days=2)).date().isoformat(),
                     midday_wed, schedule_of=lambda p: scheds[p["group"]])
w = {r["person_id"]: r for r in rows}
check("Today, before the end: not judged left early yet", w[1]["status"] == PRESENT and not w[1]["left_early"])
check("Today, Morning group's end has passed: Absent", w[2]["status"] == ABSENT)
rows = daily_summary([], people, MON.date().isoformat(), MON.date().isoformat(), MON.replace(hour=11),
                     schedule_of=lambda p: scheds[p["group"]])
check("Today, before each group's end: Not seen yet", all(r["status"] == NOT_YET for r in rows))

# ──────────────────────────────────────────────────────────────────────────
section("5. The engine uses each person's schedule")


class FakeDB:
    def __init__(self):
        self.rows = []

    def insert_log(self, record):
        record["id"] = f"r{len(self.rows)}"
        self.rows.append(dict(record))
        return dict(record)

    def get_today_logs(self):
        return list(self.rows)


class FakeFirebase:
    def push_attendance(self, _record):
        pass


clock = [MON.replace(hour=8, minute=30)]
engine = AttendanceEngine({"names": {1: "Asha", 2: "Rohan"}, "attendance": {"cooldown_minutes": 1, "review_margin": 0}},
                          FakeDB(), FakeFirebase(), clock=lambda: clock[0])
engine.set_schedule_lookup(lambda pid: MORNING if pid == 2 else DEFAULT)
check("Default person at 08:30 Monday: Present", engine.process_detection(1, True, "", 20.0)["status"] == PRESENT)
check("Morning person at 08:30 Monday: Late", engine.process_detection(2, True, "", 20.0)["status"] == LATE)
clock[0] = sunday.replace(hour=11)
check("Sunday 11:00: Present (not a working day)", engine.process_detection(1, True, "", 20.0)["status"] == PRESENT)
engine.set_schedule_lookup(lambda pid: 1 / 0)
clock[0] = MON.replace(hour=10) + timedelta(days=7)
check("A failing lookup falls back to Default, never drops the sighting",
      engine.process_detection(1, True, "", 20.0)["status"] == LATE)

# ──────────────────────────────────────────────────────────────────────────
section("6. Schedules API")


def file_hash(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


REAL_CONFIG = os.path.join(PROJECT_ROOT, "config.json")
_cfg_hash_before = file_hash(REAL_CONFIG)
tmp_dir = tempfile.mkdtemp(prefix="face_web_test_")
tmp_cfg_path = os.path.join(tmp_dir, "config.json")
if os.path.exists(REAL_CONFIG):
    with open(REAL_CONFIG, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)
cfg_copy["mongodb_db"] = "face_attendance_test"
cfg_copy["mongodb_uri"] = "mongodb://localhost:27017"
cfg_copy["names"] = {}
cfg_copy["person_groups"] = {}
cfg_copy.pop("schedules", None)
with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)
os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

from pymongo import MongoClient  # noqa: E402
_mc = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)
try:
    for name in ("attendance_logs", "persons", "app_meta", "audit_log"):
        _mc["face_attendance_test"][name].delete_many({})
    mongo_available = True
except Exception:
    mongo_available = False
_mc.close()

import server  # noqa: E402
from core.model_loader import load_config  # noqa: E402
from helpers import register_people, signed_in_client  # noqa: E402

if not mongo_available:
    print("  [SKIP] MongoDB is not running on localhost:27017")
else:
    client = signed_in_client(server)
    register_people(server, {1: "Asha Verma", 2: "Rohan Mehta"})

    data = client.get("/api/schedules").get_json()
    check("GET /api/schedules lists Default with both people",
          [g["name"] for g in data["groups"]] == [DEFAULT_GROUP] and data["groups"][0]["members"] == 2, str(data))

    res = client.put("/api/schedules", json={"groups": {DEFAULT_GROUP: DEFAULT, "Morning": MORNING}})
    check("Adding a group -> 200", res.status_code == 200, str(res.get_json()))
    check("Saved in the config", load_config()["schedules"] == {"Morning": MORNING})
    check("Default written to attendance", load_config()["attendance"]["late_after"] == "09:30")
    res = client.put("/api/schedules", json={"groups": {DEFAULT_GROUP: DEFAULT, "Morning": dict(MORNING, end="07:00")}})
    check("A bad schedule -> 400 naming the field", res.status_code == 400 and res.get_json()["field"] == "late_after",
          str(res.get_json()))
    check("Removing Default -> 400", client.put("/api/schedules", json={"groups": {"Morning": MORNING}}).status_code == 400)
    check("Nothing sent -> 400", client.put("/api/schedules", json={}).status_code == 400)

    res = client.patch("/api/people/2", json={"group": "Morning"})
    check("Moving a person -> 200", res.status_code == 200 and res.get_json()["person"]["group"] == "Morning")
    check("Group stored in the database", server.people.get(2)["group"] == "Morning")
    check("Config copy keeps only non-default groups", load_config()["person_groups"] == {"2": "Morning"})
    check("Engine now uses the Morning schedule", server.engine.schedule_for(2) == MORNING)
    check("Others keep Default", server.engine.schedule_for(1)["late_after"] == "09:30")
    edited = load_config()
    saved_schedules = edited.pop("schedules")
    server.save_config(edited)  # someone deletes the group from config.json by hand
    check("A group with no schedule counts as Default", server.people.get(2)["group"] == DEFAULT_GROUP)
    check("And is judged by the Default schedule", server.engine.schedule_for(2)["late_after"] == "09:30")
    edited["schedules"] = saved_schedules
    server.save_config(edited)
    check("Back once the group exists again", server.people.get(2)["group"] == "Morning")
    check("Unknown group -> 404", client.patch("/api/people/1", json={"group": "Nope"}).status_code == 404)
    check("Move audited", len(client.get("/api/audit", query_string={"action": "person.group_changed"})
                              .get_json()["entries"]) == 1)

    res = client.put("/api/schedules", json={"groups": {DEFAULT_GROUP: DEFAULT}})
    check("Removing a group with people -> 409", res.status_code == 409, str(res.get_json()))
    client.patch("/api/people/2", json={"group": DEFAULT_GROUP})
    res = client.put("/api/schedules", json={"groups": {DEFAULT_GROUP: DEFAULT}})
    check("Removing an empty group -> 200", res.status_code == 200 and load_config()["schedules"] == {})
    entries = client.get("/api/audit", query_string={"action": "settings.schedules"}).get_json()["entries"]
    check("Schedule changes audited, with what changed",
          any(e["details"].get("removed") == ["Morning"] for e in entries)
          and any(e["details"].get("added") == ["Morning"] for e in entries), str([e["details"] for e in entries]))

    res = client.post("/api/config", json={"attendance": {"late_after": "08:45"}})
    check("Late after through Settings moves the Default start back too",
          res.status_code == 200 and load_config()["attendance"]["start"] == "08:45", str(load_config()["attendance"]))

    # ── absence list (real clock: schedules chosen so the answer is fixed) ──
    everyday = [0, 1, 2, 3, 4, 5, 6]
    client.put("/api/schedules", json={"groups": {
        DEFAULT_GROUP: {"working_days": everyday, "start": "00:00", "late_after": "00:00", "end": "23:59"},
        "Never today": {"working_days": [(datetime.now().weekday() + 1) % 7], "start": "09:00", "late_after": "09:30", "end": "17:00"},
    }})
    register_people(server, {3: "Neha Kapoor"})
    client.patch("/api/people/3", json={"group": "Never today"})
    today = datetime.now().date().isoformat()
    server.mongo_db.insert_log({"person_id": 1, "name": "Asha Verma", "date": today, "time_in": "00:00:01",
                                "status": "Present", "manual": False})
    data = client.get("/api/absent/today").get_json()
    check("Absence: two people scheduled today", data["scheduled"] == 2, str(data))
    check("Absence: Rohan is missing and overdue",
          [(m["person_id"], m["overdue"]) for m in data["missing"]] == [(2, True)], str(data["missing"]))
    check("Absence: a group that does not work today is left out", all(m["person_id"] != 3 for m in data["missing"]))
    server.mongo_db.insert_log({"person_id": 2, "name": "Rohan Mehta", "date": today, "time_in": "00:00:02",
                                "status": "Late", "manual": False, "review": "pending"})
    data = client.get("/api/absent/today").get_json()
    check("Absence: a record waiting for review does not count as seen", [m["person_id"] for m in data["missing"]] == [2])

    from db.mongo_client import MongoAttendanceDB  # noqa: E402
    offline = MongoAttendanceDB("mongodb://localhost:29999", "face_attendance_test")
    real_db = server.mongo_db
    server.mongo_db = offline
    check("Absence offline -> 503", client.get("/api/absent/today").status_code == 503)
    server.mongo_db = real_db
    offline.close()

    for name in ("attendance_logs", "persons", "app_meta"):
        server.mongo_db.database()[name].delete_many({})

check("config.json hash unchanged", file_hash(REAL_CONFIG) == _cfg_hash_before)
shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
