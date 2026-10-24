"""
attendance/schedules.py
Groups and their schedules: working days, start, late cut-off and end.

Every person belongs to one group. The "Default" group always exists; its
schedule is the one in config.json `attendance` (late_after,
working_hours_end, working_days, start), so older configs keep working.
Other groups live in config.json `schedules`. Schedules are kept in the
config, not the database, so lateness is judged correctly while the
database is down.

Times are the local time of the computer running the app, compared as
times of day, not as text.
"""

import re
from datetime import datetime, time

DEFAULT_GROUP = "Default"
DEFAULT_WORKING_DAYS = [0, 1, 2, 3, 4]  # Monday to Friday
GROUP_NAME_RE = re.compile(r"^[^\x00-\x1f]{1,40}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ScheduleError(ValueError):
    """A schedule or group was refused. The message is shown to the user."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def parse_time(value):
    """'HH:MM' -> datetime.time. Raises ScheduleError."""
    if not isinstance(value, str) or not TIME_RE.match(value):
        raise ScheduleError(f"{value!r} is not a time like 09:30.")
    return time(int(value[:2]), int(value[3:]))


def _default_schedule(cfg):
    att = cfg.get("attendance") or {}
    late_after = att.get("late_after", "09:30")
    days = att.get("working_days")
    if not (isinstance(days, list) and days and all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days)):
        days = list(DEFAULT_WORKING_DAYS)
    return {
        "working_days": sorted(set(days)),
        "start": att.get("start", late_after),
        "late_after": late_after,
        "end": att.get("working_hours_end", "17:30"),
    }


def schedules_of(cfg):
    """{group name: schedule}, with Default first."""
    result = {DEFAULT_GROUP: _default_schedule(cfg)}
    for name, sched in (cfg.get("schedules") or {}).items():
        if name != DEFAULT_GROUP and isinstance(sched, dict):
            result[name] = sched
    return result


def check_schedule(sched):
    """A clean copy of `sched`, or ScheduleError naming the bad field."""
    if not isinstance(sched, dict):
        raise ScheduleError("A schedule must be an object.")
    days = sched.get("working_days")
    if not isinstance(days, list) or not days or \
            any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days):
        raise ScheduleError("Pick at least one working day.", "working_days")
    clean = {"working_days": sorted(set(days))}
    for key, label in (("start", "Start"), ("late_after", "Late after"), ("end", "End")):
        try:
            parse_time(sched.get(key))
        except ScheduleError:
            raise ScheduleError(f"{label} must be a time like 09:30.", key) from None
        clean[key] = sched[key]
    if not parse_time(clean["start"]) <= parse_time(clean["late_after"]) < parse_time(clean["end"]):
        raise ScheduleError("Times must run in order: start, then late after, then end.", "late_after")
    return clean


def check_group_name(name):
    if not isinstance(name, str) or not GROUP_NAME_RE.match(name.strip()) or not name.strip():
        raise ScheduleError("A group name is 1 to 40 characters.", "name")
    return " ".join(name.split())


def apply_schedules(cfg, schedules):
    """Write {group: schedule} into `cfg` (Default into `attendance`). Returns cfg."""
    if DEFAULT_GROUP not in schedules:
        raise ScheduleError("The Default group cannot be removed.")
    clean = {}
    for name, sched in schedules.items():
        if name != DEFAULT_GROUP:
            name = check_group_name(name)
        if name in clean:
            raise ScheduleError(f"There are two groups called {name}.", "name")
        clean[name] = check_schedule(sched)
    default = clean.pop(DEFAULT_GROUP)
    att = cfg.setdefault("attendance", {})
    att["working_days"] = default["working_days"]
    att["start"] = default["start"]
    att["late_after"] = default["late_after"]
    att["working_hours_end"] = default["end"]
    cfg["schedules"] = clean
    return cfg


def is_working_day(sched, day):
    return day.weekday() in sched["working_days"]


def status_at(sched, moment):
    """Present or Late for a sighting at `moment` (a datetime).
    Late only on a working day, at or after the late cut-off."""
    if not is_working_day(sched, moment):
        return "Present"
    return "Late" if moment.time() >= parse_time(sched["late_after"]) else "Present"


def left_early(sched, day, last_seen, sightings):
    """True when the camera saw the person more than once that working day
    and the last sighting was before the end. One sighting tells nothing
    about when someone left."""
    if sightings < 2 or not last_seen or not is_working_day(sched, day):
        return False
    return datetime.strptime(last_seen, "%H:%M:%S").time() < parse_time(sched["end"])
