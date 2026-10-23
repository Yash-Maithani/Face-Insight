"""
tests/test_daily_summary.py
The daily summary: one status per person per day, first and last seen.
Checked against a seeded week counted by hand. No server, no database.

Run: python tests/test_daily_summary.py
"""

import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from attendance.summary import (  # noqa: E402
    ABSENT, DEFAULT_WORKING_DAYS, LATE, NOT_YET, PRESENT,
    daily_summary, person_totals, working_days_of,
)

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


def rec(pid, date, time_in, status, manual=False):
    return {"person_id": pid, "date": date, "time_in": time_in, "status": status, "manual": manual}


# The week of Monday 2026-10-19 to Sunday 2026-10-25, working days Monday to Friday
MON, TUE, WED, THU, FRI, SAT, SUN = (f"2026-10-{d}" for d in range(19, 26))
PEOPLE = [
    {"person_id": 1, "name": "Asha Verma", "created": None},
    {"person_id": 2, "name": "Rohan Mehta", "created": None},
    {"person_id": 3, "name": "Neha Kapoor", "created": "2026-10-21T10:00:00"},  # registered Wednesday
]
RECORDS = [
    rec(1, MON, "09:00:00", PRESENT), rec(1, MON, "12:00:00", PRESENT),
    rec(1, TUE, "09:45:00", LATE),
    rec(1, WED, "09:50:00", LATE), rec(1, WED, "10:00:00", PRESENT, manual=True),
    rec(1, SAT, "10:00:00", PRESENT),
    rec(2, TUE, "09:10:00", PRESENT),
    rec(2, THU, "09:40:00", LATE), rec(2, THU, "11:00:00", PRESENT, manual=True),
    rec(2, THU, "12:00:00", LATE, manual=True),
    rec(3, WED, "09:00:00", PRESENT),
    # Unknown faces belong to nobody, even an old record with a guessed ID
    {"person_id": None, "date": MON, "time_in": "08:00:00", "status": "Unknown"},
    rec(2, FRI, "09:00:00", "Unknown"),
]
AFTER_THE_WEEK = datetime(2026, 10, 25, 20, 0)


def rows_for(rows, pid):
    return {r["date"]: r for r in rows if r["person_id"] == pid}


# ──────────────────────────────────────────────────────────────────────────
section("1. A seeded week, counted by hand")

rows = daily_summary(RECORDS, PEOPLE, MON, SUN, AFTER_THE_WEEK, DEFAULT_WORKING_DAYS, "17:30")
a, b, c = rows_for(rows, 1), rows_for(rows, 2), rows_for(rows, 3)

check("Asha: Mon Present, Tue Late, Wed Present, Thu Absent, Fri Absent, Sat Present",
      [a[d]["status"] for d in (MON, TUE, WED, THU, FRI, SAT)] == [PRESENT, LATE, PRESENT, ABSENT, ABSENT, PRESENT],
      str({d: r["status"] for d, r in a.items()}))
check("Asha has no Sunday row (non-working day, no records)", SUN not in a)
check("Rohan: Mon Absent, Tue Present, Wed Absent, Thu Late, Fri Absent",
      [b[d]["status"] for d in (MON, TUE, WED, THU, FRI)] == [ABSENT, PRESENT, ABSENT, LATE, ABSENT],
      str({d: r["status"] for d, r in b.items()}))
check("Rohan has no weekend rows", SAT not in b and SUN not in b)
check("Neha: nothing before she was registered", MON not in c and TUE not in c)
check("Neha: Wed Present, Thu Absent, Fri Absent",
      [c[d]["status"] for d in (WED, THU, FRI)] == [PRESENT, ABSENT, ABSENT], str({d: r["status"] for d, r in c.items()}))
check("14 rows in all (6 + 5 + 3)", len(rows) == 14, str(len(rows)))

totals = {t["person_id"]: t for t in person_totals(rows, PEOPLE)}
check("Asha totals: 3 present, 1 late, 2 absent",
      (totals[1][PRESENT], totals[1][LATE], totals[1][ABSENT]) == (3, 1, 2), str(totals[1]))
check("Rohan totals: 1 present, 1 late, 3 absent",
      (totals[2][PRESENT], totals[2][LATE], totals[2][ABSENT]) == (1, 1, 3), str(totals[2]))
check("Neha totals: 1 present, 0 late, 2 absent",
      (totals[3][PRESENT], totals[3][LATE], totals[3][ABSENT]) == (1, 0, 2), str(totals[3]))
check("Totals keep the order people were given", [t["person_id"] for t in person_totals(rows, PEOPLE)] == [1, 2, 3])
check("Totals add up to the rows", sum(t[PRESENT] + t[LATE] + t[ABSENT] + t[NOT_YET] for t in totals.values()) == len(rows))

# ──────────────────────────────────────────────────────────────────────────
section("2. First seen, last seen and sightings")

check("Asha Monday: first 09:00, last 12:00, 2 sightings",
      (a[MON]["first_seen"], a[MON]["last_seen"], a[MON]["sightings"]) == ("09:00:00", "12:00:00", 2))
check("Single sighting: first and last are the same",
      a[TUE]["first_seen"] == a[TUE]["last_seen"] == "09:45:00")
check("Absent day has no times and no sightings",
      (a[THU]["first_seen"], a[THU]["last_seen"], a[THU]["sightings"]) == ("", "", 0))
check("Manual flag on a day with a manual mark", a[WED]["manual"] is True and a[MON]["manual"] is False)
check("Rows are sorted by date, then name",
      [(r["date"], r["name"]) for r in rows] == sorted((r["date"], r["name"]) for r in rows))

# ──────────────────────────────────────────────────────────────────────────
section("3. Status rules")

check("A manual mark overrides the camera's Late", a[WED]["status"] == PRESENT)
check("The latest manual mark wins", b[THU]["status"] == LATE)
check("Unknown records never count for anyone", b[FRI]["status"] == ABSENT and b[FRI]["sightings"] == 0)
check("Records on a non-working day still count", a[SAT]["status"] == PRESENT)

# ──────────────────────────────────────────────────────────────────────────
section("4. Today and the future")

midday_friday = datetime(2026, 10, 23, 12, 0)
rows = daily_summary(RECORDS, PEOPLE, MON, SUN, midday_friday, DEFAULT_WORKING_DAYS, "17:30")
a = rows_for(rows, 1)
check("Today, before the day ends: Not seen yet", a[FRI]["status"] == NOT_YET)
check("Earlier days are still Absent", a[THU]["status"] == ABSENT)
check("No rows after today without records", not any(r["date"] > FRI and r["sightings"] == 0 for r in rows))

evening_friday = datetime(2026, 10, 23, 17, 30)
rows = daily_summary(RECORDS, PEOPLE, MON, FRI, evening_friday, DEFAULT_WORKING_DAYS, "17:30")
check("Today, once the day has ended: Absent", rows_for(rows, 1)[FRI]["status"] == ABSENT)

seen_today = RECORDS + [rec(1, FRI, "09:05:00", PRESENT)]
rows = daily_summary(seen_today, PEOPLE, FRI, FRI, midday_friday, DEFAULT_WORKING_DAYS, "17:30")
check("Today, already seen: Present", rows_for(rows, 1)[FRI]["status"] == PRESENT)

# ──────────────────────────────────────────────────────────────────────────
section("5. Working days and edge cases")

six_days = (0, 1, 2, 3, 4, 5)
rows = daily_summary(RECORDS, PEOPLE, MON, SUN, AFTER_THE_WEEK, six_days, "17:30")
check("With Saturday as a working day, Rohan is Absent on Saturday", rows_for(rows, 2)[SAT]["status"] == ABSENT)

check("No people: no rows", daily_summary(RECORDS, [], MON, SUN, AFTER_THE_WEEK) == [])
check("Only a weekend: no rows for people without records",
      [r["person_id"] for r in daily_summary(RECORDS, PEOPLE, SAT, SUN, AFTER_THE_WEEK)] == [1])
check("Single-day range works", len(daily_summary([], PEOPLE[:1], MON, MON, AFTER_THE_WEEK)) == 1)

check("working_days_of: default is Monday to Friday", working_days_of({}) == (0, 1, 2, 3, 4))
check("working_days_of: reads the config", working_days_of({"attendance": {"working_days": [5, 0, 0]}}) == (0, 5))
check("working_days_of: bad value falls back", working_days_of({"attendance": {"working_days": [9]}}) == DEFAULT_WORKING_DAYS)
check("working_days_of: empty list falls back", working_days_of({"attendance": {"working_days": []}}) == DEFAULT_WORKING_DAYS)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
