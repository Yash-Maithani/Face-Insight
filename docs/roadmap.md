# Roadmap

Version 1 is a local tool: one PC, one camera, a trusted network. This page lists what has to change before it can be more than that. Nothing here is scheduled.

## The first decision: where does the camera live?

V1 reads the webcam on the machine that runs the server. That only works for a camera plugged into that machine.

If the app is ever served from a public address, each visitor's camera has to be read in their own browser, which needs HTTPS, and the frames have to be sent to the server or processed in the page. That is a different architecture.

So there are two ways forward:

- **Stay local.** Keep V1 as a kiosk app on one PC. A website, if there is one, only carries public pages.
- **Go hosted.** Browser camera, recognition on the server, accounts, and each organisation's data kept apart.

Most of what follows depends on which one it is.

## Security and correctness

- There is no login. Anyone who can reach the server can change settings, delete people, mark attendance and clear records.
- The server runs on Flask's development server, accepts requests from any origin, and has a fixed secret key. It needs a production server behind HTTPS and a key from the environment.
- The database address and the Firebase credential path sit in `config.json` and are returned by `/api/config`. Secrets belong in the environment.
- `/video_feed` resends the same frame in a tight loop when nothing new has arrived, which wastes CPU for every open viewer.
- Static files get a new version number on every request, so the browser never caches them.
- The database wrapper does not reconnect by itself after a failure. The page has a Retry button, but the retry belongs in the backend.

## How attendance is modelled

- A record is written per sighting, once the cooldown has passed. One person can have several rows in a day, and totals count rows, not people. It should be one row per person per day, with a check-in and a check-out.
- The cooldown is kept in memory, so a restart forgets it.
- All unknown faces share one cooldown. A second stranger inside that window is not logged.
- The Late check compares clock times as text. It knows nothing about time zones, shifts or days off.
- A record for an unknown face still stores the ID the recogniser guessed. The page hides it, but exports show it.
- The registration API will rename a person if you reuse their ID. The form stops you. The API does not.
- A manual mark is accepted for any ID and name, registered or not.
- Deleting a record does not remove the copy already mirrored to Firebase.

## Recognition

- Haar and LBPH struggle with lighting, angle and glasses.
- There is no liveness check. A printed photo or a phone screen can be marked present.
- Accuracy has never been measured. That needs a held-out set of images and real numbers in this file.
- Worth trying: an embedding-based recogniser, and some form of anti-spoofing.
- Expression detection is the weakest and most sensitive feature. Decide whether it stays, becomes opt-in, or goes.

## Privacy

Face images are biometric data, so this part is not optional once other people's faces are involved.

- A consent step when registering someone, with the time it was given.
- A privacy policy that says what is collected, why, how long it is kept, who can see it, and how to have it deleted.
- Terms and conditions.
- Per-person export and deletion, with a record of who did it.
- Retention settings and encrypted storage for the images.
- A review of the policy text against the law wherever the app is used.

## Features

- Shifts, holidays, leave, and corrections with a history.
- Monthly reports per person and late trends.
- Roles: admin, manager, viewer.
- Guided capture with pose prompts and checks for blur, brightness and face size.
- A check that the same face is not registered twice.
- Model versions, with a way to go back to the previous one.
- Training without pausing the camera.
- Optional email or webhook alerts.

## Code and operations

- `server.py` has grown large. Split it into blueprints with an app factory.
- One validated schema for the config.
- More database indexes than just `date`, and a way to migrate data.
- Serve Chart.js and Socket.IO from the app instead of a CDN.
- Recognition accuracy tests and a browser smoke test.
- A health route, backups, a Dockerfile and CI.
- Try it on Linux and macOS.

## Before anything goes public

- [ ] The camera decision is made
- [ ] Login is in place
- [ ] Custom domain with HTTPS
- [ ] Privacy policy and terms are live and linked
- [ ] Consent step in Register
- [ ] Production server, secrets in the environment, test values removed
- [ ] Recognition accuracy measured on real data

## Rules for the interface

These applied to V1 and carry over:

- No purple, no gradients, no pill-shaped buttons.
- No emoji as icons.
- Plain wording. No slogans, no filler.
- No made-up reviews, numbers or counters. If a number is not known, show `--`.
- No stock or generated imagery. Screenshots of the real app only.
- No cursor effects and no showy scroll animation.
