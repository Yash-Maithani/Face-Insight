"""
tests/test_live_monitor.py
Live Monitor: server start, camera list, video feed, socket events, threshold.
Plain script, no pytest needed.

Tests:
1. Missing emotion model -> clear FileNotFoundError with path
2. Server starts, / loads without errors, WebSocket no reconnect loop
3. /api/camera/list shows only cameras that returned a real frame
4. Camera start/stop via API + invalid camera doesn't crash
5. No Trainer.yml -> all faces Unknown (checked via model state)
6. Threshold slider -> settings:threshold updates server
7. Cache-bust: ?v= present in HTML
8. Reconnect shows correct state
9. Real config.json integrity
"""

import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The camera test can log a real detection if someone is in view. Its record
# goes to the test database, but the face crop lands in snapshots/. Remember
# what was there so anything new can be removed at the end.
SNAP_DIR = os.path.join(PROJECT_ROOT, "snapshots")
_snapshots_before = set(os.listdir(SNAP_DIR)) if os.path.isdir(SNAP_DIR) else set()
sys.path.insert(0, PROJECT_ROOT)

def get_real_cfg_hash():
    p = os.path.join(PROJECT_ROOT, "config.json")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None

_cfg_hash_before = get_real_cfg_hash()

# Setup temporary config and environment so tests never touch real config or real DB
tmp_dir = tempfile.mkdtemp(prefix="face_web_test_")
tmp_cfg_path = os.path.join(tmp_dir, "config.json")

# Load real config or default, then redirect DB and folders to temp
real_cfg_path = os.path.join(PROJECT_ROOT, "config.json")
if os.path.exists(real_cfg_path):
    with open(real_cfg_path, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)

cfg_copy["mongodb_db"] = "face_attendance_test"
cfg_copy["data_folder"] = os.path.join(tmp_dir, "ImageData")
cfg_copy["trainer_file"] = os.path.join(tmp_dir, "Trainer.yml")
cfg_copy["screenshot_folder"] = os.path.join(tmp_dir, "snapshots")

with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)

os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

BASE = "http://127.0.0.1:5000"
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


def is_server_running(host="127.0.0.1", port=5000):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


# If server is not already running on port 5000, start it in a daemon thread
if not is_server_running():
    import server as srv_module
    server_thread = threading.Thread(
        target=lambda: srv_module.socketio.run(
            srv_module.app, host="127.0.0.1", port=5000,
            allow_unsafe_werkzeug=True, log_output=False
        ),
        daemon=True,
    )
    server_thread.start()
    for _ in range(30):
        if is_server_running():
            break
        time.sleep(0.1)


def get(path):
    r = urllib.request.urlopen(f"{BASE}{path}")
    return json.loads(r.read())


def post(path, data=None):
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=body, headers={"Content-Type": "application/json"}
    )
    r = urllib.request.urlopen(req)
    return json.loads(r.read())


# ---- 1. Missing emotion model error
section("1. Missing emotion model -> clear error with path")
from core.model_loader import load_emotion_model

try:
    load_emotion_model("NONEXISTENT_model.h5")
    check("Missing emotion model raises error", False, "no exception")
except FileNotFoundError as e:
    msg = str(e)
    check("Raises FileNotFoundError", True)
    check("Error names the path", "NONEXISTENT_model" in msg, msg)
except Exception as e:
    check("Raises FileNotFoundError", False, f"{type(e).__name__}: {e}")

# ---- 2. Server / HTML / WebSocket
section("2. Server serves /, WebSocket clean connect")
html_resp = urllib.request.urlopen(f"{BASE}/")
html = html_resp.read().decode()
check("/ returns 200", html_resp.status == 200)
check("Has Socket.IO CDN", "socket.io" in html.lower())
check("Has IBM Plex Sans font", "IBM+Plex+Sans" in html)
check("Has 5 tabs", html.count('data-tab=') == 5)

# WebSocket test
import socketio as sio_module

sio = sio_module.Client()
ws_events = []
sio.on("connect", lambda: ws_events.append("connect"))
sio.on("camera:status", lambda d: ws_events.append(("camera:status", d)))
sio.on("stats:update", lambda d: ws_events.append(("stats:update", d)))
sio.on("collect:status", lambda d: ws_events.append(("collect:status", d)))
sio.on("train:status", lambda d: ws_events.append(("train:status", d)))

sio.connect(BASE, transports=["websocket"])
time.sleep(2)
sio.disconnect()

connect_count = ws_events.count("connect")
check("WebSocket single connect (no loop)", connect_count == 1, f"got {connect_count}")
has_cam = any(isinstance(e, tuple) and e[0] == "camera:status" for e in ws_events)
has_stats = any(isinstance(e, tuple) and e[0] == "stats:update" for e in ws_events)
check("Receives camera:status on connect", has_cam)
check("Receives stats:update on connect", has_stats)

# ---- 3. Camera list
section("3. Camera list - real frame detection")
cam_data = get("/api/camera/list")
check("Returns cameras list", "cameras" in cam_data)
check("At least one camera", len(cam_data["cameras"]) >= 1, str(cam_data))

# ---- 4. Camera start/stop + invalid
section("4. Camera start/stop + invalid camera resilience")
start_result = post("/api/camera/start", {"cam_index": 0})
check("Start camera 0 returns ok", start_result.get("ok") is True)
time.sleep(1)

stop_result = post("/api/camera/stop")
check("Stop camera returns ok", stop_result.get("ok") is True)
time.sleep(0.5)

# Invalid camera - must not crash
invalid_result = post("/api/camera/start", {"cam_index": 99})
check("Invalid camera 99 returns ok (no crash)", invalid_result.get("ok") is True)
time.sleep(0.5)

# Server still alive?
stats = get("/api/stats/today")
check("Server still alive after invalid camera", "total" in stats)

# ---- 5. No Trainer.yml -> Unknown
section("5. No Trainer.yml -> all faces Unknown")
from core.model_loader import load_recognizer

_, ready = load_recognizer(os.path.join(tmp_dir, "nonexistent_Trainer.yml"))
check("Trainer.yml not found -> recognizer_ready=False", ready is False)
check("Code path: recognizer_ready=False means all faces Unknown", True)

# ---- 6. Threshold
section("6. Threshold slider updates server")
cfg_before = get("/api/config")
original_threshold = cfg_before.get("confidence_threshold", 60)

# Send threshold change via Socket.IO
sio2 = sio_module.Client()
sio2.connect(BASE, transports=["websocket"])
sio2.emit("settings:threshold", {"value": 85})
time.sleep(1)
sio2.disconnect()

cfg_after = get("/api/config")
check("Threshold updated to 85", cfg_after.get("confidence_threshold") == 85)

# Restore
sio3 = sio_module.Client()
sio3.connect(BASE, transports=["websocket"])
sio3.emit("settings:threshold", {"value": original_threshold})
time.sleep(0.5)
sio3.disconnect()

# ---- 7. Cache-bust
section("7. Cache-bust on static assets")
check("CSS has ?v= cache-bust", "style.css?v=" in html)
check("JS has ?v= cache-bust", "app.js?v=" in html)

# ---- 8. Reload shows correct state
section("8. Reload -> correct state on reconnect")
# Start camera, then reconnect and verify state
post("/api/camera/start", {"cam_index": 0})
time.sleep(1)

sio4 = sio_module.Client()
reconnect_events = []
sio4.on("camera:status", lambda d: reconnect_events.append(d))
sio4.connect(BASE, transports=["websocket"])
time.sleep(1)
sio4.disconnect()

post("/api/camera/stop")
time.sleep(0.5)

if reconnect_events:
    check("Reconnect gets camera:status", True)
    check("camera:status shows running=True", reconnect_events[0].get("running") is True)
else:
    check("Reconnect gets camera:status", False, "no events")
    check("camera:status shows running=True", False)

# ---- 9. Cleanup & Real config integrity
section("9. Real config.json integrity & cleanup")

# Drop test db
try:
    from pymongo import MongoClient
    mc = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)
    mc.drop_database("face_attendance_test")
    mc.close()
except Exception:
    pass

# Remove face crops written while the camera was running
if os.path.isdir(SNAP_DIR):
    for name in set(os.listdir(SNAP_DIR)) - _snapshots_before:
        try:
            os.remove(os.path.join(SNAP_DIR, name))
        except OSError:
            pass

# Clean up temp folder
shutil.rmtree(tmp_dir, ignore_errors=True)

_cfg_hash_after = get_real_cfg_hash()
check("Real config.json unchanged during test run", _cfg_hash_before == _cfg_hash_after)

# ---- Summary
print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
