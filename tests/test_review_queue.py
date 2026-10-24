"""
tests/test_review_queue.py
The review queue: matches close to the threshold wait for a person to
decide. A confirmed one becomes a normal record; a rejected one never
counts. Every decision is audited and announced to open pages.

Uses an isolated temp config and the test database. Never touches the real
config.json or instance/.

Run: python tests/test_review_queue.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

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
cfg_copy["confidence_threshold"] = 60
cfg_copy.setdefault("attendance", {}).update({"review_margin": 15, "cooldown_minutes": 60})
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
from db.mongo_client import MongoAttendanceDB  # noqa: E402
from helpers import register_people, signed_in_client, socket_client  # noqa: E402

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

engine = server.engine
db = server.mongo_db
client = signed_in_client(server)
register_people(server, {1: "Asha Verma", 2: "Rohan Mehta", 3: "Neha Kapoor"})
today = datetime.now().date().isoformat()

# ──────────────────────────────────────────────────────────────────────────
section("1. Which matches need review")

engine.set_threshold(60)
check("Threshold 60, margin 15: 51.0 is clear", engine.needs_review(51.0) is False)
check("51.1 needs review", engine.needs_review(51.1) is True)
check("59.9 needs review", engine.needs_review(59.9) is True)
check("A close match (30) does not", engine.needs_review(30.0) is False)
engine._review_margin = 0
check("Margin 0 turns review off", engine.needs_review(59.9) is False)
engine._review_margin = 15
server.detector.update_threshold(80)
check("Moving the threshold slider reaches the engine", engine._threshold == 80 and engine.needs_review(70.0))
server.detector.update_threshold(60)

engine.reset_cooldowns()
close = engine.process_detection(1, True, "", 30.0)
borderline = engine.process_detection(2, True, "", 55.5)
unknown = engine.process_detection(3, False, "", 75.0)
check("A close match counts at once", close is not None and "review" not in close)
check("A borderline match waits for review", borderline is not None and borderline.get("review") == "pending")
check("An unknown face never goes to review", unknown is not None and "review" not in unknown)
check("A borderline match still starts the cooldown", engine.process_detection(2, True, "", 55.5) is None)

# ──────────────────────────────────────────────────────────────────────────
section("2. Waiting records do not count")

stats = client.get("/api/stats/today").get_json()
check("Today's counts leave the waiting one out", stats["total"] == 2 and stats["present"] + stats["late"] == 1,
      str(stats))
check("Stats say one is waiting today", stats["review"] == 1)
check("Stats carry the waiting count for the sidebar", stats["review_pending"] == 1)
data = client.get(f"/api/people/2?from={today}&to={today}").get_json()
check("Rohan's day does not count yet", data["days"][0]["status"] in ("Absent", "Not seen yet"), str(data["days"]))
trend = client.get(f"/api/stats/weekly?from={today}&to={today}").get_json()[0]
check("Dashboard trend leaves it out", trend["present_count"] + trend["late_count"] == 1, str(trend))
persons = client.get(f"/api/stats/persons?from={today}&to={today}").get_json()
check("Days attended leaves it out", [p["person_id"] for p in persons] == [1], str(persons))
listed = client.get("/api/attendance").get_json()
check("The Attendance list still shows it, marked", any(r.get("review") == "pending" for r in listed))
res = client.get(f"/api/export/csv?date={today}")
check("Exports say which records wait for review", b"pending" in res.data)
res.close()

# ──────────────────────────────────────────────────────────────────────────
section("3. The queue")

data = client.get("/api/review").get_json()
check("GET /api/review lists the waiting record", [r["id"] for r in data["records"]] == [borderline["id"]], str(data))
check("It says how many are waiting", data["pending"] == 1)
check("Unknown state -> 400", client.get("/api/review?state=maybe").status_code == 400)
check("Nothing confirmed yet", client.get("/api/review?state=confirmed").get_json()["records"] == [])

sio = socket_client(server, client)
sio.get_received()
res = client.post(f"/api/review/{borderline['id']}", json={"decision": "confirm"})
check("Confirm -> 200", res.status_code == 200 and res.get_json()["record"]["review"] == "confirmed", str(res.get_json()))
check("Who confirmed it is kept", res.get_json()["record"]["reviewed_by"] == "tester")
events = [e["name"] for e in sio.get_received()]
check("Open pages are told", "review:update" in events and "stats:update" in events, str(events))

stats = client.get("/api/stats/today").get_json()
check("A confirmed record counts", stats["total"] == 3 and stats["present"] + stats["late"] == 2, str(stats))
check("Nothing waiting now", stats["review_pending"] == 0)
data = client.get(f"/api/people/2?from={today}&to={today}").get_json()
check("Rohan's day counts now", data["days"][0]["status"] in ("Present", "Late"), str(data["days"]))
check("Deciding again -> 404", client.post(f"/api/review/{borderline['id']}", json={"decision": "reject"}).status_code == 404)

engine.reset_cooldowns()
second = engine.process_detection(3, True, "", 58.0)
res = client.post(f"/api/review/{second['id']}", json={"decision": "reject"})
check("Reject -> 200", res.status_code == 200 and res.get_json()["record"]["review"] == "rejected")
stats = client.get("/api/stats/today").get_json()
check("A rejected record never counts", stats["total"] == 3, str(stats))
check("Rejected list shows it", [r["id"] for r in client.get("/api/review?state=rejected").get_json()["records"]] == [second["id"]])
data = client.get(f"/api/reports?from={today}&to={today}&people=3").get_json()
check("Reports leave the rejected record out", data["rows"][0]["sightings"] == 0, str(data["rows"]))

check("Bad decision -> 400", client.post(f"/api/review/{second['id']}", json={"decision": "maybe"}).status_code == 400)
check("Bad record ID -> 400", client.post("/api/review/not-an-id!", json={"decision": "confirm"}).status_code == 400)
check("Missing record -> 404", client.post("/api/review/0123456789abcdef01234567", json={"decision": "confirm"}).status_code == 404)

entries = client.get("/api/audit", query_string={"action": "review.confirmed"}).get_json()["entries"]
check("Confirm audited with the person and time", len(entries) == 1 and entries[0]["details"]["person_id"] == 2, str(entries))
entries = client.get("/api/audit", query_string={"action": "review.rejected"}).get_json()["entries"]
check("Reject audited", len(entries) == 1 and entries[0]["target"] == second["id"])

# ──────────────────────────────────────────────────────────────────────────
section("4. Settings and offline")

for bad in (-1, 51, "10", True):
    res = client.post("/api/config", json={"attendance": {"review_margin": bad}})
    check(f"Review margin {bad!r} -> 400", res.status_code == 400 and res.get_json()["field"] == "review_margin")
res = client.post("/api/config", json={"attendance": {"review_margin": 0}})
check("Review margin 0 saved and applied", res.status_code == 200 and engine._review_margin == 0)

offline = MongoAttendanceDB("mongodb://localhost:29999", "face_attendance_test")
server.mongo_db = offline
check("Queue offline -> 503", client.get("/api/review").status_code == 503)
check("Decision offline -> 503", client.post(f"/api/review/{second['id']}", json={"decision": "confirm"}).status_code == 503)
check("Stats offline: no waiting count", client.get("/api/stats/today").get_json()["review_pending"] is None)
server.mongo_db = db
offline.close()

for name in ("attendance_logs", "persons", "app_meta"):
    db.database()[name].delete_many({})
check("config.json hash unchanged", file_hash(REAL_CONFIG) == _cfg_hash_before)
shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
