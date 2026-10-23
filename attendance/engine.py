"""
attendance/engine.py
Core attendance logic:  cooldown tracking, Present/Late/Unknown status,
auto-attend gating, manual marks, and today-stats aggregation.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class AttendanceEngine:
    """
    Stateful attendance controller.

    Parameters
    ----------
    cfg : dict
        Full application config (``config.json``).
    db : MongoAttendanceDB
        MongoDB wrapper instance.
    firebase : FirebaseClient
        Firebase mirror instance.
    clock : callable, optional
        Returns the current ``datetime.datetime``.  Default ``datetime.now``.
        Injecting a fake clock makes time-dependent logic testable.
    """

    def __init__(self, cfg: dict, db, firebase, *, clock=None):
        self._db = db
        self._firebase = firebase
        self._clock = clock or datetime.now

        # Attendance sub-config
        att = cfg.get("attendance", {})
        self._cooldown_minutes = att.get("cooldown_minutes", 60)
        self._late_after = att.get("late_after", "09:30")
        self._working_hours_end = att.get("working_hours_end", "17:30")
        self._auto_attend = att.get("auto_attend", True)

        # Registered-person names {int_id: str}
        self._names: dict[int, str] = {int(k): v for k, v in cfg.get("names", {}).items()}

        # Cooldown tracking: {person_id: datetime_of_last_log}
        self._last_logged: dict[int, datetime] = {}
        # Single shared cooldown for unknown faces
        self._last_unknown_logged: datetime | None = None

    # ── configuration mutators ────────────────────────────────────────────

    def set_names(self, names: dict):
        self._names = {int(k): v for k, v in names.items()}

    def update_config(self, cfg: dict):
        att = cfg.get("attendance", {})
        self._cooldown_minutes = att.get("cooldown_minutes", self._cooldown_minutes)
        self._late_after = att.get("late_after", self._late_after)
        self._working_hours_end = att.get("working_hours_end", self._working_hours_end)
        self._auto_attend = att.get("auto_attend", self._auto_attend)

    def set_auto_attend(self, enabled: bool):
        self._auto_attend = enabled

    # ── core processing ───────────────────────────────────────────────────

    def process_detection(self, person_id: int, is_known: bool, emotion: str, confidence: float, face_crop=None):
        """Called once per detected face per processed frame.

        Returns the created record dict on a real log, or ``None`` if
        the detection was suppressed (auto_attend off, cooldown, etc.).
        """
        if not self._auto_attend:
            return None

        now = self._clock()

        # ── cooldown check ────────────────────────────────────────────
        if is_known:
            last = self._last_logged.get(person_id)
            if last and (now - last).total_seconds() < self._cooldown_minutes * 60:
                return None
        else:
            if self._last_unknown_logged and (now - self._last_unknown_logged).total_seconds() < self._cooldown_minutes * 60:
                return None

        # ── status ────────────────────────────────────────────────────
        if not is_known:
            status = "Unknown"
        else:
            time_str = now.strftime("%H:%M")
            status = "Late" if time_str >= self._late_after else "Present"

        name = self._names.get(person_id, "Unknown") if is_known else "Unknown"

        record = {
            # An unknown face belongs to nobody. The recogniser's closest
            # guess is kept apart, for checking later.
            "person_id": person_id if is_known else None,
            "name": name,
            "date": now.strftime("%Y-%m-%d"),
            "time_in": now.strftime("%H:%M:%S"),
            "status": status,
            "emotion": emotion,
            "confidence": round(confidence, 2),
            "manual": False,
            "has_snapshot": False,
        }
        if not is_known:
            record["guessed_person_id"] = person_id

        saved = self._db.insert_log(record)
        if saved:
            record_id = saved.get("id")
            if face_crop is not None and hasattr(face_crop, "size") and face_crop.size > 0 and record_id:
                try:
                    import os
                    import cv2
                    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    snap_dir = os.path.join(project_root, "snapshots")
                    os.makedirs(snap_dir, exist_ok=True)
                    snap_path = os.path.join(snap_dir, f"{record_id}.jpg")
                    small_crop = cv2.resize(face_crop, (112, 112))
                    cv2.imwrite(snap_path, small_crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    saved["has_snapshot"] = True
                    if hasattr(self._db, "set_has_snapshot"):
                        self._db.set_has_snapshot(record_id, True)
                except Exception as exc:
                    logger.error("Failed to save snapshot for %s: %s", record_id, exc)

            if "created_at" in saved and hasattr(saved["created_at"], "isoformat"):
                saved["created_at"] = saved["created_at"].isoformat()

            self._firebase.push_attendance(saved)
            # Update cooldown timestamp
            if is_known:
                self._last_logged[person_id] = now
            else:
                self._last_unknown_logged = now
            return saved
        return None

    def manual_mark(self, person_id: int, name: str, status: str):
        """Insert a manual attendance record.  Bypasses cooldown."""
        now = self._clock()
        record = {
            "person_id": person_id,
            "name": name,
            "date": now.strftime("%Y-%m-%d"),
            "time_in": now.strftime("%H:%M:%S"),
            "status": status,
            "emotion": "",
            "confidence": 0.0,
            "manual": True,
            "has_snapshot": False,
        }
        saved = self._db.insert_log(record)
        if saved:
            if "created_at" in saved and hasattr(saved["created_at"], "isoformat"):
                saved["created_at"] = saved["created_at"].isoformat()
            self._firebase.push_attendance(saved)
        return saved

    # ── cooldown maintenance ──────────────────────────────────────────────

    def reset_cooldowns(self):
        """Forget every cooldown, so anyone can be recorded again at once."""
        self._last_logged.clear()
        self._last_unknown_logged = None

    def forget_record(self, record: dict):
        """Drop the cooldown a deleted record started, if it is still the one
        in force. A manual mark never starts a cooldown, and an older record
        whose cooldown was already replaced by a newer one must not clear it.
        """
        if not record or record.get("manual"):
            return
        stamp = f"{record.get('date')} {record.get('time_in')}"
        fmt = "%Y-%m-%d %H:%M:%S"
        if record.get("status") == "Unknown":
            last = self._last_unknown_logged
            if last and last.strftime(fmt) == stamp:
                self._last_unknown_logged = None
        else:
            person_id = record.get("person_id")
            last = self._last_logged.get(person_id)
            if last and last.strftime(fmt) == stamp:
                del self._last_logged[person_id]

    # ── stats ─────────────────────────────────────────────────────────────

    def get_today_stats(self) -> dict:
        """Return ``{total, present, late}`` for today.

        ``total`` represents ALL of today's records including Unknown.
        ``present`` is the Present count; ``late`` is the Late count.
        """
        logs = self._db.get_today_logs()
        present = sum(1 for r in logs if r.get("status") == "Present")
        late = sum(1 for r in logs if r.get("status") == "Late")
        return {"total": len(logs), "present": present, "late": late}
