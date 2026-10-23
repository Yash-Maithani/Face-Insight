"""
tests/test_people.py
People in the database: registration with consent, the people list, a
person's history, renaming, deleting, and what still works while the
database is offline. Also the date-range APIs and guided capture prompts.

Uses an isolated temp config, a temporary image folder and the test database.
Never touches the real config.json, ImageData or instance/.

Run: python tests/test_people.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def file_hash(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


REAL_CONFIG = os.path.join(PROJECT_ROOT, "config.json")
_cfg_hash_before = file_hash(REAL_CONFIG)

tmp_dir = tempfile.mkdtemp(prefix="face_web_test_")
tmp_cfg_path = os.path.join(tmp_dir, "config.json")
data_dir = os.path.join(tmp_dir, "ImageData")
os.makedirs(data_dir)
if os.path.exists(REAL_CONFIG):
    with open(REAL_CONFIG, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)
cfg_copy["mongodb_db"] = "face_attendance_test"
cfg_copy["mongodb_uri"] = "mongodb://localhost:27017"
cfg_copy["data_folder"] = data_dir
cfg_copy["trainer_file"] = os.path.join(tmp_dir, "Trainer.yml")
cfg_copy["names"] = {}
with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)
os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

# Start from an empty test database
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
from core.collector import ANGLE_PROMPTS, prompt_for  # noqa: E402
from core.model_loader import load_config  # noqa: E402
from db.mongo_client import MongoAttendanceDB  # noqa: E402
from helpers import signed_in_client  # noqa: E402

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


if not mongo_available:
    print("  [SKIP] MongoDB is not running on localhost:27017")
    print(f"\n{'=' * 60}\n  RESULTS: 0 passed, 0 failed, 0 total\n{'=' * 60}")
    sys.exit(0)

client = signed_in_client(server)
db = server.mongo_db
check("Server uses the test database", db._db_name == "face_attendance_test")


def audit(action):
    return client.get("/api/audit", query_string={"action": action}).get_json()["entries"]


def collect(person_id, name, consent=None):
    body = {"person_id": person_id, "name": name, "max_images": 20}
    if consent is not None:
        body["consent"] = consent
    with patch.object(server.collector, "start", return_value=(True, None)):
        return client.post("/api/collect/start", json=body)


def cached_names():
    return load_config().get("names", {})


# ──────────────────────────────────────────────────────────────────────────
section("1. Registering through face collection")

res = collect(1, "Asha Verma")
check("No consent -> 400", res.status_code == 400 and "Consent" in res.get_json()["error"], str(res.get_json()))
res = collect(1, "Asha Verma", consent=False)
check("Consent false -> 400", res.status_code == 400)
check("Nobody registered without consent", db.list_persons() == [])

res = collect(1, "Asha Verma", consent=True)
check("With consent -> 200", res.status_code == 200, str(res.get_json()))
person = server.people.get(1)
check("Person stored in the database", person and person["saved"] and person["name"] == "Asha Verma")
check("Consent time stored", bool(person and person.get("consent_at")))
check("Config copy updated", cached_names() == {"1": "Asha Verma"})
check("Recogniser knows the name", server.detector._names.get(1) == "Asha Verma")
check("Registration audited", len(audit("person.registered")) == 1)

res = collect(1, "Someone Else", consent=True)
check("Same ID, other name -> 409", res.status_code == 409 and "already belongs to Asha Verma" in res.get_json()["error"])
check("Name not changed", server.people.get(1)["name"] == "Asha Verma")

res = collect(1, "asha  verma")
check("Same ID, same name (any case and spacing) -> 200 without asking consent again", res.status_code == 200, str(res.get_json()))
check("No second registration entry", len(audit("person.registered")) == 1)

# A person known only from the config copy (an older install)
cfg_now = load_config()
cfg_now["names"]["5"] = "Rohan Mehta"
server.save_config(cfg_now)
check("Config-only person is listed as not saved", server.people.get(5)["saved"] is False)
res = collect(5, "Rohan Mehta")
check("Config-only person needs consent before collecting", res.status_code == 400)
res = collect(5, "Rohan Mehta", consent=True)
check("With consent -> 200", res.status_code == 200)
p5 = server.people.get(5)
check("Now saved, with consent", p5["saved"] and bool(p5["consent_at"]))

res = collect(7, "x" * 51, consent=True)
check("Name over 50 characters -> 400", res.status_code == 400)

# ──────────────────────────────────────────────────────────────────────────
section("2. People list")

for i in range(3):
    open(os.path.join(data_dir, f"1_{i + 1}.jpg"), "wb").close()
open(os.path.join(data_dir, "5_1.jpg"), "wb").close()
open(os.path.join(data_dir, "notes.txt"), "wb").close()

today = datetime.now().date()
d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
for rec in (
    {"person_id": 1, "name": "Asha Verma", "date": d1, "time_in": "09:00:00", "status": "Present", "manual": False},
    {"person_id": 1, "name": "Asha Verma", "date": d2, "time_in": "09:45:00", "status": "Late", "manual": False},
    {"person_id": 1, "name": "Asha Verma", "date": d2, "time_in": "13:00:00", "status": "Late", "manual": False},
    {"person_id": None, "name": "Unknown", "date": d2, "time_in": "10:00:00", "status": "Unknown", "manual": False},
):
    db.insert_log(dict(rec))

res = client.get("/api/people")
data = res.get_json()
check("GET /api/people -> 200", res.status_code == 200 and data["db_connected"] is True)
by_id = {p["person_id"]: p for p in data["people"]}
check("Lists both people", sorted(by_id) == [1, 5], str(sorted(by_id)))
check("Image counts from the image folder", by_id[1]["image_count"] == 3 and by_id[5]["image_count"] == 1)
check("Last seen is the latest record", by_id[1]["last_seen"] == f"{d2} 13:00:00", str(by_id[1]["last_seen"]))
check("Never seen: no last seen", by_id[5]["last_seen"] is None)
check("Consent shown", bool(by_id[1]["consent_at"]))

# ──────────────────────────────────────────────────────────────────────────
section("3. One person's history")

res = client.get(f"/api/people/1?from={d1}&to={d2}")
data = res.get_json()
check("GET /api/people/1 -> 200", res.status_code == 200, str(data)[:200])
days = {d["date"]: d for d in data["days"]}
check("Newest day first", [d["date"] for d in data["days"]][0] >= [d["date"] for d in data["days"]][-1])
check("Days with records have their status", days[d1]["status"] == "Present" and days[d2]["status"] == "Late")
check("First and last seen", (days[d2]["first_seen"], days[d2]["last_seen"], days[d2]["sightings"]) == ("09:45:00", "13:00:00", 2))
check("Totals", data["totals"]["Present"] == 1 and data["totals"]["Late"] == 1, str(data["totals"]))
check("Records are this person's only, no Unknown faces",
      len(data["records"]) == 3 and all(r["person_id"] == 1 for r in data["records"]))
check("Person details included", data["person"]["image_count"] == 3)

res = client.get("/api/people/1")
check("Default range is the last 30 days", res.status_code == 200
      and res.get_json()["from"] == (today - timedelta(days=29)).isoformat() and res.get_json()["to"] == today.isoformat())
check("Unknown person -> 404", client.get("/api/people/999").status_code == 404)
check("Range the wrong way round -> 400", client.get(f"/api/people/1?from={d2}&to={d1}").status_code == 400)
check("Bad date -> 400", client.get("/api/people/1?from=yesterday&to=today").status_code == 400)
check("Range over 366 days -> 400",
      client.get(f"/api/people/1?from={(today - timedelta(days=400)).isoformat()}&to={today.isoformat()}").status_code == 400)

# ──────────────────────────────────────────────────────────────────────────
section("4. Renaming")

res = client.patch("/api/people/1", json={"name": "  Asha   Verma-Rao "})
check("Rename -> 200", res.status_code == 200 and res.get_json()["person"]["name"] == "Asha Verma-Rao", str(res.get_json()))
check("Database updated", server.people.get(1)["name"] == "Asha Verma-Rao")
check("Config copy updated", cached_names()["1"] == "Asha Verma-Rao")
check("Recogniser updated", server.detector._names.get(1) == "Asha Verma-Rao")
entries = audit("person.renamed")
check("Rename audited with old and new name",
      len(entries) == 1 and entries[0]["details"] == {"from": "Asha Verma", "to": "Asha Verma-Rao"}, str(entries))
client.patch("/api/people/1", json={"name": "Asha Verma-Rao"})
check("Same name again: not audited", len(audit("person.renamed")) == 1)
check("Empty name -> 400", client.patch("/api/people/1", json={"name": "  "}).status_code == 400)
check("Too long -> 400", client.patch("/api/people/1", json={"name": "y" * 51}).status_code == 400)
check("Unknown person -> 404", client.patch("/api/people/999", json={"name": "Nobody"}).status_code == 404)

# ──────────────────────────────────────────────────────────────────────────
section("5. Manual marks use the registered name")

res = client.post("/api/attendance/manual", json={"person_id": 5, "name": "rohan mehta", "status": "Late"})
check("Name matched without regard to case -> 200", res.status_code == 200)
check("Record carries the registered spelling", res.get_json()["record"]["name"] == "Rohan Mehta")

# ──────────────────────────────────────────────────────────────────────────
section("6. Unknown faces and capture prompts")

server.engine.reset_cooldowns()
rec = server.engine.process_detection(1, False, "", 80.0)
check("Unknown face: person_id is null", rec is not None and rec["person_id"] is None)
check("Unknown face: the guess is kept apart", rec is not None and rec.get("guessed_person_id") == 1)
db.delete_log(rec["id"])

check("Five angle prompts", len(ANGLE_PROMPTS) == 5)
check("Prompt starts straight on", prompt_for(0, 500) == ANGLE_PROMPTS[0])
check("Prompt moves on with progress", prompt_for(100, 500) == ANGLE_PROMPTS[1] and prompt_for(450, 500) == ANGLE_PROMPTS[4])
check("Prompt never runs past the last", prompt_for(500, 500) == ANGLE_PROMPTS[4] and prompt_for(9999, 500) == ANGLE_PROMPTS[4])
check("Prompt with no target is safe", prompt_for(5, 0) == ANGLE_PROMPTS[0])
check("Collector status carries no prompt while idle", server.collector.get_status()["prompt"] is None)

# ──────────────────────────────────────────────────────────────────────────
section("7. Date ranges on Attendance and the Dashboard")

res = client.get(f"/api/attendance?from={d1}&to={d2}")
check("Attendance range -> every record in it", res.status_code == 200 and len(res.get_json()) == 4, str(len(res.get_json())))
check("Attendance single day still works", len(client.get(f"/api/attendance?date={d1}").get_json()) == 1)
check("Attendance range the wrong way round -> 400", client.get(f"/api/attendance?from={d2}&to={d1}").status_code == 400)
check("Attendance range missing an end -> 400", client.get(f"/api/attendance?from={d1}").status_code == 400)
res = client.get(f"/api/export/csv?from={d1}&to={d2}")
check("CSV export of a range", res.status_code == 200 and f"attendance_{d1}_to_{d2}.csv" in res.headers.get("Content-Disposition", ""))
res.close()

res = client.get(f"/api/stats/weekly?from={d1}&to={d2}")
data = res.get_json()
check("Dashboard trend for a custom range", res.status_code == 200 and [d["date"] for d in data] == [d1, d2])
check("Trend counts", data[1]["late_count"] == 2 and data[1]["unknown_count"] == 1, str(data))
res = client.get(f"/api/stats/persons?from={d1}&to={d2}")
data = res.get_json()
check("Days attended leaves out Unknown faces", [p["person_id"] for p in data] == [1], str(data))
check("Days attended counts distinct days", data[0]["days"] == 2 if data else False)
check("?days= still works", client.get("/api/stats/weekly?days=14").status_code == 200
      and len(client.get("/api/stats/weekly?days=14").get_json()) == 14)
check("Dashboard bad range -> 400", client.get("/api/stats/weekly?from=2026-13-01&to=2026-13-02").status_code == 400)

# ──────────────────────────────────────────────────────────────────────────
section("8. Settings: working days, and names through the old API")

res = client.post("/api/config", json={"attendance": {"working_days": [0, 1, 2, 3, 4, 5]}})
check("Working days saved", res.status_code == 200 and load_config()["attendance"]["working_days"] == [0, 1, 2, 3, 4, 5])
for bad in ([], [7], ["Mon"], [True], "0,1"):
    res = client.post("/api/config", json={"attendance": {"working_days": bad}})
    check(f"Working days {bad!r} -> 400", res.status_code == 400 and res.get_json()["field"] == "working_days")

res = client.post("/api/config", json={"names": {"1": "Asha Verma-Rao", "5": "Rohan M."}})
check("Names through /api/config -> 200", res.status_code == 200)
check("Rename applied to the database", server.people.get(5)["name"] == "Rohan M.")
check("Settings save audit names 'names'",
      any("names" in e["details"].get("changed", []) for e in audit("settings.saved")))

# ──────────────────────────────────────────────────────────────────────────
section("9. Deleting a person")

res = client.delete("/api/persons/5")
check("Delete -> 200", res.status_code == 200, str(res.get_json()))
check("Gone from the database", all(p["person_id"] != 5 for p in db.list_persons()))
check("Gone from the config copy", "5" not in cached_names())
check("Their images are deleted", not os.path.exists(os.path.join(data_dir, "5_1.jpg")))
check("Other people's images kept", os.path.exists(os.path.join(data_dir, "1_1.jpg")))
check("Their attendance records are kept", db.find_logs("2000-01-01", "2100-01-01", [5]) != [])
check("Delete again -> 404", client.delete("/api/persons/5").status_code == 404)

# ──────────────────────────────────────────────────────────────────────────
section("10. Database offline")

offline = MongoAttendanceDB("mongodb://localhost:29999", "face_attendance_test")
server.mongo_db = offline
server.engine._db = offline
data = client.get("/api/people").get_json()
check("List falls back to the config copy", [p["person_id"] for p in data["people"]] == [1] and data["db_connected"] is False)
check("Recognition names still available", server.people.names() == {"1": "Asha Verma-Rao"})
res = collect(8, "New Person", consent=True)
check("Registering refused -> 503", res.status_code == 503 and "offline" in res.get_json()["error"].lower())
check("Renaming refused -> 503", client.patch("/api/people/1", json={"name": "Other"}).status_code == 503)
check("Deleting refused -> 503", client.delete("/api/persons/1").status_code == 503)
check("Names through /api/config refused -> 503", client.post("/api/config", json={"names": {"1": "X"}}).status_code == 503)
check("Config copy untouched", cached_names() == {"1": "Asha Verma-Rao"})
check("Person history -> 503", client.get("/api/people/1").status_code == 503)
offline.close()
server.mongo_db = db
server.engine._db = db

# ──────────────────────────────────────────────────────────────────────────
section("11. Cleanup")

for name in ("attendance_logs", "persons", "app_meta"):
    db.database()[name].delete_many({})
check("config.json hash unchanged", file_hash(REAL_CONFIG) == _cfg_hash_before)
shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
