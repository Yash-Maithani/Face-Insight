"""
server.py
Flask + Socket.IO entry point: routes, socket handlers and app wiring.

Run:  python server.py   then open http://localhost:5000
"""

import hashlib
import logging
import os
import tempfile
from datetime import datetime, timedelta

import re
import shutil

import cv2
from flask import Flask, Response, jsonify, request, render_template, send_file, after_this_request
from flask_login import current_user
from flask_socketio import SocketIO, emit

# Project imports
from core.model_loader import (
    load_config, save_config, CONFIG_PATH,
    load_haar_cascade, load_recognizer, load_emotion_model,
    build_emotion_labels,
)
from db.mongo_client import MongoAttendanceDB, mask_mongo_uri
from db.firebase_client import FirebaseClient
from attendance.engine import AttendanceEngine
from attendance.exporter import export_to_excel, export_to_csv
from core.detector import Detector, recognizer_lock
from core.collector import DataCollector
from core.trainer import ModelTrainer
from auth.audit import AuditLog
from auth.store import UserStore, prefs_of
from auth.web import fresh_required, init_auth, load_secret_key
from attendance.reports import RangeError, parse_range
from people.migration import migrate
from people.registry import PersonError, PersonRegistry
from people.web import create_blueprint as people_blueprint

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Camera enumeration ────────────────────────────────────────────────
from core.camera_utils import CAM_BACKEND


def _detect_cameras(max_index=3):
    """Probe camera indices with real frame reads. Run once at startup."""
    found = []
    for idx in range(max_index):
        cap = cv2.VideoCapture(idx, CAM_BACKEND)
        try:
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    found.append(idx)
        finally:
            cap.release()
    return found or [0]


# ── Bootstrap ─────────────────────────────────────────────────────────────────
cfg = load_config()

# Local, never-committed files (accounts, secret key, offline audit entries)
# live next to the config file in use, so tests with a temporary config get
# their own instance folder automatically.
CONFIG_FILE = os.path.abspath(os.environ.get("FACE_WEB_CONFIG", CONFIG_PATH))
INSTANCE_DIR = os.path.join(os.path.dirname(CONFIG_FILE), "instance")

# Models
cascade = load_haar_cascade()
recognizer, recognizer_ready = load_recognizer(cfg["trainer_file"])
emotion_model = load_emotion_model(cfg["emotion_model"])
emotion_labels = build_emotion_labels(cfg)

models = {
    "cascade": cascade,
    "recognizer": recognizer,
    "recognizer_ready": recognizer_ready,
    "emotion_model": emotion_model,
    "emotion_labels": emotion_labels,
}

# DB
mongo_db = MongoAttendanceDB(cfg["mongodb_uri"], cfg["mongodb_db"])
firebase = FirebaseClient(cfg.get("firebase_credentials", ""), cfg.get("firebase_url", ""))

# Engine (names are set from the people registry below)
engine = AttendanceEngine(cfg, mongo_db, firebase)

# Flask + SocketIO
app = Flask(
    __name__,
    template_folder=os.path.join("web", "templates"),
    static_folder=os.path.join("web", "static"),
)
app.config["SECRET_KEY"] = load_secret_key(INSTANCE_DIR)
# Same-origin only (the default): other sites cannot open a socket to this app
socketio = SocketIO(app, async_mode="threading")

# Accounts, the sign-in gate and the audit log
users = UserStore(INSTANCE_DIR)
audit_log = AuditLog(lambda: mongo_db, INSTANCE_DIR)
audit_event = init_auth(app, users, audit_log)

# Detector
detector = Detector(cfg, models, engine, socketio)

# Collector + Trainer
collector = DataCollector(cfg, cascade, socketio)
trainer = ModelTrainer(cfg, models, recognizer_lock, socketio)

# Camera list (cached at startup)
logger.info("Probing cameras...")
available_cameras = _detect_cameras()
logger.info("Available cameras: %s", available_cameras)

# Registered people. The `persons` collection is the source of truth; the
# config keeps a copy of the names so faces still get names while the
# database is down. load_config/save_config are looked up at call time.
BACKUP_DIR = os.path.join(INSTANCE_DIR, "backups")
people = PersonRegistry(
    lambda: mongo_db,
    lambda: load_config(),
    lambda current: save_config(current),
    on_change=lambda names: detector.update_names(names),
)


def _run_migration():
    """Move people into the database and fix old Unknown records, once.
    Backs up first; does nothing when there is nothing to change."""
    try:
        report = migrate(mongo_db, people, BACKUP_DIR)
    except Exception as exc:  # the backup was written before any change
        logger.error("Migration failed: %s", exc)
        report = None
    detector.update_names(people.names())
    return report


_run_migration()


def _image_counts():
    """{person_id: number of face images} from the flat files in the data folder."""
    current = load_config()
    data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), current.get("data_folder", "ImageData"))
    counts = {}
    if os.path.isdir(data_path):
        for fname in os.listdir(data_path):
            if not fname.lower().endswith((".jpg", ".png", ".jpeg")):
                continue
            if os.path.isdir(os.path.join(data_path, fname)):
                continue
            stem = os.path.splitext(fname)[0]
            if "_" in stem:
                try:
                    pid = int(stem.split("_", 1)[0])
                except ValueError:
                    continue
                counts[pid] = counts.get(pid, 0) + 1
    return counts


app.register_blueprint(people_blueprint(
    lambda: mongo_db, people, lambda: load_config(),
    lambda *a, **k: audit_event(*a, **k), _image_counts,
))


# ── Asset cache-busting ────────────────────────────────────────────
def static_fingerprint(root):
    """Short hash of every file under `root` (names and contents).

    It changes only when a file changes, so browsers can keep cached copies
    between requests and still fetch new ones after an update.
    """
    digest = hashlib.sha1()
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            digest.update(os.path.relpath(path, root).replace(os.sep, "/").encode("utf-8"))
            with open(path, "rb") as f:
                digest.update(f.read())
    return digest.hexdigest()[:12]


ASSET_VERSION = static_fingerprint(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))


@app.context_processor
def inject_asset_version():
    return {"asset_v": ASSET_VERSION}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", me=current_user.username, prefs=prefs_of(current_user.record))


@app.route("/video_feed")
def video_feed():
    return Response(
        detector.generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/camera/list")
def camera_list():
    return jsonify({"cameras": available_cameras})


@app.route("/api/camera/start", methods=["POST"])
def camera_start():
    # Camera contention: collector owns the camera while active
    if collector.is_active:
        return jsonify({"error": "Camera is in use by face collection"}), 409
    data = request.get_json(force=True)
    cam_index = data.get("cam_index", 0)
    detector.start(cam_index)
    # camera:status is emitted inside detector.start()
    audit_event("camera.started", target=f"camera {cam_index}")
    return jsonify({"ok": True})


@app.route("/api/camera/stop", methods=["POST"])
def camera_stop():
    detector.stop()
    # camera:status is emitted inside detector.stop()
    audit_event("camera.stopped")
    return jsonify({"ok": True})


def _parse_day_or_range(req):
    """(from, to, error). ?from=&to= for a range, ?date= for one day, today by default."""
    if req.args.get("from") or req.args.get("to"):
        try:
            date_from, date_to = parse_range(req.args.get("from"), req.args.get("to"))
        except RangeError as exc:
            return None, None, str(exc)
        return date_from, date_to, None
    date_str, err = _parse_date_arg(req)
    return date_str, date_str, err


def _parse_date_arg(req):
    date_str = req.args.get("date")
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d"), None
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str, None
    except ValueError:
        return None, "Invalid date format. Expected YYYY-MM-DD"


@app.route("/api/attendance", methods=["GET"])
def attendance_list():
    date_from, date_to, err = _parse_day_or_range(request)
    if err:
        return jsonify({"error": err}), 400
    records = mongo_db.get_logs_between(date_from, date_to)
    for r in records:
        if "created_at" in r and hasattr(r["created_at"], "isoformat"):
            r["created_at"] = r["created_at"].isoformat()
    return jsonify(records)


_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")


def _remove_snapshot(record_id):
    """Delete a record's face crop if it has one. Never raises."""
    if not record_id or not str(record_id).isalnum():
        return
    try:
        os.remove(os.path.join(_SNAPSHOT_DIR, f"{record_id}.jpg"))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove snapshot %s: %s", record_id, exc)


def _announce_deleted(ids, date_str):
    socketio.emit("attendance:deleted", {"ids": ids, "date": date_str})
    socketio.emit("stats:update", _stats_payload())


@app.route("/api/attendance/<record_id>", methods=["DELETE"])
def attendance_delete(record_id):
    """Delete one record, its snapshot, and the cooldown it started."""
    if not record_id or not record_id.isalnum():
        return jsonify({"error": "Invalid record ID"}), 400
    if not mongo_db.is_connected:
        return jsonify({"error": "Database offline. Nothing was deleted."}), 503

    record = mongo_db.delete_log(record_id)
    if record is None:
        if not mongo_db.is_connected:
            return jsonify({"error": "Database offline. Nothing was deleted."}), 503
        return jsonify({"error": "Record not found"}), 404

    _remove_snapshot(record["id"])
    engine.forget_record(record)
    _announce_deleted([record["id"]], record.get("date"))
    audit_event("attendance.deleted", target=record["id"], details={
        "name": record.get("name"), "status": record.get("status"),
        "date": record.get("date"), "time_in": record.get("time_in"),
    })
    return jsonify({"ok": True, "deleted": 1, "id": record["id"]})


@app.route("/api/attendance", methods=["DELETE"])
@fresh_required
def attendance_delete_today():
    """Delete every record dated today, their snapshots, and all cooldowns.

    The caller must name today's date. That stops a page left open past
    midnight from clearing a different day than the one it showed, and no
    other day can be cleared through this route.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if request.args.get("date") != today:
        return jsonify({"error": f"Only today's records can be cleared. Pass date={today}."}), 400
    if not mongo_db.is_connected:
        return jsonify({"error": "Database offline. Nothing was deleted."}), 503

    ids = mongo_db.delete_logs_by_date(today)
    if ids is None:
        return jsonify({"error": "Database offline. Nothing was deleted."}), 503

    for rid in ids:
        _remove_snapshot(rid)
    engine.reset_cooldowns()
    _announce_deleted(ids, today)
    audit_event("attendance.cleared_today", target=today, details={"deleted": len(ids)})
    logger.info("Cleared %d attendance record(s) for %s", len(ids), today)
    return jsonify({"ok": True, "deleted": len(ids), "date": today})


@app.route("/api/attendance/manual", methods=["POST"])
def attendance_manual():
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ("Present", "Late"):
        return jsonify({"error": "Status must be Present or Late"}), 400

    name = data.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return jsonify({"error": "Name is required and must be a non-empty string"}), 400
    name = name.strip()

    person_id = data.get("person_id")
    if person_id is None or not isinstance(person_id, int) or isinstance(person_id, bool):
        return jsonify({"error": "person_id must be an integer"}), 400

    # Only registered people, under their registered name
    person = people.get(person_id)
    if person is None:
        return jsonify({"error": f"No one is registered with ID {person_id}."}), 404
    if person["name"].casefold() != " ".join(name.split()).casefold():
        return jsonify({"error": f"ID {person_id} is {person['name']}, not {name}."}), 400
    name = person["name"]

    record = engine.manual_mark(person_id, name, status)
    if not record:
        return jsonify({"error": "Failed to save attendance record"}), 503

    clean = {k: v for k, v in record.items() if k != "_id"}
    for k, v in clean.items():
        if hasattr(v, "isoformat"):
            clean[k] = v.isoformat()

    socketio.emit("attendance:new", clean)
    socketio.emit("stats:update", _stats_payload())
    audit_event("attendance.manual_mark", target=clean.get("id"), details={
        "person_id": person_id, "name": name, "status": status,
    })

    return jsonify({"ok": True, "record": clean})


@app.route("/api/attendance/<record_id>/snapshot", methods=["GET"])
def attendance_snapshot(record_id):
    if not record_id or not record_id.isalnum():
        return jsonify({"error": "Invalid snapshot ID"}), 400

    snap_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "snapshots", f"{record_id}.jpg"
    )
    if not os.path.exists(snap_path):
        return jsonify({"error": "Snapshot not found"}), 404

    return send_file(snap_path, mimetype="image/jpeg")


@app.route("/api/export/excel", methods=["GET"])
def export_excel():
    date_from, date_to, err = _parse_day_or_range(request)
    if err:
        return jsonify({"error": err}), 400
    records = sorted(mongo_db.get_logs_between(date_from, date_to),
                     key=lambda r: (r.get("date") or "", r.get("time_in") or ""))
    label = date_from if date_from == date_to else f"{date_from}_to_{date_to}"

    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    export_to_excel(records, tmp_path)

    @after_this_request
    def cleanup(response):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return response

    return send_file(
        tmp_path,
        as_attachment=True,
        download_name=f"attendance_{label}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/export/csv", methods=["GET"])
def export_csv():
    date_from, date_to, err = _parse_day_or_range(request)
    if err:
        return jsonify({"error": err}), 400
    records = sorted(mongo_db.get_logs_between(date_from, date_to),
                     key=lambda r: (r.get("date") or "", r.get("time_in") or ""))
    label = date_from if date_from == date_to else f"{date_from}_to_{date_to}"

    fd, tmp_path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    export_to_csv(records, tmp_path)

    @after_this_request
    def cleanup(response):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return response

    return send_file(
        tmp_path,
        as_attachment=True,
        download_name=f"attendance_{label}.csv",
        mimetype="text/csv",
    )


def _stats_payload():
    """Today's attendance counts + live detector stats + DB reachability.

    ``db_connected`` lets the UI tell "no records" apart from "database offline".
    """
    stats = engine.get_today_stats()
    stats.update(detector.get_stats())
    stats["db_connected"] = mongo_db.is_connected
    return stats


@app.route("/api/stats/today")
def stats_today():
    return jsonify(_stats_payload())


@app.route("/api/status", methods=["GET"])
def status_get():
    return jsonify({
        "db_connected": mongo_db.is_connected,
        # Deleting here does not remove copies already mirrored to Firebase
        "firebase_enabled": bool(firebase.is_enabled),
    })


@app.route("/api/db/reconnect", methods=["POST"])
def db_reconnect():
    """Retry the MongoDB connection with the saved settings. Never returns 500."""
    try:
        mongo_db.reconnect()
    except Exception as exc:
        logger.warning("MongoDB reconnect error: %s", str(exc)[:100])
    connected = mongo_db.is_connected
    if connected:
        _run_migration()
        socketio.emit("stats:update", _stats_payload())
    audit_event("db.reconnect", details={"connected": connected})
    return jsonify({"ok": connected, "db_connected": connected})


_STATS_RANGES = (7, 14, 30)


def _parse_stats_range(req):
    """(from, to, error) for the dashboard: ?from=&to=, or ?days=7|14|30 up to today."""
    if req.args.get("from") or req.args.get("to"):
        try:
            date_from, date_to = parse_range(req.args.get("from"), req.args.get("to"))
        except RangeError as exc:
            return None, None, str(exc)
        return date_from, date_to, None
    days, err = _parse_days_arg(req)
    if err:
        return None, None, err
    today = datetime.now().date()
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat(), None


def _parse_days_arg(req):
    """Optional ?days= for the dashboard range. Defaults to 7."""
    raw = req.args.get("days")
    if raw is None:
        return 7, None
    try:
        days = int(raw)
    except ValueError:
        days = None
    if days not in _STATS_RANGES:
        return None, "days must be one of 7, 14, 30"
    return days, None


@app.route("/api/stats/weekly", methods=["GET"])
def stats_weekly():
    date_from, date_to, err = _parse_stats_range(request)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(mongo_db.get_range_summary(date_from, date_to))


@app.route("/api/stats/emotions", methods=["GET"])
def stats_emotions():
    return jsonify(mongo_db.get_today_emotion_counts())


@app.route("/api/stats/persons", methods=["GET"])
def stats_persons():
    date_from, date_to, err = _parse_stats_range(request)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(mongo_db.get_person_summary_range(date_from, date_to))


# ── Configuration routes ──────────────────────────────────────────

def _validate_time_str(val: str) -> bool:
    if not isinstance(val, str) or len(val) != 5 or val[2] != ":":
        return False
    hh, mm = val[:2], val[3:]
    if not (hh.isdigit() and mm.isdigit()):
        return False
    h, m = int(hh), int(mm)
    return 0 <= h <= 23 and 0 <= m <= 59


def _validate_config(data: dict, current: dict):
    # Check cooldown_minutes
    att = data.get("attendance")
    cooldown = None
    if isinstance(att, dict) and "cooldown_minutes" in att:
        cooldown = att["cooldown_minutes"]
    elif "cooldown_minutes" in data:
        cooldown = data["cooldown_minutes"]

    if cooldown is not None:
        if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 1:
            return False, "cooldown_minutes must be an integer >= 1", "cooldown_minutes"

    # Check times
    late_after = None
    if isinstance(att, dict) and "late_after" in att:
        late_after = att["late_after"]
    elif "late_after" in data:
        late_after = data["late_after"]

    if late_after is not None:
        if not _validate_time_str(late_after):
            return False, "late_after must be a valid HH:MM time", "late_after"

    working_hours_end = None
    if isinstance(att, dict) and "working_hours_end" in att:
        working_hours_end = att["working_hours_end"]
    elif "working_hours_end" in data:
        working_hours_end = data["working_hours_end"]

    if working_hours_end is not None:
        if not _validate_time_str(working_hours_end):
            return False, "working_hours_end must be a valid HH:MM time", "working_hours_end"

    # Compare late_after vs working_hours_end
    eff_late = late_after if late_after is not None else current.get("attendance", {}).get("late_after", "09:30")
    eff_end = working_hours_end if working_hours_end is not None else current.get("attendance", {}).get("working_hours_end", "17:30")
    if _validate_time_str(eff_late) and _validate_time_str(eff_end):
        if eff_late >= eff_end:
            return False, "late_after must be earlier than working_hours_end", "late_after"

    # Working days: weekday numbers, Monday = 0
    working_days = att.get("working_days") if isinstance(att, dict) else None
    if working_days is not None:
        if not isinstance(working_days, list) or not working_days or \
                any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in working_days):
            return False, "working_days must list at least one weekday, 0 (Monday) to 6 (Sunday)", "working_days"

    # Check names
    if "names" in data:
        names_val = data["names"]
        if not isinstance(names_val, dict):
            return False, "names must be a dictionary", "names"
        for k in names_val.keys():
            try:
                int(k)
            except (ValueError, TypeError):
                return False, f"names key '{k}' must be an integer", "names"

    return True, None, None


@app.route("/api/config", methods=["GET"])
def config_get():
    current = load_config()
    # The page gets the URI with any password replaced by ***
    current["mongodb_uri"] = mask_mongo_uri(current.get("mongodb_uri", ""))
    return jsonify(current)


@app.route("/api/config/db_test", methods=["POST"])
def config_db_test():
    """Test MongoDB connection without saving. Never returns 500."""
    try:
        data = request.get_json(force=True) or {}
        uri = data.get("uri", "").strip()
        if not uri:
            return jsonify({"ok": False, "error": "MongoDB URI is required"}), 200

        masked = mask_mongo_uri(uri)
        logger.info("Testing MongoDB connection to %s", masked)
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        client.close()
        return jsonify({"ok": True})
    except Exception as exc:
        err_msg = str(exc)
        err_msg = re.sub(r":([^/@:]+)@", ":***@", err_msg)
        short_msg = err_msg.split("\n")[0][:200]
        return jsonify({"ok": False, "error": short_msg}), 200


@app.route("/api/config", methods=["POST"])
def config_post():
    data = request.get_json(force=True) or {}
    current = load_config()

    ok, err_msg, field = _validate_config(data, current)
    if not ok:
        return jsonify({"error": err_msg, "field": field}), 400

    # Names belong to the people registry. Change them first: if the
    # database is offline, nothing at all is saved.
    names_changed = False
    if "names" in data:
        names_before = people.names()
        try:
            people.replace_names(data.pop("names"))
        except PersonError as exc:
            return jsonify({"error": str(exc), "field": "names"}), exc.status
        names_changed = people.names() != names_before
        current = load_config()

    old_uri = current.get("mongodb_uri")
    old_db = current.get("mongodb_db")
    old_fb_url = current.get("firebase_url")
    old_fb_cred = current.get("firebase_credentials")

    # The page only ever saw the masked URI. Sending it back unchanged means
    # "keep the stored one", password included.
    if "mongodb_uri" in data and "***" in str(data["mongodb_uri"]) \
            and data["mongodb_uri"] == mask_mongo_uri(old_uri or ""):
        data["mongodb_uri"] = old_uri

    # What changed, for the audit log. Secret values are never recorded.
    changed = sorted(k for k, v in data.items() if k != "attendance" and current.get(k) != v)
    if names_changed:
        changed = sorted(changed + ["names"])
    if isinstance(data.get("attendance"), dict):
        changed += sorted("attendance." + k for k, v in data["attendance"].items()
                          if current.get("attendance", {}).get(k) != v)

    if "attendance" in data and isinstance(data["attendance"], dict):
        current.setdefault("attendance", {}).update(data["attendance"])
    for k, v in data.items():
        if k != "attendance":
            current[k] = v

    save_config(current)

    # 1. Update engine config
    engine.update_config(current)

    # 3. Check if Mongo settings changed
    warning = None
    new_uri = current.get("mongodb_uri")
    new_db = current.get("mongodb_db")
    if new_uri != old_uri or new_db != old_db:
        try:
            mongo_db.reconnect(new_uri, new_db)
            if not mongo_db.is_connected:
                warning = f"MongoDB could not connect to {mask_mongo_uri(new_uri)}"
            else:
                _run_migration()
        except Exception as exc:
            warning = f"MongoDB reconnect failed: {str(exc)[:100]}"
            logger.warning("MongoDB reconnect error: %s", warning)

    # 4. Check if Firebase settings changed
    global firebase
    new_fb_url = current.get("firebase_url", "")
    new_fb_cred = current.get("firebase_credentials", "")
    if new_fb_url != old_fb_url or new_fb_cred != old_fb_cred:
        try:
            firebase = FirebaseClient(new_fb_cred, new_fb_url)
            engine._firebase = firebase
        except Exception as exc:
            logger.warning("Firebase rebuild error: %s", exc)

    audit_event("settings.saved", details={"changed": changed})

    current = dict(current)
    current["mongodb_uri"] = mask_mongo_uri(current.get("mongodb_uri", ""))
    resp = {"ok": True, "config": current}
    if warning:
        resp["warning"] = warning
    return jsonify(resp)


# ── Collection + Training routes ────────────────────────────────────

@app.route("/api/collect/start", methods=["POST"])
def collect_start():
    """Start face-image collection."""
    if collector.is_active:
        return jsonify({"error": "Collection already in progress"}), 409
    if trainer.is_active:
        return jsonify({"error": "Training in progress, cannot collect"}), 409

    data       = request.get_json(force=True)
    person_id  = data.get("person_id")
    name       = data.get("name", "").strip()
    max_images = data.get("max_images", cfg.get("max_images", 500))

    # ── Input validation ──────────────────────────────────────────────────
    if person_id is None or not isinstance(person_id, int) or isinstance(person_id, bool):
        return jsonify({"error": "person_id must be an integer"}), 400
    if person_id < 0:
        return jsonify({"error": "person_id must be non-negative"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400
    if len(name) > 50:
        return jsonify({"error": "name must be 50 characters or fewer"}), 400
    consent = data.get("consent")
    try:
        max_images = int(max_images)
    except (TypeError, ValueError):
        return jsonify({"error": "max_images must be an integer"}), 400
    if max_images < 20:
        return jsonify({"error": "max_images must be at least 20"}), 400
    if max_images > 2000:
        return jsonify({"error": "max_images must be at most 2000"}), 400

    # Register before the camera starts: needs consent, and an existing ID
    # must keep its name (this route never renames anyone)
    try:
        person, created = people.register(person_id, name, consent)
    except PersonError as exc:
        return jsonify({"error": str(exc)}), exc.status
    name = person["name"]
    if created:
        audit_event("person.registered", target=person_id, details={"name": name, "consent_at": person["consent_at"]})

    # ── Remember LM state so we can resume after collection + training ────
    was_running = detector.is_running
    prev_cam    = detector._cam_index

    # Stop the live detector so the camera is free
    if detector.is_running:
        detector.stop()

    def on_collect_done(reason="done"):
        """Called when collection ends (success, stop, or camera error).
        Starts training ONLY if reason == "done" (full collection).
        Otherwise resumes the LM directly if it was running.
        """
        if reason == "done":
            def on_train_done():
                if was_running and prev_cam is not None:
                    detector.start(prev_cam)

            started, _ = trainer.start(on_done=on_train_done)
            if not started:
                # If trainer could not start, resume LM immediately so it is not left stopped
                if was_running and prev_cam is not None:
                    detector.start(prev_cam)
        else:
            # Early stop or camera error: do NOT start training, keep saved images, resume LM
            if was_running and prev_cam is not None:
                detector.start(prev_cam)

    cam_index = data.get("cam_index", cfg.get("camera", 0))
    ok, err = collector.start(person_id, name, max_images, cam_index,
                              on_done=on_collect_done)
    if not ok:
        # collector already fired on_done inside start() on camera error
        return jsonify({"error": err}), 500

    audit_event("collection.started", target=person_id, details={"name": name, "max_images": max_images})

    return jsonify({"ok": True, "status": collector.get_status()})


@app.route("/api/collect/stop", methods=["POST"])
def collect_stop():
    """Stop face-image collection early.
    collector.stop() fires on_done, which resumes the LM.
    """
    collector.stop()
    return jsonify({"ok": True})


@app.route("/api/collect/feed")
def collect_feed():
    """MJPEG stream for the collection preview."""
    return Response(
        collector.generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/train", methods=["POST"])
def train_start():
    """Start model training."""
    if trainer.is_active:
        return jsonify({"error": "Training already in progress"}), 409
    if collector.is_active:
        return jsonify({"error": "Collection in progress, cannot train"}), 409

    # Remember which camera the detector was using (if any) so we can resume
    was_running = detector.is_running
    prev_cam    = detector._cam_index

    def on_train_done():
        """Auto-resume Live Monitor after training finishes."""
        if was_running and prev_cam is not None:
            detector.start(prev_cam)

    ok, err = trainer.start(on_done=on_train_done)
    if not ok:
        return jsonify({"error": err}), 409

    detector.update_names(people.names())
    audit_event("training.started")

    return jsonify({"ok": True})


@app.route("/api/persons", methods=["GET"])
def persons_list():
    """Registered people with their image counts (ImageData/<pid>_*.jpg)."""
    counts = _image_counts()
    everyone, _ = people.list()
    return jsonify([
        {
            "person_id": p["person_id"],
            "name": p["name"],
            "image_count": counts.get(p["person_id"], 0),
            "consent_at": p.get("consent_at"),
        }
        for p in everyone
    ])


@app.route("/api/persons/<int:person_id>", methods=["DELETE"])
@fresh_required
def persons_delete(person_id):
    """Delete a person: remove them from the registry, delete their images, retrain.
    Their attendance records are kept."""
    try:
        removed = people.remove(person_id)
    except PersonError as exc:
        return jsonify({"error": str(exc)}), exc.status
    audit_event("person.deleted", target=person_id, details={"name": removed["name"]})

    # Remove flat image files for this person
    current      = load_config()
    project_root = os.path.dirname(os.path.abspath(__file__))
    data_path    = os.path.join(project_root, current.get("data_folder", "ImageData"))
    prefix       = f"{person_id}_"
    if os.path.isdir(data_path):
        for fname in list(os.listdir(data_path)):
            if fname.startswith(prefix) and fname.lower().endswith((".jpg", ".png", ".jpeg")):
                try:
                    os.remove(os.path.join(data_path, fname))
                except OSError:
                    pass

    # Retrain or reset
    if people.names():
        trainer.start()
    else:
        # No persons left: delete Trainer.yml and mark recognizer as unready.
        # Do NOT replace the recognizer object (the detector holds a reference).
        trainer_path = os.path.join(project_root, current.get("trainer_file", "Trainer.yml"))
        if os.path.exists(trainer_path):
            os.remove(trainer_path)
        with recognizer_lock:
            models["recognizer_ready"] = False
        socketio.emit("train:done", {
            "persons": [], "total_images": 0, "bad_skipped": 0,
        })

    return jsonify({"ok": True})


# ── Socket.IO handlers ───────────────────────────────────────────────────────

@socketio.on("connect")
def handle_connect():
    """On every client connect, send current state. Refuse anyone not signed in."""
    if not current_user.is_authenticated:
        return False
    emit("camera:status", {
        "running": detector.is_running,
        "cam_index": detector._cam_index,
    })
    emit("stats:update", _stats_payload())
    # Real statuses for collect / train
    emit("collect:status", collector.get_status())
    emit("train:status", trainer.get_status())


@socketio.on("settings:threshold")
def handle_threshold(data):
    """Returns an ack so the client can confirm the value was saved."""
    value = (data or {}).get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 255:
        return {"ok": False, "error": "Threshold must be a number between 1 and 255"}
    detector.update_threshold(value)
    # Persist
    current = load_config()
    old_value = current.get("confidence_threshold")
    current["confidence_threshold"] = value
    save_config(current)
    audit_event("settings.threshold", details={"from": old_value, "to": value})
    logger.info("Threshold updated to %s", value)
    return {"ok": True, "value": value}


@socketio.on("settings:auto_attend")
def handle_auto_attend(data):
    """Returns an ack so the client can confirm the value was saved."""
    enabled = (data or {}).get("enabled", True)
    if not isinstance(enabled, bool):
        return {"ok": False, "error": "enabled must be true or false"}
    detector.set_auto_attend(enabled)
    # Persist
    current = load_config()
    current.setdefault("attendance", {})["auto_attend"] = enabled
    save_config(current)
    audit_event("settings.auto_attend", details={"enabled": enabled})
    logger.info("Auto-attend set to %s", enabled)
    return {"ok": True, "enabled": enabled}


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
