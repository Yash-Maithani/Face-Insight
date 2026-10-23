"""
tests/test_reports.py
Reports: the preview, CSV and PDF exports, and what happens with an empty
range or an offline database. CSV and PDF must hold the same rows.

Uses an isolated temp config and the test database. Never touches the real
config.json or instance/.

Run: python tests/test_reports.py
"""

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def file_hash(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


REAL_CONFIG = os.path.join(PROJECT_ROOT, "config.json")
_cfg_hash_before = file_hash(REAL_CONFIG)

tmp_dir = tempfile.mkdtemp(prefix="face_web_test_")
tmp_cfg_path = os.path.join(tmp_dir, "config.json")
if os.path.exists(REAL_CONFIG):
    with open(REAL_CONFIG, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)
cfg_copy["mongodb_db"] = "face_attendance_test"
cfg_copy["mongodb_uri"] = "mongodb://localhost:27017"
cfg_copy["names"] = {}
cfg_copy.setdefault("attendance", {})["working_days"] = [0, 1, 2, 3, 4]
with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)
os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

from pymongo import MongoClient  # noqa: E402
_mc = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)
try:
    for name in ("attendance_logs", "persons", "app_meta", "audit_log"):
        _mc["face_attendance_test"][name].delete_many({})
    mongo_available = True
except Exception:
    mongo_available = False
_mc.close()

import server  # noqa: E402
from attendance.reports import COLUMNS, RangeError, build_report, parse_range, row_values, to_csv, to_pdf  # noqa: E402
from db.mongo_client import MongoAttendanceDB  # noqa: E402
from helpers import signed_in_client  # noqa: E402

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


def pdf_strings(data):
    """Text drawn on the pages of an uncompressed ReportLab PDF, in order."""
    out = []
    for m in re.finditer(rb"\((.*?)(?<!\\)\) Tj", data):
        out.append(m.group(1).replace(rb"\(", b"(").replace(rb"\)", b")").decode("latin-1"))
    return out


# ──────────────────────────────────────────────────────────────────────────
section("1. Date range rules")

check("A valid range passes", parse_range("2026-09-14", "2026-09-18") == ("2026-09-14", "2026-09-18"))
for bad, why in ((("", "2026-09-18"), "missing start"), (("2026-09-18", "2026-09-14"), "backwards"),
                 (("2026-02-30", "2026-03-01"), "not a date"), (("2025-01-01", "2026-09-18"), "over 366 days")):
    try:
        parse_range(*bad)
        check(f"Refused: {why}", False, "no error")
    except RangeError:
        check(f"Refused: {why}", True)

if not mongo_available:
    print("  [SKIP] MongoDB is not running on localhost:27017")
    print(f"\n{'=' * 60}\n  RESULTS: {passed} passed, {failed} failed, {passed + failed} total\n{'=' * 60}")
    sys.exit(1 if failed else 0)

client = signed_in_client(server)
db = server.mongo_db

# Two people registered before the range, and a week of records (Mon 14 to Fri 18 September 2026)
for pid, name in ((1, "Asha Verma"), (2, "Rohan Mehta"), (3, "Neha Kapoor")):
    db.insert_person({"person_id": pid, "name": name, "consent_at": "2026-09-01T09:00:00", "created": "2026-09-01T09:00:00"})
server.people.sync_cache()
for rec in (
    (1, "2026-09-14", "09:00:00", "Present", False), (1, "2026-09-14", "15:30:00", "Present", False),
    (1, "2026-09-15", "09:40:00", "Late", False), (1, "2026-09-16", "10:00:00", "Present", True),
    (2, "2026-09-14", "09:05:00", "Present", False), (2, "2026-09-17", "09:50:00", "Late", False),
):
    pid, date, time_in, status, manual = rec
    db.insert_log({"person_id": pid, "name": "", "date": date, "time_in": time_in, "status": status, "manual": manual})
db.insert_log({"person_id": None, "name": "Unknown", "date": "2026-09-15", "time_in": "11:00:00", "status": "Unknown", "manual": False})

RANGE = {"from": "2026-09-14", "to": "2026-09-18"}

# ──────────────────────────────────────────────────────────────────────────
section("2. Preview")

res = client.get("/api/reports", query_string=RANGE)
report = res.get_json()
check("GET /api/reports -> 200", res.status_code == 200, str(report)[:200])
check("Everyone by default", [p["person_id"] for p in report["people"]] == [1, 2, 3])
check("15 rows: 3 people x 5 working days", len(report["rows"]) == 15, str(len(report["rows"])))
totals = {t["person_id"]: t for t in report["totals"]}
check("Asha: 2 present (one by hand), 1 late, 2 absent",
      (totals[1]["Present"], totals[1]["Late"], totals[1]["Absent"]) == (2, 1, 2), str(totals[1]))
check("Rohan: 1 present, 1 late, 3 absent",
      (totals[2]["Present"], totals[2]["Late"], totals[2]["Absent"]) == (1, 1, 3), str(totals[2]))
check("Neha: absent all week", totals[3]["Absent"] == 5)
check("Rows use the registered names", {r["name"] for r in report["rows"]} == {"Asha Verma", "Rohan Mehta", "Neha Kapoor"})

res = client.get("/api/reports", query_string=dict(RANGE, people="2,1"))
check("People filter", [p["person_id"] for p in res.get_json()["people"]] == [1, 2] and len(res.get_json()["rows"]) == 10)
check("Unknown person ID -> 404", client.get("/api/reports", query_string=dict(RANGE, people="1,99")).status_code == 404)
check("Bad people list -> 400", client.get("/api/reports", query_string=dict(RANGE, people="one")).status_code == 400)
check("Bad range -> 400", client.get("/api/reports", query_string={"from": "2026-09-18", "to": "2026-09-14"}).status_code == 400)

# ──────────────────────────────────────────────────────────────────────────
section("3. CSV")

res = client.get("/api/reports/export.csv", query_string=RANGE)
check("CSV -> 200", res.status_code == 200)
check("CSV content type", res.headers["Content-Type"].startswith("text/csv"))
check("CSV file name", 'attendance-report_2026-09-14_to_2026-09-18.csv' in res.headers.get("Content-Disposition", ""))
text = res.data.decode("utf-8")
check("Starts with a BOM so Excel reads the names right", text.startswith("﻿"))
csv_rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
check("Header row", tuple(csv_rows[0]) == COLUMNS)
expected = [[str(v) for v in row_values(r)] for r in report["rows"]]
check("CSV rows are exactly the preview rows", csv_rows[1:] == expected, f"{len(csv_rows) - 1} vs {len(expected)}")

# ──────────────────────────────────────────────────────────────────────────
section("4. PDF")

res = client.get("/api/reports/export.pdf", query_string=RANGE)
check("PDF -> 200", res.status_code == 200)
check("PDF content type", res.headers["Content-Type"] == "application/pdf")
check("PDF file name", 'attendance-report_2026-09-14_to_2026-09-18.pdf' in res.headers.get("Content-Disposition", ""))
check("It is a PDF", res.data.startswith(b"%PDF-"))

# Same report, written without compression so its text can be read back
working = (0, 1, 2, 3, 4)
records = db.find_logs("2026-09-14", "2026-09-18")
people = [p for p in server.people.list()[0]]
rep = build_report(records, people, "2026-09-14", "2026-09-18", datetime.now(), working, "17:30")
strings = pdf_strings(to_pdf(rep, compress=False))
joined = "\x1f".join(strings)
# Blank cells (no first or last seen on an absent day) draw no text
missing = [r for r in rep["rows"]
           if "\x1f".join(str(v) for v in row_values(r) if v != "") not in joined]
check("Every CSV row appears in the PDF, cell by cell", not missing and len(rep["rows"]) == 15, str(missing[:2]))
check("PDF has the column headings", "\x1f".join(COLUMNS) in joined)
check("PDF has the totals per person", "Days per person" in strings)
check("PDF rows match the CSV rows", [row_values(r) for r in rep["rows"]] == [tuple(
    int(x) if i in (1, 6) else x for i, x in enumerate(row)) for row in expected])

# ──────────────────────────────────────────────────────────────────────────
section("5. Empty range and offline")

weekend = {"from": "2026-09-19", "to": "2026-09-20"}
res = client.get("/api/reports", query_string=weekend)
check("Weekend with no records: preview has no rows", res.status_code == 200 and res.get_json()["rows"] == [])
res = client.get("/api/reports/export.csv", query_string=weekend)
check("Empty CSV export -> 404 with a message, not an empty file",
      res.status_code == 404 and "Nothing to export" in res.get_json()["error"])
res = client.get("/api/reports/export.pdf", query_string=weekend)
check("Empty PDF export -> 404", res.status_code == 404)

entries = client.get("/api/audit", query_string={"action": "report.exported"}).get_json()["entries"]
check("Each export is audited (2), empty ones are not", len(entries) == 2, str(len(entries)))
check("Audit says which format and range", {e["details"]["format"] for e in entries} == {"csv", "pdf"}
      and all(e["details"]["from"] == "2026-09-14" for e in entries))

offline = MongoAttendanceDB("mongodb://localhost:29999", "face_attendance_test")
server.mongo_db = offline
res = client.get("/api/reports", query_string=RANGE)
check("Offline preview -> 503", res.status_code == 503 and res.get_json()["db_connected"] is False)
res = client.get("/api/reports/export.csv", query_string=RANGE)
check("Offline export -> 503, never an empty file", res.status_code == 503)
offline.close()
server.mongo_db = db

# ──────────────────────────────────────────────────────────────────────────
section("6. Cleanup")

for name in ("attendance_logs", "persons", "app_meta"):
    db.database()[name].delete_many({})
check("config.json hash unchanged", file_hash(REAL_CONFIG) == _cfg_hash_before)
shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
