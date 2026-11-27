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

Body pace (``video.pace_mode`` in config.yaml, default ``"timelapse"``):

* ``timelapse`` - the whole recording is sped up to fit the body, as above;
* ``realtime_cuts`` - motion-active moments (see ``extract_active_segments``)
  are cut at natural speed and joined to fill the body instead. One cut is
  about one narration phrase long, so the drawing moves with the voice; the
  hook, the 28 s target and the posting flow are the same either way.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Sequence

# FFmpeg's atempo filter only accepts 0.5 - 2.0 per instance, so larger speed
# factors are built as a chain of atempo filters (see build_atempo_filter).
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0
MAX_ATEMPO_FILTERS = 64

CROP_FILTER = "crop=ih*9/16:ih,scale=1080:1920,setsar=1"
HOOK_TAIL_SECONDS = 4.0
MIN_MAIN_DURATION = 10.0

# Body pace, chosen in config.yaml under ``video.pace_mode``.
PACE_MODE_TIMELAPSE = "timelapse"
PACE_MODE_REALTIME_CUTS = "realtime_cuts"
PACE_MODES = (PACE_MODE_TIMELAPSE, PACE_MODE_REALTIME_CUTS)
# A realtime cut is about one narration phrase long: long enough to see a
# stroke land, short enough that the drawing keeps moving with the voice.
REALTIME_CUT_SECONDS = 3.5
REALTIME_CUT_MIN_SECONDS = 2.0
REALTIME_CUT_MAX_SECONDS = 6.0
# Active frames closer than this belong to the same visible moment.
REALTIME_ACTIVE_GAP_SECONDS = 1.0
# The shortest tail cut that still reads as a cut rather than a glitch.
REALTIME_MIN_TAIL_SECONDS = 0.5

# The opening of the Short, chosen per run (see improvement.py).
#   finish_flash_to_blank - a flash of the finished artwork, then the last
#                           seconds at natural speed, then the blank paper at
#                           the start of the time-lapse: "this is where it ends"
#                           before "this is where it begins";
#   finish_preview        - the previous look (the tail, no flash);
#   text_only             - no footage from the tail at all: the time-lapse
#                           starts on the blank paper and only the hook text
#                           carries the promise.
HOOK_STYLE_FLASH = "finish_flash_to_blank"
HOOK_STYLE_PREVIEW = "finish_preview"
HOOK_STYLE_TEXT_ONLY = "text_only"
HOOK_STYLES = (HOOK_STYLE_FLASH, HOOK_STYLE_PREVIEW, HOOK_STYLE_TEXT_ONLY)
FLASH_SECONDS = 0.4
FLASH_FADE_SECONDS = 0.12
# The shortest piece of tail that still reads as "the finished piece" once the
# flash has taken its share of the hook.
MIN_HOOK_TAIL_SECONDS = 0.6
# The brief's hard ceiling: the promise has the first three seconds, no more.
HOOK_SECONDS_MAX = 3.0
# How much faster than the target the body may be played (the reflection loop's
# tempo parameter), kept inside what a viewer can still follow.
TEMPO_MIN = 0.6
TEMPO_MAX = 2.5

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


def normalize_pace_mode(value: object) -> str:
    """The body pace to render: ``"timelapse"`` unless asked otherwise.

    Anything missing, blank or unknown falls back to ``"timelapse"``, so an
    old config.yaml or a typo never changes what is rendered.
    """
    cleaned = str(value or "").strip().lower()
    return cleaned if cleaned in PACE_MODES else PACE_MODE_TIMELAPSE


def group_active_moments(
    active_timestamps: Sequence[float],
    gap_seconds: float = REALTIME_ACTIVE_GAP_SECONDS,
) -> List[tuple]:
    """Merge active-frame timestamps into (start, end) moments of movement.

    Frames closer than ``gap_seconds`` belong to the same moment; a lone frame
    is a moment of zero length, still usable as a cut start. Moments are in
    recording order, so the longest ones are the moments where the drawing
    visibly progressed the most.
    """
    try:
        gap = max(float(gap_seconds), 0.0)
    except (TypeError, ValueError):
        gap = REALTIME_ACTIVE_GAP_SECONDS
    stamps: List[float] = []
    for stamp in active_timestamps or []:
        try:
            number = float(stamp)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            stamps.append(number)
    stamps.sort()
    moments: List[tuple] = []
    for stamp in stamps:
        if moments and stamp - moments[-1][1] <= gap:
            moments[-1] = (moments[-1][0], stamp)
        else:
            moments.append((stamp, stamp))
    return moments


def phrase_durations_from_words(
    words: Sequence[dict],
    narration: str = "",
) -> List[float]:
    """One duration per narration phrase, from edge-tts word timings.

    The words are grouped the way the telop closes its cards: a phrase ends
    where a word ends a sentence, never on a single word, so the drawing cuts
    land on the same beats the voice does. ``narration`` (when given) puts the
    punctuation back onto the timings first, via telop's own helper.
    """
    import telop

    timed = list(words or [])
    if narration:
        timed = telop.attach_punctuation(timed, narration)
    ends_sentence = getattr(telop, "_ends_sentence", None)
    if not callable(ends_sentence):
        def ends_sentence(text: str) -> bool:
            return bool(text) and text[-1] in ".!?\u2026"

    phrases: List[float] = []
    start: Optional[float] = None
    end: Optional[float] = None
    for word in timed:
        try:
            word_start = float(word["start"])
            word_end = float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(word_start) and math.isfinite(word_end)):
            continue
        if start is None:
            start = word_start
        end = word_end
        if ends_sentence(str(word.get("text") or "")):
            phrases.append(max(end - start, 0.05))
            start = None
            end = None
    if start is not None and end is not None:
        phrases.append(max(end - start, 0.05))
    return phrases


def align_cuts_to_phrases(
    phrase_durations: Sequence[float],
    target_seconds: float,
    default_seconds: float = REALTIME_CUT_SECONDS,
) -> List[float]:
    """Cut lengths that fill ``target_seconds``, one cut per narration phrase.

    The phrases are taken in order and cycled when there are fewer phrases
    than cuts, each clamped to a length that still reads as a cut
    (``REALTIME_CUT_MIN_SECONDS`` - ``REALTIME_CUT_MAX_SECONDS``). With no
    phrases at all the cuts fall back to the default phrase-sized length.
    The last cut is trimmed so the lengths add up to ``target_seconds``.
    """
    try:
        target = max(float(target_seconds), 0.0)
    except (TypeError, ValueError):
        target = 0.0
    phrases: List[float] = []
    for value in phrase_durations or []:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            phrases.append(
                min(max(seconds, REALTIME_CUT_MIN_SECONDS), REALTIME_CUT_MAX_SECONDS)
            )
    if not phrases:
        try:
            default = float(default_seconds)
        except (TypeError, ValueError):
            default = REALTIME_CUT_SECONDS
        if not math.isfinite(default) or default <= 0:
            default = REALTIME_CUT_SECONDS
        phrases = [min(max(default, REALTIME_CUT_MIN_SECONDS), REALTIME_CUT_MAX_SECONDS)]

    lengths: List[float] = []
    total = 0.0
    index = 0
    while total < target:
        length = phrases[index % len(phrases)]
        lengths.append(length)
        total += length
        index += 1
    if lengths:
        overflow = total - target
        if overflow > 0:
            lengths[-1] -= overflow
            if lengths[-1] < REALTIME_MIN_TAIL_SECONDS and len(lengths) > 1:
                lengths[-2] += lengths[-1]
                lengths.pop()
    return lengths


def select_realtime_cuts(
    active_timestamps: Sequence[float],
    total_duration: float,
    main_duration: float,
    phrase_durations: Optional[Sequence[float]] = None,
    cut_seconds: float = REALTIME_CUT_SECONDS,
    gap_seconds: float = REALTIME_ACTIVE_GAP_SECONDS,
) -> Dict[str, object]:
    """Pick the natural-speed cuts that fill the body of a Short.

    The recording is split into one bin per cut and the longest active moment
    in each bin is taken first, so the cuts are spread across the whole
    drawing instead of bunching at the start, and longer moments (more visible
    progress) win over brief ones. A bin with no unused moment takes the
    longest moment still unused anywhere; once every moment has been used,
    the longest moments are reused with a small offset, so the body still
    fills.

    Fallback, by design the simpler safe option: when the active moments are
    too short to fill the body they are reused (``fallback == "reuse"``);
    when there is no active moment at all no cuts are returned
    (``fallback == "timelapse"``) and the caller renders the body as a
    time-lapse instead, so a quiet recording still produces a Short.
    """
    total = max(float(total_duration or 0.0), 1.0)
    main = max(float(main_duration or 0.0), 0.0)
    moments = group_active_moments(active_timestamps, gap_seconds=gap_seconds)
    active_seconds = sum(end - start for start, end in moments)
    if not moments or main <= 0:
        return {
            "pace_mode": PACE_MODE_REALTIME_CUTS,
            "cuts": [],
            "total_seconds": 0.0,
            "fallback": "timelapse" if main > 0 else "none",
            "fallback_seconds": main,
            "active_seconds": active_seconds,
        }

    lengths = align_cuts_to_phrases(
        phrase_durations if phrase_durations is not None else [],
        main,
        default_seconds=cut_seconds,
    )
    # Longest moment first when choosing, so a cut prefers visible progress.
    by_length = sorted(
        range(len(moments)),
        key=lambda i: (moments[i][1] - moments[i][0], moments[i][0]),
        reverse=True,
    )
    used: set = set()
    reuse_count: Dict[int, int] = {}
    cuts: List[Dict[str, object]] = []
    count = len(lengths)
    for index, length in enumerate(lengths):
        bin_start = index * total / count
        bin_end = (index + 1) * total / count
        pick = None
        for candidate in by_length:
            if candidate in used:
                continue
            start, end = moments[candidate]
            if start < bin_end and end >= bin_start:
                pick = candidate
                break
        if pick is None:
            for candidate in by_length:
                if candidate not in used:
                    pick = candidate
                    break
        if pick is None:
            pick = by_length[0]
        reused = pick in used
        used.add(pick)
        start, end = moments[pick]
        if reused:
            reuse_count[pick] = reuse_count.get(pick, 0) + 1
            shift = min(0.5 * reuse_count[pick], max((end - start) / 2.0, 0.0))
            start = start + shift
        start = min(max(start, 0.0), max(total - length, 0.0))
        cuts.append({
            "start": round(start, 3),
            "length": round(float(length), 3),
            "moment_start": round(moments[pick][0], 3),
            "moment_end": round(moments[pick][1], 3),
            "reused": reused,
        })

    return {
        "pace_mode": PACE_MODE_REALTIME_CUTS,
        "cuts": cuts,
        "total_seconds": round(sum(cut["length"] for cut in cuts), 3),
        "fallback": "reuse" if active_seconds < main else "none",
        "fallback_seconds": round(max(main - active_seconds, 0.0), 3),
        "active_seconds": round(active_seconds, 3),
    }


def compute_realtime_cuts_plan(
    active_timestamps: Sequence[float],
    total_duration: float,
    target_duration: float = 38.0,
    hook_duration: float = 2.5,
    phrase_durations: Optional[Sequence[float]] = None,
    cut_seconds: float = REALTIME_CUT_SECONDS,
) -> Dict[str, object]:
    """The realtime-cuts equivalent of :func:`compute_timelapse_plan`.

    The hook and the body length come from the time-lapse plan (same target,
    same hook), only the body is filled with natural-speed cuts instead of
    one sped-up pass.
    """
    plan = compute_timelapse_plan(
        total_duration, target_duration=target_duration, hook_duration=hook_duration
    )
    selection = select_realtime_cuts(
        active_timestamps,
        plan["orig_duration"],
        plan["main_duration"],
        phrase_durations=phrase_durations,
        cut_seconds=cut_seconds,
    )
    return {
        **plan,
        "pace_mode": PACE_MODE_REALTIME_CUTS,
        "cuts": selection["cuts"],
        "fallback": selection["fallback"],
        "fallback_seconds": selection["fallback_seconds"],
        "active_seconds": selection["active_seconds"],
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
    volume_db: float = 0.0,
) -> List[str]:
    """Command for the natural-speed finish preview at the start of the Short."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{hook_start:.3f}",
        "-t", f"{hook_duration:.3f}",
        "-i", input_path,
        "-vf", CROP_FILTER,
    ]
    if keep_audio:
        try:
            boost = float(volume_db)
        except (TypeError, ValueError):
            boost = 0.0
        if math.isfinite(boost) and abs(boost) > 0.01:
            cmd += ["-af", f"volume={boost:.1f}dB"]
        cmd += AUDIO_CODEC_ARGS
    else:
        cmd += ["-an"]
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
    pad_color: str = "",
) -> str:
    """Letterbox a clip onto the shared 1080x1920 30 fps canvas (no cropping).

    ``pad_color`` keeps the untouched bars looking like paper when the input is
    a scanned sheet rather than a camera frame.
    """
    color = f":color={pad_color}" if pad_color else ""
    return (
        f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease,"
        f"pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2{color},setsar=1,fps={int(fps)}"
    )


def compute_project_plan(
    durations: Sequence[float],
    target_duration: float = 38.0,
    hook_duration: float = 2.5,
    fallback_duration: float = DEFAULT_CLIP_DURATION_SECONDS,
    tempo: float = 1.0,
) -> Dict[str, object]:
    """Split one Short across the clips of a project, proportionally.

    Every clip keeps the same time-lapse speed (``pts_multiplier``), so a clip
    that is twice as long contributes twice as many seconds, and the segments
    add up to the requested main duration. A duration of ``0``/``None`` (Drive
    had no metadata for that file) is replaced by the average of the known
    clips and flagged in ``estimated``.

    ``tempo`` above 1 plays the drawing faster than the target implies, so the
    Short lands shorter than ``target_duration`` and more of it is movement: the
    reflection loop raises it when viewers leave during the body.
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
    try:
        speed_up = float(tempo)
    except (TypeError, ValueError):
        speed_up = 1.0
    if not math.isfinite(speed_up) or speed_up <= 0:
        speed_up = 1.0
    speed_up = min(max(speed_up, TEMPO_MIN), TEMPO_MAX)
    pts = plan["pts_multiplier"] / speed_up

    return {
        "clip_count": len(resolved),
        "durations": resolved,
        "estimated": any(value is None for value in cleaned),
        "total_duration": total,
        # The hook comes from the tail of the last clip, at natural speed.
        "hook_start": hook_start,
        "hook_duration": hook_length,
        "main_duration": plan["main_duration"] / speed_up,
        "speed_factor": plan["speed_factor"] * speed_up,
        "pts_multiplier": pts,
        "tempo": speed_up,
        "segment_durations": [duration * pts for duration in resolved],
    }


# ASMR (audio-keeping) segments are written as two separate streams and joined
# afterwards. Asking one FFmpeg command to speed the picture up *and* pad the
# sound (`-af "...,apad"` together with `-shortest`) left the renderer on
# `frame=0` for an hour on 2026-11-21: `apad` never ends, so `-shortest` waits
# forever and no frame is written. A picture-only pass (`-an`), a sound-only
# pass (`-vn`) and a stream-copy join finish deterministically.

def _seek_input_args(seek: Optional[float], length: Optional[float]) -> List[str]:
    """``-ss`` / ``-t`` before ``-i`` so FFmpeg seeks the input itself."""
    args: List[str] = []
    if seek is not None:
        try:
            start = max(float(seek), 0.0)
        except (TypeError, ValueError):
            start = 0.0
        args += ["-ss", f"{start:.3f}"]
    if length is not None:
        try:
            size = max(float(length), 0.1)
        except (TypeError, ValueError):
            size = HOOK_TAIL_SECONDS
        args += ["-t", f"{size:.3f}"]
    return args


def _coerce_pts(pts_multiplier: Optional[float]) -> Optional[float]:
    """A usable time scale, or None for "keep the natural speed"."""
    if pts_multiplier is None:
        return None
    try:
        pts = float(pts_multiplier)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pts) or pts <= 0:
        return None
    return pts


def build_segment_video_command(
    input_path: str,
    output_path: str,
    pts_multiplier: Optional[float] = None,
    width: int = PROJECT_WIDTH,
    height: int = PROJECT_HEIGHT,
    fps: int = PROJECT_FPS,
    seek: Optional[float] = None,
    length: Optional[float] = None,
) -> List[str]:
    """Picture only (``-an``), fitted to the 9:16 canvas.

    ``pts_multiplier`` is the shared time scale (0.01 = 100x faster); None keeps
    the natural speed, which is what the 2.5 s finish preview needs.
    """
    pts = _coerce_pts(pts_multiplier)
    fit = build_fit_filter(width, height, fps)
    vf = fit if pts is None else f"setpts={pts:.6f}*PTS,{fit}"
    cmd = ["ffmpeg", "-y", *_seek_input_args(seek, length), "-i", input_path]
    cmd += ["-vf", vf, "-an", *SEGMENT_VIDEO_CODEC_ARGS, output_path]
    return cmd


def build_segment_audio_command(
    input_path: str,
    output_path: str,
    speed_factor: float = 1.0,
    has_audio: bool = True,
    seek: Optional[float] = None,
    length: Optional[float] = None,
    silence_seconds: Optional[float] = None,
    volume_db: float = 0.0,
) -> List[str]:
    """Sound only (``-vn``), sped up with the bounded atempo chain.

    Never pads: a clip without sound gets a silent bed of a fixed length
    instead, so every joined segment carries the same streams, the concat
    demuxer accepts them, and nothing in the graph is endless.

    ``volume_db`` lifts the original drawing sound of the opening hook: the
    pastel-on-paper noise is the strongest reason to stay past the first second.
    """
    if has_audio:
        cmd = ["ffmpeg", "-y", *_seek_input_args(seek, length), "-i", input_path]
    else:
        # The sound pass reads one input only, so the bed is 0:a:0, and it needs
        # a real length: an unbounded `anullsrc` would never finish on its own.
        try:
            bed = max(float(silence_seconds or 0.0), 0.1)
        except (TypeError, ValueError):
            bed = 0.1
        cmd = ["ffmpeg", "-y", "-t", f"{bed:.3f}"]
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={PROJECT_AUDIO_RATE}:cl=stereo"]
    source_map = "0:a:0"
    try:
        speed = float(speed_factor)
    except (TypeError, ValueError):
        speed = 1.0
    filters: List[str] = []
    if math.isfinite(speed) and speed > 0 and abs(speed - 1.0) > 1e-9:
        filters.append(build_atempo_filter(speed))
    try:
        boost = float(volume_db)
    except (TypeError, ValueError):
        boost = 0.0
    if has_audio and math.isfinite(boost) and abs(boost) > 0.01:
        filters.append(f"volume={boost:.1f}dB")
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-vn", "-map", source_map, *SEGMENT_AUDIO_CODEC_ARGS, output_path]
    return cmd


def build_segment_join_command(
    video_path: str,
    audio_path: str,
    output_path: str,
) -> List[str]:
    """Copy the picture, encode the sound, and end on the shorter stream."""
    return [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        *SEGMENT_AUDIO_CODEC_ARGS,
        "-shortest",
        output_path,
    ]


def build_still_segment_command(
    image_path: str,
    output_path: str,
    seconds: float,
    width: int = PROJECT_WIDTH,
    height: int = PROJECT_HEIGHT,
    fps: int = PROJECT_FPS,
    fade: float = FLASH_FADE_SECONDS,
    pad_color: str = "white",
    with_audio: bool = True,
) -> List[str]:
    """A still held on screen as a segment of the Short (the opening flash).

    The scanned artwork is fitted and padded on white rather than cropped, so
    the flash really shows the finished postcard, edges included. The fade-in
    keeps it from appearing as a hard jump cut.
    """
    try:
        length = max(float(seconds or 0.0), 0.05)
    except (TypeError, ValueError):
        length = FLASH_SECONDS
    vf = build_fit_filter(width, height, fps, pad_color=pad_color)
    try:
        fade_seconds = min(max(float(fade or 0.0), 0.0), length / 2.0)
    except (TypeError, ValueError):
        fade_seconds = 0.0
    if fade_seconds > 0:
        vf += f",fade=t=in:st=0:d={fade_seconds:.3f}"

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", f"{length:.3f}", "-i", image_path,
    ]
    if with_audio:
        cmd += [
            "-f", "lavfi", "-t", f"{length:.3f}",
            "-i", f"anullsrc=r={PROJECT_AUDIO_RATE}:cl=stereo",
            "-vf", vf, "-map", "0:v:0", "-map", "1:a:0",
            *SEGMENT_VIDEO_CODEC_ARGS, *SEGMENT_AUDIO_CODEC_ARGS,
            "-shortest", output_path,
        ]
    else:
        cmd += ["-vf", vf, "-an", *SEGMENT_VIDEO_CODEC_ARGS, output_path]
    return cmd


def render_still_segment(
    image_path: str,
    output_path: str,
    seconds: float,
    with_audio: bool = True,
    runner: Runner = run_command,
    **options,
) -> str:
    """Render one still as a segment that concatenates with the moving ones."""
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    runner(
        build_still_segment_command(
            image_path, output_path, seconds, with_audio=with_audio, **options
        )
    )
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"Could not render the opening still: {output_path}")
    return output_path


def compute_hook_plan(
    hook_style: str = HOOK_STYLE_PREVIEW,
    hook_duration: float = 2.5,
    flash_seconds: float = FLASH_SECONDS,
) -> Dict[str, object]:
    """Split the opening into its flash and its natural-speed tail.

    ``text_only`` gives the opening away to the time-lapse itself (the hook is
    then only on-screen text), so every second it does not use goes to the body.
    The tail is never squeezed below ``MIN_HOOK_TAIL_SECONDS``: a flash with no
    footage behind it would say "the finished piece" and then jump straight to
    speeded-up work with nothing in between.
    """
    style = str(hook_style or HOOK_STYLE_PREVIEW)
    if style not in HOOK_STYLES:
        style = HOOK_STYLE_PREVIEW
    if style == HOOK_STYLE_TEXT_ONLY:
        return {"style": style, "flash": 0.0, "tail": 0.0, "total": 0.0}
    try:
        total = float(hook_duration)
    except (TypeError, ValueError):
        total = 2.5
    if not math.isfinite(total) or total <= 0:
        total = 2.5
    total = min(total, HOOK_SECONDS_MAX)

    flash = 0.0
    if style == HOOK_STYLE_FLASH and total > MIN_HOOK_TAIL_SECONDS:
        try:
            wanted = float(flash_seconds)
        except (TypeError, ValueError):
            wanted = FLASH_SECONDS
        if math.isfinite(wanted) and wanted > 0:
            flash = min(wanted, total - MIN_HOOK_TAIL_SECONDS)
    tail = max(total - flash, 0.0)
    return {
        "style": style,
        "flash": round(flash, 3),
        "tail": round(tail, 3),
        "total": round(flash + tail, 3),
    }


def _silence_length(
    duration: Optional[float],
    length: Optional[float],
    temp_video: str,
) -> Optional[float]:
    """How long the silent bed must be: the plan's segment, else the render."""
    for value in (duration, length):
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    if os.path.exists(temp_video):
        return probe_duration(temp_video)
    return None


def render_project_segment(
    input_path: str,
    output_path: str,
    pts_multiplier: Optional[float] = None,
    keep_audio: bool = False,
    has_audio: bool = True,
    runner: Runner = run_command,
    width: int = PROJECT_WIDTH,
    height: int = PROJECT_HEIGHT,
    fps: int = PROJECT_FPS,
    seek: Optional[float] = None,
    length: Optional[float] = None,
    duration: Optional[float] = None,
    volume_db: float = 0.0,
) -> str:
    """Render one short segment of a project.

    A muted render (the story/material styles) is a single pass. An ASMR render
    is three safe passes - picture, sound, then a stream-copy join - so no FFmpeg
    invocation ever puts `apad` and `-shortest` together.
    """
    if not keep_audio:
        runner(build_segment_video_command(
            input_path, output_path, pts_multiplier, width, height, fps, seek, length
        ))
        return output_path

    stem = os.path.splitext(output_path)[0]
    temp_video = f"{stem}_silent.mp4"
    temp_audio = f"{stem}_sound.m4a"
    pts = _coerce_pts(pts_multiplier)
    speed = 1.0 / pts if pts else 1.0
    try:
        runner(build_segment_video_command(
            input_path, temp_video, pts_multiplier, width, height, fps, seek, length
        ))
        runner(build_segment_audio_command(
            input_path, temp_audio,
            # A silent bed is not sped up: it only has to be long enough.
            speed_factor=speed if has_audio else 1.0,
            has_audio=has_audio,
            seek=seek,
            length=length,
            silence_seconds=_silence_length(duration, length, temp_video),
            volume_db=volume_db,
        ))
        runner(build_segment_join_command(temp_video, temp_audio, output_path))
    finally:
        for path in (temp_video, temp_audio):
            if os.path.exists(path):
                os.remove(path)
    return output_path


def _realtime_body_cuts(
    input_path: str,
    source_seconds: float,
    main_duration: float,
    active_timestamps: Optional[Sequence[float]],
    motion_threshold: float,
    phrase_durations: Optional[Sequence[float]],
) -> Dict[str, object]:
    """The realtime cuts for one body, detecting motion unless given stamps."""
    stamps = (
        active_timestamps
        if active_timestamps is not None
        else extract_active_segments(input_path, motion_threshold=motion_threshold)
    )
    return select_realtime_cuts(
        stamps, source_seconds, main_duration, phrase_durations=phrase_durations
    )


def render_realtime_cuts(
    input_path: str,
    output_path: str,
    cuts: Sequence[dict],
    keep_audio: bool = False,
    has_audio: bool = True,
    runner: Runner = run_command,
    volume_db: float = 0.0,
) -> str:
    """Render natural-speed cuts of one recording and join them into one body.

    Every cut is an ordinary project segment at natural speed
    (``pts_multiplier=None``), so it joins with the hook and finish pieces
    exactly like the time-lapse body does. The per-cut files are removed
    afterwards; only the joined body is kept.
    """
    if not cuts:
        raise ValueError("realtime cuts need at least one cut")
    stem = os.path.splitext(output_path)[0]
    cut_paths: List[str] = []
    try:
        for index, cut in enumerate(cuts):
            cut_path = f"{stem}_cut{index:02d}.mp4"
            render_project_segment(
                input_path, cut_path,
                pts_multiplier=None,
                keep_audio=keep_audio,
                has_audio=has_audio,
                runner=runner,
                seek=float(cut["start"]),
                length=float(cut["length"]),
                duration=float(cut["length"]),
                volume_db=volume_db,
            )
            cut_paths.append(cut_path)
        concat_segments(cut_paths, output_path, runner=runner)
    finally:
        for path in cut_paths:
            if os.path.exists(path):
                os.remove(path)
    return output_path


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


# ---------------------------------------------------------------------------
# Finale: hold the finished piece, then zoom slowly into the scan
# ---------------------------------------------------------------------------
# The artist asked for an ending with weight: the last strokes at a slower
# speed, then a slow push-in on the scanned artwork. Both halves are rendered
# as ordinary project segments (same codec, same 1080x1920 30 fps canvas) so the
# existing `-c copy` concat and the ASMR pass layout stay untouched.
FINISH_SLOW_SECONDS = 4.0      # how long the finish shot stays on screen
FINISH_SLOW_FACTOR = 0.75      # 0.75x: the last strokes play out unhurried
ZOOM_OUTRO_SECONDS = 4.0       # closing Ken Burns length when nothing says otherwise
ZOOM_OUTRO_MIN_SECONDS = 3.0
ZOOM_OUTRO_MAX_SECONDS = 8.0
ZOOM_OUTRO_START = 1.0
ZOOM_OUTRO_END = 1.3
# YouTube Shorts allows 60 s; a little margin keeps a re-encode from crossing it.
SHORTS_MAX_SECONDS = 58.0
# The voice may run a moment past the planned cut; the closing zoom absorbs it.
NARRATION_TAIL_SECONDS = 1.0
# The pan is rendered on a 2x canvas: zoompan steps in whole input pixels, and
# a larger source hides that stepping, so the 4 s push-in stays smooth.
ZOOM_UPSCALE = 2


def compute_finish_window(
    last_clip_seconds: Optional[float],
    hold_seconds: float = FINISH_SLOW_SECONDS,
    slow_factor: float = FINISH_SLOW_FACTOR,
) -> Dict[str, float]:
    """Source window and on-screen length of the slow finish shot.

    The tail of the last clip is replayed at ``slow_factor`` speed, so
    ``hold_seconds`` of source becomes a longer, calmer shot. A clip shorter
    than the hold is shown in full.
    """
    try:
        length = float(last_clip_seconds)
    except (TypeError, ValueError):
        length = 0.0
    if not math.isfinite(length) or length <= 0:
        length = max(float(hold_seconds), 0.5)
    try:
        factor = float(slow_factor)
    except (TypeError, ValueError):
        factor = FINISH_SLOW_FACTOR
    if not math.isfinite(factor) or factor <= 0:
        factor = FINISH_SLOW_FACTOR
    source = min(max(float(hold_seconds), 0.5), length)
    return {
        "start": round(max(length - source, 0.0), 3),
        "source_seconds": round(source, 3),
        "screen_seconds": round(source / factor, 3),
        # A pts multiplier above 1 slows the picture down.
        "pts_multiplier": round(1.0 / factor, 6),
    }


def compute_finale_total(
    target_total: float,
    narration_seconds: Optional[float] = None,
    max_total: float = SHORTS_MAX_SECONDS,
) -> float:
    """The length the finished Short aims for: the plan, or the voice if longer."""
    try:
        target = float(target_total)
    except (TypeError, ValueError):
        target = 45.0
    if not math.isfinite(target) or target <= 0:
        target = 45.0
    try:
        ceiling = max(float(max_total), 1.0)
    except (TypeError, ValueError):
        ceiling = SHORTS_MAX_SECONDS
    total = target
    if narration_seconds is not None:
        try:
            spoken = float(narration_seconds)
        except (TypeError, ValueError):
            spoken = 0.0
        if math.isfinite(spoken) and spoken > 0:
            # `-shortest` ends the mux on the picture, so the picture has to be
            # at least as long as the narration it carries.
            total = max(total, spoken + NARRATION_TAIL_SECONDS)
    return round(min(total, ceiling), 3)


def compute_zoom_outro_seconds(
    base_seconds: float,
    target_total: float,
    min_seconds: float = ZOOM_OUTRO_MIN_SECONDS,
    max_seconds: float = ZOOM_OUTRO_MAX_SECONDS,
) -> float:
    """How long the closing zoom runs so the picture ends with the voice."""
    try:
        base = float(base_seconds)
    except (TypeError, ValueError):
        base = 0.0
    if not math.isfinite(base) or base < 0:
        base = 0.0
    try:
        target = float(target_total)
    except (TypeError, ValueError):
        target = 0.0
    if not math.isfinite(target) or target <= 0:
        target = base + ZOOM_OUTRO_SECONDS
    wanted = target - base
    return round(min(max(wanted, float(min_seconds)), float(max_seconds)), 3)


def build_zoom_outro_filter(
    duration: float = ZOOM_OUTRO_SECONDS,
    width: int = PROJECT_WIDTH,
    height: int = PROJECT_HEIGHT,
    fps: int = PROJECT_FPS,
    zoom_start: float = ZOOM_OUTRO_START,
    zoom_end: float = ZOOM_OUTRO_END,
) -> str:
    """Fit the whole scan on the canvas, then push in without ever cropping it.

    The artwork is first fitted into the frame divided by ``zoom_end`` and
    padded onto the 1080x1920 canvas. At the end of the pan the visible window
    is exactly that inner box, so even a landscape postcard keeps every edge
    (a plain zoompan would cut 30% of it away).
    """
    seconds = max(float(duration or 0.0), 0.1)
    frames = max(int(round(seconds * int(fps))), 1)
    steps = max(frames - 1, 1)
    start = min(max(float(zoom_start), 1.0), 10.0)
    end = min(max(float(zoom_end), start + 0.01), 10.0)
    inner_w = max(int(int(width) / end) // 2 * 2, 2)
    inner_h = max(int(int(height) / end) // 2 * 2, 2)
    return (
        f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease,"
        f"pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2:color=white,"
        f"scale={int(width) * ZOOM_UPSCALE}:{int(height) * ZOOM_UPSCALE}:flags=lanczos,"
        f"setsar=1,"
        f"zoompan=z='{start:.4f}+{end - start:.4f}*on/{steps}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={int(width)}x{int(height)}:fps={int(fps)},"
        f"format=yuv420p"
    )


def build_zoom_outro_command(
    artwork_path: str,
    output_path: str,
    duration: float = ZOOM_OUTRO_SECONDS,
    width: int = PROJECT_WIDTH,
    height: int = PROJECT_HEIGHT,
    fps: int = PROJECT_FPS,
    zoom_start: float = ZOOM_OUTRO_START,
    zoom_end: float = ZOOM_OUTRO_END,
) -> List[str]:
    """A still scan turned into a silent 4 s push-in segment for the concat."""
    seconds = max(float(duration or 0.0), 0.1)
    frames = max(int(round(seconds * int(fps))), 1)
    return [
        "ffmpeg", "-y",
        "-i", artwork_path,
        "-f", "lavfi", "-t", f"{seconds:.3f}",
        "-i", f"anullsrc=r={PROJECT_AUDIO_RATE}:cl=stereo",
        "-filter_complex",
        f"[0:v]{build_zoom_outro_filter(seconds, width, height, fps, zoom_start, zoom_end)}[v]",
        "-map", "[v]", "-map", "1:a:0",
        "-frames:v", str(frames),
        *SEGMENT_VIDEO_CODEC_ARGS,
        *SEGMENT_AUDIO_CODEC_ARGS,
        "-shortest", output_path,
    ]


def create_zoom_outro(
    artwork_path: str,
    output_path: str,
    duration: float = ZOOM_OUTRO_SECONDS,
    runner: Runner = run_command,
    **options,
) -> str:
    """Render the closing zoom over the scanned artwork and return its path."""
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    runner(build_zoom_outro_command(artwork_path, output_path, duration=duration, **options))
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"Could not render the closing zoom: {output_path}")
    return output_path


def probe_audio_seconds(audio_path: str) -> Optional[float]:
    """Length of the narration track, or None when it cannot be read."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        value = float(result.stdout.strip())
    except Exception:
        return None
    return value if math.isfinite(value) and value > 0 else None


def assemble_finale(
    base_video: str,
    artwork_path: str,
    output_path: str,
    target_total: float,
    narration_seconds: Optional[float] = None,
    runner: Runner = run_command,
) -> Dict[str, float]:
    """Append the closing zoom to the hook/body/finish cut and report the plan.

    The zoom is sized so the picture ends just after the narration, which is
    what keeps the last spoken line from being cut by `-shortest`. When the
    scan is missing the cut is used as it is, so a render never fails here.
    """
    base = probe_duration(base_video)
    total = compute_finale_total(target_total, narration_seconds)
    outro_seconds = compute_zoom_outro_seconds(base, total)
    if narration_seconds:
        try:
            spoken = float(narration_seconds)
        except (TypeError, ValueError):
            spoken = 0.0
        # The zoom can only grow to ZOOM_OUTRO_MAX_SECONDS: past that the Shorts
        # guard wins, and `-shortest` would end the mux before the last words.
        if spoken > 0 and spoken + NARRATION_TAIL_SECONDS > base + outro_seconds + 0.05:
            print(
                f"      warning: the narration runs {spoken:.1f}s but the picture can "
                f"only reach {base + outro_seconds:.1f}s; its last words will be cut"
            )

    if not os.path.exists(artwork_path):
        print(f"      no artwork for the closing zoom ({artwork_path}); keeping the cut as is")
        shutil.copyfile(base_video, output_path)
        return {
            "base_seconds": base,
            "zoom_outro_seconds": 0.0,
            "total_seconds": base,
            "zoom_outro": 0,
        }

    outro_path = f"{os.path.splitext(output_path)[0]}_zoom.mp4"
    try:
        create_zoom_outro(artwork_path, outro_path, duration=outro_seconds, runner=runner)
        concat_segments([base_video, outro_path], output_path, runner=runner)
    finally:
        if os.path.exists(outro_path):
            os.remove(outro_path)
    return {
        "base_seconds": base,
        "zoom_outro_seconds": outro_seconds,
        "total_seconds": round(base + outro_seconds, 3),
        "zoom_outro": 1,
    }


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


def _concat_for_runner(pieces: Sequence[str], output_path: str, runner: Runner) -> None:
    """Join pieces with the concat demuxer, leaving the check to the caller.

    Unlike :func:`concat_segments` this does not require the output to exist
    afterwards (a test runner records the command only), which keeps the
    single-recording flow testable without touching FFmpeg.
    """
    list_path = f"{os.path.splitext(output_path)[0]}_list.txt"
    with open(list_path, "w", encoding="utf-8") as handle:
        handle.write(build_segments_concat_list_content(pieces))
    try:
        runner(build_concat_command(list_path, output_path))
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)


def build_vertical_timelapse(
    input_path: str,
    output_path: str,
    target_duration: float = 38.0,
    hook_duration: float = 2.5,
    keep_audio: bool = False,
    runner: Runner = run_command,
    hook_style: str = HOOK_STYLE_PREVIEW,
    flash_image: str = "",
    flash_seconds: float = FLASH_SECONDS,
    volume_db: float = 0.0,
    body_volume_db: float = 0.0,
    pace_mode: str = PACE_MODE_TIMELAPSE,
    active_timestamps: Optional[Sequence[float]] = None,
    motion_threshold: float = 1.8,
    phrase_durations: Optional[Sequence[float]] = None,
) -> str:
    """Render the 9:16 time-lapse. Original audio is kept only on request.

    `keep_audio` is honoured only when the source really has an audio track, so
    the two concat segments always carry the same streams.

    With `hook_style="finish_flash_to_blank"` and a still of the finished piece,
    the opening becomes a flash of the artwork followed by the tail of the
    drawing; those pieces are rendered on the shared segment canvas (the whole
    frame fitted, not cropped) so they join cleanly with any `-c copy` concat.

    With `pace_mode="realtime_cuts"` the body is motion-active moments cut at
    natural speed instead of one sped-up pass; the hook is unchanged. When no
    active moment is found the body falls back to the time-lapse, so a quiet
    recording still produces a Short.
    """
    hook = compute_hook_plan(hook_style, hook_duration, flash_seconds)
    if hook["style"] == HOOK_STYLE_FLASH and flash_image and os.path.exists(flash_image):
        return _build_styled_timelapse(
            input_path, output_path, hook,
            target_duration=target_duration,
            keep_audio=keep_audio,
            flash_image=flash_image,
            volume_db=volume_db,
            body_volume_db=body_volume_db,
            runner=runner,
            pace_mode=pace_mode,
            active_timestamps=active_timestamps,
            motion_threshold=motion_threshold,
            phrase_durations=phrase_durations,
        )

    plan = compute_timelapse_plan(
        probe_duration(input_path),
        target_duration=target_duration,
        hook_duration=hook["total"],
    )
    has_audio = has_audio_stream(input_path)
    keep_audio = bool(keep_audio) and has_audio

    temp_hook = f"{os.path.splitext(output_path)[0]}_hook.mp4"
    temp_main = f"{os.path.splitext(output_path)[0]}_main.mp4"
    pieces: List[str] = []
    try:
        if hook["tail"] > 0:
            runner(
                build_hook_command(
                    input_path, temp_hook,
                    plan["hook_start"], hook["tail"],
                    keep_audio=keep_audio, volume_db=volume_db,
                )
            )
            pieces.append(temp_hook)
        if normalize_pace_mode(pace_mode) == PACE_MODE_REALTIME_CUTS:
            selection = _realtime_body_cuts(
                input_path, plan["orig_duration"], plan["main_duration"],
                active_timestamps, motion_threshold, phrase_durations,
            )
            if selection["cuts"]:
                render_realtime_cuts(
                    input_path, temp_main, selection["cuts"],
                    keep_audio=keep_audio, has_audio=has_audio, runner=runner,
                    volume_db=body_volume_db,
                )
                pieces.append(temp_main)
                _concat_for_runner(pieces, output_path, runner)
                return output_path
        runner(
            build_main_command(
                input_path, temp_main,
                plan["pts_multiplier"], plan["speed_factor"], keep_audio=keep_audio,
            )
        )
        pieces.append(temp_main)
        _concat_for_runner(pieces, output_path, runner)
    finally:
        for path in pieces:
            if os.path.exists(path):
                os.remove(path)
    return output_path


def _build_styled_timelapse(
    input_path: str,
    output_path: str,
    hook: Dict[str, object],
    target_duration: float,
    keep_audio: bool,
    flash_image: str,
    volume_db: float,
    body_volume_db: float,
    runner: Runner,
    pace_mode: str = PACE_MODE_TIMELAPSE,
    active_timestamps: Optional[Sequence[float]] = None,
    motion_threshold: float = 1.8,
    phrase_durations: Optional[Sequence[float]] = None,
) -> str:
    """Flash -> tail -> speeded body, all on the segment canvas.

    With ``pace_mode="realtime_cuts"`` the body is natural-speed cuts of the
    motion-active moments instead; the flash and tail are unchanged, and a
    recording with no active moment falls back to the speeded body.
    """
    source_seconds = probe_duration(input_path)
    has_audio = has_audio_stream(input_path)
    keep_audio = bool(keep_audio) and has_audio
    plan = compute_timelapse_plan(
        source_seconds,
        target_duration=max(float(target_duration) - float(hook["total"]), MIN_MAIN_DURATION),
        hook_duration=0.0,
    )
    stem = os.path.splitext(output_path)[0]
    flash_path = f"{stem}_flash.mp4"
    hook_path = f"{stem}_hook.mp4"
    main_path = f"{stem}_main.mp4"
    pieces: List[str] = []
    try:
        render_still_segment(
            flash_image, flash_path, hook["flash"],
            with_audio=keep_audio, runner=runner,
        )
        pieces.append(flash_path)
        if hook["tail"] > 0:
            start = min(plan["hook_start"], max(source_seconds - hook["tail"], 0.0))
            render_project_segment(
                input_path, hook_path,
                keep_audio=keep_audio, has_audio=has_audio, runner=runner,
                seek=start, length=hook["tail"], duration=hook["tail"],
                volume_db=volume_db,
            )
            pieces.append(hook_path)
        if normalize_pace_mode(pace_mode) == PACE_MODE_REALTIME_CUTS:
            selection = _realtime_body_cuts(
                input_path, source_seconds, plan["main_duration"],
                active_timestamps, motion_threshold, phrase_durations,
            )
            if selection["cuts"]:
                render_realtime_cuts(
                    input_path, main_path, selection["cuts"],
                    keep_audio=keep_audio, has_audio=has_audio, runner=runner,
                    volume_db=body_volume_db,
                )
                pieces.append(main_path)
                _concat_for_runner(pieces, output_path, runner)
                return output_path
        render_project_segment(
            input_path, main_path,
            pts_multiplier=plan["pts_multiplier"],
            keep_audio=keep_audio, has_audio=has_audio, runner=runner,
            duration=plan["main_duration"],
            volume_db=body_volume_db,
        )
        pieces.append(main_path)
        _concat_for_runner(pieces, output_path, runner)
    finally:
        for path in pieces:
            if os.path.exists(path):
                os.remove(path)
    return output_path
