# FetchCanvas

Synchronize selected Canvas course files and assignment snapshots to a local or
OneDrive-backed output folder.

## Config

Set these in `.env` (see `.env-sample`):

- **API_TOKEN** — Canvas API token (Account > Settings > Generate New Token).
- **CANVAS_BASE_URL** — Canvas instance URL (e.g. `https://canvas.nus.edu.sg`).
- **OUTPUT_PATH** — Folder where course files are saved. It may be in OneDrive,
  but FetchCanvas's code and virtualenv must remain on local disk.
- **COURSES** — Comma-separated course codes to sync (e.g. `CS3223,CS3234`), or `all` to sync every active course. Empty or missing = sync all.

## Reliability Model

Course-file delivery is the core sync operation. The remote changelog and
upcoming-assignment files are useful mirrors, but temporary OneDrive File
Provider errors must not make a completed course sync look like a failure.

- Run FetchCanvas from a local directory such as `~/fetchcanvas`, not a
  cloud-managed Desktop folder.
- The authoritative result of the most recent run is stored locally at
  `~/.fetchcanvas/last-run.json`.
- Meaningful changelog entries are stored in a local outbox at
  `~/.fetchcanvas/pending-changelog.md` until OneDrive is available. Runs with
  no course-file changes do not open `CHANGELOG.md`.
- `UPCOMING.md` and the SecondBrain snapshots are atomically published only
  when assignment data changes, or once per day. An incomplete assignment fetch
  preserves the previous snapshot.
- A deferred mirror is reported as a warning by the scheduler. Canvas API and
  required course-file delivery failures remain failures.

See [MAC_AUTOMATION.md](MAC_AUTOMATION.md) for the LaunchAgent setup and logs.
