"""
db/mongo_client.py
Thin wrapper around the app's MongoDB collections: ``attendance_logs``,
``persons``, ``audit_log`` and ``app_meta`` (migration markers).

Design rules:
- Short server-selection timeout so an unreachable Mongo fails fast.
- When disconnected, methods return empty results: never raise.
"""

import logging
from datetime import datetime, timedelta

from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

logger = logging.getLogger(__name__)

# How long pymongo waits for a usable server before giving up (ms).
_SERVER_SELECTION_TIMEOUT_MS = 3000


def _date_list(date_from: str, date_to: str) -> list[str]:
    """Every YYYY-MM-DD date from one to the other, inclusive."""
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


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
            db["persons"].create_index([("person_id", ASCENDING)], unique=True)
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

    def database(self):
        """The pymongo Database, or None while unreachable. For migrations and backups."""
        return self._client[self._db_name] if self._connected else None

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

    # ── persons ───────────────────────────────────────────────────────────

    def list_persons(self) -> list[dict] | None:
        """Every registered person, by ID. None if unreachable."""
        if not self._connected:
            return None
        try:
            return list(self._client[self._db_name]["persons"].find({}, {"_id": 0}).sort("person_id", ASCENDING))
        except PyMongoError as exc:
            logger.error("list_persons failed: %s", exc)
            self._connected = False
            return None

    def insert_person(self, person: dict) -> bool:
        """Add a person. False if unreachable or the ID is taken."""
        if not self._connected:
            return False
        try:
            self._client[self._db_name]["persons"].insert_one(dict(person))
            return True
        except DuplicateKeyError:
            return False
        except PyMongoError as exc:
            logger.error("insert_person failed: %s", exc)
            self._connected = False
            return False

    def update_person(self, person_id: int, fields: dict) -> dict | None:
        """Change fields on a person. The updated person, or None if missing or unreachable."""
        if not self._connected:
            return None
        try:
            return self._client[self._db_name]["persons"].find_one_and_update(
                {"person_id": person_id}, {"$set": fields},
                projection={"_id": 0}, return_document=ReturnDocument.AFTER,
            )
        except PyMongoError as exc:
            logger.error("update_person failed: %s", exc)
            self._connected = False
            return None

    def delete_person(self, person_id: int) -> bool | None:
        """True if deleted, False if there was no such person, None if unreachable."""
        if not self._connected:
            return None
        try:
            result = self._client[self._db_name]["persons"].delete_one({"person_id": person_id})
            return result.deleted_count == 1
        except PyMongoError as exc:
            logger.error("delete_person failed: %s", exc)
            self._connected = False
            return None

    # ── migration markers ─────────────────────────────────────────────────

    def get_meta(self, key: str):
        """A stored marker value, or None (also while unreachable)."""
        if not self._connected:
            return None
        try:
            doc = self._client[self._db_name]["app_meta"].find_one({"_id": key})
            return doc.get("value") if doc else None
        except PyMongoError as exc:
            logger.error("get_meta failed: %s", exc)
            self._connected = False
            return None

    def set_meta(self, key: str, value) -> bool:
        if not self._connected:
            return False
        try:
            self._client[self._db_name]["app_meta"].update_one(
                {"_id": key}, {"$set": {"value": value}}, upsert=True)
            return True
        except PyMongoError as exc:
            logger.error("set_meta failed: %s", exc)
            self._connected = False
            return False

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

    def get_logs_between(self, date_from: str, date_to: str) -> list[dict]:
        """All records from ``date_from`` to ``date_to`` (YYYY-MM-DD, inclusive)."""
        return self._safe_find({"date": {"$gte": date_from, "$lte": date_to}})

    def find_logs(self, date_from: str, date_to: str, person_ids=None) -> list[dict] | None:
        """Records in a date range, optionally for some people only, oldest
        first. None if unreachable, so a report never mistakes an outage for
        an empty range."""
        if not self._connected:
            return None
        query = {"date": {"$gte": date_from, "$lte": date_to}}
        if person_ids is not None:
            query["person_id"] = {"$in": list(person_ids)}
        try:
            docs = list(self._collection.find(query).sort([("date", ASCENDING), ("time_in", ASCENDING)]))
            for d in docs:
                d["id"] = str(d.pop("_id"))
                if "created_at" in d and hasattr(d["created_at"], "isoformat"):
                    d["created_at"] = d["created_at"].isoformat()
            return docs
        except PyMongoError as exc:
            logger.error("find_logs failed: %s", exc)
            self._connected = False
            return None

    def last_seen_by_person(self) -> dict:
        """{person_id: "YYYY-MM-DD HH:MM:SS"} of each person's latest record. {} if unreachable."""
        if not self._connected:
            return {}
        try:
            pipeline = [
                {"$match": {"status": {"$ne": "Unknown"}, "person_id": {"$ne": None}}},
                {"$group": {"_id": "$person_id",
                            "last": {"$max": {"$concat": ["$date", " ", {"$ifNull": ["$time_in", ""]}]}}}},
            ]
            return {doc["_id"]: doc["last"].strip() for doc in self._collection.aggregate(pipeline)}
        except PyMongoError as exc:
            logger.error("last_seen_by_person failed: %s", exc)
            self._connected = False
            return {}

    def get_today_logs(self) -> list[dict]:
        today = datetime.now().strftime("%Y-%m-%d")
        return self._safe_find({"date": today})

    def get_weekly_summary(self, days: int = 7) -> list[dict]:
        """The last *days* days up to today. See ``get_range_summary``."""
        today = datetime.now().date()
        return self.get_range_summary((today - timedelta(days=days - 1)).isoformat(), today.isoformat())

    def get_range_summary(self, date_from: str, date_to: str) -> list[dict]:
        """One dict per day from ``date_from`` to ``date_to``, oldest first, with
        ``date``, ``present_count``, ``late_count``, ``unknown_count``.
        Days with no records still appear with zero counts.
        """
        dates = _date_list(date_from, date_to)

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
            logger.error("get_range_summary failed: %s", exc)
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
        """The last *days* days up to today. See ``get_person_summary_range``."""
        today = datetime.now().date()
        return self.get_person_summary_range((today - timedelta(days=days - 1)).isoformat(), today.isoformat())

    def get_person_summary_range(self, date_from: str, date_to: str) -> list[dict]:
        """Per-person attendance from ``date_from`` to ``date_to`` as
        [{person_id, name, count, days}]. Unknown faces are left out.

        ``count`` is the number of records; ``days`` is the number of distinct
        dates the person has at least one record on.
        """
        if not self._connected:
            return []
        try:
            pipeline = [
                {"$match": {"date": {"$gte": date_from, "$lte": date_to}, "status": {"$ne": "Unknown"}}},
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
