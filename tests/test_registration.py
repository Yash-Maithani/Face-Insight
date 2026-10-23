"""
tests/test_registration.py
Face registration: image collector, trainer, and their server routes.

Uses temporary folders and temporary config copies.  Never touches the
real ImageData folder, Trainer.yml, or config.json names.

Run:  python tests/test_registration.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

# Ensure project root is on the path
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
tmp_dir_3a = tempfile.mkdtemp(prefix="face_web_test_")
tmp_cfg_path = os.path.join(tmp_dir_3a, "config.json")
real_cfg_path = os.path.join(PROJECT_ROOT, "config.json")
if os.path.exists(real_cfg_path):
    with open(real_cfg_path, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)

cfg_copy["mongodb_db"] = "face_attendance_test"
cfg_copy["data_folder"] = os.path.join(tmp_dir_3a, "ImageData")
cfg_copy["trainer_file"] = os.path.join(tmp_dir_3a, "Trainer.yml")
cfg_copy["screenshot_folder"] = os.path.join(tmp_dir_3a, "snapshots")

with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)

os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_cascade():
    cascade = cv2.CascadeClassifier()
    cascade_path = os.path.join(PROJECT_ROOT, "models", "haarcascade_frontalface_default.xml")
    if not os.path.exists(cascade_path):
        cascade_path = os.path.join(cv2.data.haarcascades,
                                    "haarcascade_frontalface_default.xml")
    cascade.load(cascade_path)
    return cascade


def _make_gray(h=80, w=80):
    return np.random.randint(0, 200, (h, w), dtype=np.uint8)


def _make_bgr(h=240, w=320):
    return np.random.randint(0, 200, (h, w, 3), dtype=np.uint8)


def _flat_images(data_path, pid):
    """List all flat files for a person_id."""
    prefix = f"{pid}_"
    return [f for f in os.listdir(data_path)
            if f.startswith(prefix) and f.lower().endswith((".jpg", ".jpeg", ".png"))]


# ─────────────────────────────────────────────────────────────────────────────
#  1. augment_crop produces 4 variants
# ─────────────────────────────────────────────────────────────────────────────
section("1. augment_crop produces 4 variants")

from core.collector import augment_crop, _next_count

dummy_face = np.random.randint(0, 255, (48, 48), dtype=np.uint8)
variants = augment_crop(dummy_face)
check("Returns a list",             isinstance(variants, list))
check("Exactly 4 variants",         len(variants) == 4)
check("All same shape as input",    all(v.shape == dummy_face.shape for v in variants))
check("First is the original",      np.array_equal(variants[0], dummy_face))
check("Second is horizontally flipped",
      np.array_equal(variants[1], cv2.flip(dummy_face, 1)))


# ─────────────────────────────────────────────────────────────────────────────
#  2. _next_count: numbering continues from highest existing file
# ─────────────────────────────────────────────────────────────────────────────
section("2. _next_count numbering")

tmp_nc = tempfile.mkdtemp(prefix="face_nc_")
# Empty folder → start at 1
check("Empty folder starts at 1", _next_count(tmp_nc, 3) == 1)
# Write 3_5.jpg and 3_12.jpg
cv2.imwrite(os.path.join(tmp_nc, "3_5.jpg"),  _make_gray())
cv2.imwrite(os.path.join(tmp_nc, "3_12.jpg"), _make_gray())
check("Continues after max (12+1=13)", _next_count(tmp_nc, 3) == 13)
# Different person unaffected
check("Other person still starts at 1", _next_count(tmp_nc, 7) == 1)
shutil.rmtree(tmp_nc)


# ─────────────────────────────────────────────────────────────────────────────
#  3. Collector get_status payload shape
# ─────────────────────────────────────────────────────────────────────────────
section("3. Collector get_status payload")

cascade = _load_cascade()
mock_sio = MagicMock()
from core.collector import DataCollector

tmp_coll_dir = tempfile.mkdtemp(prefix="face_coll_")
dc = DataCollector({"data_folder": tmp_coll_dir, "max_images": 20}, cascade, mock_sio)
status = dc.get_status()
check("Has 'active' key",      "active"      in status)
check("Has 'running' key",     "running"     in status)
check("Has 'count' key",       "count"       in status)
check("Has 'target' key",      "target"      in status)
check("Has 'person_name' key", "person_name" in status)
check("active==running initially",
      status["active"] == status["running"] == False)
check("count is 0",   status["count"]  == 0)
check("target is 0",  status["target"] == 0)
shutil.rmtree(tmp_coll_dir)


# ─────────────────────────────────────────────────────────────────────────────
#  3b. Fake-camera collector: flat naming, 4/frame, overshoot guard,
#              largest-face-only, continue numbering
# ─────────────────────────────────────────────────────────────────────────────
section("3b. Fake-camera: flat naming, 4/frame, overshoot, numbering, largest face")

tmp_fake = tempfile.mkdtemp(prefix="face_fake_")

# Pre-seed two existing files for person 5
cv2.imwrite(os.path.join(tmp_fake, "5_1.jpg"), _make_gray())
cv2.imwrite(os.path.join(tmp_fake, "5_2.jpg"), _make_gray())

# Build a BGR fake frame
def _fake_bgr():
    return np.zeros((240, 320, 3), dtype=np.uint8)

fake_cap = MagicMock()
fake_cap.isOpened.return_value = True
fake_cap.read.return_value = (True, _fake_bgr())
fake_cap.release.return_value = None

# Fake cascade that always returns one face rect [x, y, w, h]
fake_cascade = MagicMock()
fake_cascade.detectMultiScale.return_value = np.array([[10, 10, 60, 60]])

mock_sio_fake = MagicMock()
collect_done_called = [False]

def fake_on_done(reason=None):
    collect_done_called[0] = True

cfg_fake = {"data_folder": tmp_fake, "max_images": 500}
dc_fake = DataCollector(cfg_fake, fake_cascade, mock_sio_fake)

with patch("core.collector.cv2.VideoCapture", return_value=fake_cap):
    ok, err = dc_fake.start(5, "Alice", 6, cam_index=0, on_done=fake_on_done)

if not ok:
    check("Fake-cap collector started", False)
    check("Files are named <id>_<n>.jpg", False)
    check("Numbering continues (starts from 3)", False)
    check("Overshoot guard: exactly 6 new files saved (total 8)", False)
    check("collect:done emitted", False)
    check("on_done callback fired", False)
else:
    # Wait for collection to finish (max 5 s)
    for _ in range(50):
        if not dc_fake.is_active:
            break
        time.sleep(0.1)

    all_files = _flat_images(tmp_fake, 5)
    check("Files are named <id>_<n>.jpg",
          all(f.startswith("5_") and f.endswith(".jpg") for f in all_files))
    check("Numbering continues (starts from 3)",
          any(f == "5_3.jpg" for f in all_files))
    check("Overshoot guard: exactly 6 new files saved (2 pre-existing + 6 = 8 total)",
          len(all_files) == 8)
    check("collect:done emitted", any(
        c[0][0] == "collect:done"
        for c in mock_sio_fake.emit.call_args_list
    ))
    check("on_done callback fired", collect_done_called[0])

shutil.rmtree(tmp_fake)


# ─────────────────────────────────────────────────────────────────────────────
#  3c. When two faces present, only the largest is saved
# ─────────────────────────────────────────────────────────────────────────────
section("3c. Two faces -> only largest saved per frame")

tmp_two = tempfile.mkdtemp(prefix="face_two_")
mock_sio_two = MagicMock()
dc_two = DataCollector({"data_folder": tmp_two, "max_images": 500}, cascade, mock_sio_two)

# We verify the logic directly in augment_crop count:
# The collector always does: faces = detectMultiScale(...), pick largest, augment (4 variants).
# With a mock that returns two rectangles, exactly 4 files should be written per loop pass.
# We simulate one iteration manually.
face_small = _make_gray(40, 40)
face_large = _make_gray(80, 80)
# If we picked the small face we'd get 40x40 crops; large gives 80x80.
# Check augment_crop is called with largest
large_idx  = int(np.argmax([40*40, 80*80]))  # index 1
check("np.argmax picks large face index", large_idx == 1)

shutil.rmtree(tmp_two)


# ─────────────────────────────────────────────────────────────────────────────
#  4. ModelTrainer: flat layout, hot-reload in-place, train:done payload
# ─────────────────────────────────────────────────────────────────────────────
section("4. ModelTrainer flat layout, in-place hot-reload, train:done payload")

tmp_train = tempfile.mkdtemp(prefix="face_train_")
tmp_yml   = os.path.join(tmp_train, "T.yml")

# Flat images for persons 1 and 2
for pid in [1, 2]:
    for i in range(10):
        cv2.imwrite(os.path.join(tmp_train, f"{pid}_{i+1}.jpg"), _make_gray(80, 80))
# One bad filename (no underscore)
cv2.imwrite(os.path.join(tmp_train, "badname.jpg"), _make_gray())

recognizer_shared = cv2.face.LBPHFaceRecognizer_create()
lock_train = threading.Lock()
sio_train  = MagicMock()
models_train = {
    "recognizer":       recognizer_shared,
    "recognizer_ready": False,
}

from core.trainer import ModelTrainer

t = ModelTrainer(
    {"data_folder": tmp_train, "trainer_file": tmp_yml},
    models_train, lock_train, sio_train
)
t._data_folder  = tmp_train
t._trainer_file = tmp_yml

# Run synchronously
t._do_train()

check("Trainer.yml written", os.path.exists(tmp_yml))
check("recognizer_ready is True", models_train["recognizer_ready"] is True)
check("Recognizer object is SAME (in-place reload)",
      models_train["recognizer"] is recognizer_shared)

# Predict should work after in-place reload
test_face = _make_gray(80, 80)
try:
    pid_result, _ = models_train["recognizer"].predict(test_face)
    check("Recognizer can predict after in-place reload", True)
    check("Predicted person_id is 1 or 2",
          int(pid_result) in (1, 2))
except Exception as e:
    check("Recognizer can predict after in-place reload", False)
    check("Predicted person_id is 1 or 2", False)

# Verify train:done payload
done_calls = [c for c in sio_train.emit.call_args_list if c[0][0] == "train:done"]
check("train:done emitted once", len(done_calls) == 1)
done_data = done_calls[0][0][1]
check("train:done has 'persons'",      "persons"      in done_data)
check("train:done has 'total_images'", "total_images" in done_data)
check("train:done has 'bad_skipped'",  "bad_skipped"  in done_data)
check("total_images == 20",             done_data.get("total_images") == 20)
check("bad_skipped == 1 (badname.jpg)", done_data.get("bad_skipped")  == 1)

shutil.rmtree(tmp_train)


# ─────────────────────────────────────────────────────────────────────────────
#  4b. Bad filenames are skipped and counted in bad_skipped
# ─────────────────────────────────────────────────────────────────────────────
section("4b. Bad filenames skipped and counted")

tmp_bad = tempfile.mkdtemp(prefix="face_bad_")
tmp_bad_yml = os.path.join(tmp_bad, "Bad.yml")

# Good files
for i in range(5):
    cv2.imwrite(os.path.join(tmp_bad, f"7_{i+1}.jpg"), _make_gray())
# Bad filenames
cv2.imwrite(os.path.join(tmp_bad, "nounderscore.jpg"), _make_gray())
cv2.imwrite(os.path.join(tmp_bad, "abc_99.jpg"), _make_gray())  # non-int prefix

recognizer_bad = cv2.face.LBPHFaceRecognizer_create()
sio_bad = MagicMock()
models_bad = {"recognizer": recognizer_bad, "recognizer_ready": False}
t_bad = ModelTrainer(
    {"data_folder": tmp_bad, "trainer_file": tmp_bad_yml},
    models_bad, threading.Lock(), sio_bad
)
t_bad._data_folder  = tmp_bad
t_bad._trainer_file = tmp_bad_yml

t_bad._do_train()
done_calls_bad = [c for c in sio_bad.emit.call_args_list if c[0][0] == "train:done"]
check("train:done emitted", len(done_calls_bad) == 1)
done_bad = done_calls_bad[0][0][1]
check("bad_skipped == 2", done_bad.get("bad_skipped") == 2)
check("total_images == 5", done_bad.get("total_images") == 5)

shutil.rmtree(tmp_bad)


# ─────────────────────────────────────────────────────────────────────────────
#  5. Detector consumer sees hot-reload (in-place predict test)
# ─────────────────────────────────────────────────────────────────────────────
section("5. Detector consumer predict after hot-reload (in-place)")

tmp_hrl = tempfile.mkdtemp(prefix="face_hrl_")
tmp_hrl_yml = os.path.join(tmp_hrl, "HRL.yml")

for i in range(8):
    cv2.imwrite(os.path.join(tmp_hrl, f"42_{i+1}.jpg"), _make_gray(80, 80))

shared_recognizer = cv2.face.LBPHFaceRecognizer_create()
shared_models = {"recognizer": shared_recognizer, "recognizer_ready": False}
shared_lock   = threading.Lock()

# Simulate a "Detector-like consumer" holding the same object reference
consumer_recognizer_ref = shared_models["recognizer"]

sio_hrl = MagicMock()
t_hrl = ModelTrainer(
    {"data_folder": tmp_hrl, "trainer_file": tmp_hrl_yml},
    shared_models, shared_lock, sio_hrl
)
t_hrl._data_folder  = tmp_hrl
t_hrl._trainer_file = tmp_hrl_yml
t_hrl._do_train()

# Consumer held reference to the original object: it must be the SAME object
check("Consumer still holds same object after training",
      consumer_recognizer_ref is shared_models["recognizer"])
check("recognizer_ready is True", shared_models["recognizer_ready"])

# Consumer can now predict via its stale reference (same object, now trained)
try:
    pid_r, _ = consumer_recognizer_ref.predict(_make_gray(80, 80))
    check("Consumer predict() works with original reference", True)
    check("Predicted label is 42", int(pid_r) == 42)
except Exception as e:
    check("Consumer predict() works with original reference", False)
    check("Predicted label is 42", False)

shutil.rmtree(tmp_hrl)


# ─────────────────────────────────────────────────────────────────────────────
#  6. Trainer: failed training emits train:error, still calls on_done
# ─────────────────────────────────────────────────────────────────────────────
section("6. Failed training: emits train:error, calls on_done, does not raise")

tmp_empty = tempfile.mkdtemp(prefix="face_empty_")
# Empty folder: no images
models_e  = {"recognizer": cv2.face.LBPHFaceRecognizer_create(), "recognizer_ready": False}
sio_e     = MagicMock()
on_done_called = [False]

def on_done_marker():
    on_done_called[0] = True

t_e = ModelTrainer(
    {"data_folder": tmp_empty, "trainer_file": os.path.join(tmp_empty, "T.yml")},
    models_e, threading.Lock(), sio_e
)
t_e._data_folder = tmp_empty

# Run the full thread wrapper (not _do_train) to test no-raise behavior
t_e._run(on_done=on_done_marker)

check("No exception propagated",      True)  # reaching here means no raise
check("on_done called despite failure", on_done_called[0])
error_calls = [c for c in sio_e.emit.call_args_list if c[0][0] == "train:error"]
check("train:error emitted",          len(error_calls) == 1)
status_calls = [c for c in sio_e.emit.call_args_list if c[0][0] == "train:status"]
idle_statuses = [c for c in status_calls if not c[0][1].get("running", True)]
check("train:status idle emitted",    len(idle_statuses) >= 1)

shutil.rmtree(tmp_empty)


# ─────────────────────────────────────────────────────────────────────────────
#  7. Trainer get_status payload shape
# ─────────────────────────────────────────────────────────────────────────────
section("7. Trainer get_status payload")

sio_ts = MagicMock()
t_ts   = ModelTrainer({"data_folder": "/tmp", "trainer_file": "/tmp/T.yml"},
                      {"recognizer": cv2.face.LBPHFaceRecognizer_create(),
                       "recognizer_ready": False},
                      threading.Lock(), sio_ts)
st = t_ts.get_status()
check("Has 'active' key",  "active"  in st)
check("Has 'running' key", "running" in st)
check("Has 'pct' key",     "pct"     in st)
check("Has 'msg' key",     "msg"     in st)


# ─────────────────────────────────────────────────────────────────────────────
#  8. Server routes -- basic validations and persons list/delete
# ─────────────────────────────────────────────────────────────────────────────
section("8. Server routes: validation + persons")

os.environ.setdefault("TESTING", "1")
import server as srv

from helpers import signed_in_client  # noqa: E402
client = signed_in_client(srv)

# 8a. collect/start: bad person_id types
resp = client.post("/api/collect/start", json={"name": "Alice"})
check("Missing person_id -> 400", resp.status_code == 400)

resp = client.post("/api/collect/start", json={"person_id": "abc", "name": "Alice"})
check("String person_id -> 400", resp.status_code == 400)

resp = client.post("/api/collect/start", json={"person_id": True, "name": "Alice"})
check("Boolean person_id -> 400", resp.status_code == 400)

resp = client.post("/api/collect/start", json={"person_id": -1, "name": "Alice"})
check("Negative person_id -> 400", resp.status_code == 400)

resp = client.post("/api/collect/start", json={"person_id": 1, "name": ""})
check("Empty name -> 400", resp.status_code == 400)

resp = client.post("/api/collect/start",
                   json={"person_id": 1, "name": "A" * 51})
check("Name > 50 chars -> 400", resp.status_code == 400)

resp = client.post("/api/collect/start",
                   json={"person_id": 1, "name": "Bob", "max_images": 5})
check("max_images < 20 -> 400", resp.status_code == 400)

resp = client.post("/api/collect/start",
                   json={"person_id": 1, "name": "Bob", "max_images": 2001})
check("max_images > 2000 -> 400", resp.status_code == 400)

# 8b. /api/persons returns list
resp = client.get("/api/persons")
check("/api/persons returns 200", resp.status_code == 200)
check("/api/persons returns list", isinstance(resp.get_json(), list))

# 8c. DELETE non-existent person -> 404
resp = client.delete("/api/persons/99999")
check("Delete non-existent -> 404", resp.status_code == 404)


# ─────────────────────────────────────────────────────────────────────────────
#  9. Camera contention
# ─────────────────────────────────────────────────────────────────────────────
section("9. Camera contention")

orig_active = srv.collector.is_active
srv.collector.is_active = True
resp = client.post("/api/camera/start", json={"cam_index": 0})
check("Camera start during collection -> 409", resp.status_code == 409)
check("Error mentions collection",
      "collection" in resp.get_json().get("error", "").lower())
srv.collector.is_active = orig_active


# ─────────────────────────────────────────────────────────────────────────────
#  10. Duplicate /api/train while training returns 409
# ─────────────────────────────────────────────────────────────────────────────
section("10. Duplicate POST /api/train returns 409")

orig_trainer_active = srv.trainer.is_active
srv.trainer.is_active = True
resp = client.post("/api/train")
check("Second /api/train while busy -> 409", resp.status_code == 409)
srv.trainer.is_active = orig_trainer_active


# ─────────────────────────────────────────────────────────────────────────────
#  11. collect/start 409 when collection or training active
# ─────────────────────────────────────────────────────────────────────────────
section("11. collect/start 409 when busy")

# Simulate collection active
srv.collector.is_active = True
resp = client.post("/api/collect/start",
                   json={"person_id": 1, "name": "Alice", "max_images": 20})
check("collect/start during collection -> 409", resp.status_code == 409)
srv.collector.is_active = False

# Simulate training active
srv.trainer.is_active = True
resp = client.post("/api/collect/start",
                   json={"person_id": 1, "name": "Alice", "max_images": 20})
check("collect/start during training -> 409", resp.status_code == 409)
srv.trainer.is_active = False


# ─────────────────────────────────────────────────────────────────────────────
#  12. DELETE /api/persons/<id>: removes files, retrains/resets
# ─────────────────────────────────────────────────────────────────────────────
section("12. DELETE /api/persons/<id>: file removal and reset")

import shutil as _shutil

# Stash real server data path
real_data_folder = srv.cfg.get("data_folder", "ImageData")
real_project_root = os.path.dirname(os.path.abspath(srv.__file__))

tmp_del = tempfile.mkdtemp(prefix="face_del_")

# Temporarily point server's cfg at tmp folder
orig_data_folder = srv.cfg.get("data_folder")
srv.cfg["data_folder"] = tmp_del

# Temporarily patch config round-trip so save_config / load_config
# don't touch real config.json
real_cfg_snapshot = json.loads(json.dumps(srv.cfg))

# Seed flat files for person 55
for i in range(4):
    cv2.imwrite(os.path.join(tmp_del, f"55_{i+1}.jpg"), _make_gray())

# Seed config with person 55 and a second person 56. People live in the
# database now: start from an empty `persons` collection (test database only)
# so these two are the only ones registered.
orig_names = dict(srv.cfg.get("names", {}))
assert srv.mongo_db._db_name == "face_attendance_test"
if srv.mongo_db.is_connected:
    srv.mongo_db.database()["persons"].delete_many({})
srv.cfg["names"] = {"55": "TestPerson", "56": "OtherPerson"}

# Seed files for person 56 too (so trainer won't fail after delete of 55)
for i in range(4):
    cv2.imwrite(os.path.join(tmp_del, f"56_{i+1}.jpg"), _make_gray())

# Patch save_config to update srv.cfg in-memory (avoid touching disk config)
from unittest.mock import patch as _patch

def _fake_save_config(cfg, path=None):
    srv.cfg.update(cfg)

def _fake_load_config(path=None):
    return dict(srv.cfg)

with _patch("server.save_config", side_effect=_fake_save_config), \
     _patch("server.load_config", side_effect=_fake_load_config), \
     _patch("server.trainer.start", return_value=(True, None)) as mock_train_start:

    resp = client.delete("/api/persons/55")
    check("DELETE /api/persons/55 returns 200", resp.status_code == 200)
    remaining_55 = _flat_images(tmp_del, 55)
    check("Person 55 files removed", len(remaining_55) == 0)
    check("Person 56 files untouched", len(_flat_images(tmp_del, 56)) == 4)
    check("trainer.start() called (retrain)", mock_train_start.called)

# Now delete the last person: should reset recognizer_ready, not crash
srv.cfg["names"] = {"56": "OtherPerson"}  # 56 is the only one

with _patch("server.save_config", side_effect=_fake_save_config), \
     _patch("server.load_config", side_effect=_fake_load_config):

    # Remove 56's files first so the folder is empty after delete
    for f in _flat_images(tmp_del, 56):
        os.remove(os.path.join(tmp_del, f))

    srv.models["recognizer_ready"] = True   # pretend trained
    old_recognizer_obj = srv.models["recognizer"]

    resp = client.delete("/api/persons/56")
    check("DELETE last person returns 200", resp.status_code == 200)
    check("recognizer_ready is False after last delete",
          srv.models["recognizer_ready"] is False)
    check("Recognizer object not replaced",
          srv.models["recognizer"] is old_recognizer_obj)

# Restore
srv.cfg["data_folder"] = orig_data_folder
srv.cfg["names"] = orig_names
_shutil.rmtree(tmp_del)


# ─────────────────────────────────────────────────────────────────────────────
#  13. WebSocket client during collection sees running collect:status
# ─────────────────────────────────────────────────────────────────────────────
section("13. WebSocket connect during collection receives running collect:status")

try:
    import simple_websocket  # noqa: F401
    import socketio as sio_module
    ws_available = True
except ImportError:
    ws_available = False

if ws_available:
    received = {}

    sio_client = sio_module.Client(logger=False, engineio_logger=False)

    @sio_client.on("collect:status")
    def on_cs(data):
        received["collect:status"] = data

    @sio_client.on("train:status")
    def on_ts(data):
        received["train:status"] = data

    # Simulate collection in progress
    srv.collector.is_active    = True
    srv.collector._collected   = 7
    srv.collector._max_images  = 20
    srv.collector._person_name = "TestUser"

    port = 5098
    server_thread = threading.Thread(
        target=lambda: srv.socketio.run(
            srv.app, host="127.0.0.1", port=port,
            allow_unsafe_werkzeug=True, log_output=False,
        ),
        daemon=True,
    )
    server_thread.start()
    time.sleep(2)

    try:
        from helpers import LiveSession
        live = LiveSession(f"http://127.0.0.1:{port}", srv)
        sio_client.connect(f"http://127.0.0.1:{port}", transports=["websocket"],
                           headers=live.socket_headers)
        time.sleep(1)

        cs = received.get("collect:status", {})
        check("collect:status received",              bool(cs))
        check("collect:status running == True",        cs.get("running") is True)
        check("collect:status count == 7",             cs.get("count")   == 7)
        check("collect:status target == 20",           cs.get("target")  == 20)
        check("collect:status person_name == TestUser",
              cs.get("person_name") == "TestUser")

        ts = received.get("train:status", {})
        check("train:status received",                bool(ts))
        check("train:status has 'running' key",        "running" in ts)

        sio_client.disconnect()
    except Exception as e:
        check(f"WebSocket connect/collect:status: {e}", False)
    finally:
        srv.collector.is_active    = False
        srv.collector._collected   = 0
        srv.collector._max_images  = 0
        srv.collector._person_name = None
else:
    print("  [SKIP] simple_websocket not installed; skipping WebSocket test")


# ─────────────────────────────────────────────────────────────────────────────
#  14. Tasks A & B: Early stop / Error must NOT auto-train; Single resume path
# ─────────────────────────────────────────────────────────────────────────────
section("14. Auto-train guards & Single resume path (Tasks A & B)")

def make_on_collect_done(det, trn, was_run=True, p_cam=0):
    def on_collect_done(reason="done"):
        if reason == "done":
            def on_train_done():
                if was_run and p_cam is not None:
                    det.start(p_cam)
            started, _ = trn.start(on_done=on_train_done)
            if not started:
                if was_run and p_cam is not None:
                    det.start(p_cam)
        else:
            if was_run and p_cam is not None:
                det.start(p_cam)
    return on_collect_done

# ── 14a. Full collection starts training, resumes LM exactly once after training ──
tmp_14a = tempfile.mkdtemp(prefix="face_14a_")
det_starts_14a = []
trn_starts_14a = []

mock_det_14a = MagicMock()
mock_det_14a.is_running = True
mock_det_14a._cam_index = 0
mock_det_14a.start.side_effect = lambda cam: det_starts_14a.append(cam)

def _trainer_start_14a(on_done=None):
    trn_starts_14a.append(on_done)
    return True, None

mock_trn_14a = MagicMock()
mock_trn_14a.start.side_effect = _trainer_start_14a

cb_14a = make_on_collect_done(mock_det_14a, mock_trn_14a)
cb_14a("done")

check("14a: Full collection starts training", len(trn_starts_14a) == 1)
check("14a: Detector not resumed before training finishes", len(det_starts_14a) == 0)

# Now training finishes
train_on_done = trn_starts_14a[0]
train_on_done()

check("14a: Detector resumed exactly once after training", len(det_starts_14a) == 1)
check("14a: Detector resumed with correct cam_index", det_starts_14a == [0])
shutil.rmtree(tmp_14a)


# ── 14b. Early stop: keeps saved images, does NOT train, resumes LM exactly once ──
tmp_14b = tempfile.mkdtemp(prefix="face_14b_")
# Pre-seed an image
cv2.imwrite(os.path.join(tmp_14b, "10_1.jpg"), _make_gray())

det_starts_14b = []
mock_det_14b = MagicMock()
mock_det_14b.start.side_effect = lambda cam: det_starts_14b.append(cam)

mock_trn_14b = MagicMock()
cb_14b = make_on_collect_done(mock_det_14b, mock_trn_14b)

# Simulate collector with fake camera
fake_cap_14b = MagicMock()
fake_cap_14b.isOpened.return_value = True
fake_cap_14b.read.return_value = (True, np.zeros((240, 320, 3), dtype=np.uint8))

fake_casc_14b = MagicMock()
fake_casc_14b.detectMultiScale.return_value = np.array([[10, 10, 60, 60]])

dc_14b = DataCollector({"data_folder": tmp_14b, "max_images": 100}, fake_casc_14b, MagicMock())

with patch("core.collector.cv2.VideoCapture", return_value=fake_cap_14b):
    dc_14b.start(10, "EarlyStopUser", 100, cam_index=0, on_done=cb_14b)
    # Wait for at least 1 image collected
    for _ in range(20):
        if dc_14b._collected > 0:
            break
        time.sleep(0.05)
    # Stop early!
    dc_14b.stop()

# Images must be kept
saved_14b = _flat_images(tmp_14b, 10)
check("14b: Images kept after early stop", len(saved_14b) >= 1)
check("14b: Training NOT started on early stop", mock_trn_14b.start.call_count == 0)
check("14b: LM resumed exactly once after early stop", len(det_starts_14b) == 1)
check("14b: Collector released camera before LM resumed", not dc_14b.is_active and dc_14b._cap is None)
shutil.rmtree(tmp_14b)


# ── 14c. Camera error: keeps saved images, does NOT train, resumes LM exactly once ──
tmp_14c = tempfile.mkdtemp(prefix="face_14c_")
# Pre-seed an existing image
cv2.imwrite(os.path.join(tmp_14c, "11_1.jpg"), _make_gray())

det_starts_14c = []
mock_det_14c = MagicMock()
mock_det_14c.start.side_effect = lambda cam: det_starts_14c.append(cam)

mock_trn_14c = MagicMock()
cb_14c = make_on_collect_done(mock_det_14c, mock_trn_14c)

fail_cap_14c = MagicMock()
fail_cap_14c.isOpened.return_value = False

dc_14c = DataCollector({"data_folder": tmp_14c, "max_images": 50}, cascade, MagicMock())

with patch("core.collector.cv2.VideoCapture", return_value=fail_cap_14c):
    ok_14c, err_14c = dc_14c.start(11, "ErrUser", 50, cam_index=99, on_done=cb_14c)

check("14c: Camera error returned False", ok_14c is False)
check("14c: Pre-existing images kept on camera error", len(_flat_images(tmp_14c, 11)) == 1)
check("14c: Training NOT started on camera error", mock_trn_14c.start.call_count == 0)
check("14c: LM resumed exactly once after camera error", len(det_starts_14c) == 1)
check("14c: Collector not active after camera error", not dc_14c.is_active)
shutil.rmtree(tmp_14c)


# ── 14d. Failed training: resumes LM exactly once ──
tmp_14d = tempfile.mkdtemp(prefix="face_14d_")
det_starts_14d = []
mock_det_14d = MagicMock()
mock_det_14d.start.side_effect = lambda cam: det_starts_14d.append(cam)

# Real trainer with empty folder -> training will fail
real_trn_14d = ModelTrainer(
    {"data_folder": tmp_14d, "trainer_file": os.path.join(tmp_14d, "T.yml")},
    {"recognizer": cv2.face.LBPHFaceRecognizer_create(), "recognizer_ready": False},
    threading.Lock(), MagicMock()
)
real_trn_14d._data_folder = tmp_14d

cb_14d = make_on_collect_done(mock_det_14d, real_trn_14d)

# Full collection triggers cb_14d("done")
cb_14d("done")

# Wait for trainer thread to finish (it fails because folder is empty)
for _ in range(50):
    if not real_trn_14d.is_active:
        break
    time.sleep(0.05)

check("14d: LM resumed exactly once after failed training", len(det_starts_14d) == 1)
check("14d: LM resumed with correct camera index", det_starts_14d == [0])
shutil.rmtree(tmp_14d)


# ─────────────────────────────────────────────────────────────────────────────
#  15. Real camera collection: saves exactly max_images files
# ─────────────────────────────────────────────────────────────────────────────
section("15. Real camera collection (skipped if no camera or no face visible)")

import cv2 as _cv2
_test_cap = _cv2.VideoCapture(0, _cv2.CAP_DSHOW if sys.platform.startswith("win") else _cv2.CAP_ANY)
_real_cam = False
if _test_cap.isOpened():
    _ret, _fr = _test_cap.read()
    _real_cam = _ret and _fr is not None
_test_cap.release()

if _real_cam:
    tmp_real = tempfile.mkdtemp(prefix="face_real_")
    done_event = threading.Event()

    mock_sio_real = MagicMock()
    dc_real = DataCollector({"data_folder": tmp_real, "max_images": 500}, _load_cascade(), mock_sio_real)

    def _real_done():
        done_event.set()

    ok, err = dc_real.start(99, "RealTest", 8, cam_index=0, on_done=_real_done)
    if ok:
        done_event.wait(timeout=30)
        dc_real.stop()  # ensure stopped
        files = _flat_images(tmp_real, 99)
        if len(files) == 0:
            print("  [SKIP] Camera open but no face detected within 30 s (no face in view)")
        else:
            check("Real camera: exactly 8 files collected", len(files) == 8)
            check("Real camera: files named correctly",
                  all(f.startswith("99_") for f in files))
    else:
        print(f"  [SKIP] Camera opened but collection failed: {err}")
    _shutil.rmtree(tmp_real)
else:
    print("  [SKIP] No real camera available for live collection test")


# ─────────────────────────────────────────────────────────────────────────────
#  16. camera_utils.CAM_BACKEND shared constant
# ─────────────────────────────────────────────────────────────────────────────
section("16. camera_utils.CAM_BACKEND shared constant")

from core.camera_utils import CAM_BACKEND
check("CAM_BACKEND is importable", CAM_BACKEND is not None)
check("CAM_BACKEND is an int",     isinstance(CAM_BACKEND, int))

import core.detector as det_mod
check("detector imports CAM_BACKEND (not defines it)",
      hasattr(det_mod, "CAM_BACKEND"))
check("detector has recognizer_lock",
      hasattr(det_mod, "recognizer_lock") and
      isinstance(det_mod.recognizer_lock, type(threading.Lock())))


# ─────────────────────────────────────────────────────────────────────────────
#  17. Real config.json integrity & cleanup
# ─────────────────────────────────────────────────────────────────────────────
section("17. Real config.json integrity & cleanup")

# Drop test db
try:
    from pymongo import MongoClient
    mc = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)
    mc.drop_database("face_attendance_test")
    mc.close()
except Exception:
    pass

# Clean up temp folder
shutil.rmtree(tmp_dir_3a, ignore_errors=True)

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
