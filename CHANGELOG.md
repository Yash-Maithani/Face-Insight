# Changelog

All notable changes to Face Insight.

## 1.0.0 (2026-10-08)

First tagged version.

### Recognition
- Face detection with OpenCV's Haar cascade, identity with an LBPH recogniser, expression with a small Keras model.
- Register a person from the browser: collect face images, train, and reload the model without a restart.
- Adjustable recognition threshold, saved as you change it.

### Attendance
- Automatic records with a per-person cooldown and a Late cut-off.
- Manual marks for registered people.
- Day view with filters, sorting, a detail panel with the face crop, and Excel and CSV export.
- Delete a single record, or clear today's records from Settings.

### Dashboard
- Today's totals and late rate.
- Attendance trend and days attended per person over 7, 14 or 30 days.
- Today's expression breakdown.

### Interface
- Light, dark and system modes with eight colour themes.
- The open tab, the attendance date and the dashboard period are kept in the URL.
- A clear "Database offline" state instead of empty tables and zeros.
- Unsaved settings are flagged and survive switching tabs.
- Keyboard support for tables, dialogs and the detail panel.
- Tables turn into stacked rows on a phone.

### Known limits
- No login or access control.
- No liveness check, and recognition accuracy has not been measured.
- Only run on Windows so far.
