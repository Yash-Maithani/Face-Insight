"""
db/mongo_client.py
Thin wrapper around a single ``attendance_logs`` MongoDB collection.

Design rules:
- Short server-selection timeout so an unreachable Mongo fails fast.
- When disconnected, methods return empty results: never raise.
"""

import logging
from datetime import datetime, timedelta

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

# How long pymongo waits for a usable server before giving up (ms).
_SERVER_SELECTION_TIMEOUT_MS = 3000


def mask_mongo_uri(uri: str) -> str:
    """Mask password in a MongoDB URI for safe logging."""
    if not uri:
        return ""
    import re
    return re.sub(r":([^/@:]+)@", ":***@", uri)


class MongoAttendanceDB:
    """Manages the ``attendance_logs`` collection inside *db_name*."""

    def __init__(self, uri: str, db_name: str):
        self._uri = uri
        self._db_name = db_name
        self._client = None
        self._collection = None
        self._connected = False
        self._connect()

    # ── connection management ─────────────────────────────────────────────

    def _connect(self):
        try:
            self._client = MongoClient(
                self._uri,
                serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS,
            )
            # Force a round-trip to verify the server is reachable.
            self._client.admin.command("ping")
            db = self._client[self._db_name]
            self._collection = db["attendance_logs"]
            # Index on `date` for fast queries.
            self._collection.create_index([("date", ASCENDING)])
            db["audit_log"].create_index([("ts", DESCENDING)])
            self._connected = True
            logger.info("MongoDB connected: %s / %s", mask_mongo_uri(self._uri), self._db_name)
        except PyMongoError as exc:
            logger.warning("MongoDB unreachable (%s): %s", mask_mongo_uri(self._uri), exc)
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def reconnect(self, uri: str = None, db_name: str = None):
        """Close current connection (if any) and try again, optionally with new uri/db_name."""
        if uri is not None:
            self._uri = uri
        if db_name is not None:
            self._db_name = db_name
        self.close()
        self._connect()

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._collection = None
        self._connected = False

    def insert_log(self, record: dict) -> dict | None:
        """Insert an attendance record.  Returns the record with a string ``id``
        field added, or *None* if the DB is unreachable."""
        if not self._connected:
            return None
        try:
            now_dt = datetime.utcnow()
            record["created_at"] = now_dt
            result = self._collection.insert_one(record)
            record["id"] = str(result.inserted_id)
            record["created_at"] = now_dt.isoformat()
            return record
        except PyMongoError as exc:
            logger.error("insert_log failed: %s", exc)
            self._connected = False
            return None

    def set_has_snapshot(self, record_id: str, has_snapshot: bool = True) -> bool:
        """Update has_snapshot flag on a record."""
        if not self._connected:
            return False
        try:
            from bson import ObjectId
            from bson.errors import InvalidId
            try:
                q = {"_id": ObjectId(record_id)}
            except (InvalidId, TypeError):
                q = {"_id": record_id}
            self._collection.update_one(q, {"$set": {"has_snapshot": has_snapshot}})
            return True
        except Exception as exc:
            logger.error("set_has_snapshot failed: %s", exc)
            return False

    # ── deletes ───────────────────────────────────────────────────────────

    def delete_log(self, record_id: str) -> dict | None:
        """Delete one record by id.

        Returns the deleted record (with a string ``id``), or *None* if there
        is no such record or the DB is unreachable. Callers tell the two apart
        with ``is_connected``.
        """
        if not self._connected:
            return None
        try:
            from bson import ObjectId
            from bson.errors import InvalidId
            try:
                q = {"_id": ObjectId(record_id)}
            except (InvalidId, TypeError):
                q = {"_id": record_id}
            doc = self._collection.find_one_and_delete(q)
            if doc is None:
                return None
            doc["id"] = str(doc.pop("_id"))
            if "created_at" in doc and hasattr(doc["created_at"], "isoformat"):
                doc["created_at"] = doc["created_at"].isoformat()
            return doc
        except PyMongoError as exc:
            logger.error("delete_log failed: %s", exc)
            self._connected = False
            return None

    def delete_logs_by_date(self, date_str: str) -> list[str] | None:
        """Delete every record for a ``YYYY-MM-DD`` date.

        Returns the ids that were deleted (possibly empty), or *None* if the
        DB is unreachable. Records are deleted by the ids found first, so the
        returned list is exactly what was removed.
        """
        if not self._connected:
            return None
        try:
            ids = [d["_id"] for d in self._collection.find({"date": date_str}, {"_id": 1})]
            if ids:
                self._collection.delete_many({"_id": {"$in": ids}})
            return [str(i) for i in ids]
        except PyMongoError as exc:
            logger.error("delete_logs_by_date failed: %s", exc)
            self._connected = False
            return None

    # ── audit log ─────────────────────────────────────────────────────────

    def insert_audit(self, entry: dict) -> bool:
        """Store one audit entry. False if the DB is unreachable."""
        if not self._connected:
            return False
        try:
            self._client[self._db_name]["audit_log"].insert_one(entry)
            return True
        except PyMongoError as exc:
            logger.error("insert_audit failed: %s", exc)
            self._connected = False
            return False

    def find_audit(self, query: dict, limit=None) -> list | None:
        """Audit entries matching `query`, newest first. None if unreachable."""
        if not self._connected:
            return None
        try:
            cursor = self._client[self._db_name]["audit_log"].find(query, {"_id": 0}).sort("ts", DESCENDING)
            if limit:
                cursor = cursor.limit(limit)
            return list(cursor)
        except PyMongoError as exc:
            logger.error("find_audit failed: %s", exc)
            self._connected = False
            return None

    def count_audit(self, query: dict) -> int | None:
        """Number of audit entries matching `query`. None if unreachable."""
        if not self._connected:
            return None
        try:
            return self._client[self._db_name]["audit_log"].count_documents(query)
        except PyMongoError as exc:
            logger.error("count_audit failed: %s", exc)
            self._connected = False
            return None

    # ── reads ─────────────────────────────────────────────────────────────

    def _safe_find(self, query: dict) -> list[dict]:
        """Run a find(); return [] on any error."""
        if not self._connected:
            return []
        try:
            docs = list(self._collection.find(query))
            for d in docs:
                d["id"] = str(d.pop("_id"))
                if "created_at" in d and hasattr(d["created_at"], "isoformat"):
                    d["created_at"] = d["created_at"].isoformat()
            return docs
        except PyMongoError as exc:
            logger.error("Query failed: %s", exc)
            self._connected = False
            return []

    def get_logs_by_date(self, date_str: str) -> list[dict]:
        """All records for a given ``YYYY-MM-DD`` string."""
        return self._safe_find({"date": date_str})

    def get_today_logs(self) -> list[dict]:
        today = datetime.now().strftime("%Y-%m-%d")
        return self._safe_find({"date": today})

    def get_weekly_summary(self, days: int = 7) -> list[dict]:
        """Return a list of *days* dicts (last *days* days, oldest first) with keys:
        ``date``, ``present_count``, ``late_count``, ``unknown_count``.
        Days with no records still appear with zero counts.
        """
        today = datetime.now().date()
        dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

        # Pre-fill with zeros
        summary = {d: {"date": d, "present_count": 0, "late_count": 0, "unknown_count": 0} for d in dates}

        if not self._connected:
            return list(summary.values())

        try:
            pipeline = [
                {"$match": {"date": {"$in": dates}}},
                {"$group": {
                    "_id": {"date": "$date", "status": "$status"},
                    "count": {"$sum": 1},
                }},
            ]
            for doc in self._collection.aggregate(pipeline):
                d = doc["_id"]["date"]
                status = doc["_id"]["status"]
                if d in summary:
                    if status == "Present":
                        summary[d]["present_count"] = doc["count"]
                    elif status == "Late":
                        summary[d]["late_count"] = doc["count"]
                    elif status == "Unknown":
                        summary[d]["unknown_count"] = doc["count"]
            return list(summary.values())
        except PyMongoError as exc:
            logger.error("get_weekly_summary failed: %s", exc)
            self._connected = False
            return list(summary.values())

    def get_today_emotion_counts(self) -> dict:
        """{emotion_label: count} for today's records.

        Excludes manual records and records where emotion is empty, missing, or '--'.
        """
        if not self._connected:
            return {}
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            pipeline = [
                {
                    "$match": {
                        "date": today,
                        "manual": {"$ne": True},
                        "emotion": {"$exists": True, "$nin": ["", "--", None]},
                    }
                },
                {"$group": {"_id": "$emotion", "count": {"$sum": 1}}},
            ]
            return {
                doc["_id"]: doc["count"]
                for doc in self._collection.aggregate(pipeline)
                if doc["_id"] and doc["_id"] not in ("", "--")
            }
        except PyMongoError as exc:
            logger.error("get_today_emotion_counts failed: %s", exc)
            self._connected = False
            return {}

    def get_person_summary(self, days: int = 7) -> list[dict]:
        """Return per-person attendance for the last *days* days as:
        [{person_id, name, count, days}].

        ``count`` is the number of records; ``days`` is the number of distinct
        dates the person has at least one record on.
        """
        if not self._connected:
            return []
        today = datetime.now().date()
        dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        try:
            pipeline = [
                {"$match": {"date": {"$in": dates}}},
                {"$group": {
                    "_id": {"person_id": "$person_id", "name": "$name"},
                    "count": {"$sum": 1},
                    "dates": {"$addToSet": "$date"},
                }},
                {"$sort": {"count": -1}},
            ]
            results = []
            for doc in self._collection.aggregate(pipeline):
                results.append({
                    "person_id": doc["_id"]["person_id"],
                    "name": doc["_id"]["name"],
                    "count": doc["count"],
                    "days": len(doc.get("dates", [])),
                })
            return results
        except PyMongoError as exc:
            logger.error("get_person_summary failed: %s", exc)
            self._connected = False
            return []
