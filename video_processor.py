"""Video processing for CrayonJapan making-of Shorts.

Turns a raw hand-drawing recording into a 9:16 vertical time-lapse with a short
"finish preview" hook taken from the last seconds of the drawing.

`keep_audio=True` keeps the original drawing sound (used by the ASMR style)
instead of stripping the audio track with ``-an``.
"""

from __future__ import annotations

import math
import os
import subprocess
from typing import Callable, Dict, List, Sequence

# FFmpeg's atempo filter only accepts 0.5 - 2.0 per instance, so larger speed
# factors are built as a chain of atempo filters (see build_atempo_filter).
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0
MAX_ATEMPO_FILTERS = 64

CROP_FILTER = "crop=ih*9/16:ih,scale=1080:1920,setsar=1"
HOOK_TAIL_SECONDS = 4.0
MIN_MAIN_DURATION = 10.0

VIDEO_CODEC_ARGS = ["-c:v", "libx264", "-preset", "fast", "-crf", "22"]
AUDIO_CODEC_ARGS = ["-c:a", "aac", "-b:a", "128k"]

Runner = Callable[[Sequence[str]], None]


def run_command(cmd: Sequence[str]) -> None:
    """Run an FFmpeg/FFprobe command and fail loudly on error."""
    subprocess.run(list(cmd), check=True)


def _import_cv2():
    import cv2  # imported lazily so pure helpers are importable without OpenCV

    return cv2


def probe_duration(video_path: str) -> float:
    """Return the source duration in seconds (OpenCV first, ffprobe fallback)."""
    try:
        cv2 = _import_cv2()
        cap = cv2.VideoCapture(video_path)
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        finally:
            cap.release()
        if total_frames > 0 and fps > 0:
            return max(total_frames / fps, 1.0)
    except Exception:
        pass
    return _ffprobe_duration(video_path)


def _ffprobe_duration(video_path: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return max(float(result.stdout.strip()), 1.0)
    except Exception:
        return 1.0


def has_audio_stream(video_path: str) -> bool:
    """True when the file carries at least one audio stream.

    Returns False when ffprobe is unavailable or the probe fails, so callers
    degrade to a muted (but valid) render instead of failing.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index", "-of", "csv=p=0", video_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def extract_active_segments(video_path: str, motion_threshold: float = 1.8) -> List[float]:
    """Timestamps (seconds) of frames where the drawing is visibly moving."""
    cv2 = _import_cv2()
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    prev_gray = None
    active_frames: List[float] = []
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % 3 == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            if prev_gray is not None:
                diff = cv2.absdiff(prev_gray, gray)
                score = np.mean(diff)
                if score > motion_threshold:
                    active_frames.append(frame_idx / fps)
            prev_gray = gray
        frame_idx += 1

    cap.release()
    return active_frames


def extract_keyframes(video_path: str, output_dir: str, count: int = 4) -> List[str]:
    """Save up to `count` evenly spaced frames for the Gemini visual analysis."""
    cv2 = _import_cv2()
    os.makedirs(output_dir, exist_ok=True)
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        return []

    positions = [int(total_frames * r) for r in [0.15, 0.45, 0.75, 0.95][:count]]
    saved_paths: List[str] = []
    for i, pos in enumerate(positions):
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ret, frame = cap.read()
        if ret:
            path = os.path.join(output_dir, f"keyframe_{i}.jpg")
            cv2.imwrite(path, frame)
            saved_paths.append(path)
    cap.release()
    return saved_paths


def compute_timelapse_plan(
    orig_duration: float,
    target_duration: float = 38.0,
    hook_duration: float = 2.5,
) -> Dict[str, float]:
    """Decide hook window, time-lapse speed and the setpts multiplier."""
    orig = max(float(orig_duration), 1.0)
    target = max(float(target_duration), MIN_MAIN_DURATION)
    hook = min(max(float(hook_duration), 0.0), target, orig)
    main_duration = max(target - hook, MIN_MAIN_DURATION)
    speed_factor = orig / main_duration
    pts_multiplier = 1.0 / max(speed_factor, 1.0)
    hook_start = max(orig - HOOK_TAIL_SECONDS, 0.0)
    return {
        "orig_duration": orig,
        "hook_start": hook_start,
        "hook_duration": hook,
        "main_duration": main_duration,
        "speed_factor": speed_factor,
        "pts_multiplier": pts_multiplier,
        # Short recordings are never slowed down, so the audio must follow the
        # speed actually applied to the video (1 / pts_multiplier), not
        # speed_factor, or sound and picture drift apart.
        "audio_speed_factor": 1.0 / pts_multiplier if pts_multiplier > 0 else 1.0,
    }


def build_atempo_filter(speed_factor: float) -> str:
    """Return an atempo filter chain that speeds audio up by `speed_factor`.

    Every instance stays inside FFmpeg's 0.5 - 2.0 range, so a 10x time-lapse
    becomes ``atempo=2,atempo=2,atempo=2,atempo=1.25`` instead of an invalid
    ``atempo=10``.
    """
    try:
        value = float(speed_factor)
    except (TypeError, ValueError):
        raise ValueError(f"speed_factor must be a positive number, got {speed_factor!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"speed_factor must be a positive number, got {speed_factor!r}")

    factors: List[float] = []
    remaining = value
    while remaining > ATEMPO_MAX + 1e-9:
        factors.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN - 1e-9:
        factors.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN
    factors.append(min(max(remaining, ATEMPO_MIN), ATEMPO_MAX))

    if len(factors) > MAX_ATEMPO_FILTERS:
        raise ValueError(
            f"speed_factor {value} needs {len(factors)} atempo stages "
            f"(limit {MAX_ATEMPO_FILTERS}); split the source video instead"
        )
    return ",".join(f"atempo={factor:.6f}" for factor in factors)


def build_hook_command(
    input_path: str,
    output_path: str,
    hook_start: float,
    hook_duration: float,
    keep_audio: bool = False,
) -> List[str]:
    """Command for the natural-speed finish preview at the start of the Short."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{hook_start:.3f}",
        "-t", f"{hook_duration:.3f}",
        "-i", input_path,
        "-vf", CROP_FILTER,
    ]
    cmd += AUDIO_CODEC_ARGS if keep_audio else ["-an"]
    cmd += VIDEO_CODEC_ARGS
    cmd.append(output_path)
    return cmd


def build_main_command(
    input_path: str,
    output_path: str,
    pts_multiplier: float,
    speed_factor: float,
    keep_audio: bool = False,
) -> List[str]:
    """Command for the sped-up time-lapse body of the Short."""
    # The video speed really applied is 1 / pts_multiplier (short sources are
    # left at natural speed), so the audio is stretched by the same amount.
    audio_speed = 1.0 / pts_multiplier if pts_multiplier > 0 else max(float(speed_factor), 1.0)
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"setpts={pts_multiplier:.6f}*PTS,{CROP_FILTER}",
    ]
    if keep_audio:
        cmd += ["-af", build_atempo_filter(audio_speed)] + AUDIO_CODEC_ARGS
    else:
        cmd += ["-an"]
    cmd += VIDEO_CODEC_ARGS
    cmd.append(output_path)
    return cmd


def build_concat_command(list_path: str, output_path: str) -> List[str]:
    return [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path, "-c", "copy", output_path,
    ]


def build_concat_list_content(hook_path: str, main_path: str) -> str:
    """FFmpeg concat demuxer list; absolute paths keep it cwd independent."""
    return (
        f"file '{os.path.abspath(hook_path)}'\n"
        f"file '{os.path.abspath(main_path)}'\n"
    )


def build_vertical_timelapse(
    input_path: str,
    output_path: str,
    target_duration: float = 38.0,
    hook_duration: float = 2.5,
    keep_audio: bool = False,
    runner: Runner = run_command,
) -> str:
    """Render the 9:16 time-lapse. Original audio is kept only on request.

    `keep_audio` is honoured only when the source really has an audio track, so
    the two concat segments always carry the same streams.
    """
    plan = compute_timelapse_plan(
        probe_duration(input_path),
        target_duration=target_duration,
        hook_duration=hook_duration,
    )
    keep_audio = bool(keep_audio) and has_audio_stream(input_path)

    temp_hook = f"{os.path.splitext(output_path)[0]}_hook.mp4"
    temp_main = f"{os.path.splitext(output_path)[0]}_main.mp4"
    concat_list = f"{os.path.splitext(output_path)[0]}_list.txt"

    runner(
        build_hook_command(
            input_path, temp_hook,
            plan["hook_start"], plan["hook_duration"], keep_audio=keep_audio,
        )
    )
    runner(
        build_main_command(
            input_path, temp_main,
            plan["pts_multiplier"], plan["speed_factor"], keep_audio=keep_audio,
        )
    )

    with open(concat_list, "w", encoding="utf-8") as f:
        f.write(build_concat_list_content(temp_hook, temp_main))

    runner(build_concat_command(concat_list, output_path))

    for path in (temp_hook, temp_main, concat_list):
        if os.path.exists(path):
            os.remove(path)

    return output_path
