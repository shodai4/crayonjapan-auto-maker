"""Google Drive hub for the CrayonJapan pipeline (phase 2).

Folder layout inside the personal Drive::

    AI秘書 / 12_動画自動作成/
        01_RAW_VIDEO/      <- drop the raw drawing recording here
        02_READY_TO_POST/  <- finished MP4, artwork JPEG and English metadata
        03_ARCHIVE/        <- the raw recording, once it has been rendered

The repository is public, so no folder ID or account address is hard-coded
here: the three IDs come from the environment (``.env``) and the OAuth token is
kept in ``drive_token.json`` (both git-ignored).

Only the transport layer (``build_drive_service``, ``_drive_media_fetcher``,
``_drive_media_upload``) imports ``googleapiclient``, and only when it is
called, so the orchestration below runs and is tested without credentials.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

RAW_FOLDER_ENV = "DRIVE_RAW_FOLDER_ID"
READY_FOLDER_ENV = "DRIVE_READY_FOLDER_ID"
ARCHIVE_FOLDER_ENV = "DRIVE_ARCHIVE_FOLDER_ID"

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_SECRET_PATH = "client_secret.json"
DRIVE_TOKEN_PATH = "drive_token.json"
DEFAULT_STATE_PATH = "drive_state.json"

# CI hands the credentials in through the environment instead of a local file:
# a long-lived authorized-user JSON (``GDRIVE_TOKEN_JSON``) or the short-lived
# access token that the Apps Script trigger forwards with each dispatch
# (``GDRIVE_ACCESS_TOKEN``). Neither is ever written to the repository.
TOKEN_JSON_ENV = "GDRIVE_TOKEN_JSON"
ACCESS_TOKEN_ENV = "GDRIVE_ACCESS_TOKEN"

RAW_EXTENSIONS = (".mp4", ".mov", ".avi")
MIME_BY_EXTENSION = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".txt": "text/plain",
    ".json": "application/json",
    ".srt": "application/x-subrip",
}
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum"

DEFAULT_MIN_AGE_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 60
STATE_VERSION = 1

# Artifacts the pipeline returns and that end up in 02_READY_TO_POST.
ARTIFACT_KEYS = ("final_video", "artwork", "metadata_text", "metadata_json")

Pipeline = Callable[[str], dict]
Log = Callable[[str], None]


class DriveSyncError(RuntimeError):
    """Anything that should be logged and skipped instead of crashing a run."""


class MissingFolderIdError(DriveSyncError):
    """The three Drive folder IDs are not configured yet."""


@dataclass(frozen=True)
class DriveFolders:
    raw: str
    ready: str
    archive: str


def load_drive_folders(env: Optional[Dict[str, str]] = None) -> DriveFolders:
    """Read the three folder IDs from the environment (``.env``)."""
    source = os.environ if env is None else env
    values = {}
    for key, env_name in (
        ("raw", RAW_FOLDER_ENV),
        ("ready", READY_FOLDER_ENV),
        ("archive", ARCHIVE_FOLDER_ENV),
    ):
        value = str(source.get(env_name, "") or "").strip()
        if not value:
            raise MissingFolderIdError(
                f"{env_name} is not set. Copy .env.example to .env and fill in the "
                "ID of the matching Drive folder."
            )
        values[key] = value
    return DriveFolders(**values)


def is_raw_video_name(name: str) -> bool:
    return str(name or "").lower().endswith(RAW_EXTENSIONS)


def guess_mime_type(path: str) -> str:
    return MIME_BY_EXTENSION.get(os.path.splitext(str(path))[1].lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Transport layer (needs the Google client libraries and a real OAuth token)
# ---------------------------------------------------------------------------

def load_drive_credentials(
    client_secret_path: str = CLIENT_SECRET_PATH,
    token_path: str = DRIVE_TOKEN_PATH,
    scopes: Sequence[str] = DRIVE_SCOPES,
    force_authorize: bool = False,
):
    """Load (or create) the Drive OAuth token.

    Drive keeps its own token file: the YouTube token already holds YouTube
    scopes only, and mixing them would force a re-consent on every API.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(token_path) and not force_authorize:
        creds = Credentials.from_authorized_user_file(token_path, list(scopes))
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secret_path):
                raise DriveSyncError(
                    f"{client_secret_path} not found. Create a Desktop OAuth client in "
                    "Google Cloud Console with the Drive API enabled and save the JSON "
                    "in the project root."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, list(scopes))
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())
    return creds


def load_credentials_from_env(env: Optional[Dict[str, str]] = None):
    """Credentials handed in by CI, or ``None`` when the local token is used.

    ``GDRIVE_ACCESS_TOKEN`` wins: it is the token the Apps Script trigger puts
    in the dispatch payload, and it only lives for about an hour. Otherwise
    ``GDRIVE_TOKEN_JSON`` (a full authorized-user JSON with a refresh token)
    keeps a scheduled run working without the Apps Script hop.
    """
    source = os.environ if env is None else env
    access_token = str(source.get(ACCESS_TOKEN_ENV, "") or "").strip()
    token_json = str(source.get(TOKEN_JSON_ENV, "") or "").strip()
    if not access_token and not token_json:
        return None

    from google.oauth2.credentials import Credentials

    if access_token:
        return Credentials(token=access_token)
    try:
        info = json.loads(token_json)
    except json.JSONDecodeError as exc:
        raise DriveSyncError(f"{TOKEN_JSON_ENV} is not valid JSON") from exc
    if not isinstance(info, dict) or not info.get("refresh_token"):
        raise DriveSyncError(
            f"{TOKEN_JSON_ENV} must hold an authorized-user JSON that includes a "
            "refresh_token (the content of drive_token.json)."
        )
    return Credentials.from_authorized_user_info(info, list(DRIVE_SCOPES))


def build_drive_service(credentials=None, token_path: str = DRIVE_TOKEN_PATH):
    """Build the Drive v3 client (first local call opens the consent screen)."""
    from googleapiclient.discovery import build

    creds = credentials or load_credentials_from_env() or load_drive_credentials(token_path=token_path)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _drive_media_fetcher(service) -> Callable[[str, object], None]:
    """Return a function that streams one Drive file into an open binary file."""
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise DriveSyncError(
            "google-api-python-client is not installed; run "
            "`pip install -r requirements.txt`"
        ) from exc

    def fetch(file_id: str, handle) -> None:
        request = service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return fetch


def _drive_media_upload(path: str):
    """Resumable upload body, so a big MP4 survives a dropped connection."""
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise DriveSyncError(
            "google-api-python-client is not installed; run "
            "`pip install -r requirements.txt`"
        ) from exc
    return MediaFileUpload(path, chunksize=-1, resumable=True, mimetype=guess_mime_type(path))


# ---------------------------------------------------------------------------
# Drive operations
# ---------------------------------------------------------------------------

def _list_all(service, query: str, page_size: int = 100) -> List[dict]:
    files: List[dict] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                pageSize=page_size,
                fields=f"nextPageToken, files({FILE_FIELDS})",
                pageToken=page_token,
                spaces="drive",
            )
            .execute()
        )
        files.extend(response.get("files", []) or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def list_raw_videos(
    service,
    folder_id: str,
    extensions: Sequence[str] = RAW_EXTENSIONS,
) -> List[dict]:
    """Videos waiting in 01_RAW_VIDEO, oldest name first."""
    query = (
        f"'{folder_id}' in parents and trashed = false "
        f"and mimeType != '{FOLDER_MIME_TYPE}'"
    )
    videos = [
        info
        for info in _list_all(service, query)
        if str(info.get("name", "")).lower().endswith(tuple(extensions))
    ]
    return sorted(videos, key=lambda info: str(info.get("name", "")).lower())


def download_video(
    service,
    file_id: str,
    dest_path: str,
    fetcher: Optional[Callable[[str, object], None]] = None,
) -> str:
    """Download one Drive file to ``dest_path`` (partial file never left behind)."""
    fetch = fetcher or _drive_media_fetcher(service)
    directory = os.path.dirname(dest_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    partial = f"{dest_path}.part"
    try:
        with open(partial, "wb") as handle:
            fetch(file_id, handle)
        if os.path.getsize(partial) == 0:
            raise DriveSyncError(f"Drive returned an empty file for {file_id}")
        os.replace(partial, dest_path)
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    return dest_path


def upload_file(
    service,
    local_path: str,
    folder_id: str,
    name: Optional[str] = None,
    mime_type: Optional[str] = None,
    media_factory: Optional[Callable[[str], object]] = None,
) -> dict:
    """Upload one artifact into a folder and return the created file metadata."""
    if not os.path.exists(local_path):
        raise DriveSyncError(f"Cannot upload missing file: {local_path}")
    factory = media_factory or _drive_media_upload
    body = {
        "name": name or os.path.basename(local_path),
        "parents": [folder_id],
        "mimeType": mime_type or guess_mime_type(local_path),
    }
    return (
        service.files()
        .create(body=body, media_body=factory(local_path), fields="id,name,webViewLink,size")
        .execute()
    )


def move_file(service, file_id: str, new_parent_id: str, old_parent_id: Optional[str] = None) -> dict:
    """Re-parent a file (Drive's way of moving) and return its new metadata."""
    kwargs = {
        "fileId": file_id,
        "addParents": new_parent_id,
        "fields": "id,name,parents,webViewLink",
    }
    if old_parent_id:
        kwargs["removeParents"] = old_parent_id
    return service.files().update(**kwargs).execute()


# ---------------------------------------------------------------------------
# Local state: remembers done files so a failed move never renders twice
# ---------------------------------------------------------------------------

def _empty_state() -> dict:
    return {"version": STATE_VERSION, "processed": {}}


def load_state(path: str = DEFAULT_STATE_PATH) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return _empty_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(state, dict) or not isinstance(state.get("processed"), dict):
        return _empty_state()
    state.setdefault("version", STATE_VERSION)
    return state


def save_state(state: dict, path: str = DEFAULT_STATE_PATH) -> str:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def is_processed(state: dict, file_id: str) -> bool:
    return file_id in (state.get("processed") or {})


def mark_processed(
    state: dict,
    file_id: str,
    name: str = "",
    status: str = "done",
    video_id: str = "",
    uploaded: Optional[Sequence[str]] = None,
    finished_at: Optional[str] = None,
) -> dict:
    state.setdefault("processed", {})[file_id] = {
        "name": name,
        "status": status,
        "video_id": video_id,
        "uploaded": list(uploaded or []),
        "finished_at": finished_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    return state


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _parse_modified(modified_time: Optional[str]) -> Optional[datetime]:
    if not modified_time:
        return None
    text = str(modified_time).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_settled(
    file_info: dict,
    now: Optional[datetime] = None,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
) -> bool:
    """False while the upload is still running (modified seconds ago).

    A video that is still being copied into Drive would otherwise be downloaded
    half-finished and fail mid-render.
    """
    if min_age_seconds <= 0:
        return True
    modified = _parse_modified(file_info.get("modifiedTime"))
    if modified is None:
        return True
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference - modified >= timedelta(seconds=min_age_seconds)


def _safe_local_name(file_id: str, name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in os.path.basename(name))
    cleaned = cleaned or "video.mp4"
    return f"{file_id[:8]}_{cleaned}"


def sync_once(
    service,
    folders: DriveFolders,
    pipeline: Pipeline,
    download_dir: str = "input",
    state_path: str = DEFAULT_STATE_PATH,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    keep_local_raw: bool = True,
    now: Optional[datetime] = None,
    downloader: Optional[Callable[[object, str, str], str]] = None,
    uploader: Optional[Callable[[object, str, str], dict]] = None,
    mover: Optional[Callable[[object, str, str, Optional[str]], dict]] = None,
    log: Log = print,
) -> List[dict]:
    """One scan: pull new raw videos, render them, publish to 02_READY_TO_POST.

    A video is only marked done after its artifacts are uploaded *and* the raw
    file has been moved to 03_ARCHIVE, so an interruption repeats at most one
    render instead of silently losing a drawing.
    """
    download = downloader or (lambda svc, fid, path: download_video(svc, fid, path))
    upload = uploader or (lambda svc, path, folder: upload_file(svc, path, folder))
    move = mover or move_file

    state = load_state(state_path)
    results: List[dict] = []
    try:
        videos = list_raw_videos(service, folders.raw)
    except DriveSyncError as exc:
        log(f"[drive] could not list {RAW_FOLDER_ENV}: {exc}")
        return results

    for info in videos:
        file_id = str(info.get("id", ""))
        name = str(info.get("name", ""))
        if not file_id:
            continue
        if is_processed(state, file_id):
            continue
        if not is_settled(info, now=now, min_age_seconds=min_age_seconds):
            log(f"[drive] skip (still uploading): {name}")
            continue

        local_path = os.path.join(download_dir, _safe_local_name(file_id, name))
        try:
            log(f"[drive] downloading {name}")
            download(service, file_id, local_path)
            artifacts = pipeline(local_path) or {}
        except Exception as exc:  # one bad video must not stop the watcher
            log(f"[drive] failed: {name}: {exc}")
            results.append({"file_id": file_id, "name": name, "status": "failed", "error": str(exc)})
            continue

        uploaded: List[str] = []
        try:
            for key in ARTIFACT_KEYS:
                path = artifacts.get(key)
                if path and os.path.exists(path):
                    created = upload(service, path, folders.ready)
                    uploaded.append(str(created.get("id", "")))
                    log(f"[drive] uploaded {os.path.basename(path)}")
            move(service, file_id, folders.archive, folders.raw)
        except Exception as exc:
            log(f"[drive] publish failed: {name}: {exc}")
            results.append(
                {
                    "file_id": file_id,
                    "name": name,
                    "status": "publish_failed",
                    "error": str(exc),
                    "artifacts": artifacts,
                    "uploaded": uploaded,
                }
            )
            continue

        mark_processed(
            state,
            file_id,
            name=name,
            video_id=str(artifacts.get("video_id") or ""),
            uploaded=uploaded,
        )
        save_state(state, state_path)
        if not keep_local_raw and os.path.exists(local_path):
            os.remove(local_path)
        results.append(
            {
                "file_id": file_id,
                "name": name,
                "status": "done",
                "video_id": str(artifacts.get("video_id") or ""),
                "artifacts": artifacts,
                "uploaded": uploaded,
            }
        )

    save_state(state, state_path)
    return results


def watch(
    service,
    folders: DriveFolders,
    pipeline: Pipeline,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_iterations: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
) -> List[List[dict]]:
    """Keep scanning the raw folder every ``interval_seconds``.

    ``max_iterations`` exists for tests and for one-shot dry runs; production
    stops only on Ctrl+C (``KeyboardInterrupt``).
    """
    log = kwargs.pop("log", print)
    runs: List[List[dict]] = []
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        try:
            results = sync_once(service, folders, pipeline, **kwargs)
        except DriveSyncError as exc:  # keep watching, just report this cycle
            log(f"[drive] scan failed: {exc}")
            results = []
        runs.append(results)
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        sleep(interval_seconds)
    return runs
