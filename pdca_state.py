"""Keep the learning history in Drive, because the Actions run cannot push.

The workflow that renders every Short has ``contents: read`` only (and may not
touch ``.github/workflows/``), so a run cannot commit the refreshed
``performance.csv`` or the chosen parameters. Without a copy outside the runner
every 48-hour run would start from the empty file in the repository and the
loop would never learn anything.

So the two state files live in one Drive folder and are pulled down at the start
of a run and written back at the end:

* ``performance.csv``          - one row per uploaded Short, with its numbers
* ``improvement_state.json``   - last parameters, the reflection, the history

The folder is ``DRIVE_STATE_FOLDER_ID`` when it is set; otherwise the archive
folder (``03_ARCHIVE``) is used, which nothing else reads, so the files never
show up in the artist's raw or ready folders. The repository's
``analytics/performance.csv`` is only the starting point when Drive holds none.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, Optional

import drive_sync
import improvement

PERFORMANCE_NAME = "performance.csv"
STATE_NAME = "improvement_state.json"
STATE_FOLDER_ENV = "DRIVE_STATE_FOLDER_ID"

Downloader = Callable[[object, str, str], str]
Uploader = Callable[[object, str, str], dict]
Log = Callable[[str], None]


def state_folder_id(folders: "drive_sync.DriveFolders", env: Optional[Dict[str, str]] = None) -> str:
    """Where the state files live: the dedicated folder, else the archive."""
    source = os.environ if env is None else env
    dedicated = str(source.get(STATE_FOLDER_ENV, "") or "").strip()
    if dedicated:
        return dedicated
    return str(getattr(folders, "archive", "") or "")


def restore_file(
    service,
    folder_id: str,
    local_path: str,
    name: str,
    downloader: Optional[Downloader] = None,
    log: Log = print,
) -> bool:
    """Copy one state file down from Drive; False when Drive has none."""
    if not folder_id:
        return False
    remote = drive_sync.find_child_by_name(service, folder_id, name, fields="id,name")
    if not remote or not remote.get("id"):
        log(f"[pdca] no {name} in the Drive state folder yet; keeping the local copy")
        return False
    fetch = downloader or (lambda svc, fid, path: drive_sync.download_video(svc, fid, path))
    try:
        fetch(service, str(remote["id"]), local_path)
    except Exception as exc:  # noqa: BLE001 - a restore must never stop a render
        log(f"[pdca] could not restore {name} from Drive ({exc}); keeping the local copy")
        return False
    log(f"[pdca] restored {name} from Drive ({os.path.getsize(local_path)} bytes)")
    return True


def backup_file(
    service,
    folder_id: str,
    local_path: str,
    name: str,
    uploader: Optional[Uploader] = None,
    log: Log = print,
) -> Optional[dict]:
    """Write one state file back to Drive (replacing the previous copy)."""
    if not folder_id or not os.path.exists(local_path):
        return None
    push = uploader or (lambda svc, path, folder: drive_sync.upload_or_replace(svc, path, folder, name=name))
    try:
        created = push(service, local_path, folder_id)
    except Exception as exc:  # noqa: BLE001 - the render already happened
        log(f"[pdca] could not save {name} to Drive: {exc}")
        return None
    log(f"[pdca] saved {name} to Drive")
    return created


def restore_all(
    service,
    folders: "drive_sync.DriveFolders",
    csv_path: str = improvement.PERFORMANCE_PATH,
    state_path: str = improvement.STATE_PATH,
    downloader: Optional[Downloader] = None,
    log: Log = print,
) -> Dict[str, bool]:
    """Pull both state files before the run measures anything."""
    folder_id = state_folder_id(folders)
    return {
        "performance": restore_file(
            service, folder_id, csv_path, PERFORMANCE_NAME, downloader=downloader, log=log
        ),
        "state": restore_file(
            service, folder_id, state_path, STATE_NAME, downloader=downloader, log=log
        ),
    }


def backup_all(
    service,
    folders: "drive_sync.DriveFolders",
    csv_path: str = improvement.PERFORMANCE_PATH,
    state_path: str = improvement.STATE_PATH,
    uploader: Optional[Uploader] = None,
    log: Log = print,
) -> Dict[str, Optional[dict]]:
    """Write both state files back after the run."""
    folder_id = state_folder_id(folders)
    return {
        "performance": backup_file(
            service, folder_id, csv_path, PERFORMANCE_NAME, uploader=uploader, log=log
        ),
        "state": backup_file(
            service, folder_id, state_path, STATE_NAME, uploader=uploader, log=log
        ),
    }
