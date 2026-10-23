"""
auth/audit.py
The audit log: who did what, and when.

Entries go to the `audit_log` collection in MongoDB. While the database is
unreachable they are appended to `<instance>/audit-offline.jsonl` instead,
and reads merge both sources, so nothing recorded during an outage is lost.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

ACTIONS = {
    "auth.setup": "Created the first admin",
    "auth.login": "Signed in",
    "auth.login_failed": "Failed sign-in",
    "auth.logout": "Signed out",
    "auth.password_changed": "Changed password",
    "user.created": "Added an admin",
    "attendance.manual_mark": "Marked attendance by hand",
    "attendance.deleted": "Deleted a record",
    "attendance.cleared_today": "Cleared today's records",
    "person.deleted": "Deleted a person",
    "settings.saved": "Saved settings",
    "settings.threshold": "Changed the recognition threshold",
    "settings.auto_attend": "Switched auto attendance",
    "collection.started": "Started face collection",
    "training.started": "Started training",
    "camera.started": "Started the camera",
    "camera.stopped": "Stopped the camera",
    "db.reconnect": "Retried the database",
}


class AuditLog:
    """`get_db` returns the current MongoAttendanceDB (it can be replaced at runtime)."""

    def __init__(self, get_db, instance_dir, clock=datetime.now):
        self._get_db = get_db
        self._offline_path = os.path.join(instance_dir, "audit-offline.jsonl")
        self._clock = clock
        self._lock = threading.Lock()
        os.makedirs(instance_dir, exist_ok=True)

    def record(self, action, user=None, target=None, details=None, address=None):
        """Write one entry. Never raises: auditing must not break the action itself."""
        if action not in ACTIONS:
            logger.warning("Unknown audit action %r", action)
        now = self._clock().replace(microsecond=0)
        entry = {
            "time": now.isoformat(),
            "ts": time.mktime(now.timetuple()),
            "user": user,
            "action": action,
            "target": None if target is None else str(target),
            "details": details or {},
            "address": address,
        }
        try:
            db = self._get_db()
            if db is not None and db.insert_audit(dict(entry)):
                return entry
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Audit write to MongoDB failed: %s", exc)
        try:
            with self._lock, open(self._offline_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.error("Audit entry lost (%s): %s", action, exc)
        return entry

    def _offline_entries(self):
        if not os.path.exists(self._offline_path):
            return []
        entries = []
        with self._lock, open(self._offline_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        return entries

    def query(self, user=None, action=None, date_from=None, date_to=None, limit=500):
        """Newest first. Dates are YYYY-MM-DD and inclusive.

        Returns (entries, total_matching, db_available).
        """
        def matches(e):
            if user and e.get("user") != user:
                return False
            if action and e.get("action") != action:
                return False
            day = (e.get("time") or "")[:10]
            if date_from and day < date_from:
                return False
            if date_to and day > date_to:
                return False
            return True

        # Filter and limit in MongoDB; the offline file is small and filtered here
        mongo_query = {}
        if user:
            mongo_query["user"] = user
        if action:
            mongo_query["action"] = action
        span = {}
        if date_from:
            span["$gte"] = date_from
        if date_to:
            span["$lte"] = date_to + "T23:59:59"
        if span:
            mongo_query["time"] = span

        db_entries, db_total, db_ok = [], 0, False
        db = self._get_db()
        if db is not None:
            found = db.find_audit(mongo_query, limit=limit)
            total = db.count_audit(mongo_query) if found is not None else None
            if found is not None and total is not None:
                db_entries, db_total, db_ok = found, total, True

        offline = [e for e in self._offline_entries() if matches(e)]
        everything = db_entries + offline
        everything.sort(key=lambda e: e.get("ts", 0), reverse=True)
        for e in everything:
            e.pop("_id", None)
        return everything[:limit], db_total + len(offline), db_ok
