"""
attendance/exporter.py
Export attendance records to Excel (.xlsx) and CSV files.
Column order (both formats): Name, ID, Time In, Status, Emotion, Confidence, Manual.
"""

import csv
import logging

from openpyxl import Workbook

logger = logging.getLogger(__name__)

COLUMNS = ["Name", "ID", "Time In", "Status", "Emotion", "Confidence", "Manual"]


def _record_to_row(record: dict) -> list:
    """Extract a row list from a record dict in the canonical column order."""
    return [
        record.get("name", ""),
        record.get("person_id", ""),
        record.get("time_in", ""),
        record.get("status", ""),
        record.get("emotion", ""),
        record.get("confidence", ""),
        record.get("manual", ""),
    ]


def export_to_excel(records: list[dict], path: str) -> str:
    """Write *records* to an ``.xlsx`` file at *path*.  Returns the path."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    ws.append(COLUMNS)
    for rec in records:
        ws.append(_record_to_row(rec))
    wb.save(path)
    logger.info("Exported %d records to Excel: %s", len(records), path)
    return path


def export_to_csv(records: list[dict], path: str) -> str:
    """Write *records* to a ``.csv`` file at *path*.  Returns the path."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for rec in records:
            writer.writerow(_record_to_row(rec))
    logger.info("Exported %d records to CSV: %s", len(records), path)
    return path
