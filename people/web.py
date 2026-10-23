"""
people/web.py
People and Reports APIs.

GET   /api/people                       everyone registered, with image count and last seen
GET   /api/people/<id>?from=&to=        one person: daily summary, totals and the records behind it
PATCH /api/people/<id>  {name}          rename
GET   /api/reports?from=&to=&people=    report preview (people: comma-separated IDs, blank = everyone)
GET   /api/reports/export.csv | .pdf    the same report as a file
"""

from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request

from attendance.reports import RangeError, build_report, parse_range, to_csv, to_pdf
from attendance.summary import working_days_of
from people.registry import PersonError

OFFLINE = {"error": "Database offline. Reports need the database.", "db_connected": False}


def create_blueprint(get_db, people, load_config, audit_event, image_counts, clock=datetime.now):
    bp = Blueprint("people", __name__)

    def rules():
        cfg = load_config()
        att = cfg.get("attendance") or {}
        return working_days_of(cfg), att.get("working_hours_end", "17:30")

    def range_args(default_days=30):
        """(from, to) from the query string, or the last `default_days` days."""
        date_from, date_to = request.args.get("from"), request.args.get("to")
        if not date_from and not date_to:
            today = clock().date()
            return (today - timedelta(days=default_days - 1)).isoformat(), today.isoformat()
        return parse_range(date_from, date_to)

    def make_report(person_list, keep_records=False):
        date_from, date_to = range_args()
        db = get_db()
        records = db.find_logs(date_from, date_to, [p["person_id"] for p in person_list])
        if records is None:
            return None
        working_days, day_end = rules()
        report = build_report(records, person_list, date_from, date_to, clock(), working_days, day_end)
        if keep_records:
            report["records"] = records
        return report

    def chosen_people():
        """The people named in ?people=, in ID order. Raises PersonError."""
        everyone, _ = people.list()
        raw = (request.args.get("people") or "").strip()
        if not raw:
            return everyone
        try:
            wanted = {int(x) for x in raw.split(",") if x.strip()}
        except ValueError:
            raise PersonError("people must be a comma-separated list of IDs.") from None
        chosen = [p for p in everyone if p["person_id"] in wanted]
        missing = wanted - {p["person_id"] for p in chosen}
        if missing:
            raise PersonError(f"Unknown person ID: {', '.join(str(m) for m in sorted(missing))}.", 404)
        return chosen

    @bp.errorhandler(PersonError)
    def person_error(exc):
        return jsonify({"error": str(exc)}), exc.status

    @bp.errorhandler(RangeError)
    def range_error(exc):
        return jsonify({"error": str(exc)}), 400

    # ── people ────────────────────────────────────────────────────────────

    @bp.route("/api/people", methods=["GET"])
    def people_list():
        everyone, db_ok = people.list()
        counts = image_counts()
        db = get_db()
        last = db.last_seen_by_person() if db_ok else {}
        result = []
        for p in everyone:
            result.append({
                "person_id": p["person_id"],
                "name": p["name"],
                "consent_at": p.get("consent_at"),
                "created": p.get("created"),
                "saved": p.get("saved", False),
                "image_count": counts.get(p["person_id"], 0),
                "last_seen": (last or {}).get(p["person_id"]),
            })
        return jsonify({"people": result, "db_connected": db_ok})

    @bp.route("/api/people/<int:person_id>", methods=["GET"])
    def person_detail(person_id):
        person = people.get(person_id)
        if person is None:
            return jsonify({"error": "Person not found."}), 404
        report = make_report([person], keep_records=True)
        if report is None:
            return jsonify(dict(OFFLINE, person=person)), 503
        records = [r for r in report["records"] if r.get("status") != "Unknown"]
        return jsonify({
            "person": dict(person, image_count=image_counts().get(person_id, 0)),
            "from": report["from"],
            "to": report["to"],
            "days": list(reversed(report["rows"])),
            "totals": report["totals"][0],
            "records": records,
        })

    @bp.route("/api/people/<int:person_id>", methods=["PATCH"])
    def person_rename(person_id):
        data = request.get_json(silent=True) or {}
        old_name, person = people.rename(person_id, data.get("name"))
        if old_name != person["name"]:
            audit_event("person.renamed", target=person_id, details={"from": old_name, "to": person["name"]})
        return jsonify({"ok": True, "person": person})

    # ── reports ───────────────────────────────────────────────────────────

    @bp.route("/api/reports", methods=["GET"])
    def report_preview():
        chosen = chosen_people()
        report = make_report(chosen)
        if report is None:
            return jsonify(OFFLINE), 503
        return jsonify(report)

    def export(fmt):
        chosen = chosen_people()
        report = make_report(chosen)
        if report is None:
            return jsonify(OFFLINE), 503
        if not report["rows"]:
            return jsonify({"error": "Nothing to export for this range and these people."}), 404
        name = f"attendance-report_{report['from']}_to_{report['to']}"
        if fmt == "csv":
            body, mimetype = to_csv(report).encode("utf-8"), "text/csv; charset=utf-8"
        else:
            body, mimetype = to_pdf(report), "application/pdf"
        audit_event("report.exported", details={
            "format": fmt, "from": report["from"], "to": report["to"],
            "people": len(report["people"]), "rows": len(report["rows"]),
        })
        return Response(body, mimetype=mimetype, headers={
            "Content-Disposition": f'attachment; filename="{name}.{fmt}"',
        })

    @bp.route("/api/reports/export.csv", methods=["GET"])
    def report_csv():
        return export("csv")

    @bp.route("/api/reports/export.pdf", methods=["GET"])
    def report_pdf():
        return export("pdf")

    return bp
