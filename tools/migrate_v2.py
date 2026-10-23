"""
tools/migrate_v2.py
Run the version 2 data migration by hand. The app also runs it on start.

  python tools/migrate_v2.py                 show what would change, then change it
  python tools/migrate_v2.py --dry-run       only show what would change
  python tools/migrate_v2.py --restore DIR   put the collections back from a backup folder

Uses config.json (or the file named by FACE_WEB_CONFIG, or --config). Backups
go to instance/backups next to that config file. Safe to run more than once.
"""

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.model_loader import CONFIG_PATH, load_config, save_config  # noqa: E402
from db.mongo_client import MongoAttendanceDB, mask_mongo_uri  # noqa: E402
from people.migration import migrate, pending, restore  # noqa: E402
from people.registry import PersonRegistry  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Face Insight version 2 data migration")
    parser.add_argument("--config", default=os.environ.get("FACE_WEB_CONFIG", CONFIG_PATH))
    parser.add_argument("--dry-run", action="store_true", help="only show what would change")
    parser.add_argument("--restore", metavar="DIR", help="restore the collections from a backup folder")
    args = parser.parse_args()

    config_path = os.path.abspath(args.config)
    cfg = load_config(config_path)
    db = MongoAttendanceDB(cfg["mongodb_uri"], cfg["mongodb_db"])
    if not db.is_connected:
        print(f"Cannot reach MongoDB at {mask_mongo_uri(cfg['mongodb_uri'])}. Nothing was changed.")
        return 1
    print(f"Database: {cfg['mongodb_db']} at {mask_mongo_uri(cfg['mongodb_uri'])}")

    if args.restore:
        folder = os.path.abspath(args.restore)
        if not os.path.isdir(folder):
            print(f"No such folder: {folder}")
            return 1
        answer = input(f"Replace attendance_logs and persons with the backup in {folder}? Type yes: ")
        if answer.strip().lower() != "yes":
            print("Nothing was changed.")
            return 1
        restore(db.database(), folder)
        print("Restored.")
        return 0

    registry = PersonRegistry(lambda: db, lambda: load_config(config_path),
                              lambda current: save_config(current, config_path))
    todo = pending(db, registry)
    print(f"People to move into the database: {len(todo['people'])}")
    print(f"Unknown-face records still carrying a guessed ID: {todo['unknown_records']}")
    if args.dry_run:
        return 0

    report = migrate(db, registry, os.path.join(os.path.dirname(config_path), "instance", "backups"))
    if not report["ran"]:
        print("Nothing to do.")
    else:
        print(f"Done. Backup: {report['backup']}")
        print(f"People imported: {report['people_imported']}, records fixed: {report['unknown_records_fixed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
