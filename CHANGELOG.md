# Changelog

All notable changes to Face Insight.

## Unreleased (version 2, in progress)

### Accounts and sign-in
- Every page, API route and live connection needs a signed-in admin. The first start asks you to create one; there is no default password.
- Admins can add other admins and change their own password from Settings.
- Five wrong passwords within ten minutes lock that username out for a minute.
- Deleting a person, clearing today and adding an admin ask for the password again if it was not typed in the last 15 minutes.
- The database password is no longer sent to the browser.

### People and reports
- Registered people are stored in the database. On first start, names from `config.json` are moved there, after a backup.
- A People tab with search, and a page per person: attendance day by day over any dates, first and last seen, the records behind each day, and renaming.
- A daily summary per person: Present, Late or Absent, with first and last seen. Working days are a new setting.
- A Reports tab: pick dates and people, then download CSV or PDF. Both files hold the same rows.
- Date ranges on the Attendance tab and the Dashboard, and range exports.
- Registration asks for the person's consent and stores when it was given. Prompts during capture ask the person to turn their head.
- Collecting images for a taken ID can no longer rename that person, and manual marks only accept registered people.
- Unknown faces are stored without a person ID; the recogniser's guess is kept separately.

### Audit log
- A new tab lists sign-ins, failed sign-ins and every change or delete, with who did it and when. Filter by person, action and date.
- Entries made while the database is offline are kept in a local file and still shown.

### Interface
- A sidebar instead of top tabs, and a status bar for the camera, database and connection.
- Mode, colour theme and font are saved to your account. Four fonts to pick from.
- Chart.js, Socket.IO and the fonts are served by the app, so it works without internet.

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
