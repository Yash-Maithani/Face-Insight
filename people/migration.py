"""
people/migration.py
The version 2 data migration. Safe to run any number of times.

1. People move from config.json `names` into the `persons` collection.
2. Unknown-face records stop carrying the recogniser's guessed person ID:
   `person_id` becomes null and the guess is kept as `guessed_person_id`.

Before anything changes, the affected collections are written to a backup
folder as MongoDB Extended JSON. If nothing needs changing, no backup is made
and nothing is written.
"""

import logging
import os
from datetime import datetime

from bson import json_util

logger = logging.getLogger(__name__)

MARKER = "migration.v2_people"
BACKED_UP = ("attendance_logs", "persons")
UNKNOWN_WITH_ID = {"status": "Unknown", "person_id": {"$ne": None}}


def backup(database, backup_root, clock=datetime.now):
    """Write every document of BACKED_UP to `<backup_root>/<timestamp>/`. Returns the folder."""
    stamp = clock().strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(backup_root, stamp)
    suffix = 1
    while os.path.exists(folder):
        suffix += 1
        folder = os.path.join(backup_root, f"{stamp}-{suffix}")
    os.makedirs(folder)
    for name in BACKED_UP:
        docs = list(database[name].find({}))
        with open(os.path.join(folder, f"{name}.json"), "w", encoding="utf-8") as f:
            f.write(json_util.dumps(docs, json_options=json_util.CANONICAL_JSON_OPTIONS, indent=1))
    return folder


def restore(database, folder):
    """Put the collections back exactly as they were in a backup folder."""
    for name in BACKED_UP:
        path = os.path.join(folder, f"{name}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            docs = json_util.loads(f.read())
        database[name].delete_many({})
        if docs:
            database[name].insert_many(docs)


def pending(db, registry):
    """What the migration would change: {"people": [ids], "unknown_records": n}, or None if offline."""
    database = db.database()
    if database is None:
        return None
    try:
        unknown = database["attendance_logs"].count_documents(UNKNOWN_WITH_ID)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Migration check failed: %s", exc)
        return None
    return {"people": registry.pending_import(), "unknown_records": unknown}


def migrate(db, registry, backup_root, clock=datetime.now):
    """Run the migration. Returns a report dict; `ran` is False when there was nothing to do."""
    todo = pending(db, registry)
    if todo is None:
        return {"ran": False, "reason": "database offline"}
    if not todo["people"] and not todo["unknown_records"]:
        registry.sync_cache()
        return {"ran": False, "reason": "nothing to do"}

    database = db.database()
    folder = backup(database, backup_root, clock)
    imported = registry.import_cache()
    result = database["attendance_logs"].update_many(
        UNKNOWN_WITH_ID,
        [{"$set": {"guessed_person_id": "$person_id", "person_id": None}}],
    )
    registry.sync_cache()
    report = {
        "ran": True,
        "at": clock().replace(microsecond=0).isoformat(),
        "backup": folder,
        "people_imported": imported,
        "unknown_records_fixed": result.modified_count,
    }
    db.set_meta(MARKER, report)
    logger.info("Migration done: %d people imported, %d unknown records fixed, backup in %s",
                imported, result.modified_count, folder)
    return report
