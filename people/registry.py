"""
people/registry.py
Registered people: who they are, which group they belong to, and when they
agreed to have their face stored.

The `persons` collection in MongoDB is the source of truth. `config.json`
keeps a copy of the names (`names`) and groups (`person_groups`, only for
people outside the Default group), rewritten after every change, so the
recogniser can still put names on faces, and lateness is still judged by
the right schedule, while the database is unreachable.
Adding, renaming or removing a person needs the database.

A name that is in the copy but not yet in the database (an older install
before its first migration, or a name added to config.json by hand) still
counts as registered until the migration imports it.
"""

import logging
from datetime import datetime

from attendance.schedules import DEFAULT_GROUP

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 50
OFFLINE_MESSAGE = "Database offline. People cannot be added, renamed or removed right now."


class PersonError(ValueError):
    """A request about a person was refused. The message is shown to the user."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clean_name(name):
    """The name with outer spaces removed. Raises PersonError if unusable."""
    if not isinstance(name, str) or not name.strip():
        raise PersonError("Name is required.")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_LENGTH:
        raise PersonError(f"Name must be {MAX_NAME_LENGTH} characters or fewer.")
    return name


def check_person_id(person_id):
    if isinstance(person_id, bool) or not isinstance(person_id, int) or person_id < 0:
        raise PersonError("person_id must be a whole number, 0 or more.")
    return person_id


class PersonRegistry:
    """`get_db` returns the current MongoAttendanceDB. `load_config` and
    `save_config` read and write config.json. `on_change(names)` is called
    with the new {str id: name} map whenever the names change."""

    def __init__(self, get_db, load_config, save_config, on_change=None, clock=datetime.now):
        self._get_db = get_db
        self._load_config = load_config
        self._save_config = save_config
        self._on_change = on_change
        self._clock = clock

    # ── reading ───────────────────────────────────────────────────────────

    def _cached_names(self):
        names = self._load_config().get("names") or {}
        return {str(k): v for k, v in names.items()}

    def group_of(self, person_id, cfg=None):
        """The group from the config copy. Fast enough for every new record."""
        cfg = cfg if cfg is not None else self._load_config()
        return (cfg.get("person_groups") or {}).get(str(person_id), DEFAULT_GROUP)

    @staticmethod
    def _known_group(group, cfg):
        """A group that no longer has a schedule (config edited by hand) counts as Default."""
        return group if group == DEFAULT_GROUP or group in (cfg.get("schedules") or {}) else DEFAULT_GROUP

    def _db_persons(self):
        db = self._get_db()
        if db is None or not db.is_connected:
            return None
        return db.list_persons()

    def list(self):
        """(people sorted by ID, database_available). Each person has
        person_id, name, group, consent_at, created and saved (False while
        only in the config copy)."""
        cfg = self._load_config()
        cached = {str(k): v for k, v in (cfg.get("names") or {}).items()}
        stored = self._db_persons()
        people = {}
        for p in stored or []:
            people[p["person_id"]] = dict(p, saved=True, group=self._known_group(p.get("group") or DEFAULT_GROUP, cfg))
        for key, name in cached.items():
            pid = int(key)
            if pid not in people:
                people[pid] = {"person_id": pid, "name": name, "consent_at": None, "created": None,
                               "saved": False, "group": self._known_group(self.group_of(pid, cfg), cfg)}
        return [people[k] for k in sorted(people)], stored is not None

    def get(self, person_id):
        return next((p for p in self.list()[0] if p["person_id"] == person_id), None)

    def names(self):
        """{str id: name} for everyone registered."""
        return {str(p["person_id"]): p["name"] for p in self.list()[0]}

    # ── keeping the copy in step ──────────────────────────────────────────

    def sync_cache(self):
        """Rewrite the names copy in config.json from the registry and tell
        the recogniser. Returns the names."""
        everyone = self.list()[0]
        names = {str(p["person_id"]): p["name"] for p in everyone}
        groups = {str(p["person_id"]): p["group"] for p in everyone if p["group"] != DEFAULT_GROUP}
        current = self._load_config()
        if {str(k): v for k, v in (current.get("names") or {}).items()} != names                 or (current.get("person_groups") or {}) != groups:
            current["names"] = names
            current["person_groups"] = groups
            self._save_config(current)
        if self._on_change:
            self._on_change(names)
        return names

    def import_cache(self):
        """Copy names that are only in config.json into the database. Returns how many."""
        db = self._require_db()
        stored = {p["person_id"] for p in (db.list_persons() or [])}
        imported = 0
        for key, name in self._cached_names().items():
            pid = int(key)
            if pid in stored:
                continue
            if db.insert_person({"person_id": pid, "name": name, "consent_at": None,
                                 "created": None, "imported_at": self._now()}):
                imported += 1
        return imported

    def pending_import(self):
        """IDs in the config copy that the database does not have yet."""
        stored = self._db_persons()
        if stored is None:
            return []
        have = {p["person_id"] for p in stored}
        return sorted(int(k) for k in self._cached_names() if int(k) not in have)

    # ── changes ───────────────────────────────────────────────────────────

    def _now(self):
        return self._clock().replace(microsecond=0).isoformat()

    def _require_db(self):
        db = self._get_db()
        if db is None or not db.is_connected:
            raise PersonError(OFFLINE_MESSAGE, 503)
        return db

    def register(self, person_id, name, consent):
        """Make sure `person_id` belongs to `name`, recording consent.

        A new ID needs consent. An existing ID must keep its name: this
        never renames anyone. Returns (person, created).
        """
        check_person_id(person_id)
        name = clean_name(name)
        db = self._require_db()
        existing = self.get(person_id)
        if existing is not None:
            if existing["name"].casefold() != name.casefold():
                raise PersonError(
                    f"ID {person_id} already belongs to {existing['name']}. "
                    "Pick another ID, or rename them on the People page.", 409)
            if not existing.get("saved"):
                # Known only from the config copy: store them now
                db.insert_person({"person_id": person_id, "name": existing["name"],
                                  "consent_at": None, "created": None, "imported_at": self._now()})
            if not existing.get("consent_at"):
                if consent is not True:
                    raise PersonError("Consent is required before collecting face images.")
                existing = db.update_person(person_id, {"consent_at": self._now()})
                if existing is None:
                    raise PersonError(OFFLINE_MESSAGE, 503)
            self.sync_cache()
            return dict(existing, saved=True), False

        if consent is not True:
            raise PersonError("Consent is required before collecting face images.")
        now = self._now()
        person = {"person_id": person_id, "name": name, "consent_at": now, "created": now}
        if not db.insert_person(person):
            if not db.is_connected:
                raise PersonError(OFFLINE_MESSAGE, 503)
            raise PersonError(f"ID {person_id} was registered a moment ago. Pick another ID.", 409)
        self.sync_cache()
        return dict(person, saved=True), True

    def rename(self, person_id, name):
        """Returns (old name, person)."""
        name = clean_name(name)
        db = self._require_db()
        existing = self.get(person_id)
        if existing is None:
            raise PersonError("Person not found.", 404)
        if not existing.get("saved"):
            db.insert_person({"person_id": person_id, "name": existing["name"],
                              "consent_at": None, "created": None, "imported_at": self._now()})
        person = db.update_person(person_id, {"name": name})
        if person is None:
            raise PersonError(OFFLINE_MESSAGE, 503)
        self.sync_cache()
        return existing["name"], dict(person, saved=True)

    def set_group(self, person_id, group, known_groups):
        """Move a person to another group. Returns (old group, person)."""
        if group not in known_groups:
            raise PersonError(f"There is no group called {group}.", 404)
        db = self._require_db()
        existing = self.get(person_id)
        if existing is None:
            raise PersonError("Person not found.", 404)
        if not existing.get("saved"):
            db.insert_person({"person_id": person_id, "name": existing["name"],
                              "consent_at": None, "created": None, "imported_at": self._now()})
        person = db.update_person(person_id, {"group": group})
        if person is None:
            raise PersonError(OFFLINE_MESSAGE, 503)
        self.sync_cache()
        return existing["group"], dict(person, saved=True, group=group)

    def move_group(self, old, new):
        """Move everyone in group `old` to `new`. Returns how many."""
        db = self._require_db()
        moved = 0
        for p in self.list()[0]:
            if p["group"] == old:
                if not p.get("saved"):
                    db.insert_person({"person_id": p["person_id"], "name": p["name"],
                                      "consent_at": None, "created": None, "imported_at": self._now()})
                db.update_person(p["person_id"], {"group": new})
                moved += 1
        self.sync_cache()
        return moved

    def remove(self, person_id):
        """Remove a person from the database and the config copy. Returns them."""
        db = self._require_db()
        existing = self.get(person_id)
        if existing is None:
            raise PersonError("Person not found.", 404)
        if db.delete_person(person_id) is None:
            raise PersonError(OFFLINE_MESSAGE, 503)
        current = self._load_config()
        names = {str(k): v for k, v in (current.get("names") or {}).items()}
        if names.pop(str(person_id), None) is not None:
            current["names"] = names
            self._save_config(current)
        self.sync_cache()
        return existing

    def replace_names(self, names):
        """Make the registry hold exactly `names` ({id: name}). Kept for
        `POST /api/config`; the People page changes one person at a time.
        Removing a name here keeps the person's face images."""
        wanted = {int(k): clean_name(v) for k, v in names.items()}
        db = self._require_db()
        for p in self.list()[0]:
            pid = p["person_id"]
            if pid not in wanted:
                db.delete_person(pid)
            elif wanted[pid] != p["name"] or not p.get("saved"):
                if p.get("saved"):
                    db.update_person(pid, {"name": wanted[pid]})
                else:
                    db.insert_person({"person_id": pid, "name": wanted[pid], "consent_at": None,
                                      "created": None, "imported_at": self._now()})
        have = {p["person_id"] for p in (db.list_persons() or [])}
        for pid, name in wanted.items():
            if pid not in have:
                db.insert_person({"person_id": pid, "name": name, "consent_at": None,
                                  "created": None, "imported_at": self._now()})
        if not db.is_connected:
            raise PersonError(OFFLINE_MESSAGE, 503)
        current = self._load_config()
        current["names"] = {str(k): v for k, v in wanted.items()}
        self._save_config(current)
        return self.sync_cache()
