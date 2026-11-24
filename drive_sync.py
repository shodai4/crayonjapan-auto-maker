"""Google Drive hub for the CrayonJapan pipeline (phase 2).

Folder layout inside the personal Drive::

    AI秘書 / 12_動画自動作成/
        01_RAW_VIDEO/      <- one subfolder per artwork, renamed GO_... when complete
        02_READY_TO_POST/  <- finished MP4, artwork JPEG and English metadata
        03_ARCHIVE/        <- the GO_ folder, once it has been rendered

A subfolder only counts once its name starts with ``GO_`` (case-insensitive):
that is the user's signal that every clip and the finished scan are uploaded.
Folders without the prefix are still being filled and are left alone. A loose
video file dropped straight into 01_RAW_VIDEO keeps the old single-file flow.

One finished drawing also gets its own subfolder inside 02_READY_TO_POST
(``{base_name}_{YYYYMMDD_HHMM}``), the same name as the render's local output
folder, so the four artifacts of a Short stay together instead of mixing with
the next artwork in a flat folder.

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
import re
import shutil
import time
from dataclasses import dataclass, field
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
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
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
# A project scan also needs each clip's own length: Drive reports it in
# videoMediaMetadata, so the plan can be built before a 6 GB file is pulled.
ITEM_FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,"
    "videoMediaMetadata(durationMillis,width,height)"
)

# The user's signal that an upload is finished: the folder is renamed to GO_...
GO_PREFIX = "GO_"
GO_FOLDER_PATTERN = re.compile(r"^GO_", re.IGNORECASE)
KIND_PROJECT = "project"
KIND_VIDEO = "video"

# Names that mean "this clip shows the finished picture" and therefore sort last
# inside a project folder.
COMPLETION_CLIP_PATTERN = re.compile(
    r"(?:^|[_\-\.\s])(finish|finished|final|complete|completed|end|done|完成)(?:$|[_\-\.,\s\d])",
    re.IGNORECASE,
)

DEFAULT_MIN_AGE_SECONDS = 60
DEFAULT_GO_FOLDER_MIN_AGE_SECONDS = 5
DEFAULT_INTERVAL_SECONDS = 60
STATE_VERSION = 1

# Artifacts the pipeline returns and that end up in 02_READY_TO_POST.
ARTIFACT_KEYS = ("final_video", "artwork", "metadata_text", "metadata_json")
# Suffix for the package folder's name when the pipeline did not name it:
# ``{base_name}_{YYYYMMDD_HHMM}``, the render's local output folder name.
READY_STAMP_FORMAT = "%Y%m%d_%H%M"

Pipeline = Callable[["ProjectSource"], dict]
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


@dataclass(frozen=True)
class DriveFileRef:
    """One child of a project folder (or the single video of a plain item)."""

    id: str
    name: str
    size: int = 0
    modified_time: Optional[str] = None
    duration_seconds: Optional[float] = None


@dataclass(frozen=True)
class RawItem:
    """Something waiting in 01_RAW_VIDEO: a GO_ folder or a loose video."""

    id: str
    name: str
    kind: str
    stem: str
    modified_time: Optional[str] = None
    videos: List[DriveFileRef] = field(default_factory=list)
    images: List[DriveFileRef] = field(default_factory=list)

    @property
    def is_project(self) -> bool:
        return self.kind == KIND_PROJECT


@dataclass(frozen=True)
class ProjectSource:
    """What the pipeline receives; it downloads clips one at a time."""

    item_id: str
    name: str
    stem: str
    kind: str
    work_dir: str
    videos: List[DriveFileRef]
    images: List[DriveFileRef]
    fetch: Callable[[str, str], str]

    @property
    def is_project(self) -> bool:
        return self.kind == KIND_PROJECT


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


def is_raw_image_name(name: str) -> bool:
    return str(name or "").lower().endswith(IMAGE_EXTENSIONS)


def is_go_folder_name(name: str) -> bool:
    """True for the only folders the pipeline may touch: ``GO_...``.

    Anything else in 01_RAW_VIDEO is a folder whose upload is still in progress,
    so it is left alone until it is renamed with the GO_ prefix.
    """
    return bool(GO_FOLDER_PATTERN.match(str(name or "")))


def strip_go_prefix(name: str) -> str:
    """``GO_20261121_sushi`` -> ``20261121_sushi`` (the output name stem)."""
    text = str(name or "").strip()
    match = GO_FOLDER_PATTERN.match(text)
    if not match:
        return text
    stripped = text[match.end():].strip()
    return stripped or text


def natural_sort_key(name: str):
    """Sort ``part2.mp4`` before ``part10.mp4`` and keep the completion last.

    Digits are compared as numbers. A clip that looks like the finished shot
    (``finish``, ``final``, ``complete``, ``end``, ``完成``...) is ranked after the
    drawing clips, because it is the end of the timeline the finish-preview
    hook is cut from.
    """
    text = str(name or "")
    parts = re.split(r"(\d+)", text.lower())
    key = []
    for part in parts:
        if not part:
            continue
        key.append((0, int(part)) if part.isdigit() else (1, part))
    stem = os.path.splitext(os.path.basename(text))[0].lower()
    rank = 1 if COMPLETION_CLIP_PATTERN.search(stem) else 0
    return (rank, key)


def sort_naturally(items: Sequence[dict]) -> List[dict]:
    return sorted(items, key=lambda info: natural_sort_key(str(info.get("name", ""))))


def guess_mime_type(path: str) -> str:
    return MIME_BY_EXTENSION.get(os.path.splitext(str(path))[1].lower(), "application/octet-stream")


def ready_package_name(
    artifacts: dict,
    now: Optional[datetime] = None,
    fallback_stem: str = "",
) -> str:
    """The package folder for one render: ``{base_name}_{YYYYMMDD_HHMM}``.

    The pipeline names the folder itself (``output_folder``) so the local output
    folder and the Drive subfolder of 02_READY_TO_POST are the same name; the
    fallback keeps a bare dict-shaped result useful too.
    """
    named = str((artifacts or {}).get("output_folder") or "").strip()
    if named:
        return named
    stem = (
        str((artifacts or {}).get("base_name") or "").strip()
        or str(fallback_stem or "").strip()
        or "artwork"
    )
    stamp = (now or datetime.now(timezone.utc)).strftime(READY_STAMP_FORMAT)
    return f"{stem}_{stamp}"


def _quote_query_value(value: object) -> str:
    """A Drive query string literal's inside: backslash and quote escaped."""
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def create_folder(service, name: str, parent_id: str) -> dict:
    """Create one folder under ``parent_id`` and return its metadata."""
    body = {
        "name": str(name),
        "mimeType": FOLDER_MIME_TYPE,
        "parents": [parent_id],
    }
    return service.files().create(body=body, fields="id,name,webViewLink").execute()


def find_child_by_name(
    service,
    folder_id: str,
    name: str,
    fields: str = FILE_FIELDS,
) -> Optional[dict]:
    """The file called ``name`` directly inside ``folder_id``, or None."""
    query = (
        f"'{folder_id}' in parents and trashed = false "
        f"and name = '{_quote_query_value(name)}'"
    )
    for info in _list_all(service, query, fields=fields):
        if str(info.get("name", "")) == str(name):
            return info
    return None


def update_file_content(
    service,
    file_id: str,
    local_path: str,
    mime_type: Optional[str] = None,
    media_factory: Optional[Callable[[str], object]] = None,
) -> dict:
    """Replace the bytes of an existing Drive file (its id and link stay)."""
    if not os.path.exists(local_path):
        raise DriveSyncError(f"Cannot upload missing file: {local_path}")
    factory = media_factory or _drive_media_upload
    body = {"mimeType": mime_type or guess_mime_type(local_path)}
    return (
        service.files()
        .update(
            fileId=file_id,
            body=body,
            media_body=factory(local_path),
            fields="id,name,size,modifiedTime",
        )
        .execute()
    )


def upload_or_replace(
    service,
    local_path: str,
    folder_id: str,
    name: Optional[str] = None,
    media_factory: Optional[Callable[[str], object]] = None,
    finder: Optional[Callable[[object, str, str], Optional[dict]]] = None,
) -> dict:
    """Keep one file of this name in the folder: update it, or create it.

    The PDCA state files are rewritten on every run; replacing them in place
    keeps a single file in Drive instead of a pile of versions.
    """
    target = str(name or os.path.basename(local_path))
    lookup = finder or find_child_by_name
    existing = lookup(service, folder_id, target)
    if existing and existing.get("id"):
        return update_file_content(
            service, str(existing["id"]), local_path, media_factory=media_factory
        )
    return upload_file(service, local_path, folder_id, name=target, media_factory=media_factory)


def ensure_subfolder(
    service,
    parent_id: str,
    name: str,
    creator: Optional[Callable[[object, str, str], dict]] = None,
) -> dict:
    """The folder ``name`` under ``parent_id``, created only when missing.

    A retried upload finds the folder its first attempt already made instead of
    leaving two packages behind, so the name alone identifies the package.
    """
    lookup = (
        f"'{parent_id}' in parents and trashed = false "
        f"and mimeType = '{FOLDER_MIME_TYPE}' "
        f"and name = '{_quote_query_value(name)}'"
    )
    for info in _list_all(service, lookup, fields="id,name"):
        if str(info.get("name", "")) == str(name):
            return info
    return (creator or create_folder)(service, str(name), parent_id)


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

def _list_all(service, query: str, page_size: int = 100, fields: str = FILE_FIELDS) -> List[dict]:
    files: List[dict] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                pageSize=page_size,
                fields=f"nextPageToken, files({fields})",
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


def _duration_from_metadata(info: dict) -> Optional[float]:
    metadata = info.get("videoMediaMetadata") or {}
    try:
        millis = float(metadata.get("durationMillis") or 0)
    except (TypeError, ValueError):
        return None
    return millis / 1000.0 if millis > 0 else None


def _file_ref(info: dict) -> DriveFileRef:
    try:
        size = int(info.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return DriveFileRef(
        id=str(info.get("id", "")),
        name=str(info.get("name", "")),
        size=size,
        modified_time=info.get("modifiedTime"),
        duration_seconds=_duration_from_metadata(info),
    )


def list_project_files(service, folder_id: str):
    """The videos and scanned images inside one GO_ folder, naturally sorted."""
    query = f"'{folder_id}' in parents and trashed = false and mimeType != '{FOLDER_MIME_TYPE}'"
    children = _list_all(service, query, fields=ITEM_FILE_FIELDS)
    videos = sort_naturally([info for info in children if is_raw_video_name(str(info.get("name", "")))])
    images = sort_naturally([info for info in children if is_raw_image_name(str(info.get("name", "")))])
    return [_file_ref(info) for info in videos], [_file_ref(info) for info in images]


def list_pending_raw_items(service, folder_id: str) -> List[RawItem]:
    """Everything in 01_RAW_VIDEO that may be rendered right now.

    * a subfolder whose name starts with ``GO_`` (case-insensitive) - a project
      of several raw clips plus an optional scan;
    * a loose ``.mp4`` / ``.mov`` / ``.avi`` file - the old single-video flow.

    Other folders are uploads still in progress and are ignored until renamed.
    Items come back oldest name first so the queue stays predictable.
    """
    query = f"'{folder_id}' in parents and trashed = false"
    children = _list_all(service, query, fields=ITEM_FILE_FIELDS)

    items: List[RawItem] = []
    for info in sort_naturally(children):
        name = str(info.get("name", ""))
        item_id = str(info.get("id", ""))
        if not item_id:
            continue
        if info.get("mimeType") == FOLDER_MIME_TYPE:
            if not is_go_folder_name(name):
                continue
            videos, images = list_project_files(service, item_id)
            items.append(
                RawItem(
                    id=item_id,
                    name=name,
                    kind=KIND_PROJECT,
                    stem=strip_go_prefix(name),
                    modified_time=info.get("modifiedTime"),
                    videos=videos,
                    images=images,
                )
            )
        elif is_raw_video_name(name):
            items.append(
                RawItem(
                    id=item_id,
                    name=name,
                    kind=KIND_VIDEO,
                    stem=os.path.splitext(name)[0],
                    modified_time=info.get("modifiedTime"),
                    videos=[_file_ref(info)],
                )
            )
    return items


def download_video(
    service,
    file_id: str,
    dest_path: str,
    fetcher: Optional[Callable[[str, object], None]] = None,
) -> str:
    """Download one Drive file to ``dest_path`` (partial file never left behind).

    ``MediaIoBaseDownload`` streams the bytes in chunks straight to disk, so a
    multi-GB recording is never held in memory.
    """
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


def item_is_settled(
    item: RawItem,
    now: Optional[datetime] = None,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    go_min_age_seconds: int = DEFAULT_GO_FOLDER_MIN_AGE_SECONDS,
) -> bool:
    """Whether an item is complete enough to render.

    A loose video waits ``min_age_seconds`` (its upload is still finishing).
    A ``GO_`` project does not need that: the rename is the explicit "done"
    signal, so only a few seconds are waited out in case the folder was just
    renamed while an upload was still wrapping up.
    """
    if item.is_project:
        return is_settled(
            {"modifiedTime": item.modified_time},
            now=now,
            min_age_seconds=go_min_age_seconds,
        )
    return is_settled(
        {"modifiedTime": item.modified_time},
        now=now,
        min_age_seconds=min_age_seconds,
    )


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
    go_min_age_seconds: int = DEFAULT_GO_FOLDER_MIN_AGE_SECONDS,
    max_items: Optional[int] = None,
    keep_local_raw: bool = True,
    now: Optional[datetime] = None,
    downloader: Optional[Callable[[object, str, str], str]] = None,
    uploader: Optional[Callable[[object, str, str], dict]] = None,
    mover: Optional[Callable[[object, str, str, Optional[str]], dict]] = None,
    folder_creator: Optional[Callable[[object, str, str], dict]] = None,
    log: Log = print,
) -> List[dict]:
    """One scan: pull new raw items, render them, publish to 02_READY_TO_POST.

    The pipeline receives a ``ProjectSource`` that can download each clip on
    demand, so a project's multi-GB recordings are fetched one at a time (and
    deleted again by the pipeline) instead of all at once.

    Each package is uploaded into its own subfolder of 02_READY_TO_POST
    (``{base_name}_{YYYYMMDD_HHMM}``) so the four artifacts of one artwork stay
    together; the folder is looked up first, so a retry reuses it.

    An item is only marked done after its artifacts are uploaded *and* the
    source has been moved to 03_ARCHIVE, so an interruption repeats at most one
    render instead of silently losing a drawing.

    ``max_items`` caps how many items are rendered in this one scan (the
    posting rhythm keeps it at one): items are taken in the deterministic order
    ``list_pending_raw_items`` returns, and everything after the cap stays in
    01_RAW_VIDEO untouched for the next run.
    """
    download = downloader or (lambda svc, fid, path: download_video(svc, fid, path))
    upload = uploader or (lambda svc, path, folder: upload_file(svc, path, folder))
    move = mover or move_file
    ensure_folder = folder_creator or ensure_subfolder

    state = load_state(state_path)
    results: List[dict] = []
    attempted = 0
    try:
        items = list_pending_raw_items(service, folders.raw)
    except DriveSyncError as exc:
        log(f"[drive] could not list {RAW_FOLDER_ENV}: {exc}")
        return results

    for index, item in enumerate(items):
        if not item.id or is_processed(state, item.id):
            continue
        if max_items is not None and attempted >= max_items:
            waiting = len(items) - index
            log(
                f"[drive] stopping after {attempted} item(s) (max {max_items} per "
                f"run); {waiting} item(s) stay in {RAW_FOLDER_ENV} for the next run"
            )
            break
        if not item_is_settled(
            item, now=now,
            min_age_seconds=min_age_seconds,
            go_min_age_seconds=go_min_age_seconds,
        ):
            log(f"[drive] skip (still uploading): {item.name}")
            continue
        if not item.videos:
            log(f"[drive] skip (no video inside yet): {item.name}")
            continue

        work_dir = os.path.join(download_dir, _safe_local_name(item.id, item.stem))
        source = ProjectSource(
            item_id=item.id,
            name=item.name,
            stem=item.stem,
            kind=item.kind,
            work_dir=work_dir,
            videos=list(item.videos),
            images=list(item.images),
            fetch=lambda file_id, path: download(service, file_id, path),
        )
        try:
            log(f"[drive] rendering {item.name} ({len(item.videos)} clip(s))")
            attempted += 1
            artifacts = pipeline(source) or {}
        except Exception as exc:  # one bad item must not stop the watcher
            log(f"[drive] failed: {item.name}: {exc}")
            results.append(
                {"file_id": item.id, "name": item.name, "status": "failed", "error": str(exc)}
            )
            if not keep_local_raw:
                shutil.rmtree(work_dir, ignore_errors=True)
            continue

        uploaded: List[str] = []
        package = ready_package_name(artifacts, now=now, fallback_stem=item.stem)
        try:
            ready_folder = ensure_folder(service, folders.ready, package)
            ready_folder_id = str(ready_folder.get("id") or folders.ready)
            log(f"[drive] package -> 02_READY_TO_POST/{package}")
            for key in ARTIFACT_KEYS:
                path = artifacts.get(key)
                if path and os.path.exists(path):
                    created = upload(service, path, ready_folder_id)
                    uploaded.append(str(created.get("id", "")))
                    log(f"[drive] uploaded {package}/{os.path.basename(path)}")
            move(service, item.id, folders.archive, folders.raw)
        except Exception as exc:
            log(f"[drive] publish failed: {item.name}: {exc}")
            results.append(
                {
                    "file_id": item.id,
                    "name": item.name,
                    "status": "publish_failed",
                    "error": str(exc),
                    "artifacts": artifacts,
                    "package": package,
                    "uploaded": uploaded,
                }
            )
            continue

        mark_processed(
            state,
            item.id,
            name=item.name,
            video_id=str(artifacts.get("video_id") or ""),
            uploaded=uploaded,
        )
        save_state(state, state_path)
        if not keep_local_raw:
            shutil.rmtree(work_dir, ignore_errors=True)
        results.append(
            {
                "file_id": item.id,
                "name": item.name,
                "kind": item.kind,
                "status": "done",
                "video_id": str(artifacts.get("video_id") or ""),
                "artifacts": artifacts,
                "package": package,
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
