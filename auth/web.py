"""
auth/web.py
Sign-in pages, the gate in front of every route, and the account APIs.

Every page and API route needs a signed-in admin, except the sign-in page,
the first-run setup page and static files. API calls without a session get
401 JSON; page requests are sent to the sign-in page. Socket.IO connections
are checked separately in server.py.
"""

import os
import re
import secrets
import time
from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user

from auth.audit import ACTIONS
from auth.store import AccountError, SignInThrottle, prefs_of

# Destructive actions need a password entered within this many seconds
FRESH_SECONDS = 15 * 60
SESSION_SECONDS = 12 * 60 * 60

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PUBLIC_ENDPOINTS = {"auth.login", "auth.setup", "static"}


class User(UserMixin):
    def __init__(self, record):
        self.id = record["id"]
        self.username = record["username"]
        self.record = record


def load_secret_key(instance_dir):
    """FACE_WEB_SECRET_KEY if set, otherwise a random key kept in the instance folder."""
    key = os.environ.get("FACE_WEB_SECRET_KEY", "").strip()
    if key:
        return key
    path = os.path.join(instance_dir, "secret_key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    os.makedirs(instance_dir, exist_ok=True)
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def client_address():
    return request.remote_addr or ""


def current_username():
    try:
        return current_user.username if current_user.is_authenticated else None
    except Exception:
        return None


def safe_next(target):
    """Only ever redirect to a path on this site."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def wants_json():
    path = request.path
    return path.startswith("/api/") or path == "/video_feed"


def fresh_required(view):
    """Refuse with 403 unless the password was entered in the last FRESH_SECONDS."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if time.time() - session.get("auth_at", 0) > FRESH_SECONDS:
            return jsonify({"error": "Confirm your password to continue.", "fresh_required": True}), 403
        return view(*args, **kwargs)
    return wrapper


def init_auth(app, store, audit):
    """Wire sessions, the gate and the auth routes into `app`. Returns `audit_event`."""
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=SESSION_SECONDS,
    )
    manager = LoginManager(app)
    manager.session_protection = "strong"
    throttle = SignInThrottle()
    app.extensions["face_web_throttle"] = throttle

    @manager.user_loader
    def load_user(user_id):
        record = store.get(user_id)
        return User(record) if record else None

    def audit_event(action, target=None, details=None, user=None):
        audit.record(action, user=user or current_username(), target=target,
                     details=details, address=client_address())

    def sign_in(record):
        session.clear()  # a new session id on every sign-in
        login_user(User(record))
        session.permanent = True
        session["auth_at"] = time.time()
        store.touch_login(record["id"])

    @app.before_request
    def require_sign_in():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if store.count() == 0:
            if wants_json():
                return jsonify({"error": "No admin account exists yet.", "setup_required": True}), 401
            return redirect(url_for("auth.setup"))
        if current_user.is_authenticated:
            return None
        if wants_json():
            return jsonify({"error": "Sign in required.", "login_required": True}), 401
        return redirect(url_for("auth.login", next=request.path))

    bp = Blueprint("auth", __name__)

    # ── pages ─────────────────────────────────────────────────────────────

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        if store.count() == 0:
            return redirect(url_for("auth.setup"))
        next_url = safe_next(request.values.get("next"))
        if request.method == "GET":
            if current_user.is_authenticated:
                return redirect(next_url)
            return render_template("login.html", error=None, username="", next=next_url)

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        address = client_address()

        wait = throttle.wait_seconds(username, address)
        if wait:
            audit_event("auth.login_failed", target=username, details={"reason": "too many attempts"})
            return render_template("login.html", username=username, next=next_url,
                                   error=f"Too many attempts. Try again in {wait} seconds."), 429

        record = store.verify(username, password)
        if record is None:
            throttle.failed(username, address)
            audit_event("auth.login_failed", target=username, details={"reason": "wrong username or password"})
            return render_template("login.html", username=username, next=next_url,
                                   error="Wrong username or password."), 401

        throttle.succeeded(username, address)
        sign_in(record)
        audit_event("auth.login", user=record["username"])
        return redirect(next_url)

    @bp.route("/logout", methods=["POST"])
    def logout():
        if current_user.is_authenticated:
            audit_event("auth.logout")
        logout_user()
        session.clear()
        return redirect(url_for("auth.login"))

    @bp.route("/setup", methods=["GET", "POST"])
    def setup():
        """Create the first admin. Only works while no account exists."""
        if store.count() > 0:
            return redirect(url_for("auth.login"))
        if request.method == "GET":
            return render_template("setup.html", error=None, username="")

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if password != (request.form.get("confirm") or ""):
            return render_template("setup.html", username=username,
                                   error="The two passwords do not match."), 400
        try:
            record = store.create(username, password)
        except AccountError as exc:
            return render_template("setup.html", username=username, error=str(exc)), 400
        sign_in(record)
        audit_event("auth.setup", user=record["username"])
        return redirect("/")

    # ── account APIs ──────────────────────────────────────────────────────

    @bp.route("/api/me", methods=["GET"])
    def me():
        return jsonify({"username": current_user.username, "prefs": prefs_of(current_user.record)})

    @bp.route("/api/me/preferences", methods=["PUT"])
    def save_preferences():
        changes = request.get_json(silent=True)
        if not isinstance(changes, dict) or not changes:
            return jsonify({"error": "Send the preferences to change as a JSON object."}), 400
        try:
            record = store.set_prefs(current_user.id, changes)
        except AccountError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "prefs": prefs_of(record)})

    def check_own_password(password, purpose):
        """None if right, otherwise a (response, status) to return."""
        username, address = current_user.username, client_address()
        wait = throttle.wait_seconds(username, address)
        if wait:
            return jsonify({"error": f"Too many attempts. Try again in {wait} seconds."}), 429
        if store.verify(username, password) is None:
            throttle.failed(username, address)
            audit_event("auth.login_failed", target=username, details={"reason": purpose})
            # 400, not 401: the session itself is still valid
            return jsonify({"error": "Wrong password."}), 400
        throttle.succeeded(username, address)
        return None

    @bp.route("/api/auth/confirm", methods=["POST"])
    def confirm_password():
        data = request.get_json(silent=True) or {}
        refused = check_own_password(data.get("password") or "", "password confirmation")
        if refused:
            return refused
        session["auth_at"] = time.time()
        return jsonify({"ok": True})

    @bp.route("/api/auth/password", methods=["POST"])
    def change_password():
        data = request.get_json(silent=True) or {}
        refused = check_own_password(data.get("current") or "", "password change")
        if refused:
            return refused
        try:
            store.set_password(current_user.id, data.get("new") or "")
        except AccountError as exc:
            return jsonify({"error": str(exc)}), 400
        session["auth_at"] = time.time()
        audit_event("auth.password_changed")
        return jsonify({"ok": True})

    @bp.route("/api/users", methods=["GET"])
    def list_users():
        return jsonify(store.list())

    @bp.route("/api/users", methods=["POST"])
    @fresh_required
    def add_user():
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        try:
            store.create(username, data.get("password") or "")
        except AccountError as exc:
            return jsonify({"error": str(exc)}), 400
        audit_event("user.created", target=username)
        return jsonify({"ok": True, "users": store.list()}), 201

    @bp.route("/api/audit", methods=["GET"])
    def audit_list():
        try:
            limit = max(1, min(int(request.args.get("limit", 500)), 2000))
        except ValueError:
            return jsonify({"error": "limit must be a number"}), 400
        dates = {key: request.args.get(key) or None for key in ("from", "to")}
        for key, value in dates.items():
            if value and not DATE_RE.match(value):
                return jsonify({"error": f"{key} must be a date like 2026-01-31"}), 400
        entries, total, db_ok = audit.query(
            user=request.args.get("user") or None,
            action=request.args.get("action") or None,
            date_from=dates["from"],
            date_to=dates["to"],
            limit=limit,
        )
        return jsonify({
            "entries": entries,
            "total": total,
            "db_available": db_ok,
            "actions": ACTIONS,
            "users": [u["username"] for u in store.list()],
        })

    app.register_blueprint(bp)
    return audit_event
