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


# From upstream (Jun Yu), minus ':' — the vault already holds NST2046 folders such as
# "Chapter 7: Prediction", and replacing ':' would deliver every file in them a second time.
# canvas-mirror.py on gram-server applies the identical rule so both sides agree on paths.
_ILLEGAL_FS_CHARS = re.compile(r'[<>"/\\|?*]')

# Canvas answers these when a course hides or disables an area (Files tab, Modules) for
# students. That is a property of the course, not a failed sync: note it and skip the area.
HIDDEN_AREA_STATUSES = (401, 403, 404)


def sanitize_name(name):
    """Replace filesystem-illegal characters and trim whitespace/dots.

    Course codes such as CS4246/CS5446 contain a slash and would otherwise nest a directory.
    """
    name = _ILLEGAL_FS_CHARS.sub("-", name or "")
    return name.strip(". ") or "unnamed"


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
    if parts and parts[0] == sanitize_name(course_code):
        parts = parts[1:]
    course_dir = os.path.join(VAULT_ROOT, sanitize_name(course_code), COURSE_SUBDIR)
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


def get_optional(fetch, url, description, hidden_note):
    """Fetch an area a course may hide. Returns None (after printing why) when it is hidden.

    Any other HTTP or network error still raises: only "not visible to you" licenses a skip.
    """
    try:
        return fetch(url)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status not in HIDDEN_AREA_STATUSES:
            raise
        reason = e.response.reason if e.response is not None else ""
        print(f"  {description}: {status} {reason}. {hidden_note}")
        return None


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

    folder_list = get_optional(
        get_json_paginated, url, f"folders for {course_name} ({course_id})",
        "Folders are not accessible (the course 'Files' area is often hidden); "
        "files will be placed without their Canvas folder path.",
    )
    if folder_list is None:
        return None

    folder_id_name_map = {}
    for folder in folder_list:
        folder_id = folder['id']
        folder_name = folder['name']
        parent_folder_id = folder['parent_folder_id']

        if folder_name == 'course files':
            folder_name = course_name

        folder_id_name_map[folder_id] = (sanitize_name(folder_name), parent_folder_id)

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
                "moved_out": len(result.get("moved_out", [])),
                "hidden": len(result.get("hidden", [])),
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
    seen = set()

    # Stops at a folder the map does not know: with the folders area hidden the map is empty
    # and the file lands at the course root instead of crashing the run.
    while folder_id is not None and folder_id in folder_id_name_map and folder_id not in seen:
        seen.add(folder_id)
        name, folder_id = folder_id_name_map[folder_id]
        path.append(name)
    
    path.reverse()
    return os.path.join(*path) if path else ""


def new_course_result(course_code):
    return {
        "course_code": course_code,
        "downloaded": [],
        "updated": [],
        "skipped": [],
        "moved_out": [],
        "failed": [],
        "hidden": [],       # areas or module files Canvas does not show you (not a failure)
        "file_ids": set(),  # Canvas ids the Files listing returned; Modules skips these
    }


def sync_file(file, folder_path, course_code, push_state, result):
    """Deliver one Canvas file object into the course's OneDrive folder."""
    display_name = sanitize_name(file.get('filename') or file.get('display_name'))
    save_path, rel_key = destination_for(course_code, folder_path, display_name)
    state_key = f"{course_code}/{rel_key}"
    canvas_version = str(file.get('updated_at') or file.get('size') or '')
    record = {
        "canvas_version": canvas_version,
        "canvas_id": file.get("id"),
        "path": save_path,
    }

    if not file.get('url'):
        result["failed"].append((display_name, folder_path, "No download url in file object"))
        return

    try:
        local_stat = os.stat(save_path)
        exists = True
    except FileNotFoundError:
        exists = False
        local_stat = None
    except OSError as e:
        result["failed"].append((display_name, folder_path, str(e)))
        return

    if not exists:
        # Absent locally. If we previously delivered THIS EXACT Canvas version, the user
        # moved or deleted it on purpose — respect that and do not resurrect it. A newer
        # version from Canvas falls through and is delivered normally.
        prior = push_state.get(state_key)
        if prior and prior.get("canvas_version") == canvas_version:
            result["moved_out"].append((display_name, folder_path))
            return
        ok, err = download_file(file['url'], save_path)
        if ok:
            result["downloaded"].append((display_name, folder_path))
            push_state[state_key] = record
        else:
            result["failed"].append((display_name, folder_path, err or "Unknown error"))
        return

    try:
        canvas_updated = datetime.fromisoformat(
            file['updated_at'].replace('Z', '+00:00')
        ).timestamp()
    except (KeyError, AttributeError, ValueError):
        canvas_updated = float('inf')

    local_mtime = local_stat.st_mtime
    if canvas_updated > local_mtime:
        ok, err = download_file(file['url'], save_path)
        if ok:
            result["updated"].append((display_name, folder_path))
            push_state[state_key] = record
        else:
            result["failed"].append((display_name, folder_path, err or "Unknown error"))
    else:
        result["skipped"].append((display_name, folder_path))
        push_state[state_key] = record


def download_all_files(course_id, folder_id_name_map, course_code, push_state=None, result=None):
    url = f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}/files?per_page=100"
    result = result if result is not None else new_course_result(course_code)
    own_state = push_state is None
    push_state = load_push_state() if own_state else push_state

    file_list = get_optional(
        get_json_paginated, url, f"files for {course_code} ({course_id})",
        "Files are not accessible (the course 'Files' tab is usually hidden); "
        "only files linked from Modules will be synced.",
    )
    if file_list is None:
        result["hidden"].append(("Files", "files listing hidden"))
        file_list = []

    for file in file_list:
        result["file_ids"].add(file.get("id"))
        folder_path = resolve_path_for_file(file.get('folder_id'), folder_id_name_map or {})
        sync_file(file, folder_path, course_code, push_state, result)

    if own_state:
        save_push_state(push_state)
    return result


def download_module_files(course_id, course_code, push_state, result):
    """Sync files linked from Modules that the Files listing did not already return.

    From upstream (Jun Yu), narrowed: only module-ONLY files are fetched, so a course whose
    Files tab is hidden still syncs, while a file that is in both places is not delivered
    twice. They land in OneDrive/<COURSE>/FetchCanvas/Modules/<module name>/<file>, the same
    path canvas-mirror.py uses on gram-server.
    """
    modules = get_optional(
        get_json_paginated,
        f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}/modules?per_page=100",
        f"modules for {course_code} ({course_id})",
        "Modules are not accessible; modules sync will be skipped for this course.",
    )
    if modules is None:
        result["hidden"].append(("Modules", "modules listing hidden"))
        return result

    seen_ids = set(result["file_ids"])
    for module in modules:
        module_name = sanitize_name(module.get('name'))
        folder_path = os.path.join(sanitize_name(course_code), "Modules", module_name)
        items = get_optional(
            get_json_paginated,
            f"{CANVAS_BASE_URL}/api/v1/courses/{course_id}/modules/{module['id']}/items?per_page=100",
            f"items for module '{module_name}'",
            "Skipping this module.",
        )
        if items is None:
            result["hidden"].append((module_name, "module items hidden"))
            continue

        for item in items:
            if item.get('type') != 'File':
                continue
            file_id = item.get('content_id')
            if file_id in seen_ids:
                continue  # already synced from Files, or linked from an earlier module
            seen_ids.add(file_id)

            title = sanitize_name(item.get('title') or str(file_id))
            if not item.get('url'):
                result["failed"].append((title, folder_path, "No file API url in module item"))
                continue
            try:
                file = get_optional(
                    get_json, item['url'], f"file '{title}' in module '{module_name}'",
                    "It is locked or hidden; skipping.",
                )
            except (requests.exceptions.RequestException, RuntimeError) as e:
                result["failed"].append((title, folder_path, str(e)))
                continue
            if file is None:
                result["hidden"].append((os.path.join(module_name, title), "file locked or hidden"))
                continue
            sync_file(file, folder_path, course_code, push_state, result)

    return result


def print_course_result(result):
    course_code = result["course_code"]
    downloaded, updated = result["downloaded"], result["updated"]
    moved_out, failed = result["moved_out"], result["failed"]
    hidden = result.get("hidden", [])

    # Optional: adjust console printing to show paths too
    print(f"\n--- {course_code} ---")
    print(f"Downloaded (new):    {len(downloaded)}")
    for name, folder in downloaded:
        print(f"  - {os.path.join(folder, name)}")
    print(f"Updated (replaced):  {len(updated)}")
    for name, folder in updated:
        print(f"  - {os.path.join(folder, name)}")
    print(f"Skipped (up to date): {len(result['skipped'])}")
    if moved_out:
        print(f"Left alone (moved by you): {len(moved_out)}")
        for name, folder in moved_out:
            print(f"  - {os.path.join(folder, name)}")
    if hidden:
        print(f"Not visible to you:  {len(hidden)}")
        for what, why in hidden:
            print(f"  - {what}: {why}")
    if failed:
        print(f"Failed:             {len(failed)}")
        for name, folder, err in failed:
            print(f"  - {os.path.join(folder, name)}: {err}")


def sync_course(course_id, course_code):
    """Files, then module-only files, for one course. Never raises for a Canvas/network error:
    one unreachable course is recorded as a failure instead of aborting every other course."""
    result = new_course_result(course_code)
    push_state = load_push_state()
    try:
        folder_id_name_map = get_all_folders(course_id, course_code)
        download_all_files(course_id, folder_id_name_map, course_code, push_state, result)
        download_module_files(course_id, course_code, push_state, result)
    except (requests.exceptions.RequestException, RuntimeError) as e:
        result["failed"].append(("(course listing)", course_code, str(e)))
    save_push_state(push_state)
    print_course_result(result)
    return result


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

def warn_unmatched_courses(courses):
    """Upstream's Known Issue: a COURSES typo, or a course you are no longer enrolled in,
    used to sync nothing for it without a word."""
    if SYNC_ALL_COURSES:
        return []
    enrolled = {code for _, code in courses}
    unmatched = sorted(COURSE_CODES - enrolled)
    if unmatched:
        print(f"Warning: COURSES lists {', '.join(unmatched)}, which match(es) no active "
              f"enrolment. Active course codes: {', '.join(sorted(enrolled)) or 'none'}")
    if not COURSE_CODES & enrolled:
        print("Warning: no configured course matched; nothing will be synced.")
    return unmatched


def run():
    if not OUTPUT_PATH:
        raise RuntimeError("OUTPUT_PATH is missing from .env")

    run_started_at = datetime.now(TIMEZONE)
    courses = get_all_courses()

    print(f"Syncing courses: {COURSE_CODES if not SYNC_ALL_COURSES else 'all'}")
    warn_unmatched_courses(courses)

    courses_log = []
    course_results = []
    any_changes_or_errors = False

    for course_id, course_code in courses:
        if not (SYNC_ALL_COURSES or course_code in COURSE_CODES):
            continue

        print(f"\nCourse: {course_code}")
        result = sync_course(course_id, course_code)
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
