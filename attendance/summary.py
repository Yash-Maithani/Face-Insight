"""
attendance/summary.py
Daily summary per person: first seen, last seen and one status per day.

Built from the stored sightings, which are never changed. Rules:

- A day with records: the status of the first camera sighting (Present or
  Late, decided when it was recorded). A manual mark that day overrides it;
  the latest manual mark wins.
- A working day without records, up to today: Absent. Today only counts as
  Absent once the working day is over; before that it is "Not seen yet".
- Days before a person was registered, future days and non-working days
  without records are left out.
- Unknown faces belong to nobody and are not part of any person's summary.
"""

from datetime import datetime, timedelta

PRESENT, LATE, ABSENT, NOT_YET = "Present", "Late", "Absent", "Not seen yet"
STATUSES = (PRESENT, LATE, ABSENT, NOT_YET)
DEFAULT_WORKING_DAYS = (0, 1, 2, 3, 4)  # Monday to Friday
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def date_range(date_from, date_to):
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def working_days_of(cfg):
    days = (cfg.get("attendance") or {}).get("working_days")
    if isinstance(days, list) and days and all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        return tuple(sorted(set(days)))
    return DEFAULT_WORKING_DAYS


def _day_status(records):
    manual = [r for r in records if r.get("manual") and r.get("status") in (PRESENT, LATE)]
    if manual:
        return max(manual, key=lambda r: r.get("time_in") or "")["status"]
    auto = sorted((r for r in records if r.get("status") in (PRESENT, LATE)), key=lambda r: r.get("time_in") or "")
    return auto[0]["status"] if auto else PRESENT


def daily_summary(records, persons, date_from, date_to, now, working_days=DEFAULT_WORKING_DAYS, day_end="17:30"):
    """Rows sorted by date, then name.

    `records`: attendance records in the range. `persons`: dicts with
    person_id, name and optionally created (ISO time). `now`: a datetime.
    """
    today = now.date().isoformat()
    day_over = now.strftime("%H:%M") >= day_end
    by_key = {}
    for r in records:
        pid = r.get("person_id")
        if pid is None or r.get("status") == "Unknown":
            continue
        by_key.setdefault((pid, r.get("date")), []).append(r)

    rows = []
    for day in date_range(date_from, date_to):
        iso = day.isoformat()
        for p in persons:
            pid = p["person_id"]
            recs = by_key.get((pid, iso))
            if recs:
                times = sorted(r.get("time_in") or "" for r in recs)
                rows.append({
                    "date": iso, "person_id": pid, "name": p["name"],
                    "status": _day_status(recs),
                    "first_seen": times[0], "last_seen": times[-1],
                    "sightings": len(recs),
                    "manual": any(r.get("manual") for r in recs),
                })
                continue
            if iso > today or day.weekday() not in working_days:
                continue
            created = (p.get("created") or "")[:10]
            if created and iso < created:
                continue
            status = NOT_YET if iso == today and not day_over else ABSENT
            rows.append({
                "date": iso, "person_id": pid, "name": p["name"], "status": status,
                "first_seen": "", "last_seen": "", "sightings": 0, "manual": False,
            })
    rows.sort(key=lambda r: (r["date"], r["name"].casefold(), r["person_id"]))
    return rows


def person_totals(rows, persons):
    """Per person: days present, late, absent and not seen yet, in `persons` order."""
    totals = {p["person_id"]: {"person_id": p["person_id"], "name": p["name"],
                               PRESENT: 0, LATE: 0, ABSENT: 0, NOT_YET: 0}
              for p in persons}
    for r in rows:
        if r["person_id"] in totals:
            totals[r["person_id"]][r["status"]] += 1
    return [totals[p["person_id"]] for p in persons]
