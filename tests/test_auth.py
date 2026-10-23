"""
tests/test_auth.py
Accounts, sign-in, the sign-in gate, password confirmation for destructive
actions, and the account APIs.

Uses an isolated temp config, so accounts and the secret key go to a temporary
instance folder. Never touches the real config.json or instance/.

Run: python tests/test_auth.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def file_hash(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


REAL_CONFIG = os.path.join(PROJECT_ROOT, "config.json")
REAL_USERS = os.path.join(PROJECT_ROOT, "instance", "users.json")
_cfg_hash_before = file_hash(REAL_CONFIG)
_users_hash_before = file_hash(REAL_USERS)

# Setup isolated temp config
tmp_dir = tempfile.mkdtemp(prefix="face_web_test_")
tmp_cfg_path = os.path.join(tmp_dir, "config.json")
if os.path.exists(REAL_CONFIG):
    with open(REAL_CONFIG, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)

cfg_copy["mongodb_db"] = "face_attendance_test"
cfg_copy["mongodb_uri"] = "mongodb://tester:s3cret-pass@localhost:27017"
with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)

os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path
os.environ.pop("FACE_WEB_SECRET_KEY", None)

import server
from auth.store import AccountError, SignInThrottle, UserStore
from auth.web import FRESH_SECONDS, load_secret_key, safe_next
from helpers import TEST_PASSWORD, TEST_USER, sign_in

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}  -- {detail}")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def location(resp):
    return resp.headers.get("Location", "")


throttle = server.app.extensions["face_web_throttle"]

# ──────────────────────────────────────────────────────────────────────────
section("1. First run: no account exists yet")

check("Instance folder is the temporary one",
      os.path.dirname(server.INSTANCE_DIR) == tmp_dir, server.INSTANCE_DIR)
check("No accounts at start", server.users.count() == 0)

anon = server.app.test_client()
res = anon.get("/")
check("/ sends to /setup", res.status_code == 302 and location(res).endswith("/setup"), location(res))
res = anon.get("/login")
check("/login sends to /setup", res.status_code == 302 and location(res).endswith("/setup"), location(res))
res = anon.get("/api/stats/today")
check("API answers 401 setup_required",
      res.status_code == 401 and res.get_json().get("setup_required") is True, str(res.get_json()))
res = anon.get("/setup")
check("/setup page loads", res.status_code == 200 and b'name="confirm"' in res.data)

# ──────────────────────────────────────────────────────────────────────────
section("2. Setup validation and first admin")

res = anon.post("/setup", data={"username": "admin", "password": "long-enough-1", "confirm": "other-pass-1"})
check("Mismatched passwords -> 400", res.status_code == 400 and b"do not match" in res.data)
res = anon.post("/setup", data={"username": "a b", "password": "long-enough-1", "confirm": "long-enough-1"})
check("Bad username -> 400", res.status_code == 400 and b"Username must be" in res.data)
res = anon.post("/setup", data={"username": "admin", "password": "short", "confirm": "short"})
check("Short password -> 400", res.status_code == 400 and b"at least 8" in res.data)
check("Nothing created by failed attempts", server.users.count() == 0)

setup_client = server.app.test_client()
res = setup_client.post("/setup", data={"username": TEST_USER, "password": TEST_PASSWORD,
                                        "confirm": TEST_PASSWORD})
check("Valid setup -> redirect to /", res.status_code == 302 and location(res).endswith("/"), location(res))
check("One account exists", server.users.count() == 1)
check("Setup signs the new admin in", setup_client.get("/api/me").status_code == 200)

with open(os.path.join(server.INSTANCE_DIR, "users.json"), encoding="utf-8") as f:
    raw = f.read()
check("Password is not stored in plain text", TEST_PASSWORD not in raw)
check("Password is stored as a bcrypt hash", '"password_hash": "$2' in raw)

res = anon.get("/setup")
check("/setup is closed once an account exists",
      res.status_code == 302 and location(res).endswith("/login"), location(res))
res = anon.post("/setup", data={"username": "intruder", "password": "long-enough-1", "confirm": "long-enough-1"})
check("POST /setup cannot add a second account", server.users.count() == 1 and res.status_code == 302)

# ──────────────────────────────────────────────────────────────────────────
section("3. The gate for signed-out visitors")

res = anon.get("/")
check("/ sends to the sign-in page with next",
      res.status_code == 302 and "/login?next=/" in location(res), location(res))
res = anon.get("/api/stats/today")
check("API answers 401 login_required",
      res.status_code == 401 and res.get_json().get("login_required") is True, str(res.get_json()))
res = anon.get("/api/users")
check("Account list needs sign-in", res.status_code == 401)
res = anon.get("/video_feed")
check("Video feed needs sign-in", res.status_code == 401)
res = anon.get("/api/audit")
check("Audit log needs sign-in", res.status_code == 401)
res = anon.get("/static/css/style.css")
check("Static files stay public", res.status_code == 200)
res.close()
res = anon.get("/login")
check("Sign-in page loads", res.status_code == 200 and b'name="password"' in res.data)

sio_anon = server.socketio.test_client(server.app, flask_test_client=anon)
check("Socket connection refused when signed out", not sio_anon.is_connected())

# ──────────────────────────────────────────────────────────────────────────
section("4. Signing in")

c = server.app.test_client()
res = sign_in(c, TEST_USER, "wrong-password")
check("Wrong password -> 401", res.status_code == 401)
check("Wrong password message", b"Wrong username or password." in res.data)
res = sign_in(c, "nobody", "wrong-password")
check("Unknown user gets the same message",
      res.status_code == 401 and b"Wrong username or password." in res.data)
check("Still signed out", c.get("/api/me").status_code == 401)
throttle.succeeded(TEST_USER, "127.0.0.1")
throttle.succeeded("nobody", "127.0.0.1")

res = sign_in(c, TEST_USER.upper(), TEST_PASSWORD)
check("Username is case-insensitive", res.status_code == 302)
res = c.get("/api/me")
check("/api/me returns the account", res.status_code == 200 and res.get_json().get("username") == TEST_USER)
check("/api/me never returns the hash", "password_hash" not in json.dumps(res.get_json()))
check("Last sign-in time is recorded", server.users.find(TEST_USER).get("last_login") is not None)

c2 = server.app.test_client()
res = c2.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/api/status"})
check("next is followed", location(res).endswith("/api/status"), location(res))
res = c2.get("/login")
check("Signed-in visit to /login goes home", res.status_code == 302)

for bad in ("//evil.example", "https://evil.example/", "/\\evil.example", "evil", ""):
    check(f"Unsafe next {bad!r} becomes /", safe_next(bad) == "/")
check("Local next is kept", safe_next("/api/status") == "/api/status")

c3 = server.app.test_client()
res = c3.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "//evil.example"})
check("Sign-in never redirects off site", location(res) in ("/", "http://localhost/"), location(res))

cookie = res.headers.get("Set-Cookie", "")
check("Session cookie is HttpOnly", "HttpOnly" in cookie, cookie)
check("Session cookie is SameSite=Lax", "SameSite=Lax" in cookie, cookie)

sio = server.socketio.test_client(server.app, flask_test_client=c)
check("Socket connection accepted when signed in", sio.is_connected())
if sio.is_connected():
    sio.disconnect()

# ──────────────────────────────────────────────────────────────────────────
section("5. Throttling password guesses")

t = server.app.test_client()
for _ in range(SignInThrottle.MAX_FAILURES):
    sign_in(t, TEST_USER, "wrong-password")
res = sign_in(t, TEST_USER, TEST_PASSWORD)
check("Locked after 5 failures, even with the right password", res.status_code == 429)
check("Lock message gives the wait", b"Too many attempts" in res.data)
check("Still signed out while locked", t.get("/api/me").status_code == 401)
throttle.succeeded(TEST_USER, "127.0.0.1")

now = [1000.0]
th = SignInThrottle(clock=lambda: now[0])
for _ in range(5):
    th.failed("Admin", "1.2.3.4")
check("Throttle locks after 5 failures", th.wait_seconds("admin", "1.2.3.4") == 60)
check("Lock is per address", th.wait_seconds("admin", "5.6.7.8") == 0)
now[0] += 61
check("Lock ends after 60 seconds", th.wait_seconds("admin", "1.2.3.4") == 0)
th.failed("admin", "1.2.3.4")
check("One more failure locks again (still in the window)", th.wait_seconds("admin", "1.2.3.4") > 0)
th.succeeded("admin", "1.2.3.4")
check("Success clears the count", th.wait_seconds("admin", "1.2.3.4") == 0)
th2 = SignInThrottle(clock=lambda: now[0])
for _ in range(4):
    th2.failed("x", "a")
now[0] += SignInThrottle.WINDOW + 1
th2.failed("x", "a")
check("Old failures fall out of the window", th2.wait_seconds("x", "a") == 0)

# ──────────────────────────────────────────────────────────────────────────
section("6. Signing out")

out = server.app.test_client()
sign_in(out)
res = out.get("/logout")
check("GET /logout is not allowed", res.status_code == 405)
res = out.post("/logout")
check("POST /logout -> sign-in page", res.status_code == 302 and location(res).endswith("/login"), location(res))
check("Signed out after logout", out.get("/api/me").status_code == 401)

# ──────────────────────────────────────────────────────────────────────────
section("7. Password confirmation for destructive actions")

f = server.app.test_client()
sign_in(f)
with f.session_transaction() as s:
    s["auth_at"] = time.time() - FRESH_SECONDS - 5
res = f.delete("/api/persons/99999")
check("Stale sign-in -> 403 fresh_required",
      res.status_code == 403 and res.get_json().get("fresh_required") is True, str(res.get_json()))
res = f.delete("/api/attendance")
check("Clear today also needs it", res.status_code == 403 and res.get_json().get("fresh_required") is True)
res = f.post("/api/users", json={"username": "second", "password": "long-enough-2"})
check("Adding an admin also needs it", res.status_code == 403)
res = f.get("/api/stats/today")
check("Reading still works with a stale sign-in", res.status_code == 200)

res = f.post("/api/auth/confirm", json={"password": "wrong-password"})
check("Wrong confirmation -> 400 (session kept)", res.status_code == 400)
check("Session survives a wrong confirmation", f.get("/api/me").status_code == 200)
throttle.succeeded(TEST_USER, "127.0.0.1")
res = f.post("/api/auth/confirm", json={"password": TEST_PASSWORD})
check("Right confirmation -> 200", res.status_code == 200 and res.get_json().get("ok") is True)
res = f.delete("/api/persons/99999")
# Past the password check: 404 for a missing person, or 503 because this
# test's database address is deliberately unreachable
check("Destructive route runs after confirming (no password request)",
      res.status_code in (404, 503) and not (res.get_json() or {}).get("fresh_required"), str(res.status_code))

# ──────────────────────────────────────────────────────────────────────────
section("8. Accounts API")

res = f.get("/api/users")
users = res.get_json()
check("GET /api/users lists the admin", res.status_code == 200 and [u["username"] for u in users] == [TEST_USER])
check("Account list has no hashes", all("password_hash" not in u for u in users))

res = f.post("/api/users", json={"username": TEST_USER.upper(), "password": "long-enough-2"})
check("Duplicate username (any case) -> 400", res.status_code == 400 and "taken" in res.get_json()["error"])
res = f.post("/api/users", json={"username": "x", "password": "long-enough-2"})
check("Bad username -> 400", res.status_code == 400)
res = f.post("/api/users", json={"username": "second", "password": "short"})
check("Short password -> 400", res.status_code == 400)
res = f.post("/api/users", json={"username": "second", "password": "long-enough-2"})
check("Valid new admin -> 201", res.status_code == 201)
check("Response lists both admins", [u["username"] for u in res.get_json()["users"]] == [TEST_USER, "second"])
second = server.app.test_client()
check("New admin can sign in", sign_in(second, "second", "long-enough-2").status_code == 302)

# ──────────────────────────────────────────────────────────────────────────
section("9. Changing your password")

p = server.app.test_client()
sign_in(p, "second", "long-enough-2")
res = p.post("/api/auth/password", json={"current": "wrong-password", "new": "new-password-2"})
check("Wrong current password -> 400", res.status_code == 400)
throttle.succeeded("second", "127.0.0.1")
res = p.post("/api/auth/password", json={"current": "long-enough-2", "new": "short"})
check("Short new password -> 400", res.status_code == 400 and "at least 8" in res.get_json()["error"])
res = p.post("/api/auth/password", json={"current": "long-enough-2", "new": "new-password-2"})
check("Valid change -> 200", res.status_code == 200)
check("Old password no longer works", sign_in(server.app.test_client(), "second", "long-enough-2").status_code == 401)
throttle.succeeded("second", "127.0.0.1")
check("New password works", sign_in(server.app.test_client(), "second", "new-password-2").status_code == 302)

# ──────────────────────────────────────────────────────────────────────────
section("10. Password rules in the store")

store_dir = tempfile.mkdtemp(prefix="face_web_store_")
store = UserStore(store_dir)
try:
    store.create("longpw", "x" * 73)
    check("Password over 72 bytes is refused", False, "no error")
except AccountError:
    check("Password over 72 bytes is refused", True)
try:
    store.create("multibyte", "é" * 37)  # 74 bytes in UTF-8
    check("Byte length counts, not characters", False, "no error")
except AccountError:
    check("Byte length counts, not characters", True)
store.create("exact", "y" * 72)
check("Exactly 72 bytes is allowed", store.verify("exact", "y" * 72) is not None)
check("73 bytes does not match a 72-byte password", store.verify("exact", "y" * 73) is None)
check("Wrong type is rejected safely", store.verify("exact", None) is None)
check("Unknown user returns None", store.verify("ghost", "anything-123") is None)
shutil.rmtree(store_dir, ignore_errors=True)

# ──────────────────────────────────────────────────────────────────────────
section("11. Secret key and database address")

key_path = os.path.join(server.INSTANCE_DIR, "secret_key")
check("Secret key file created in the instance folder", os.path.exists(key_path))
with open(key_path, encoding="utf-8") as fh:
    stored_key = fh.read().strip()
check("Secret key is long and random", len(stored_key) == 64)
check("App uses the stored key", server.app.secret_key == stored_key)
check("Key is reused on restart", load_secret_key(server.INSTANCE_DIR) == stored_key)
os.environ["FACE_WEB_SECRET_KEY"] = "from-the-environment"
check("FACE_WEB_SECRET_KEY wins", load_secret_key(server.INSTANCE_DIR) == "from-the-environment")
os.environ.pop("FACE_WEB_SECRET_KEY", None)

cfg = f.get("/api/config").get_json()
check("Database password is masked in /api/config",
      "s3cret-pass" not in json.dumps(cfg) and "***" in cfg.get("mongodb_uri", ""), cfg.get("mongodb_uri"))

# ──────────────────────────────────────────────────────────────────────────
section("12. Real files untouched")

check("config.json hash unchanged", file_hash(REAL_CONFIG) == _cfg_hash_before)
check("instance/users.json unchanged", file_hash(REAL_USERS) == _users_hash_before)

shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
