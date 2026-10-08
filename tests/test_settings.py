"""
tests/test_settings.py
Settings routes and applying configuration changes live.

Uses temporary copies of config.json and never touches the real config.json,
ImageData, or Trainer.yml.

Run: python tests/test_settings.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

# Ensure project root is on path
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
tmp_dir = tempfile.mkdtemp(prefix="phase3b_cfg_")
tmp_cfg_file = os.path.join(tmp_dir, "test_config.json")

real_cfg_path = os.path.join(PROJECT_ROOT, "config.json")
if os.path.exists(real_cfg_path):
    with open(real_cfg_path, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)

cfg_copy["mongodb_db"] = "face_attendance_test"
with open(tmp_cfg_file, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)

os.environ["FACE_WEB_CONFIG"] = tmp_cfg_file
os.environ.setdefault("TESTING", "1")

import server as srv
client = srv.app.test_client()

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")


def section(title):
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)

# ─────────────────────────────────────────────────────────────────────────────
#  1. POST /api/config/db_test
# ─────────────────────────────────────────────────────────────────────────────
section("1. POST /api/config/db_test")

# 1a. Missing or empty URI
resp = client.post("/api/config/db_test", json={"uri": ""})
check("Missing URI returns 200 (not 500)", resp.status_code == 200)
data = resp.get_json()
check("Missing URI ok is False", data.get("ok") is False)
check("Missing URI has error message", "required" in data.get("error", "").lower())

# 1b. Bad / unreachable URI
resp = client.post("/api/config/db_test", json={"uri": "mongodb://localhost:29999"})
check("Unreachable URI returns 200 (never 500)", resp.status_code == 200)
data = resp.get_json()
check("Unreachable URI ok is False", data.get("ok") is False)
check("Unreachable URI has error message", bool(data.get("error")))

# 1c. URI with password (ensure password not leaked)
resp = client.post("/api/config/db_test", json={"uri": "mongodb://user:supersecretpass@localhost:29999"})
check("Password URI returns 200", resp.status_code == 200)
data = resp.get_json()
check("Password URI ok is False", data.get("ok") is False)
check("Password NOT in error message", "supersecretpass" not in data.get("error", ""))

# 1d. Valid local Mongo URI (local mongo is running)
resp = client.post("/api/config/db_test", json={"uri": "mongodb://localhost:27017"})
check("Valid URI returns 200", resp.status_code == 200)
data = resp.get_json()
check("Valid URI ok is True", data.get("ok") is True)


# ─────────────────────────────────────────────────────────────────────────────
#  2. POST /api/config validation
# ─────────────────────────────────────────────────────────────────────────────
section("2. POST /api/config input validation")

# 2a. cooldown_minutes invalid (0, negative, string, boolean)
resp = client.post("/api/config", json={"attendance": {"cooldown_minutes": 0}})
check("cooldown_minutes 0 -> 400", resp.status_code == 400)
data = resp.get_json()
check("Error names cooldown_minutes", data.get("field") == "cooldown_minutes")

resp = client.post("/api/config", json={"attendance": {"cooldown_minutes": -5}})
check("cooldown_minutes negative -> 400", resp.status_code == 400)

resp = client.post("/api/config", json={"attendance": {"cooldown_minutes": "60"}})
check("cooldown_minutes string -> 400", resp.status_code == 400)

resp = client.post("/api/config", json={"attendance": {"cooldown_minutes": True}})
check("cooldown_minutes bool -> 400", resp.status_code == 400)

# 2b. late_after invalid format
resp = client.post("/api/config", json={"attendance": {"late_after": "9:30"}})
check("late_after '9:30' (not HH:MM) -> 400", resp.status_code == 400)
check("Error names late_after", resp.get_json().get("field") == "late_after")

resp = client.post("/api/config", json={"attendance": {"late_after": "25:00"}})
check("late_after '25:00' -> 400", resp.status_code == 400)

# 2c. working_hours_end invalid format
resp = client.post("/api/config", json={"attendance": {"working_hours_end": "invalid"}})
check("working_hours_end 'invalid' -> 400", resp.status_code == 400)
check("Error names working_hours_end", resp.get_json().get("field") == "working_hours_end")

# 2d. late_after >= working_hours_end
resp = client.post("/api/config", json={
    "attendance": {
        "late_after": "18:00",
        "working_hours_end": "17:30"
    }
})
check("late_after >= working_hours_end -> 400", resp.status_code == 400)
check("Error mentions late_after", resp.get_json().get("field") == "late_after")

# 2e. names keys not integers
resp = client.post("/api/config", json={"names": {"alice": "Alice"}})
check("Non-integer names key -> 400", resp.status_code == 400)
check("Error names 'names'", resp.get_json().get("field") == "names")


# ─────────────────────────────────────────────────────────────────────────────
#  3. POST /api/config live apply (no restart)
# ─────────────────────────────────────────────────────────────────────────────
section("3. POST /api/config live apply without restart")

# Setup isolated temp config
tmp_dir = tempfile.mkdtemp(prefix="phase3b_cfg_")
tmp_cfg_file = os.path.join(tmp_dir, "test_config.json")

# Start with a copy of real cfg
test_cfg = json.loads(json.dumps(srv.load_config()))
with open(tmp_cfg_file, "w") as f:
    json.dump(test_cfg, f)

def fake_load(path=None):
    with open(tmp_cfg_file, "r") as f:
        return json.load(f)

def fake_save(cfg_data, path=None):
    with open(tmp_cfg_file, "w") as f:
        json.dump(cfg_data, f)

with patch("server.load_config", side_effect=fake_load), \
     patch("server.save_config", side_effect=fake_save):

    # 3a. Update cooldown_minutes and times
    resp = client.post("/api/config", json={
        "attendance": {
            "cooldown_minutes": 15,
            "late_after": "10:00",
            "working_hours_end": "18:00"
        }
    })
    check("Valid config POST returns 200", resp.status_code == 200)
    data = resp.get_json()
    check("Response has ok: True", data.get("ok") is True)
    check("Response contains updated config", data.get("config", {}).get("attendance", {}).get("cooldown_minutes") == 15)

    # Check live engine update (NO restart)
    check("Engine cooldown updated live", srv.engine._cooldown_minutes == 15)
    check("Engine late_after updated live", srv.engine._late_after == "10:00")
    check("Engine working_hours_end updated live", srv.engine._working_hours_end == "18:00")

    # 3b. Update names
    resp = client.post("/api/config", json={
        "names": {"101": "Dr. Watson", "102": "Sherlock Holmes"}
    })
    check("Names update returns 200", resp.status_code == 200)
    check("Detector names updated live", srv.detector._names.get(101) == "Dr. Watson")
    check("Engine names updated live", srv.engine._names.get(102) == "Sherlock Holmes")

    # 3c. Update MongoDB settings to unreachable URI (must not crash, must return warning)
    resp = client.post("/api/config", json={
        "mongodb_uri": "mongodb://localhost:29999",
        "mongodb_db": "test_db_unreachable"
    })
    check("Unreachable Mongo config returns 200 (never 500)", resp.status_code == 200)
    data = resp.get_json()
    check("Returns warning on unreachable Mongo", "warning" in data)
    check("Warning does not crash server", data.get("ok") is True)

    # 3d. Restore Mongo to working URI
    resp = client.post("/api/config", json={
        "mongodb_uri": "mongodb://localhost:27017",
        "mongodb_db": "face_attendance_test"
    })
    check("Restore Mongo returns 200", resp.status_code == 200)
    check("Mongo reconnected successfully", srv.mongo_db.is_connected is True)

    # 3e. GET /api/config
    resp = client.get("/api/config")
    check("GET /api/config returns 200", resp.status_code == 200)
    cfg_got = resp.get_json()
    check("GET /api/config has names", "101" in cfg_got.get("names", {}))

# ─────────────────────────────────────────────────────────────────────────────
#  4. Real config.json integrity & cleanup
# ─────────────────────────────────────────────────────────────────────────────
section("4. Real config.json integrity & cleanup")

# Drop test db
try:
    from pymongo import MongoClient
    mc = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)
    mc.drop_database("face_attendance_test")
    mc.close()
except Exception:
    pass

shutil.rmtree(tmp_dir, ignore_errors=True)

_cfg_hash_after = get_real_cfg_hash()
check("Real config.json unchanged during test run", _cfg_hash_before == _cfg_hash_after)


# ─────────────────────────────────────────────────────────────────────────────
#  RESULTS
# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print("=" * 60)

sys.exit(1 if failed > 0 else 0)
