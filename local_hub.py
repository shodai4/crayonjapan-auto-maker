"""Windows local folder hub: the same pipeline, with no Google Drive at all.

The Drive hub (``drive_sync.py``) was built because a CI runner cannot write to
the repository and needs a place for the artist to drop a recording from the
phone. On the artist's own Windows PC neither is true, and the free 15 GB of
Drive ran out, so this module replaces the three Drive folders with three
folders on the PC::

    C:\\CrayonJapan\\                      <- the base folder (configurable)
        01_RAW_VIDEO\\                     <- drop the GO_ folder(s) here
            GO_20261121_sushi\\               one artwork: part1.mp4 ... scan.jpg
            GO_20261122_cat\\                 waiting for its posting slot
        02_READY_TO_POST\\                 <- one finished package per Short
            20261121_sushi_20261121_1830\\
        03_ARCHIVE\\                       <- the GO_ folder moved here when done
        _WORK\\                            <- temporary render work (safe to delete)
        logs\\                             <- run_local.bat appends its output here
        local_state.json                  <- what has been rendered already

Everything the pipeline needs is the file system: a ``GO_`` project folder or a
loose recording is rendered in place, the four artifacts of the Short are moved
into its own package folder in ``02_READY_TO_POST``, and the source is moved to
``03_ARCHIVE``. The posting rhythm (one Short per run, 48 h apart) is unchanged:
main.py still asks YouTube for the newest upload before it renders anything.

The rules, the names and the state file shape are the ones ``drive_sync.py``
already established (``GO_`` prefix, natural clip order, a completion clip last,
a package named ``{base_name}_{YYYYMMDD_HHMM}``), so the two hubs behave the
same and the existing tests for that logic keep covering it.

A clip is not copied: it is hard-linked into the work folder when the file
system allows it (NTFS does), so an hour-long recording costs no extra disk, and
the pipeline deleting its work copy can never touch the original. Where a link
is impossible (another volume, FAT32) it falls back to a real copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

import drive_sync

# The base folder: an explicit argument wins, then the environment, then
# config.yaml's ``local.base_dir``, then the default below. Only the base is
# configurable - the three folder names are the artist's habit and the README's.
BASE_ENV = "CRAYONJAPAN_HOME"
DEFAULT_BASE_DIRNAME = "CrayonJapan"
RAW_DIRNAME = "01_RAW_VIDEO"
READY_DIRNAME = "02_READY_TO_POST"
ARCHIVE_DIRNAME = "03_ARCHIVE"
WORK_DIRNAME = "_WORK"
OUTPUT_SUBDIR = "output"
LOGS_DIRNAME = "logs"
DEFAULT_STATE_NAME = "local_state.json"

DEFAULT_MIN_AGE_SECONDS = 60
DEFAULT_GO_FOLDER_MIN_AGE_SECONDS = 5
DEFAULT_SCAN_INTERVAL_SECONDS = 60
# Free space the render needs before a clip is touched (the project flow already
# checks this per clip; the hub only warns about it).
RECOMMENDED_FREE_BYTES = 6 * 1024 * 1024 * 1024

FOLDER_DIRS = (RAW_DIRNAME, READY_DIRNAME, ARCHIVE_DIRNAME)


class LocalHubError(RuntimeError):
    """A local-folder problem that should be logged and skipped, not crash."""


@dataclass(frozen=True)
class LocalFolders:
    """The three folders, the work area and the state file of one base folder."""

    base: str
    raw: str
    ready: str
    archive: str
    work: str
    logs: str
    state_path: str

    @property
    def output(self) -> str:
        """Where the pipeline writes a render before its artifacts are moved."""
        return os.path.join(self.work, OUTPUT_SUBDIR)

    def as_dict(self) -> Dict[str, str]:
        return {
            "base": self.base,
            "raw": self.raw,
            "ready": self.ready,
            "archive": self.archive,
            "work": self.work,
            "logs": self.logs,
            "state": self.state_path,
        }


def default_base_dir(
    platform_name: Optional[str] = None,
    home: Optional[str] = None,
) -> str:
    """``C:\\CrayonJapan`` on Windows, ``~/CrayonJapan`` elsewhere.

    ``os.name`` is ``nt`` on Windows; the other spellings are accepted so the
    choice can be tested (and read) without being on the platform.
    """
    name = str(platform_name if platform_name is not None else os.name).lower()
    if name in {"nt", "win", "win32", "windows"} or name.startswith("win"):
        return "C:\\CrayonJapan"
    return os.path.join(home or os.path.expanduser("~"), DEFAULT_BASE_DIRNAME)


def _clean_base(value: object) -> str:
    """A usable absolute base path from config/env/argument, or ''."""
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    text = os.path.expandvars(os.path.expanduser(text))
    return os.path.abspath(text)


def load_local_folders(
    base_dir: Optional[str] = None,
    config: Optional[dict] = None,
    environ: Optional[Dict[str, str]] = None,
) -> LocalFolders:
    """Resolve the base folder and every path under it.

    The order is: ``base_dir`` (``--local-base``) > ``CRAYONJAPAN_HOME`` >
    ``local.base_dir`` in config.yaml > the default. Nothing is created here, so
    a dry run can describe a layout that does not exist yet.
    """
    source = os.environ if environ is None else environ
    block = dict(((config or {}).get("local") or {}))
    candidates = (
        base_dir,
        source.get(BASE_ENV, ""),
        block.get("base_dir", ""),
    )
    base = ""
    for candidate in candidates:
        base = _clean_base(candidate)
        if base:
            break
    if not base:
        base = default_base_dir()
    state_name = str(block.get("state_file") or DEFAULT_STATE_NAME).strip() or DEFAULT_STATE_NAME
    return LocalFolders(
        base=base,
        raw=os.path.join(base, RAW_DIRNAME),
        ready=os.path.join(base, READY_DIRNAME),
        archive=os.path.join(base, ARCHIVE_DIRNAME),
        work=os.path.join(base, WORK_DIRNAME),
        logs=os.path.join(base, LOGS_DIRNAME),
        state_path=os.path.join(base, state_name),
    )


def ensure_local_layout(folders: LocalFolders, with_logs: bool = True) -> LocalFolders:
    """Create every folder the hub uses; returns the same description."""
    for path in (
        folders.base,
        folders.raw,
        folders.ready,
        folders.archive,
        folders.work,
        folders.output,
        folders.logs if with_logs else "",
        os.path.dirname(folders.state_path),
    ):
        if path:
            os.makedirs(path, exist_ok=True)
    return folders


def missing_folders(folders: LocalFolders) -> List[str]:
    """The folders of the layout that do not exist yet (for --dry-run)."""
    return [path for path in (folders.raw, folders.ready, folders.archive) if not os.path.isdir(path)]


def describe_layout(folders: LocalFolders) -> str:
    """The folder tree as the artist sees it in Explorer."""
    exists = "  (created)" if os.path.isdir(folders.base) else "  (not created yet)"
    lines = [
        f"{folders.base}\\{exists}",
        f"    {RAW_DIRNAME}\\           drop the drawings here (GO_<name> folder or one video)",
        f"    {READY_DIRNAME}\\   the finished Short, artwork and pack, one folder each",
        f"    {ARCHIVE_DIRNAME}\\         the GO_ folder, once it has been rendered",
        f"    {WORK_DIRNAME}\\              temporary render files (safe to delete)",
        f"    {LOGS_DIRNAME}\\              run_local.bat writes its log here",
        f"    {os.path.basename(folders.state_path)}            what has been rendered already",
    ]
    return "\n".join(lines)


def local_task_name(config: Optional[dict] = None) -> str:
    """The Task Scheduler entry name ``register_local_task.bat`` creates."""
    block = dict(((config or {}).get("local") or {}))
    return str(block.get("task_name") or "CrayonJapan Auto-Maker").strip()


# ---------------------------------------------------------------------------
# What is waiting
# ---------------------------------------------------------------------------

def _mtime_of(path: str) -> Optional[str]:
    try:
        moment = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    except OSError:
        return None
    return moment.replace(microsecond=0).isoformat()


def _file_ref(path: str, name: str, size: int = 0) -> "drive_sync.DriveFileRef":
    return drive_sync.DriveFileRef(
        id=path,
        name=name,
        size=size,
        modified_time=_mtime_of(path),
    )


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def list_local_items(folders: LocalFolders, now: Optional[datetime] = None) -> List["drive_sync.RawItem"]:
    """Everything in ``01_RAW_VIDEO`` that may be rendered right now.

    * a subfolder named ``GO_...`` (case-insensitive): one artwork of several
      clips plus an optional scan;
    * a loose ``.mp4`` / ``.mov`` / ``.avi``: the older single-video flow.

    Any other folder is an upload still in progress and is ignored, exactly like
    the Drive hub. Items come back oldest name first (natural order), so a
    backlog is published in the order the drawings were made.

    ``id`` is the item's absolute path: it is what the state file records and
    what the pipeline is handed, so no file id has to come from anywhere.
    """
    if not os.path.isdir(folders.raw):
        return []

    entries = []
    for name in os.listdir(folders.raw):
        path = os.path.join(folders.raw, name)
        entries.append({"name": name, "path": path})

    items: List[drive_sync.RawItem] = []
    for entry in drive_sync.sort_naturally(entries):
        name = entry["name"]
        path = entry["path"]
        if os.path.isdir(path):
            if not drive_sync.is_go_folder_name(name):
                continue
            videos, images = _list_project_files(path)
            items.append(
                drive_sync.RawItem(
                    id=path,
                    name=name,
                    kind=drive_sync.KIND_PROJECT,
                    stem=drive_sync.strip_go_prefix(name),
                    modified_time=_mtime_of(path),
                    videos=videos,
                    images=images,
                )
            )
        elif drive_sync.is_raw_video_name(name):
            items.append(
                drive_sync.RawItem(
                    id=path,
                    name=name,
                    kind=drive_sync.KIND_VIDEO,
                    stem=os.path.splitext(name)[0],
                    modified_time=_mtime_of(path),
                    videos=[_file_ref(path, name, _safe_size(path))],
                )
            )
    return items


def _list_project_files(folder: str):
    """The videos and scans inside one GO_ folder, naturally sorted."""
    children = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            children.append({"name": name, "path": path})
    videos = [
        info for info in drive_sync.sort_naturally(children)
        if drive_sync.is_raw_video_name(info["name"])
    ]
    images = [
        info for info in drive_sync.sort_naturally(children)
        if drive_sync.is_raw_image_name(info["name"])
    ]
    return (
        [_file_ref(info["path"], info["name"], _safe_size(info["path"])) for info in videos],
        [_file_ref(info["path"], info["name"], _safe_size(info["path"])) for info in images],
    )


def item_is_settled(
    item: "drive_sync.RawItem",
    now: Optional[datetime] = None,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    go_min_age_seconds: int = DEFAULT_GO_FOLDER_MIN_AGE_SECONDS,
) -> bool:
    """Whether a loose video (still being copied) or a fresh rename may render.

    The same rule as the Drive hub: a loose file waits ``min_age_seconds``, a
    ``GO_`` folder only a few seconds because the rename is the "done" signal.
    """
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    moment = drive_sync._parse_modified(item.modified_time)
    if moment is None:
        return True
    limit = go_min_age_seconds if item.is_project else min_age_seconds
    if limit <= 0:
        return True
    return reference - moment >= timedelta(seconds=limit)


# ---------------------------------------------------------------------------
# Handing a local item to the pipeline
# ---------------------------------------------------------------------------

def link_or_copy(source: str, destination: str) -> str:
    """Make ``destination`` the same bytes as ``source`` without a copy if possible.

    A hard link is the default: the pipeline deletes its work copy after each
    clip, and deleting one link never deletes the original. A file system that
    refuses the link (another volume, FAT32, a connected network drive) gets an
    ordinary copy instead, so the run still works at the price of disk.
    """
    directory = os.path.dirname(destination)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(destination):
        os.remove(destination)
    try:
        os.link(source, destination)
    except (OSError, AttributeError, NotImplementedError):
        shutil.copy2(source, destination)
    return destination


def source_for(
    item: "drive_sync.RawItem",
    folders: LocalFolders,
    prober: Optional[Callable[[str], float]] = None,
) -> "drive_sync.ProjectSource":
    """The ``ProjectSource`` the existing pipelines already know how to render.

    ``fetch`` links the clip into the work folder instead of downloading it, and
    a clip's own length is read here (the Drive hub got it from the API
    metadata) so the project plan can be computed before any work starts.
    """
    probe = prober or _probe_duration
    work_dir = os.path.join(
        folders.work, drive_sync._safe_local_name(_path_key(item.id), item.stem)
    )
    videos = []
    for clip in item.videos:
        duration = clip.duration_seconds
        if duration is None:
            duration = probe(clip.id)
        videos.append(
            drive_sync.DriveFileRef(
                id=clip.id,
                name=clip.name,
                size=clip.size,
                modified_time=clip.modified_time,
                duration_seconds=duration,
            )
        )
    return drive_sync.ProjectSource(
        item_id=item.id,
        name=item.name,
        stem=item.stem,
        kind=item.kind,
        work_dir=work_dir,
        videos=videos,
        images=list(item.images),
        fetch=link_or_copy,
    )


def _probe_duration(path: str) -> Optional[float]:
    """A clip's length in seconds, or None when it cannot be read."""
    try:
        import video_processor

        length = float(video_processor.probe_duration(path))
    except Exception:  # noqa: BLE001 - ffprobe/OpenCV may both refuse a file
        return None
    return length if length > 0 else None


def _path_key(path: str) -> str:
    """A stable, short key for one path (used for the work folder name)."""
    return hashlib.sha1(
        os.path.normcase(os.path.abspath(str(path))).encode("utf-8")
    ).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Publishing and archiving
# ---------------------------------------------------------------------------

def unique_path(path: str, now: Optional[datetime] = None) -> str:
    """``path``, or a free variant of it, so nothing is ever overwritten.

    Archiving a folder whose name is already in ``03_ARCHIVE`` (the same artwork
    rendered twice after a failure) keeps both: the second gets ``_2``, then the
    timestamp, instead of replacing the first render's record.
    """
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(path)
    for index in range(2, 100):
        candidate = f"{stem}_{index}{extension}"
        if not os.path.exists(candidate):
            return candidate
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    return f"{stem}_{stamp}{extension}"


def move_artifacts(
    artifacts: dict,
    folders: LocalFolders,
    package: str,
    keys: Sequence[str] = drive_sync.ARTIFACT_KEYS,
    mover: Optional[Callable[[str, str], str]] = None,
) -> List[str]:
    """Move the four artifacts of one Short into ``02_READY_TO_POST/<package>``.

    Moving (not copying) on the same disk is instant and leaves no second copy
    of a finished MP4 behind. The returned list is the published paths; an
    artifact that was promised but missing is simply not listed, so the caller
    can tell a full package from a partial one.
    """
    move = mover or shutil.move
    destination_dir = os.path.join(folders.ready, package)
    os.makedirs(destination_dir, exist_ok=True)
    published: List[str] = []
    for key in keys:
        source = str((artifacts or {}).get(key) or "")
        if not source or not os.path.exists(source):
            continue
        target = os.path.join(destination_dir, os.path.basename(source))
        if os.path.abspath(source) == os.path.abspath(target):
            published.append(target)
            continue
        if os.path.exists(target):
            target = unique_path(target)
        move(source, target)
        published.append(target)
    return published


def archive_item(
    item: "drive_sync.RawItem",
    folders: LocalFolders,
    archive: bool = True,
    mover: Optional[Callable[[str, str], str]] = None,
    now: Optional[datetime] = None,
) -> str:
    """Move the rendered ``GO_`` folder (or loose video) into ``03_ARCHIVE``.

    With ``archive=False`` the source stays where it is: the state file still
    prevents a second render, which is what a user wants when they keep their
    recordings in ``01_RAW_VIDEO`` as the master copy.
    """
    if not archive:
        return ""
    move = mover or shutil.move
    source = str(item.id)
    if not os.path.exists(source):
        return ""
    destination = unique_path(os.path.join(folders.archive, item.name), now=now)
    os.makedirs(folders.archive, exist_ok=True)
    move(source, destination)
    return destination


# ---------------------------------------------------------------------------
# State: never render the same artwork twice
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    return drive_sync.load_state(path)


def save_state(state: dict, path: str) -> str:
    return drive_sync.save_state(state, path)


def is_processed(state: dict, item_id: str) -> bool:
    return drive_sync.is_processed(state, item_id)


def mark_processed(state: dict, item_id: str, **kwargs) -> dict:
    return drive_sync.mark_processed(state, item_id, **kwargs)


# ---------------------------------------------------------------------------
# One scan
# ---------------------------------------------------------------------------

def cleanup_render_folder(artifacts: dict, folders: LocalFolders, log=print) -> bool:
    """Delete the render's own working folder once its artifacts are moved.

    The intermediates (the vertical cut, the voice, the ASS document, the
    frame grabs) stay in ``_WORK`` for the length of the render; once the four
    artifacts are in ``02_READY_TO_POST`` nothing reads them again, and an
    hour-long recording leaves a few hundred MB behind on every run. Only a
    folder that really sits inside the hub's own work area is removed.
    """
    candidates = set()
    for key in drive_sync.ARTIFACT_KEYS:
        source = str((artifacts or {}).get(key) or "")
        if source:
            candidates.add(os.path.dirname(os.path.abspath(source)))
    work_root = os.path.normcase(os.path.abspath(folders.work))
    removed = False
    for folder in candidates:
        if not os.path.isdir(folder):
            continue
        target = os.path.normcase(folder)
        if target == work_root or not target.startswith(work_root + os.sep):
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed = True
    if removed:
        log(f"[local] cleaned the render leftovers in {WORK_DIRNAME}")
    return removed


def sync_once(
    folders: LocalFolders,
    pipeline: Callable[["drive_sync.RawItem"], dict],
    state_path: str = "",
    max_items: Optional[int] = None,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    go_min_age_seconds: int = DEFAULT_GO_FOLDER_MIN_AGE_SECONDS,
    archive_after_render: bool = True,
    cleanup: bool = True,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    log=print,
    item_lister=None,
    mover: Optional[Callable[[str, str], str]] = None,
) -> List[dict]:
    """One pass over ``01_RAW_VIDEO``: render, publish, archive.

    The same shape as ``drive_sync.sync_once``: nothing is marked done until the
    package is in ``02_READY_TO_POST`` *and* the source has moved to
    ``03_ARCHIVE``, so an interrupted run repeats at most one artwork instead of
    silently losing a drawing. ``max_items`` is the posting rhythm's one-per-run
    cap; the rest stays in ``01_RAW_VIDEO`` for the next slot.

    ``dry_run`` renders nothing and creates nothing: it returns what would have
    happened (``status: "planned"``), which is what ``main.py --dry-run`` prints.
    """
    state_file = state_path or folders.state_path
    # The state is only read in a dry run, so the answer is the one a real slot
    # would give (an item already rendered is not "waiting" any more).
    state = load_state(state_file)
    results: List[dict] = []
    attempted = 0

    lister = item_lister or list_local_items
    try:
        items = lister(folders, now=now)
    except OSError as exc:
        log(f"[local] could not read {folders.raw}: {exc}")
        return results

    for index, item in enumerate(items):
        if not item.id or is_processed(state, item.id):
            continue
        if max_items is not None and attempted >= max_items:
            waiting = len(items) - index
            log(
                f"[local] stopping after {attempted} item(s) (max {max_items} per run); "
                f"{waiting} item(s) stay in {RAW_DIRNAME} for the next run"
            )
            break
        if not item_is_settled(
            item, now=now,
            min_age_seconds=min_age_seconds,
            go_min_age_seconds=go_min_age_seconds,
        ):
            log(f"[local] skip (still copying): {item.name}")
            continue
        if not item.videos:
            log(f"[local] skip (no video inside yet): {item.name}")
            continue

        if dry_run:
            results.append(
                {
                    "file_id": item.id,
                    "name": item.name,
                    "kind": item.kind,
                    "status": "planned",
                    "clips": len(item.videos),
                    "scans": len(item.images),
                }
            )
            attempted += 1
            continue

        log(f"[local] rendering {item.name} ({len(item.videos)} clip(s))")
        attempted += 1
        try:
            artifacts = pipeline(item) or {}
        except Exception as exc:  # one bad artwork must not stop the scan
            log(f"[local] failed: {item.name}: {exc}")
            results.append(
                {"file_id": item.id, "name": item.name, "status": "failed", "error": str(exc)}
            )
            continue

        package = drive_sync.ready_package_name(artifacts, now=now, fallback_stem=item.stem)
        try:
            published = move_artifacts(artifacts, folders, package, mover=mover)
            if not published:
                raise LocalHubError(f"the render of {item.name} produced no artifact to publish")
            log(f"[local] package -> {READY_DIRNAME}\\{package}")
            for path in published:
                log(f"[local]   {os.path.basename(path)}")
            if cleanup:
                cleanup_render_folder(artifacts, folders, log=log)
            archived = archive_item(
                item, folders, archive=archive_after_render, mover=mover, now=now
            )
            if archived:
                log(f"[local] {item.name} -> {ARCHIVE_DIRNAME}\\{os.path.basename(archived)}")
        except Exception as exc:  # the package is not settled: try again next run
            log(f"[local] publish failed: {item.name}: {exc}")
            results.append(
                {
                    "file_id": item.id,
                    "name": item.name,
                    "status": "publish_failed",
                    "error": str(exc),
                    "artifacts": artifacts,
                    "package": package,
                }
            )
            continue

        mark_processed(
            state,
            item.id,
            name=item.name,
            video_id=str(artifacts.get("video_id") or ""),
            uploaded=published,
        )
        save_state(state, state_file)
        results.append(
            {
                "file_id": item.id,
                "name": item.name,
                "kind": item.kind,
                "status": "done",
                "video_id": str(artifacts.get("video_id") or ""),
                "artifacts": artifacts,
                "package": package,
                "published": published,
            }
        )

    if not dry_run:
        save_state(state, state_file)
    return results


def watch(
    folders: LocalFolders,
    pipeline: Callable[["drive_sync.RawItem"], dict],
    interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
    max_iterations: Optional[int] = None,
    sleep: Callable[[float], None] = None,
    **kwargs,
) -> List[List[dict]]:
    """Keep scanning ``01_RAW_VIDEO`` every ``interval_seconds``.

    ``max_iterations`` is for tests and one-shot runs; the real loop stops on
    Ctrl+C. The posting gate stays with the one-shot run (the scheduled task),
    so this watcher renders whatever is waiting.
    """
    import time

    wait = sleep or time.sleep
    log = kwargs.pop("log", print)
    runs: List[List[dict]] = []
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        try:
            results = sync_once(folders, pipeline, log=log, **kwargs)
        except LocalHubError as exc:  # keep watching, just report this cycle
            log(f"[local] scan failed: {exc}")
            results = []
        runs.append(results)
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        wait(interval_seconds)
    return runs


# ---------------------------------------------------------------------------
# Serialising a scan result (for --dry-run and the log)
# ---------------------------------------------------------------------------

def format_results(results: Sequence[dict], dry_run: bool = False) -> str:
    """A short human summary of one scan."""
    if not results:
        return "Nothing was waiting in 01_RAW_VIDEO."
    lines = []
    for result in results:
        status = result.get("status")
        label = {
            "done": "rendered + published",
            "planned": "would render",
            "failed": "failed",
            "publish_failed": "rendered but not published",
        }.get(str(status), str(status))
        extra = ""
        if status == "planned":
            extra = f" ({result.get('clips', 0)} clip(s), {result.get('scans', 0)} scan(s))"
        if result.get("package") and status == "done":
            extra = f" -> {READY_DIRNAME}\\{result['package']}"
        lines.append(f"  - {result.get('name')}: {label}{extra}")
    return "\n".join(lines)


def write_scan_log(path: str, text: str, when: Optional[datetime] = None) -> str:
    """Append one line-stamped block to a log file (used by --local-once runs)."""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"===== {stamp} =====\n{text.rstrip()}\n\n")
    return path


def free_space_bytes(path: str) -> Optional[int]:
    """Free bytes on the volume holding ``path`` (None when unknown)."""
    target = path
    while target and not os.path.isdir(target):
        parent = os.path.dirname(target)
        if parent == target:
            break
        target = parent
    try:
        return int(shutil.disk_usage(target or ".").free)
    except OSError:
        return None


def state_summary(path: str) -> Dict[str, int]:
    """How many items the state file already knows, by status."""
    state = load_state(path)
    counts: Dict[str, int] = {}
    for record in (state.get("processed") or {}).values():
        status = str((record or {}).get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def dump_json(data: dict) -> str:
    """Stable JSON for a log line or a test."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
