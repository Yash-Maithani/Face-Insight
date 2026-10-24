"""
attendance/summary.py
Daily summary per person: first seen, last seen and one status per day.

Built from the stored sightings, which are never changed. Rules:

- A day with records: the status of the first camera sighting (Present or
  Late, decided when it was recorded from the person's group schedule). A
  manual mark that day overrides it; the latest manual mark wins.
- Left early is a flag next to the status: the camera saw the person more
  than once that working day and the last sighting was before the end.
- A working day without records, up to today: Absent. Today only counts as
  Absent once the person's working day is over; before that it is
  "Not seen yet".
- Days before a person was registered, future days and non-working days
  without records are left out.
- Unknown faces belong to nobody. Records waiting for review, or rejected
  in review, do not count until confirmed.
"""

from datetime import datetime, timedelta

from attendance.schedules import left_early, parse_time

PRESENT, LATE, ABSENT, NOT_YET = "Present", "Late", "Absent", "Not seen yet"
LEFT_EARLY = "Left early"
STATUSES = (PRESENT, LATE, ABSENT, NOT_YET)
DEFAULT_WORKING_DAYS = (0, 1, 2, 3, 4)  # Monday to Friday
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
NOT_COUNTED = ("pending", "rejected")


def date_range(date_from, date_to):
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def working_days_of(cfg):
    days = (cfg.get("attendance") or {}).get("working_days")
    if isinstance(days, list) and days and all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        return tuple(sorted(set(days)))
    return DEFAULT_WORKING_DAYS


def counts(record):
    """Whether a record counts towards attendance (not waiting for or rejected in review)."""
    return record.get("review") not in NOT_COUNTED


def _day_status(records):
    manual = [r for r in records if r.get("manual") and r.get("status") in (PRESENT, LATE)]
    if manual:
        return max(manual, key=lambda r: r.get("time_in") or "")["status"]
    auto = sorted((r for r in records if r.get("status") in (PRESENT, LATE)), key=lambda r: r.get("time_in") or "")
    return auto[0]["status"] if auto else PRESENT


def daily_summary(records, persons, date_from, date_to, now, working_days=DEFAULT_WORKING_DAYS,
                  day_end="17:30", schedule_of=None):
    """Rows sorted by date, then name.

    `records`: attendance records in the range. `persons`: dicts with
    person_id, name and optionally created (ISO time). `now`: a datetime.
    `schedule_of(person)` gives that person's schedule; without it every
    person gets `working_days` and `day_end`.
    """
    if schedule_of is None:
        shared = {"working_days": list(working_days), "end": day_end}

        def schedule_of(_person):
            return shared

    today = now.date().isoformat()
    by_key = {}
    for r in records:
        pid = r.get("person_id")
        if pid is None or r.get("status") == "Unknown" or not counts(r):
            continue
        by_key.setdefault((pid, r.get("date")), []).append(r)

    rows = []
    days = date_range(date_from, date_to)
    for p in persons:
        sched = schedule_of(p)
        pid = p["person_id"]
        day_over = now.time() >= parse_time(sched["end"])
        created = (p.get("created") or "")[:10]
        for day in days:
            iso = day.isoformat()
            recs = by_key.get((pid, iso))
            if recs:
                times = sorted(r.get("time_in") or "" for r in recs)
                camera = sorted(r.get("time_in") or "" for r in recs if not r.get("manual"))
                rows.append({
                    "date": iso, "person_id": pid, "name": p["name"],
                    "status": _day_status(recs),
                    "left_early": left_early(sched, day, camera[-1] if camera else "", len(camera))
                    if iso < today or day_over else False,
                    "first_seen": times[0], "last_seen": times[-1],
                    "sightings": len(recs),
                    "manual": any(r.get("manual") for r in recs),
                })
                continue
            if iso > today or day.weekday() not in sched["working_days"]:
                continue
            if created and iso < created:
                continue
            status = NOT_YET if iso == today and not day_over else ABSENT
            rows.append({
                "date": iso, "person_id": pid, "name": p["name"], "status": status, "left_early": False,
                "first_seen": "", "last_seen": "", "sightings": 0, "manual": False,
            })
    rows.sort(key=lambda r: (r["date"], r["name"].casefold(), r["person_id"]))
    return rows


def person_totals(rows, persons):
    """Per person: days present, late, absent, not seen yet and left early, in `persons` order."""
    totals = {p["person_id"]: {"person_id": p["person_id"], "name": p["name"],
                               PRESENT: 0, LATE: 0, ABSENT: 0, NOT_YET: 0, LEFT_EARLY: 0}
              for p in persons}
    for r in rows:
        if r["person_id"] in totals:
            totals[r["person_id"]][r["status"]] += 1
            if r.get("left_early"):
                totals[r["person_id"]][LEFT_EARLY] += 1
    return [totals[p["person_id"]] for p in persons]
