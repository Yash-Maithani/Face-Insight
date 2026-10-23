# How it fits together

## The pieces

| Path | What lives there |
|---|---|
| `server.py` | The Flask app. Every HTTP route, the Socket.IO handlers, and the wiring between the parts below. |
| `core/model_loader.py` | Reads and writes `config.json`. Loads the Haar cascade, the recogniser and the expression model. |
| `core/detector.py` | The camera thread. Finds faces, recognises them, labels the expression, draws the boxes, and produces the MJPEG stream. |
| `core/collector.py` | Captures face crops for a person during registration. |
| `core/trainer.py` | Trains the recogniser from `ImageData/` and swaps the new model in while the app keeps running. |
| `core/camera_utils.py` | Picks the camera backend for the operating system. |
| `attendance/engine.py` | Decides whether a sighting becomes a record: cooldown, Present, Late or Unknown, and manual marks. |
| `attendance/exporter.py` | Excel and CSV export. |
| `auth/store.py` | Admin accounts in `instance/users.json`, bcrypt password hashes, and the sign-in throttle. |
| `auth/audit.py` | The audit log: writes to MongoDB, or to `instance/audit-offline.jsonl` while it is down. |
| `auth/web.py` | The sign-in gate in front of every route, the sign-in and setup pages, and the account APIs. |
| `db/mongo_client.py` | A thin wrapper around two collections, `attendance_logs` and `audit_log`. |
| `db/firebase_client.py` | Optional mirror to Firebase. Does nothing unless it is configured. |
| `web/templates/index.html` | The app page. `login.html` and `setup.html` are the only others. |
| `web/static/js/app.js` | Shared code: socket, theme, URL handling, dialogs, toasts, and the Live Monitor tab. |
| `web/static/js/*.js` | One file each for Attendance, Dashboard, Register, Settings and the Audit log. |
| `web/static/css/style.css` | All the styles. The colour tokens are at the top. |

## What happens to a frame

1. The detector reads a frame from the camera.
2. On every third frame it shrinks the image and runs the Haar cascade to find faces.
3. Each face goes to the recogniser, which returns a person ID and a distance. A distance under the threshold is a match. Anything else is Unknown.
4. The same face, resized to 48 by 48 pixels, goes to the expression model.
5. The engine gets the result. If auto attendance is on and the cooldown for that person has passed, it writes a record, saves a small face crop to `snapshots/`, and the server emits `attendance:new`.
6. The frame, with boxes and labels drawn on it, becomes the next image in the `/video_feed` stream.

The frames in between reuse the last set of boxes, which keeps the feed smooth without running detection on all of them.

## One owner for the camera

Only one thing can hold the camera. Starting a collection on the Register tab stops the Live Monitor, and it is started again when training finishes or the collection is stopped. The server refuses to start the Live Monitor while a collection is running.

## When things are missing

The app is meant to keep running when optional parts are absent:

- No `Trainer.yml`: every face is Unknown until the first training.
- No Firebase settings: the mirror is skipped.
- MongoDB unreachable: reads come back empty, writes are refused, and the status routes say so. The page shows a banner and `--` in place of counts, because a zero would be a false statement.

The one thing that stops startup is a missing expression model, and the error names the path it looked in.

## Signing in

Every route needs a signed-in admin, apart from `/login`, `/setup` and static files. A page request without a session is sent to `/login`; an API request gets `401` with `{"login_required": true}`. Socket.IO connections without a session are refused.

- While no account exists, everything leads to `/setup`, which creates the first admin. After that it is closed.
- Accounts are kept in a JSON file, not in MongoDB, so you can still sign in and fix the database settings while it is down.
- Five wrong passwords for one username from one address lock that pair out for a minute.
- Deleting a person, clearing today and adding an admin need the password typed in the last 15 minutes. Otherwise the server answers `403` with `{"fresh_required": true}`, and the page asks for the password and tries again.
- Sessions last 12 hours. The session key is generated on first start and kept in `instance/secret_key`, or set with `FACE_WEB_SECRET_KEY`.

## HTTP routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | The page |
| GET, POST | `/login` | Sign in. `next` must be a path on this site. |
| POST | `/logout` | Sign out |
| GET, POST | `/setup` | Create the first admin (only while there is none) |
| GET | `/api/me` | The signed-in username and saved appearance |
| PUT | `/api/me/preferences` `{mode, theme, font}` | Save any of the three |
| POST | `/api/auth/confirm` `{password}` | Confirm the password before a destructive action |
| POST | `/api/auth/password` `{current, new}` | Change your password |
| GET | `/api/users` | Admin accounts, without password hashes |
| POST | `/api/users` `{username, password}` | Add an admin |
| GET | `/api/audit?user=&action=&from=&to=&limit=` | Audit entries, newest first, with the total and `db_available` |
| GET | `/video_feed` | MJPEG stream for the Live Monitor |
| GET | `/api/camera/list` | Camera indexes that returned a real frame at startup |
| POST | `/api/camera/start` `{cam_index}` | Start the Live Monitor |
| POST | `/api/camera/stop` | Stop it |
| GET | `/api/attendance?date=` | Records for a day (today if no date) |
| POST | `/api/attendance/manual` `{person_id, name, status}` | Add a record by hand |
| GET | `/api/attendance/<id>/snapshot` | The face crop for a record |
| DELETE | `/api/attendance/<id>` | Delete one record, its face crop, and the cooldown it started |
| DELETE | `/api/attendance?date=<today>` | Delete today's records. The date is required and must be today. |
| GET | `/api/export/excel`, `/api/export/csv` `?date=` | Download a day |
| GET | `/api/stats/today` | Today's counts, live camera numbers, and `db_connected` |
| GET | `/api/stats/weekly?days=7\|14\|30` | Counts per day |
| GET | `/api/stats/persons?days=7\|14\|30` | Records and distinct days per person |
| GET | `/api/stats/emotions` | Today's expression counts |
| GET | `/api/status` | `{db_connected, firebase_enabled}` |
| POST | `/api/db/reconnect` | Try the database again with the saved settings |
| GET, POST | `/api/config` | Read and save settings |
| POST | `/api/config/db_test` `{uri}` | Test a MongoDB address without saving it |
| POST | `/api/collect/start` `{person_id, name, max_images}` | Start collecting face images |
| POST | `/api/collect/stop` | Stop early. Images already taken are kept. |
| GET | `/api/collect/feed` | MJPEG preview during collection |
| POST | `/api/train` | Train the recogniser |
| GET | `/api/persons` | Registered people with image counts |
| DELETE | `/api/persons/<id>` | Remove a person and their images, then retrain |

## Socket.IO events

Sent by the server:

| Event | When |
|---|---|
| `camera:status`, `camera:error` | The Live Monitor starts, stops or fails |
| `stats:update` | Counts changed |
| `attendance:new` | A record was written |
| `attendance:deleted` `{ids, date}` | Records were deleted |
| `collect:status`, `collect:progress`, `collect:done`, `collect:error` | Image collection |
| `train:status`, `train:progress`, `train:done`, `train:error` | Training |

When a browser connects, the server immediately sends the current `camera:status`, `stats:update`, `collect:status` and `train:status`, so a freshly loaded page shows the real state.

Sent by the browser: `settings:threshold` and `settings:auto_attend`. Both are checked on the server and answered with `{ok: true}` or `{ok: false, error}`. The page shows "Saved" only after an `ok`.

The camera buttons work the same way. The page does not trust the HTTP response. It waits for `camera:status`.

## URLs in the browser

The open tab and one argument are kept after the `#`:

- `#attendance/2026-10-07` opens the Attendance tab on that date. Today has no argument.
- `#dashboard/14` opens the Dashboard on a 14 day period. Seven days has no argument.

Anything the app does not recognise falls back to the default, and the URL is corrected.

## Front-end conventions

`app.js` creates one global, `App`, and the other files use it instead of repeating code:

- `App.socket` is the one Socket.IO connection.
- `App.theme`, `App.route` and `App.db` hold the theme, the URL and the database status.
- `App.showToast`, `App.confirm`, `App.trapFocus` and `App.download` cover messages, dialogs and file downloads.
- `App.fetchFresh` runs a request that may need the password again, and returns `null` if the person cancels.
- Any `401` from the API sends the page to the sign-in page.

Tabs talk to each other through events on `window`: `tab:navigate`, `route:change`, `themechange`, `db:status` and `threshold:change`.

Every number on the page comes from the server. Until it arrives, the page shows `--`.

## Styling

All colours are CSS custom properties at the top of `style.css`. Nothing in the HTML or JavaScript hardcodes a colour, and the charts read the tokens when they draw.

Two attributes on `<html>` pick the look:

- `data-mode` is `light` or `dark`. The System option follows the operating system.
- `data-theme` is one of `orange` (the default), `ink`, `teal`, `blue`, `navy`, `olive`, `coffee` or `steel`.
- `data-font` is one of `plex` (IBM Plex Sans, the default), `source` (Source Sans 3), `atkinson` (Atkinson Hyperlegible) or `inter`.

Green, amber and red are not offered as themes, because they already mean Present, Late and Unknown.

The choices are saved to the account and also kept in the browser. A short inline script applies them before the first paint, so the page does not flash the wrong look. The sign-in page, which has no account yet, uses the browser's copy.

House rules for the interface: flat surfaces with a one pixel border, small corner radii, no gradients, no blur, no decorative animation, and plain wording.
