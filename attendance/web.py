"""
attendance/web.py
Schedules, the absence list and the review queue.

GET  /api/schedules                  groups with their schedules and member counts
PUT  /api/schedules {groups}         replace every group's schedule (Default must stay)
GET  /api/absent/today               who is scheduled today and has not been seen
GET  /api/review?state=pending       records waiting for review (or confirmed / rejected)
POST /api/review/<id> {decision}     confirm or reject one
"""

from datetime import datetime

from flask import Blueprint, jsonify, request

from attendance.schedules import (
    DEFAULT_GROUP, ScheduleError, apply_schedules, is_working_day, parse_time, schedules_of,
)
from attendance.summary import counts

REVIEW_STATES = ("pending", "confirmed", "rejected")
DECISIONS = {"confirm": "confirmed", "reject": "rejected"}


def create_blueprint(get_db, people, load_config, save_config, audit_event, current_username,
                     on_review, clock=datetime.now):
    bp = Blueprint("attendance_web", __name__)

    @bp.errorhandler(ScheduleError)
    def schedule_error(exc):
        return jsonify({"error": str(exc), "field": exc.field}), 400

    # ── schedules ─────────────────────────────────────────────────────────

    def groups_payload(cfg):
        members = {}
        for p in people.list()[0]:
            members[p["group"]] = members.get(p["group"], 0) + 1
        return [dict(schedule, name=name, members=members.get(name, 0))
                for name, schedule in schedules_of(cfg).items()]

    @bp.route("/api/schedules", methods=["GET"])
    def schedules_get():
        return jsonify({"groups": groups_payload(load_config()), "default": DEFAULT_GROUP})

    @bp.route("/api/schedules", methods=["PUT"])
    def schedules_put():
        data = request.get_json(silent=True) or {}
        groups = data.get("groups")
        if not isinstance(groups, dict) or not groups:
            return jsonify({"error": "Send the groups as an object of name: schedule."}), 400
        cfg = load_config()
        before = schedules_of(cfg)
        cleaned = {}
        for name, sched in groups.items():
            key = name if name == DEFAULT_GROUP else " ".join(str(name).split())
            cleaned[key] = sched
        if DEFAULT_GROUP not in cleaned:
            return jsonify({"error": "The Default group cannot be removed.", "field": "name"}), 400
        removed = [g for g in before if g not in cleaned]
        busy = [g for g in removed if any(p["group"] == g for p in people.list()[0])]
        if busy:
            return jsonify({"error": f"Move everyone out of {busy[0]} before removing it.", "field": "name"}), 409
        apply_schedules(cfg, cleaned)
        save_config(cfg)
        after = schedules_of(cfg)
        audit_event("settings.schedules", details={
            "added": sorted(g for g in after if g not in before),
            "removed": sorted(removed),
            "changed": sorted(g for g in after if g in before and after[g] != before[g]),
        })
        return jsonify({"ok": True, "groups": groups_payload(cfg)})

    # ── absence ───────────────────────────────────────────────────────────

    @bp.route("/api/absent/today", methods=["GET"])
    def absent_today():
        """Everyone whose group works today and who has not been seen yet.
        `overdue` is true once their late cut-off has passed."""
        now = clock()
        today = now.strftime("%Y-%m-%d")
        cfg = load_config()
        scheds = schedules_of(cfg)
        db = get_db()
        records = db.find_logs(today, today)
        if records is None:
            return jsonify({"error": "Database offline. The absence list needs the database.",
                            "db_connected": False}), 503
        seen = {r.get("person_id") for r in records if r.get("status") != "Unknown" and counts(r)}
        missing, scheduled = [], 0
        for p in people.list()[0]:
            sched = scheds.get(p["group"], scheds[DEFAULT_GROUP])
            created = (p.get("created") or "")[:10]
            if not is_working_day(sched, now) or (created and created > today):
                continue
            scheduled += 1
            if p["person_id"] in seen:
                continue
            missing.append({
                "person_id": p["person_id"], "name": p["name"], "group": p["group"],
                "late_after": sched["late_after"],
                "overdue": now.time() >= parse_time(sched["late_after"]),
            })
        missing.sort(key=lambda m: (not m["overdue"], m["late_after"], m["name"].casefold()))
        cutoffs = sorted({m["late_after"] for m in missing})
        return jsonify({
            "time": now.strftime("%H:%M"),
            "scheduled": scheduled,
            "missing": missing,
            "next_cutoff": next((c for c in cutoffs if parse_time(c) > now.time()), None),
        })

    # ── review queue ──────────────────────────────────────────────────────

    @bp.route("/api/review", methods=["GET"])
    def review_list():
        state = request.args.get("state", "pending")
        if state not in REVIEW_STATES:
            return jsonify({"error": f"state must be one of {', '.join(REVIEW_STATES)}"}), 400
        db = get_db()
        records = db.find_review(state)
        if records is None:
            return jsonify({"error": "Database offline. The review queue needs the database.",
                            "db_connected": False}), 503
        return jsonify({"state": state, "records": records, "pending": db.count_review("pending") or 0})

    @bp.route("/api/review/<record_id>", methods=["POST"])
    def review_decide(record_id):
        data = request.get_json(silent=True) or {}
        decision = DECISIONS.get(data.get("decision"))
        if decision is None:
            return jsonify({"error": "decision must be confirm or reject"}), 400
        if not record_id.isalnum():
            return jsonify({"error": "Invalid record ID"}), 400
        db = get_db()
        if not db.is_connected:
            return jsonify({"error": "Database offline. Nothing was changed."}), 503
        record = db.set_review(record_id, "pending", decision, current_username(),
                               clock().replace(microsecond=0).isoformat())
        if record is None:
            if not db.is_connected:
                return jsonify({"error": "Database offline. Nothing was changed."}), 503
            return jsonify({"error": "That record is not waiting for review."}), 404
        audit_event(f"review.{decision}", target=record["id"], details={
            "person_id": record.get("person_id"), "name": record.get("name"),
            "date": record.get("date"), "time_in": record.get("time_in"),
            "distance": record.get("confidence"),
        })
        on_review(record)
        return jsonify({"ok": True, "record": record, "pending": db.count_review("pending") or 0})

    return bp
