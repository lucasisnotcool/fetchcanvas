import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

API_TOKEN = os.getenv('API_TOKEN')
CANVAS_BASE_URL = os.getenv('CANVAS_BASE_URL')
OUTPUT_PATH = os.getenv('OUTPUT_PATH')

CHANGELOG_NAME = "CHANGELOG.md"
TIMEZONE = ZoneInfo("Asia/Singapore")
LAST_SYNC_PREFIX = "# Last Sync: "
EVENT_MARKER_RE = re.compile(r"<!-- fetchcanvas-event: ([0-9a-f-]+) -->")
REQUEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120
RETRY_DELAYS = (0, 2, 5, 10)
METADATA_DEFERRED_EXIT_CODE = 10

LOCAL_STATE_DIR = Path.home() / ".fetchcanvas"
PENDING_CHANGELOG_PATH = LOCAL_STATE_DIR / "pending-changelog.md"
RUN_STATE_PATH = LOCAL_STATE_DIR / "last-run.json"

# Course files land in the module's own OneDrive folder, not a single FetchCanvas tree:
#   OneDrive/<COURSE>/FetchCanvas/<canvas folder path>/<file>
# Rationale and the migration are in the dochub (2026-08-19 changelog). VAULT_ROOT is the
# OneDrive root = the Obsidian vault root.
VAULT_ROOT = os.getenv("VAULT_ROOT") or os.path.expanduser(
    "~/Library/CloudStorage/OneDrive-NationalUniversityofSingapore")
COURSE_SUBDIR = "FetchCanvas"

# Files the user has deliberately moved OUT of <COURSE>/FetchCanvas/ must not reappear on the
# next run. We remember the exact Canvas version we last wrote; if that same version is now
# absent locally, it was moved on purpose. A NEW version from Canvas is still delivered.
PUSH_STATE_PATH = LOCAL_STATE_DIR / "pushed-files.json"


def _fs_safe(name):
    """Course codes such as CS4246/CS5446 contain a slash and would nest a directory."""
    return name.replace(os.sep, "-").replace("/", "-").strip()


def load_push_state():
    try:
        with open(PUSH_STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_push_state(state):
    try:
        LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PUSH_STATE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, PUSH_STATE_PATH)
    except OSError as e:
        print(f"[push-state] could not persist {PUSH_STATE_PATH}: {e}")


def destination_for(course_code, folder_path, display_name):
    """OneDrive/<COURSE>/FetchCanvas/<subpath>/<file>.

    folder_path arrives as "<course code>/<sub>/<sub>" because get_all_folders renames Canvas's
    root 'course files' folder to the course code. Strip that first segment — it becomes the
    top-level module directory instead.
    """
    parts = folder_path.split(os.sep) if folder_path else []
    if parts and parts[0] == course_code:
        parts = parts[1:]
    course_dir = os.path.join(VAULT_ROOT, _fs_safe(course_code), COURSE_SUBDIR)
    rel = os.path.join(*parts, display_name) if parts else display_name
    return os.path.join(course_dir, rel), rel

_raw_courses = (os.getenv('COURSES') or '').strip()
if not _raw_courses or _raw_courses.lower() == 'all':
    SYNC_ALL_COURSES = True
    COURSE_CODES = None
else:
    SYNC_ALL_COURSES = False
    COURSE_CODES = {c.strip() for c in _raw_courses.split(',') if c.strip()}

headers = {
    "Authorization": f"Bearer {API_TOKEN}"
}


def get_json(url):
    """Fetch and validate a Canvas JSON response with a bounded timeout."""
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as e:
        raise RuntimeError(f"Canvas returned invalid JSON for {url}") from e


def get_json_paginated(url):
    """Fetch EVERY page of a Canvas list endpoint.

    Canvas paginates with RFC 5988 Link headers. Passing per_page=100 and reading a single
    response silently drops everything past the first 100 rows with no error and no warning
    — a course with 100+ files simply mirrors short. Fixed 2026-08-19; the server port in
    projects/second-brain/scripts/canvas-upcoming.py has always done this correctly.
    """
    out = []
    while url:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        try:
            page = response.json()
        except ValueError as e:
            raise RuntimeError(f"Canvas returned invalid JSON for {url}") from e
        if not isinstance(page, list):
            return page
        out.extend(page)
        url = response.links.get("next", {}).get("url")
    return out


def get_all_courses():
    url = f"{CANVAS_BASE_URL}/api/v1/courses?enrollment_state=active&per_page=100"
    course_list = get_json_paginated(url)
    courses = []
    for course in course_list:
        if "course_code" in course:
            courses.append((course['id'], course["course_code"]))
        else:
            print("Course with no Course code found")
    return courses


def get_all_folders(course_id, course_name):
    """
    Returns a mapping of id to the folder name
    """
    url = f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}/folders?per_page=100"

    folder_list = get_json_paginated(url)

    folder_id_name_map = {}
    for folder in folder_list:
        folder_id = folder['id']
        folder_name = folder['name']
        parent_folder_id = folder['parent_folder_id']

        if folder_name == 'course files':
            folder_name = course_name

        folder_id_name_map[folder_id] = (folder_name, parent_folder_id)
    
    return folder_id_name_map


def download_file(url, save_path):
    temp_path = f"{save_path}.fetchcanvas.tmp"
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        response = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        # Never leave a partially downloaded course file visible in OneDrive.
        with open(temp_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)
        os.replace(temp_path, save_path)
        return True, None
    except (requests.exceptions.RequestException, OSError) as e:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return False, str(e)

def build_run_entry(run_started_at, courses_log, event_id):
    run_time = run_started_at.astimezone(TIMEZONE)
    ts_str = run_time.strftime("%A %Y-%m-%d %H:%M:%S %Z")
    lines = []
    # This marker makes deferred publication idempotent if a cloud write's
    # completion is uncertain when the File Provider drops the connection.
    lines.append(f"<!-- fetchcanvas-event: {event_id} -->")
    lines.append(f"## Sync run at {ts_str}")
    lines.append("")
    for course_code, summary_lines in courses_log:
        lines.append(f"### Course: {course_code}")
        lines.extend(summary_lines)
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _atomic_write_text(path, contents):
    """Replace a file without ever truncating its previous version in place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.fetchcanvas.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(contents)
    os.replace(temp_path, path)


def load_pending_changelog():
    try:
        return PENDING_CHANGELOG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as e:
        raise RuntimeError(f"Could not read local changelog outbox: {e}") from e


def save_pending_changelog(entries):
    """Keep unpublished entries locally until OneDrive accepts them."""
    try:
        _atomic_write_text(PENDING_CHANGELOG_PATH, entries)
        return True, None
    except OSError as e:
        return False, str(e)


def clear_pending_changelog():
    try:
        PENDING_CHANGELOG_PATH.unlink()
    except FileNotFoundError:
        pass


def unpublished_entries(entries, existing):
    """Drop queued event blocks that are already present in the remote log."""
    published_ids = set(EVENT_MARKER_RE.findall(existing))
    if not published_ids:
        return entries

    blocks = list(re.finditer(
        r"(?s)(<!-- fetchcanvas-event: ([0-9a-f-]+) -->.*?)(?=<!-- fetchcanvas-event: |\Z)",
        entries,
    ))
    if not blocks:
        # Compatibility with an outbox created by an older FetchCanvas build.
        return entries
    return "".join(block.group(1) for block in blocks if block.group(2) not in published_ids)


def write_local_run_state(run_started_at, course_results, changelog_status, upcoming_status):
    """Record the authoritative run result outside the OneDrive File Provider."""
    payload = {
        "last_canvas_sync": run_started_at.isoformat(),
        "courses": {
            result["course_code"]: {
                "downloaded": len(result["downloaded"]),
                "updated": len(result["updated"]),
                "skipped": len(result["skipped"]),
                "failed": len(result["failed"]),
            }
            for result in course_results
        },
        "changelog": changelog_status,
        "upcoming": upcoming_status,
    }
    try:
        _atomic_write_text(RUN_STATE_PATH, json.dumps(payload, indent=2) + "\n")
        return True, None
    except OSError as e:
        return False, str(e)


def update_changelog(output_path, run_started_at, entry_md):
    """Publish queued changelog entries atomically, with bounded retries."""
    changelog_path = os.path.join(output_path, CHANGELOG_NAME)

    # Format Last Sync line.
    run_time = run_started_at.astimezone(TIMEZONE)
    last_sync_str = run_time.strftime("%A %Y-%m-%d %H:%M:%S %Z")
    header_line = f"{LAST_SYNC_PREFIX}{last_sync_str}\n\n"

    last_error = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if delay:
            print(f"Retrying changelog update in {delay}s (attempt {attempt}/{len(RETRY_DELAYS)}).")
            time.sleep(delay)
        try:
            existing = ""
            old_body = ""
            try:
                with open(changelog_path, "r", encoding="utf-8") as f:
                    existing = f.read()
            except FileNotFoundError:
                pass
            # Strip old Last Sync header if present.
            if existing.startswith(LAST_SYNC_PREFIX):
                lines = existing.splitlines(True)[1:]
                while lines and lines[0].strip() == "":
                    lines.pop(0)
                old_body = "".join(lines)
            else:
                old_body = existing

            entries_to_add = unpublished_entries(entry_md, existing)
            if not entries_to_add:
                return True, None

            new_body = header_line
            new_body += entries_to_add
            new_body += old_body

            # Do not truncate the existing changelog in place on a cloud volume.
            _atomic_write_text(changelog_path, new_body)
            return True, None
        except OSError as e:
            last_error = str(e)
            print(f"Changelog update attempt {attempt}/{len(RETRY_DELAYS)} failed: {e}")

    print(f"Changelog publication deferred: {changelog_path}")
    return False, last_error or "Unknown OneDrive error"

def resolve_path_for_file(folder_id, folder_id_name_map):
    """
    given a file, resolve it's file path
    """
    path = []

    while folder_id is not None:
        name, folder_id = folder_id_name_map[folder_id]
        path.append(name)
    
    path.reverse()
    path = os.path.join(*path)
    return path


def download_all_files(course_id, folder_id_name_map, course_code):
    url = f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}/files?per_page=100"
    file_list = get_json_paginated(url)

    downloaded = []
    updated = []
    skipped = []
    moved_out = []
    failed = []
    push_state = load_push_state()

    for file in file_list:
        display_name = file['filename']
        folder_path = resolve_path_for_file(file['folder_id'], folder_id_name_map)
        save_path, rel_key = destination_for(course_code, folder_path, display_name)
        state_key = f"{course_code}/{rel_key}"
        canvas_version = str(file.get('updated_at') or file.get('size') or '')

        try:
            local_stat = os.stat(save_path)
            exists = True
        except FileNotFoundError:
            exists = False
            local_stat = None
        except OSError as e:
            failed.append((display_name, folder_path, str(e)))
            continue

        if not exists:
            # Absent locally. If we previously delivered THIS EXACT Canvas version, the user
            # moved or deleted it on purpose — respect that and do not resurrect it. A newer
            # version from Canvas falls through and is delivered normally.
            prior = push_state.get(state_key)
            if prior and prior.get("canvas_version") == canvas_version:
                moved_out.append((display_name, folder_path))
                continue
            ok, err = download_file(file['url'], save_path)
            if ok:
                downloaded.append((display_name, folder_path))
                push_state[state_key] = {
                    "canvas_version": canvas_version,
                    "canvas_id": file.get("id"),
                    "path": save_path,
                }
            else:
                failed.append((display_name, folder_path, err or "Unknown error"))
            continue

        try:
            canvas_updated = datetime.fromisoformat(
                file['updated_at'].replace('Z', '+00:00')
            ).timestamp()
        except (KeyError, ValueError):
            canvas_updated = float('inf')

        local_mtime = local_stat.st_mtime
        if canvas_updated > local_mtime:
            ok, err = download_file(file['url'], save_path)
            if ok:
                updated.append((display_name, folder_path))
                push_state[state_key] = {
                    "canvas_version": canvas_version,
                    "canvas_id": file.get("id"),
                    "path": save_path,
                }
            else:
                failed.append((display_name, folder_path, err or "Unknown error"))
        else:
            skipped.append((display_name, folder_path))
            push_state[state_key] = {
                "canvas_version": canvas_version,
                "canvas_id": file.get("id"),
                "path": save_path,
            }

    save_push_state(push_state)

    # Optional: adjust console printing to show paths too
    print(f"\n--- {course_code} ---")
    print(f"Downloaded (new):    {len(downloaded)}")
    for name, folder in downloaded:
        print(f"  - {os.path.join(folder, name)}")
    print(f"Updated (replaced):  {len(updated)}")
    for name, folder in updated:
        print(f"  - {os.path.join(folder, name)}")
    print(f"Skipped (up to date): {len(skipped)}")
    if moved_out:
        print(f"Left alone (moved by you): {len(moved_out)}")
        for name, folder in moved_out:
            print(f"  - {os.path.join(folder, name)}")
    if failed:
        print(f"Failed:             {len(failed)}")
        for name, folder, err in failed:
            print(f"  - {os.path.join(folder, name)}: {err}")

    return {
        "course_code": course_code,
        "downloaded": downloaded,
        "updated": updated,
        "skipped": skipped,
        "moved_out": moved_out,
        "failed": failed,
    }


def format_course_summary(course_result):
    downloaded = course_result["downloaded"]   # list of (name, folder_path)
    updated = course_result["updated"]         # list of (name, folder_path)
    skipped = course_result["skipped"]         # list of (name, folder_path)
    failed = course_result["failed"]           # list of (name, folder_path, err)

    if not downloaded and not updated and not failed:
        return None

    lines = []
    lines.append(f"- Downloaded (new): {len(downloaded)}")
    for name, folder in downloaded:
        rel = os.path.join(folder, name)
        lines.append(f"  - `{rel}`")

    lines.append(f"- Updated (replaced): {len(updated)}")
    for name, folder in updated:
        rel = os.path.join(folder, name)
        lines.append(f"  - `{rel}`")

    lines.append(f"- Skipped (up to date): {len(skipped)}")

    if failed:
        lines.append(f"- Failed: {len(failed)}")
        for name, folder, err in failed:
            rel = os.path.join(folder, name)
            lines.append(f"  - `{rel}` — {err}")

    return lines

def run():
    if not OUTPUT_PATH:
        raise RuntimeError("OUTPUT_PATH is missing from .env")

    run_started_at = datetime.now(TIMEZONE)
    courses = get_all_courses()

    print(f"Syncing courses: {COURSE_CODES if not SYNC_ALL_COURSES else 'all'}")

    courses_log = []
    course_results = []
    any_changes_or_errors = False

    for course_id, course_code in courses:
        if not (SYNC_ALL_COURSES or course_code in COURSE_CODES):
            continue

        print(f"\nCourse: {course_code}")
        folder_id_name_map = get_all_folders(course_id, course_code)
        result = download_all_files(course_id, folder_id_name_map, course_code)
        course_results.append(result)
        summary_lines = format_course_summary(result)

        if summary_lines is None:
            print("  No changes (all files up to date).")
        else:
            any_changes_or_errors = True
            for line in summary_lines:
                print(line)
            courses_log.append((course_code, summary_lines))

    # Only touch the cloud changelog when there is new information to publish.
    # A no-change Canvas run must not depend on a potentially evicted OneDrive file.
    if any_changes_or_errors:
        fresh_entry = build_run_entry(run_started_at, courses_log, str(uuid.uuid4()))
    else:
        fresh_entry = ""

    pending_entry = load_pending_changelog()
    entries_to_publish = fresh_entry + pending_entry
    changelog_status = {"status": "not_needed", "error": None}
    if entries_to_publish:
        # Persist before attempting the cloud write, so a connection failure can
        # never lose a record of downloaded, updated, or failed course files.
        queued, queue_error = save_pending_changelog(entries_to_publish)
        if not queued:
            changelog_status = {
                "status": "deferred_outbox_write_failed",
                "error": f"local outbox: {queue_error}",
            }
        else:
            changelog_ok, changelog_error = update_changelog(
                OUTPUT_PATH, run_started_at, entries_to_publish
            )
            if changelog_ok:
                try:
                    clear_pending_changelog()
                    changelog_status = {"status": "published", "error": None}
                except OSError as e:
                    # A later run will detect the event marker and avoid duplication.
                    changelog_status = {"status": "published_outbox_cleanup_failed", "error": str(e)}
            else:
                changelog_status = {"status": "deferred", "error": changelog_error}

    # --- Second Brain: upcoming assignments tracker (added 2026-08-12 by Claude) ---
    upcoming_status = {"status": "not_run", "error": None}
    try:
        import fetch_upcoming
        upcoming_result = fetch_upcoming.main()
        upcoming_status = {
            "status": (
                "published"
                if upcoming_result["ok"] and upcoming_result.get("targets")
                else ("unchanged" if upcoming_result["ok"] else "deferred")
            ),
            "error": upcoming_result.get("error"),
        }
    except Exception as e:
        print(f"[fetch_upcoming] skipped due to error: {e}")
        upcoming_status = {"status": "deferred", "error": str(e)}

    state_ok, state_error = write_local_run_state(
        run_started_at, course_results, changelog_status, upcoming_status
    )
    if not state_ok:
        print(f"[fetchcanvas] could not record local run state: {state_error}")

    content_failures = sum(len(result["failed"]) for result in course_results)
    if content_failures:
        print(f"FetchCanvas completed with {content_failures} course-file failure(s).")
        return 1

    metadata_deferred = (
        changelog_status["status"] not in {"not_needed", "published"}
        or upcoming_status["status"] == "deferred"
        or not state_ok
    )
    if metadata_deferred:
        print("Canvas sync completed; OneDrive metadata is deferred. See ~/.fetchcanvas/last-run.json.")
        return METADATA_DEFERRED_EXIT_CODE

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
