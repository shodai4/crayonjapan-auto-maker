"""Video processing for CrayonJapan making-of Shorts.

Turns a raw hand-drawing recording into a 9:16 vertical time-lapse with a short
"finish preview" hook taken from the last seconds of the drawing.

`keep_audio=True` keeps the original drawing sound (used by the ASMR style)
instead of stripping the audio track with ``-an``.

Two input shapes are supported:

* one recording (``build_vertical_timelapse``) - the original behaviour;
* a project of several recordings (``GO_`` folder in 01_RAW_VIDEO). Each clip is
  rendered straight into its own short 9:16 segment (``render_project_segment``)
  and only the tiny segments are concatenated, so an hour-long 6 GB recording
  never becomes a second multi-GB intermediate file on the runner's disk.
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

# The finished artwork is pulled from the very end of the recording, where the
# paper holds the completed drawing. It is kept uncropped (the 9:16 crop would
# cut the postcard in half) so it can be used for the shop listing as well.
DEFAULT_ARTWORK_OFFSET = 0.0
ARTWORK_JPEG_QUALITY = 92
ARTWORK_MAX_WIDTH = 1080
ARTWORK_SEEK_ATTEMPTS = 8

VIDEO_CODEC_ARGS = ["-c:v", "libx264", "-preset", "fast", "-crf", "22"]
AUDIO_CODEC_ARGS = ["-c:a", "aac", "-b:a", "128k"]

# Project rendering: every clip is normalised to the same canvas so the short
# segments can be concatenated with `-c copy`. "Fit + pad" (instead of the 9:16
# crop) never cuts a landscape clip in half and keeps both orientations valid.
PROJECT_WIDTH = 1080
PROJECT_HEIGHT = 1920
PROJECT_FPS = 30
PROJECT_AUDIO_RATE = 48000
PROJECT_AUDIO_CHANNELS = 2
SEGMENT_VIDEO_CODEC_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
]
SEGMENT_AUDIO_CODEC_ARGS = [
    "-c:a", "aac", "-b:a", "128k",
    "-ar", str(PROJECT_AUDIO_RATE), "-ac", str(PROJECT_AUDIO_CHANNELS),
]
# Used only when a clip's length is unknown (Drive normally reports
# videoMediaMetadata.durationMillis, so this is a rare fallback).
DEFAULT_CLIP_DURATION_SECONDS = 60.0
# Below this breathing room the pipeline refuses to pull another raw clip.
DISK_MARGIN_BYTES = 1500 * 1024 * 1024

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


def build_artwork_command(
    video_path: str,
    output_image_path: str,
    offset_seconds: float = DEFAULT_ARTWORK_OFFSET,
    jpeg_quality: int = ARTWORK_JPEG_QUALITY,
) -> List[str]:
    """FFmpeg command that grabs the finished drawing as a JPEG.

    ``-sseof`` counts back from the end of the file, so only the tail is
    decoded: the last frame of the recording is the completed illustration.
    """
    try:
        requested = float(offset_seconds or 0.0)
    except (TypeError, ValueError):
        requested = 0.0
    if not math.isfinite(requested) or requested < 0:
        requested = 0.0
    # FFmpeg refuses a "seek from the end" of exactly zero.
    seek = max(requested, 0.1)
    quality = min(max(int(jpeg_quality), 1), 100)
    qscale = int(max(2, min(31, round(31 - (quality / 100.0) * 29))))
    return [
        "ffmpeg", "-y",
        "-sseof", f"-{seek:.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", str(qscale),
        output_image_path,
    ]


def _shrink_frame(cv2, frame, max_width: int):
    """Keep a phone-camera sized still, but never upscale it."""
    if not max_width or max_width <= 0:
        return frame
    height, width = frame.shape[:2]
    if width <= max_width:
        return frame
    scale = max_width / float(width)
    size = (int(max_width), max(1, int(round(height * scale))))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


def _save_last_frame_with_cv2(
    video_path: str,
    output_image_path: str,
    offset_seconds: float,
    jpeg_quality: int,
    max_width: int,
) -> bool:
    """Write the tail frame with OpenCV; False means "let FFmpeg try"."""
    try:
        cv2 = _import_cv2()
    except Exception:
        return False

    cap = cv2.VideoCapture(video_path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            return False
        try:
            offset = float(offset_seconds or 0.0)
        except (TypeError, ValueError):
            offset = 0.0
        offset_frames = int(round(max(offset, 0.0) * fps))
        target = max(total_frames - 1 - offset_frames, 0)
        # The very last frame of a recording often sits past the final keyframe
        # and will not decode; step back a second at a time until one does.
        step = max(int(fps), 1)
        for attempt in range(ARTWORK_SEEK_ATTEMPTS):
            position = max(target - attempt * step, 0)
            cap.set(cv2.CAP_PROP_POS_FRAMES, position)
            ok, frame = cap.read()
            if ok and frame is not None:
                frame = _shrink_frame(cv2, frame, max_width)
                params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
                return bool(cv2.imwrite(output_image_path, frame, params))
            if position == 0:
                break
    except Exception:
        return False
    finally:
        cap.release()
    return False


def extract_final_artwork(
    video_path: str,
    output_image_path: str,
    offset_seconds: float = DEFAULT_ARTWORK_OFFSET,
    jpeg_quality: int = ARTWORK_JPEG_QUALITY,
    max_width: int = ARTWORK_MAX_WIDTH,
    runner: Runner = run_command,
) -> str:
    """Save the finished illustration of a drawing video as a JPEG.

    Used for the shop listing and the Short thumbnail, so it comes from the raw
    recording (full frame, no 9:16 crop). OpenCV is tried first because the
    keyframes already need it; FFmpeg's ``-sseof`` is the fallback when OpenCV
    cannot open or decode the file.
    """
    directory = os.path.dirname(output_image_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    saved = _save_last_frame_with_cv2(
        video_path, output_image_path,
        offset_seconds=offset_seconds,
        jpeg_quality=jpeg_quality,
        max_width=max_width,
    )
    if not saved:
        runner(build_artwork_command(video_path, output_image_path, offset_seconds, jpeg_quality))

    if not os.path.exists(output_image_path) or os.path.getsize(output_image_path) == 0:
        raise RuntimeError(f"Could not extract the finished artwork from {video_path}")
    return output_image_path


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


# ---------------------------------------------------------------------------
# Project mode: several raw clips -> one Short, rendered clip by clip
# ---------------------------------------------------------------------------

def build_fit_filter(
    width: int = PROJECT_WIDTH,
    height: int = PROJECT_HEIGHT,
    fps: int = PROJECT_FPS,
) -> str:
    """Letterbox a clip onto the shared 1080x1920 30 fps canvas (no cropping)."""
    return (
        f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease,"
        f"pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={int(fps)}"
    )


def compute_project_plan(
    durations: Sequence[float],
    target_duration: float = 38.0,
    hook_duration: float = 2.5,
    fallback_duration: float = DEFAULT_CLIP_DURATION_SECONDS,
) -> Dict[str, object]:
    """Split one Short across the clips of a project, proportionally.

    Every clip keeps the same time-lapse speed (``pts_multiplier``), so a clip
    that is twice as long contributes twice as many seconds, and the segments
    add up to the requested main duration. A duration of ``0``/``None`` (Drive
    had no metadata for that file) is replaced by the average of the known
    clips and flagged in ``estimated``.
    """
    if not durations:
        raise ValueError("a project needs at least one video clip")

    cleaned: List[Optional[float]] = []
    for value in durations:
        try:
            number = float(value)
        except (TypeError, ValueError):
            cleaned.append(None)
            continue
        cleaned.append(number if math.isfinite(number) and number > 0 else None)

    known = [value for value in cleaned if value]
    fallback = (sum(known) / len(known)) if known else float(fallback_duration)
    resolved = [value if value else fallback for value in cleaned]
    total = sum(resolved)

    plan = compute_timelapse_plan(total, target_duration=target_duration, hook_duration=hook_duration)
    last_duration = resolved[-1]
    hook_length = min(plan["hook_duration"], last_duration)
    hook_start = max(last_duration - HOOK_TAIL_SECONDS, 0.0)
    pts = plan["pts_multiplier"]

    return {
        "clip_count": len(resolved),
        "durations": resolved,
        "estimated": any(value is None for value in cleaned),
        "total_duration": total,
        # The hook comes from the tail of the last clip, at natural speed.
        "hook_start": hook_start,
        "hook_duration": hook_length,
        "main_duration": plan["main_duration"],
        "speed_factor": plan["speed_factor"],
        "pts_multiplier": pts,
        "segment_durations": [duration * pts for duration in resolved],
    }


def _segment_inputs(keep_audio: bool, has_audio: bool) -> List[str]:
    """Extra FFmpeg inputs needed so every segment carries the same streams."""
    if keep_audio and not has_audio:
        return [
            "-f", "lavfi",
            "-i", f"anullsrc=r={PROJECT_AUDIO_RATE}:cl=stereo",
        ]
    return []


def _segment_output_args(keep_audio: bool, has_audio: bool) -> List[str]:
    """Maps and codec arguments shared by the hook and the body segments."""
    if not keep_audio:
        return ["-an", *SEGMENT_VIDEO_CODEC_ARGS]
    audio_map = "0:a:0" if has_audio else "1:a:0"
    return [
        "-map", "0:v:0",
        "-map", audio_map,
        *SEGMENT_VIDEO_CODEC_ARGS,
        *SEGMENT_AUDIO_CODEC_ARGS,
        "-shortest",
    ]


def build_project_segment_command(
    input_path: str,
    output_path: str,
    pts_multiplier: float,
    keep_audio: bool = False,
    has_audio: bool = True,
    width: int = PROJECT_WIDTH,
    height: int = PROJECT_HEIGHT,
    fps: int = PROJECT_FPS,
) -> List[str]:
    """Speed one raw clip up and write its short 9:16 segment.

    ``pts_multiplier`` is the shared time scale (0.01 = 100x faster), so the
    segment is ``clip_duration * pts_multiplier`` seconds long. Audio, when kept
    (ASMR), is stretched by the same factor and padded so ``-shortest`` ends on
    the picture.
    """
    try:
        pts = float(pts_multiplier)
    except (TypeError, ValueError):
        pts = 1.0
    if not math.isfinite(pts) or pts <= 0:
        pts = 1.0

    vf = f"setpts={pts:.6f}*PTS,{build_fit_filter(width, height, fps)}"
    cmd = ["ffmpeg", "-y", "-i", input_path]
    cmd += _segment_inputs(keep_audio, has_audio)
    cmd += ["-vf", vf]
    if keep_audio:
        cmd += ["-af", f"{build_atempo_filter(1.0 / pts)},apad"]
    cmd += _segment_output_args(keep_audio, has_audio)
    cmd.append(output_path)
    return cmd


def build_project_hook_command(
    input_path: str,
    output_path: str,
    hook_start: float,
    hook_duration: float,
    keep_audio: bool = False,
    has_audio: bool = True,
    width: int = PROJECT_WIDTH,
    height: int = PROJECT_HEIGHT,
    fps: int = PROJECT_FPS,
) -> List[str]:
    """2.5 s finish preview cut from the tail of the last clip, natural speed."""
    try:
        start = max(float(hook_start), 0.0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        length = max(float(hook_duration), 0.1)
    except (TypeError, ValueError):
        length = HOOK_TAIL_SECONDS

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{length:.3f}",
        "-i", input_path,
    ]
    cmd += _segment_inputs(keep_audio, has_audio)
    cmd += ["-vf", build_fit_filter(width, height, fps)]
    if keep_audio:
        cmd += ["-af", "apad"]
    cmd += _segment_output_args(keep_audio, has_audio)
    cmd.append(output_path)
    return cmd


def build_segments_concat_list_content(paths: Sequence[str]) -> str:
    return "".join(f"file '{os.path.abspath(path)}'\n" for path in paths)


def concat_segments(
    paths: Sequence[str],
    output_path: str,
    runner: Runner = run_command,
    list_path: str = "",
) -> str:
    """Join the identically-encoded hook/body segments without re-encoding."""
    if not paths:
        raise ValueError("nothing to concatenate")
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    list_file = list_path or f"{os.path.splitext(output_path)[0]}_segments.txt"
    with open(list_file, "w", encoding="utf-8") as handle:
        handle.write(build_segments_concat_list_content(paths))
    try:
        runner(build_concat_command(list_file, output_path))
    finally:
        if os.path.exists(list_file):
            os.remove(list_file)
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"Concatenating the segments produced no video: {output_path}")
    return output_path


def ensure_disk_space(directory: str, required_bytes: int = 0, margin: int = DISK_MARGIN_BYTES) -> None:
    """Raise before a download that would fill the runner's disk."""
    import shutil

    try:
        free = shutil.disk_usage(directory or ".").free
    except OSError:
        return
    if required_bytes and free < int(required_bytes) + margin:
        raise RuntimeError(
            f"not enough free disk for {required_bytes / (1024 ** 3):.2f} GB "
            f"(+{margin / (1024 ** 3):.1f} GB margin): {free / (1024 ** 3):.2f} GB left"
        )


def prepare_scanned_artwork(
    image_path: str,
    output_path: str,
    max_width: int = ARTWORK_MAX_WIDTH,
    jpeg_quality: int = ARTWORK_JPEG_QUALITY,
) -> str:
    """Turn the scanned drawing (jpg/png, any size) into the artwork JPEG.

    The scan is never cropped: it is only shrunk to ``max_width`` so the whole
    sheet stays in the thumbnail and the shop listing. A transparent PNG is
    flattened onto white, the way the paper looks.
    """
    import numpy as np

    cv2 = _import_cv2()
    frame = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
    if frame is None:
        raise RuntimeError(f"Could not read the scanned artwork: {image_path}")
    if frame.ndim == 3 and frame.shape[2] == 4:
        alpha = frame[:, :, 3:4].astype("float32") / 255.0
        rgb = frame[:, :, :3].astype("float32")
        frame = (rgb * alpha + 255.0 * (1.0 - alpha)).astype(np.uint8)
    elif frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

    frame = _shrink_frame(cv2, frame, max_width)
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
    if not cv2.imwrite(output_path, frame, params):
        raise RuntimeError(f"Could not write the artwork JPEG: {output_path}")
    if os.path.getsize(output_path) == 0:
        raise RuntimeError(f"The artwork JPEG is empty: {output_path}")
    return output_path


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
