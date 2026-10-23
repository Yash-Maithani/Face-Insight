"""
auth/store.py
Admin accounts, password hashing and sign-in throttling.

Accounts live in a JSON file in the instance folder, not in MongoDB, so an
admin can still sign in (and fix the database settings) while the database
is unreachable. Passwords are stored only as bcrypt hashes.
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime

import bcrypt

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 72  # bcrypt only looks at the first 72 bytes

MODES = ("dark", "light", "system")
THEMES = ("orange", "ink", "teal", "blue", "navy", "olive", "coffee", "steel")
FONTS = ("plex", "source", "atkinson", "inter")
DEFAULT_PREFS = {"mode": "dark", "theme": "orange", "font": "plex"}

# Used when a username does not exist, so a wrong username takes as long to
# reject as a wrong password and the response time does not reveal which
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=12))


class AccountError(ValueError):
    """A rule about usernames or passwords was broken. The message is shown to the user."""


def check_username(username):
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise AccountError("Username must be 3 to 32 characters: letters, numbers, dot, dash or underscore.")


def check_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise AccountError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes.")


def hash_password(password):
    check_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


class UserStore:
    """Admin accounts in `<instance>/users.json`."""

    def __init__(self, instance_dir):
        self._path = os.path.join(instance_dir, "users.json")
        self._lock = threading.Lock()
        os.makedirs(instance_dir, exist_ok=True)

    # ── file access ───────────────────────────────────────────────────────

    def _read(self):
        if not os.path.exists(self._path):
            return []
        with open(self._path, "r", encoding="utf-8") as f:
            return json.load(f).get("users", [])

    def _write(self, users):
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"users": users}, f, indent=2)
        os.replace(tmp, self._path)  # never leaves a half-written file

    # ── queries ───────────────────────────────────────────────────────────

    def count(self):
        with self._lock:
            return len(self._read())

    def get(self, user_id):
        with self._lock:
            return next((u for u in self._read() if u["id"] == user_id), None)

    def find(self, username):
        if not isinstance(username, str):
            return None
        with self._lock:
            return next((u for u in self._read() if u["username"].lower() == username.lower()), None)

    def list(self):
        """Accounts without their password hashes."""
        with self._lock:
            return [
                {"username": u["username"], "created": u.get("created"), "last_login": u.get("last_login")}
                for u in self._read()
            ]

    # ── changes ───────────────────────────────────────────────────────────

    def create(self, username, password):
        check_username(username)
        password_hash = hash_password(password)
        with self._lock:
            users = self._read()
            if any(u["username"].lower() == username.lower() for u in users):
                raise AccountError("That username is already taken.")
            user = {
                "id": uuid.uuid4().hex,
                "username": username,
                "password_hash": password_hash,
                "created": _now(),
                "last_login": None,
                "prefs": dict(DEFAULT_PREFS),
            }
            users.append(user)
            self._write(users)
            return user

    def verify(self, username, password):
        """The account if the password is right, otherwise None."""
        user = self.find(username)
        candidate = (password if isinstance(password, str) else "").encode("utf-8")
        if user is None or len(candidate) > MAX_PASSWORD_BYTES:
            # No stored password can be this long; still spend the same time
            bcrypt.checkpw(candidate[:MAX_PASSWORD_BYTES], _DUMMY_HASH)
            return None
        if bcrypt.checkpw(candidate, user["password_hash"].encode("ascii")):
            return user
        return None

    def _update(self, user_id, fields):
        with self._lock:
            users = self._read()
            for u in users:
                if u["id"] == user_id:
                    u.update(fields)
                    self._write(users)
                    return u
        return None

    def set_password(self, user_id, password):
        return self._update(user_id, {"password_hash": hash_password(password)})

    def touch_login(self, user_id):
        return self._update(user_id, {"last_login": _now()})

    def set_prefs(self, user_id, changes):
        """Merge valid preference changes. Raises AccountError on a bad value."""
        allowed = {"mode": MODES, "theme": THEMES, "font": FONTS}
        for key, value in changes.items():
            if key not in allowed:
                raise AccountError(f"Unknown preference: {key}")
            if value not in allowed[key]:
                raise AccountError(f"{key} must be one of: {', '.join(allowed[key])}")
        user = self.get(user_id)
        if user is None:
            return None
        prefs = dict(DEFAULT_PREFS)
        prefs.update(user.get("prefs") or {})
        prefs.update(changes)
        return self._update(user_id, {"prefs": prefs})


def prefs_of(user):
    prefs = dict(DEFAULT_PREFS)
    if user:
        prefs.update(user.get("prefs") or {})
    return prefs


class SignInThrottle:
    """Slows down password guessing.

    After MAX_FAILURES wrong passwords for the same username from the same
    address within WINDOW seconds, further attempts are refused for LOCK
    seconds. A successful sign-in clears the count.
    """

    MAX_FAILURES = 5
    WINDOW = 10 * 60
    LOCK = 60

    def __init__(self, clock=time.time):
        self._clock = clock
        self._failures = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username, address):
        return ((username or "").lower(), address or "")

    def wait_seconds(self, username, address):
        """Seconds until another attempt is allowed (0 = allowed now)."""
        now = self._clock()
        with self._lock:
            recent = [t for t in self._failures.get(self._key(username, address), []) if now - t < self.WINDOW]
            if len(recent) < self.MAX_FAILURES:
                return 0
            return max(0, int(recent[-1] + self.LOCK - now + 0.999))

    def failed(self, username, address):
        now = self._clock()
        with self._lock:
            key = self._key(username, address)
            recent = [t for t in self._failures.get(key, []) if now - t < self.WINDOW]
            recent.append(now)
            self._failures[key] = recent

    def succeeded(self, username, address):
        with self._lock:
            self._failures.pop(self._key(username, address), None)
