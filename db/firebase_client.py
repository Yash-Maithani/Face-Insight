"""
db/firebase_client.py
Optional Firebase Realtime Database mirror for attendance records.

Design rules:
- If credentials_path or database_url is blank → every method is a silent no-op.
- A misconfigured or absent Firebase must never break attendance logging.
- All real SDK calls are wrapped in try/except and errors are logged.
"""

import logging

logger = logging.getLogger(__name__)


class FirebaseClient:
    """Thin wrapper around firebase_admin for attendance mirroring."""

    def __init__(self, credentials_path: str, database_url: str):
        self._enabled = False
        self._db = None

        if not credentials_path or not database_url:
            logger.info("Firebase disabled (blank credentials or URL).")
            return

        try:
            import firebase_admin
            from firebase_admin import credentials, db

            # Avoid double-init if the default app already exists.
            try:
                firebase_admin.get_app()
            except ValueError:
                cred = credentials.Certificate(credentials_path)
                firebase_admin.initialize_app(cred, {"databaseURL": database_url})

            self._db = db
            self._enabled = True
            logger.info("Firebase initialized (URL: %s).", database_url)
        except Exception as exc:
            logger.error("Firebase init failed (non-fatal): %s", exc)

    # ── public API ────────────────────────────────────────────────────────

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def push_attendance(self, record: dict) -> None:
        """Push an attendance record to Firebase.  Silent no-op if disabled."""
        if not self._enabled:
            return
        try:
            # Strip non-serialisable fields
            clean = {k: v for k, v in record.items() if k != "_id"}
            # Convert datetime objects to ISO strings
            for key, val in clean.items():
                if hasattr(val, "isoformat"):
                    clean[key] = val.isoformat()
            ref = self._db.reference("attendance_logs")
            ref.push(clean)
        except Exception as exc:
            logger.error("Firebase push_attendance failed (non-fatal): %s", exc)
