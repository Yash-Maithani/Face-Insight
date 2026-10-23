"""
attendance/reports.py
Attendance reports for a date range and a set of people, as CSV or PDF.

Both formats are written from the same rows (the daily summary), so they
always agree. An empty report is never written to a file: callers check
`report["rows"]` first and show a message instead.
"""

import csv
import io
from datetime import datetime

from attendance.summary import ABSENT, LATE, NOT_YET, PRESENT, daily_summary, person_totals

COLUMNS = ("Date", "Person ID", "Name", "Status", "First seen", "Last seen", "Sightings", "Manual")
MAX_RANGE_DAYS = 366


class RangeError(ValueError):
    """A date range was refused. The message is shown to the user."""


def parse_range(date_from, date_to, max_days=MAX_RANGE_DAYS):
    """Check a YYYY-MM-DD range. Returns (from, to) or raises RangeError."""
    if not date_from or not date_to:
        raise RangeError("Choose a start and an end date.")
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise RangeError("Dates must look like 2026-01-31.") from None
    if start > end:
        raise RangeError("The start date must be on or before the end date.")
    if (end - start).days + 1 > max_days:
        raise RangeError(f"A range can be at most {max_days} days.")
    return start.isoformat(), end.isoformat()


def build_report(records, persons, date_from, date_to, now, working_days, day_end):
    """{range, generated, people, rows, totals}. `persons` is the chosen people, in order."""
    rows = daily_summary(records, persons, date_from, date_to, now, working_days, day_end)
    return {
        "from": date_from,
        "to": date_to,
        "generated": now.replace(microsecond=0).isoformat(sep=" "),
        "people": [{"person_id": p["person_id"], "name": p["name"]} for p in persons],
        "rows": rows,
        "totals": person_totals(rows, persons),
    }


def row_values(row):
    return (
        row["date"], row["person_id"], row["name"], row["status"],
        row["first_seen"] or "", row["last_seen"] or "", row["sightings"],
        "Yes" if row["manual"] else "No",
    )


def to_csv(report):
    """The report as CSV text (UTF-8 with BOM, so Excel shows names correctly)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in report["rows"]:
        writer.writerow(row_values(row))
    return "﻿" + buf.getvalue()


def to_pdf(report, compress=True):
    """The report as PDF bytes: a totals table per person, then every row."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Attendance {report['from']} to {report['to']}", author="Face Insight",
        pageCompression=1 if compress else 0, invariant=0 if compress else 1,
    )
    styles = getSampleStyleSheet()
    grid = TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.black),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])

    story = [
        Paragraph("Attendance report", styles["Title"]),
        Paragraph(f"{report['from']} to {report['to']}. {len(report['people'])} people. "
                  f"Made {report['generated']}.", styles["Normal"]),
        Spacer(1, 6 * mm),
        Paragraph("Days per person", styles["Heading2"]),
    ]
    totals = [("Person ID", "Name", PRESENT, LATE, ABSENT, NOT_YET)]
    totals += [(t["person_id"], t["name"], t[PRESENT], t[LATE], t[ABSENT], t[NOT_YET]) for t in report["totals"]]
    story.append(Table(totals, repeatRows=1, style=grid, hAlign="LEFT"))
    story += [Spacer(1, 6 * mm), Paragraph("Every day", styles["Heading2"])]
    rows = [COLUMNS] + [row_values(r) for r in report["rows"]]
    story.append(Table(rows, repeatRows=1, style=grid, hAlign="LEFT"))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(landscape(A4)[0] - 15 * mm, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
