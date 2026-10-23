"""
tests/helpers.py
Signing in for the tests. Every page, API route and socket needs a signed-in
admin, so each test creates one in its own temporary instance folder.

Import server.py only after FACE_WEB_CONFIG points at a temporary config:
the instance folder sits next to the config in use, so the test admin never
reaches the real instance/users.json.
"""

import atexit
import http.cookiejar
import os
import urllib.parse
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_USER = "tester"
TEST_PASSWORD = "test-password-1"
USER_AGENT = "face-insight-tests"
TEST_DB = "face_attendance_test"
TEST_MONGO_URI = "mongodb://localhost:27017"

_cleanup_registered = False


def _clean_test_db():
    """Remove what sign-ins and registrations wrote: audit entries, people
    and migration markers. Only ever the test database.

    Opens its own connection: the tests close theirs before exiting.
    """
    try:
        from pymongo import MongoClient
        client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=2000)
        for name in ("audit_log", "persons", "app_meta"):
            client[TEST_DB][name].delete_many({})
        client.close()
    except Exception:
        pass  # MongoDB not running: there is nothing to clean


def _register_cleanup():
    global _cleanup_registered
    if not _cleanup_registered:
        atexit.register(_clean_test_db)
        _cleanup_registered = True


def ensure_admin(server, username=TEST_USER, password=TEST_PASSWORD):
    """Create the test admin unless it exists. Refuses to touch the real instance folder."""
    real_instance = os.path.join(PROJECT_ROOT, "instance")
    if os.path.abspath(server.INSTANCE_DIR) == os.path.abspath(real_instance):
        raise RuntimeError("Tests must run with FACE_WEB_CONFIG set to a temporary config.")
    _register_cleanup()
    if server.users.find(username) is None:
        server.users.create(username, password)


def register_people(server, people):
    """Register {id: name} with consent, through the app's own registry.
    Needs the test database. Only ever the test database."""
    db = server.mongo_db
    if getattr(db, "_db_name", None) != TEST_DB:
        raise RuntimeError("register_people only runs against the test database.")
    _register_cleanup()
    for pid, name in people.items():
        server.people.register(int(pid), name, True)


def sign_in(client, username=TEST_USER, password=TEST_PASSWORD):
    """Sign a Flask test client in. Returns the response."""
    return client.post("/login", data={"username": username, "password": password})


def signed_in_client(server):
    """A Flask test client with a fresh admin session."""
    ensure_admin(server)
    client = server.app.test_client()
    resp = sign_in(client)
    if resp.status_code != 302:
        raise RuntimeError(f"Test sign-in failed with status {resp.status_code}")
    return client


def socket_client(server, client):
    """A Socket.IO test client that shares the Flask test client's session."""
    return server.socketio.test_client(server.app, flask_test_client=client)


class LiveSession:
    """Signs in to a real server over HTTP for the tests that run one.

    Installs a cookie-aware opener, so plain urllib.request.urlopen calls in
    the test carry the session. `socket_headers` gives the same session to a
    socketio.Client. Both send the same User-Agent, because the session is
    tied to it.
    """

    def __init__(self, base_url, server):
        ensure_admin(server)
        self.base_url = base_url
        self.jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        opener.addheaders = [("User-Agent", USER_AGENT)]
        urllib.request.install_opener(opener)
        body = urllib.parse.urlencode({"username": TEST_USER, "password": TEST_PASSWORD}).encode()
        resp = urllib.request.urlopen(f"{base_url}/login", data=body)
        if resp.status != 200 or resp.url.rstrip("/") != base_url:
            raise RuntimeError(f"Live sign-in failed, ended at {resp.url}")

    @property
    def socket_headers(self):
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.jar)
        return {"Cookie": cookie, "User-Agent": USER_AGENT}
