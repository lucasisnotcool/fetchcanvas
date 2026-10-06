# macOS Automation

> **Since 2026-08-19 the hourly LaunchAgent no longer runs this repo.** gram-server's
> `canvas-mirror.py` fetches from Canvas (same file/module/naming rules as `main.py`), and
> `~/bin/fetchcanvas_startup.sh` now runs `~/bin/canvas-deliver.py`, which pulls that mirror
> and places files into `OneDrive/<COURSE>/FetchCanvas/`. Its log is
> `~/Library/Logs/fetchcanvas.log`. Source and rationale live in `~/gram-server-setup`
> (`projects/second-brain/scripts/`, decision #27). `main.py` remains the standalone/manual
> fetcher. The sections below describe the LaunchAgent itself, which is unchanged.

FetchCanvas is scheduled with a per-user macOS LaunchAgent.

## Where It Is Configured

The LaunchAgent plist is outside this repo:

```bash
~/Library/LaunchAgents/com.lucas.fetchcanvas.plist
```

It runs this wrapper script:

```bash
~/bin/fetchcanvas_startup.sh
```

The wrapper script enters the local (non-OneDrive) runtime directory at:

```bash
~/fetchcanvas
```

and runs the project's existing virtualenv directly:

```bash
./.venv/bin/python main.py
```

The repository must stay outside `~/Desktop`: on this Mac, Desktop is managed by
the OneDrive File Provider. FetchCanvas writes its synced course files to
`OUTPUT_PATH` in OneDrive, but its code, virtualenv, and Python-version metadata
must remain local so OneDrive hydration cannot prevent a scheduled run from
starting.

## OneDrive Output Resilience

FetchCanvas treats course-file delivery as the core sync. Changelog and upcoming
assignment snapshots are OneDrive mirrors, so a temporary File Provider outage
does not invalidate an otherwise successful course sync.

- The current, authoritative result is stored locally in
  `~/.fetchcanvas/last-run.json`.
- Changelog entries are kept in `~/.fetchcanvas/pending-changelog.md` until they
  can be published. Ordinary runs with no file changes do not read or rewrite
  `CHANGELOG.md`.
- Generated upcoming snapshots are written only when assignment data changes (or
  once per day), and use atomic replacement rather than in-place overwrites.
- The launcher records a deferred metadata publication as completed with a
  warning, while real Canvas/API or course-file write failures remain failures.

## Schedule

The plist has:

```xml
<key>StartInterval</key>
<integer>3600</integer>
```

That means launchd tries to run FetchCanvas every 3600 seconds, or once per hour.

The current plist also has:

```xml
<key>RunAtLoad</key>
<false/>
```

So it should not run immediately just because the LaunchAgent is loaded.

## Disable It

Stop it for the current login session:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.lucas.fetchcanvas.plist
```

Keep it disabled across future loads:

```bash
launchctl disable gui/$(id -u)/com.lucas.fetchcanvas
```

To stop it now and keep it off, run both:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.lucas.fetchcanvas.plist
launchctl disable gui/$(id -u)/com.lucas.fetchcanvas
```

## Re-Enable It

Allow the LaunchAgent to run again:

```bash
launchctl enable gui/$(id -u)/com.lucas.fetchcanvas
```

Load it again:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lucas.fetchcanvas.plist
```

## Check Status

```bash
launchctl print gui/$(id -u)/com.lucas.fetchcanvas
```

Look for:

- `state = not running` or `state = running`
- `run interval = 3600 seconds`
- `last exit code`

If the service is stopped and not loaded, this command may instead print an error like:

```text
Could not find service "com.lucas.fetchcanvas"
```

That means the LaunchAgent is not currently loaded or running.

## Confirm It Is Disabled

There are two useful checks:

1. Confirm it is not currently loaded:

```bash
launchctl print gui/$(id -u)/com.lucas.fetchcanvas
```

If disabled with `bootout`, this should fail with something like:

```text
Could not find service "com.lucas.fetchcanvas"
```

2. Confirm it is persistently disabled:

```bash
launchctl print-disabled gui/$(id -u)
```

Look for:

```text
"com.lucas.fetchcanvas" => disabled
```

If both are true, FetchCanvas is not running now and should not automatically restart on a future login or launchd load.

## Logs

The automation writes logs here:

```bash
~/Library/Logs/fetchcanvas.log
~/Library/Logs/fetchcanvas.out
~/Library/Logs/fetchcanvas.err
```

Use these when the LaunchAgent is loaded but syncs are failing.
