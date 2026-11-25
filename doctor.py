"""Pre-flight checks for a Windows local run: ``python main.py --doctor``.

A local run has more ways to fail quietly than a CI run had: FFmpeg not on the
PATH, a Python without the packages, a base folder on a drive that does not
exist yet, no YouTube token, no Gemini key. Each of those produces a clear line
here instead of a traceback halfway through a render, and the exit code says
whether the PC is ready (0), merely limited (0 with warnings) or not usable (1).

Nothing in this module touches the network or creates media: it only looks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

import local_hub

STATE_OK = "ok"
STATE_WARN = "warn"
STATE_FAIL = "fail"
STATE_LABEL = {STATE_OK: "OK  ", STATE_WARN: "WARN", STATE_FAIL: "FAIL"}

# FFmpeg has to be new enough for amix=normalize=0 and the ass filter.
MIN_MAJOR_VERSION = 5


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    detail: str
    hint: str = ""

    @property
    def failed(self) -> bool:
        return self.state == STATE_FAIL


def _major_version(text: str) -> Optional[int]:
    """The integer after "version " in FFmpeg's first line."""
    for token in str(text or "").split():
        head = token.split(".")[0]
        if head.isdigit():
            return int(head)
    return None


def check_python(version: Optional[tuple] = None) -> Check:
    major, minor = version or sys.version_info[:2]
    if (major, minor) >= (3, 10):
        return Check("Python", STATE_OK, f"{major}.{minor}")
    return Check(
        "Python",
        STATE_FAIL,
        f"{major}.{minor} is too old",
        "install Python 3.10 or newer from python.org and run setup_local.bat again",
    )


def check_ffmpeg(
    which: Callable[[str], Optional[str]] = shutil.which,
    runner: Callable[..., object] = subprocess.run,
) -> Check:
    path = which("ffmpeg")
    if not path:
        return Check(
            "FFmpeg",
            STATE_FAIL,
            "ffmpeg is not on PATH",
            "install it (winget install Gyan.FFmpeg) and reopen the terminal, "
            "or put ffmpeg.exe beside run_local.bat",
        )
    try:
        result = runner(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("FFmpeg", STATE_FAIL, f"could not run {path}: {exc}", "reinstall FFmpeg")
    first_line = (getattr(result, "stdout", "") or "").splitlines()[:1]
    text = first_line[0] if first_line else ""
    major = _major_version(text.replace("ffmpeg version ", "version ", 1))
    if major is not None and major < MIN_MAJOR_VERSION:
        return Check(
            "FFmpeg",
            STATE_FAIL,
            f"{text} is older than {MIN_MAJOR_VERSION}",
            "update FFmpeg: the audio mix needs amix=normalize=0",
        )
    return Check("FFmpeg", STATE_OK, text or path)


def check_ffprobe(which: Callable[[str], Optional[str]] = shutil.which) -> Check:
    path = which("ffprobe")
    if path:
        return Check("ffprobe", STATE_OK, path)
    return Check(
        "ffprobe",
        STATE_WARN,
        "not on PATH (clip lengths fall back to OpenCV)",
        "it ships with FFmpeg; add its folder to PATH",
    )


def check_ffmpeg_filters(
    runner: Callable[..., object] = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Check:
    """The two filters and one encoder the render cannot do without.

    ``ffmpeg -h filter=<name>`` answers 0 only when that filter was compiled in,
    which is the check worth making here: the telop needs ``ass``/libass and the
    encode needs ``libx264``.
    """
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        return Check("FFmpeg filters", STATE_WARN, "skipped: no ffmpeg")
    missing = []
    for name, args in (
        ("ass", ["-hide_banner", "-h", "filter=ass"]),
        ("subtitles", ["-hide_banner", "-h", "filter=subtitles"]),
        ("libx264", ["-hide_banner", "-h", "encoder=libx264"]),
    ):
        try:
            result = runner([ffmpeg, *args], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            missing.append(name)
            continue
        if getattr(result, "returncode", 1) != 0:
            missing.append(name)
    if not missing:
        return Check("FFmpeg filters", STATE_OK, "ass, subtitles and libx264 are present")
    return Check(
        "FFmpeg filters",
        STATE_FAIL,
        f"missing: {', '.join(missing)}",
        "install a full FFmpeg build (the telop needs libass and libx264)",
    )


def check_font(project_root: str) -> Check:
    path = os.path.join(project_root, "assets", "fonts", "NotoSansJP-Bold.otf")
    if os.path.exists(path):
        return Check("Japanese font", STATE_OK, os.path.relpath(path, project_root))
    return Check(
        "Japanese font",
        STATE_WARN,
        "assets/fonts/NotoSansJP-Bold.otf is missing",
        "restore it from the repository; Japanese telop falls back to a system font",
    )


def check_gemini_key(env: Optional[Dict[str, str]] = None) -> Check:
    source = os.environ if env is None else env
    key = str(source.get("GEMINI_API_KEY", "") or "").strip()
    if key:
        return Check("Gemini API key", STATE_OK, "set (the script is written by Gemini)")
    return Check(
        "Gemini API key",
        STATE_WARN,
        "not set: the offline template script is used (title, hook, narration and "
        "the Japanese text from config.yaml and the artwork name)",
        "set GEMINI_API_KEY in .env to have Gemini write the script again",
    )


def check_client_secret(project_root: str) -> Check:
    path = os.path.join(project_root, "client_secret.json")
    if os.path.exists(path):
        return Check("OAuth client", STATE_OK, "client_secret.json")
    return Check(
        "OAuth client",
        STATE_FAIL,
        "client_secret.json is missing",
        "Google Cloud Console > APIs & Services > Credentials > create an OAuth "
        "client (Desktop app) and save the JSON as client_secret.json",
    )


def check_youtube_token(project_root: str, env: Optional[Dict[str, str]] = None) -> Check:
    source = os.environ if env is None else env
    path = os.path.join(project_root, "token.json")
    if os.path.exists(path):
        return Check(
            "YouTube token",
            STATE_OK,
            "token.json (run --auth again if the upload is refused)",
        )
    if str(source.get("YOUTUBE_TOKEN_JSON", "") or "").strip():
        return Check("YouTube token", STATE_OK, "YOUTUBE_TOKEN_JSON")
    return Check(
        "YouTube token",
        STATE_WARN,
        "not created yet: the first publishing run opens a browser to sign in",
        "run  python main.py --auth  once to sign in now",
    )


def check_base_folder(folders: "local_hub.LocalFolders") -> Check:
    parent = os.path.dirname(folders.base.rstrip("\\/")) or folders.base
    if os.path.isdir(folders.base):
        writable = os.access(folders.base, os.W_OK)
        if not writable:
            return Check(
                "Base folder",
                STATE_FAIL,
                f"{folders.base} is not writable",
                "choose another folder with --local-base or CRAYONJAPAN_HOME",
            )
        return Check("Base folder", STATE_OK, folders.base)
    if os.path.isdir(parent):
        if os.access(parent, os.W_OK):
            return Check(
                "Base folder",
                STATE_WARN,
                f"{folders.base} does not exist yet (setup_local.bat creates it)",
                "run  python main.py --local-init",
            )
        return Check(
            "Base folder",
            STATE_FAIL,
            f"{parent} is not writable, so {folders.base} cannot be created",
            "pick another folder with --local-base",
        )
    drive = os.path.splitdrive(folders.base)[0] if os.name == "nt" else ""
    hint = f"check that the drive {drive} exists" if drive else "choose another folder"
    return Check("Base folder", STATE_FAIL, f"{folders.base} cannot be created", hint)


def _size_text(value: float) -> str:
    """Bytes as GB (or TB on a very large volume), one decimal."""
    gb = value / (1024 ** 3)
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb:.1f} GB"


def check_free_space(folders: "local_hub.LocalFolders") -> Check:
    free = local_hub.free_space_bytes(folders.base)
    if free is None:
        return Check("Free disk space", STATE_WARN, "could not be read")
    if free < local_hub.RECOMMENDED_FREE_BYTES:
        return Check(
            "Free disk space",
            STATE_WARN,
            f"{_size_text(free)} free: a long recording needs about 6 GB while it renders",
            "free some space in the base folder's drive",
        )
    return Check("Free disk space", STATE_OK, f"{_size_text(free)} free")


def check_pending(folders: "local_hub.LocalFolders") -> Check:
    try:
        items = local_hub.list_local_items(folders)
    except OSError as exc:
        return Check("Waiting drawings", STATE_WARN, f"could not list 01_RAW_VIDEO: {exc}")
    if not items:
        return Check(
            "Waiting drawings",
            STATE_OK,
            "none yet: drop a GO_<name> folder into 01_RAW_VIDEO",
        )
    names = ", ".join(item.name for item in items[:3])
    more = "" if len(items) <= 3 else f" (+{len(items) - 3} more)"
    return Check("Waiting drawings", STATE_OK, f"{len(items)} waiting: {names}{more}")


def check_schedule(config: Optional[dict]) -> Check:
    import main as main_module  # local import: main imports this module

    settings = main_module.schedule_settings(config)
    if not settings["enabled"]:
        return Check("Posting rhythm", STATE_WARN, "schedule.enabled is false: every run posts")
    return Check(
        "Posting rhythm",
        STATE_OK,
        f"one Short every {settings['min_interval_hours']:g} h, "
        f"{settings['max_videos_per_run']} per run",
    )


def run_checks(
    config: Optional[dict] = None,
    folders: Optional["local_hub.LocalFolders"] = None,
    project_root: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    runner: Callable[..., object] = subprocess.run,
    version: Optional[tuple] = None,
) -> List[Check]:
    """Every check, in the order a person would want to read them."""
    root = project_root or os.path.dirname(os.path.abspath(__file__))
    layout = folders or local_hub.load_local_folders(config=config, environ=env)
    return [
        check_python(version),
        check_ffmpeg(which=which, runner=runner),
        check_ffprobe(which=which),
        check_ffmpeg_filters(runner=runner, which=which),
        check_font(root),
        check_base_folder(layout),
        check_free_space(layout),
        check_client_secret(root),
        check_youtube_token(root, env=env),
        check_gemini_key(env=env),
        check_pending(layout),
        check_schedule(config),
    ]


def format_checks(checks: Sequence[Check]) -> str:
    lines = []
    width = max((len(check.name) for check in checks), default=0)
    for check in checks:
        label = STATE_LABEL.get(check.state, check.state)
        line = f"[{label}] {check.name.ljust(width)}  {check.detail}"
        lines.append(line)
        if check.hint and check.state != STATE_OK:
            lines.append(f"         {' ' * width}  -> {check.hint}")
    return "\n".join(lines)


def exit_code(checks: Sequence[Check]) -> int:
    return 1 if any(check.failed for check in checks) else 0


def ready(checks: Sequence[Check]) -> bool:
    return exit_code(checks) == 0
