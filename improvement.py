"""The measure -> reflect -> retune loop for CrayonJapan Shorts (PDCA).

Every Short is recorded together with the parameters it was made with, YouTube
reports how much of it people actually watched, and the next render starts from
the parameters those numbers argue for. Gemini does the reflection - it reads
the table and writes down what to change and why - but nothing here depends on
it: with no measured Short yet, a Gemini failure or an answer outside the safe
ranges, the new defaults ship and the reason is logged.

The parameters are plain data, so they can be logged, validated and handed to
Gemini without touching the render code:

* target duration (the Short got long and people left)
* the opening (finished-artwork flash, plain finish preview, or text only)
* how loud the real drawing sound is, and how loud the opening hook is
* whether a voice is used at all, and how much of the Short it may speak
* the on-screen language (English / Japanese / both)
* the tempo of the time-lapse, and how long the finished piece is held
* the closing question that asks for the next idea

``improvement_state.json`` carries the last chosen parameters, the reflection
that produced them and a short history; ``pdca_state.py`` keeps that file and
``performance.csv`` in Drive, because the Actions workflow may not push.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from analytics_updater import read_performance_csv


STATE_PATH = "analytics/improvement_state.json"
PERFORMANCE_PATH = "analytics/performance.csv"

# The five styles the brief asks for, as data.
HOOK_STYLES = ("finish_flash_to_blank", "finish_preview", "text_only")
# All three modes still exist for an explicit choice in config.yaml; the
# reflection is barred from switching between them in decide_params below,
# so the loop cannot undo the overseas direction on its own.
LANGUAGE_MODES = ("bilingual", "ja", "en")
TTS_MODES = ("short", "full", "none")
# The body pace: "timelapse" speeds the whole recording up to fit the Short,
# "realtime_cuts" cuts motion-active moments at natural speed instead (see
# video_processor.py). The reflection may switch it, but only after one mode
# has run for MIN_PACE_RUN consecutive Shorts, so the comparison means
# anything at one video a day.
PACE_MODES = ("timelapse", "realtime_cuts")
MIN_PACE_RUN = 2

# The safe landing place: 20-30 s, the finished piece flashed at the start, the
# drawing sound under the voice, English on screen, and a closing
# question that asks for the next motif.
DEFAULT_PARAMS: Dict[str, object] = {
    "target_duration_sec": 28.0,
    "hook_style": "finish_flash_to_blank",
    "hook_duration_sec": 2.4,
    "hook_flash_seconds": 0.4,
    "hook_audio_boost_db": 6.0,
    "finish_hold_seconds": 2.5,
    "drawing_audio_volume": 0.5,
    "narration_ratio": 0.62,
    "tempo": 1.0,
    "language_mode": "en",
    "pace_mode": "timelapse",
    "tts_mode": "short",
    "cta_text": "Follow for more oil pastel art!",
    "cta_text_ja": "フォローしてね！",
    "cta_question_en": "What should I draw next?",
    "cta_question_ja": "次は何を描いてほしい？",
}

# name -> (min, max); a value outside the range is clamped, never rejected.
NUMERIC_BOUNDS: Dict[str, Tuple[float, float]] = {
    "target_duration_sec": (20.0, 32.0),
    "hook_duration_sec": (1.2, 3.0),
    "hook_flash_seconds": (0.0, 0.9),
    "hook_audio_boost_db": (0.0, 12.0),
    "finish_hold_seconds": (1.0, 5.0),
    "drawing_audio_volume": (0.0, 1.0),
    "narration_ratio": (0.3, 0.95),
    "tempo": (0.8, 2.0),
}
CHOICES: Dict[str, Sequence[str]] = {
    "hook_style": HOOK_STYLES,
    "language_mode": LANGUAGE_MODES,
    "pace_mode": PACE_MODES,
    "tts_mode": TTS_MODES,
}
TEXT_PARAMS = ("cta_text", "cta_question_en", "cta_question_ja")
TEXT_MAX_CHARS = 90

# Roughly how fast edge-tts speaks the narrative voice (words per second); the
# narration budget is worked out from the target length, not fixed in words.
WORDS_PER_SECOND = 2.7
MIN_NARRATION_WORDS = 16
MAX_NARRATION_WORDS = 120

# Never let one Gemini answer throw away the whole learning history.
HISTORY_LIMIT = 40
MODEL_NAME = "gemini-3-flash-preview"

# The render reads the run's parameters from here: the Drive pipeline keeps its
# call shape (`pipeline(source)`) while the choice of the run travels beside it.
_ACTIVE: Optional[Dict[str, object]] = None


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def _float(value: object, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _text(value: object, fallback: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned[:TEXT_MAX_CHARS] if cleaned else fallback


def validate_params(
    raw: Optional[Mapping[str, object]] = None,
    base: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """A full, safe parameter set: every missing or broken value is replaced.

    Numbers are clamped to their range (the renderer must never be handed a
    60-second target or a negative volume), choices fall back to the base value,
    and unknown keys are dropped so a chatty Gemini answer cannot inject
    anything into the render.
    """
    start = dict(DEFAULT_PARAMS)
    if base:
        start.update({k: v for k, v in base.items() if k in DEFAULT_PARAMS})
    supplied = dict(raw or {})

    params: Dict[str, object] = {}
    for name, default in start.items():
        if name in NUMERIC_BOUNDS:
            low, high = NUMERIC_BOUNDS[name]
            value = _float(supplied.get(name, default), _float(default, low))
            params[name] = round(min(max(value, low), high), 3)
        elif name in CHOICES:
            value = str(supplied.get(name, default) or "").strip()
            params[name] = value if value in CHOICES[name] else default
        else:
            params[name] = _text(supplied.get(name, default), str(default))
    return params


def config_params(config: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """The parameters a run starts from: config.yaml's improvement.defaults."""
    block = dict((config or {}).get("improvement") or {})
    defaults = dict(block.get("defaults") or {})
    # video.* stays the plain render config; it is the base when the improvement
    # block is absent, so an older config still renders what it says.
    video = dict((config or {}).get("video") or {})
    for name in ("target_duration_sec", "hook_duration_sec", "pace_mode"):
        if name not in defaults and name in video:
            defaults[name] = video[name]
    telop = dict((config or {}).get("telop") or {})
    if "cta_text" not in defaults and telop.get("cta_text"):
        defaults["cta_text"] = telop["cta_text"]
    return validate_params(defaults)


def set_active_params(params: Optional[Mapping[str, object]]) -> Dict[str, object]:
    """Pin the parameters of this run (see ``_ACTIVE``)."""
    global _ACTIVE
    _ACTIVE = validate_params(params) if params else None
    return dict(_ACTIVE or {})


def clear_active_params() -> None:
    global _ACTIVE
    _ACTIVE = None


def active_params(config: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """The run's parameters: the pinned set, else config.yaml's defaults."""
    if _ACTIVE:
        return dict(_ACTIVE)
    return config_params(config)


def improvement_enabled(config: Optional[Mapping[str, object]] = None) -> bool:
    block = dict((config or {}).get("improvement") or {})
    return bool(block.get("enabled", True))


def narration_word_budget(params: Optional[Mapping[str, object]] = None) -> Tuple[int, int]:
    """The word range Gemini should write for this Short's voiceover."""
    chosen = dict(params or DEFAULT_PARAMS)
    try:
        target = float(chosen.get("target_duration_sec", 28.0))
    except (TypeError, ValueError):
        target = 28.0
    ratio = _float(chosen.get("narration_ratio"), 0.62)
    minutes = max(target * ratio, 0.0)
    centre = int(round(minutes * WORDS_PER_SECOND))
    centre = min(max(centre, MIN_NARRATION_WORDS), MAX_NARRATION_WORDS)
    low = max(int(round(centre * 0.86)), MIN_NARRATION_WORDS)
    high = max(int(round(centre * 1.14)), low + 4)
    return low, min(high, MAX_NARRATION_WORDS)


def narrated_seconds(params: Optional[Mapping[str, object]] = None) -> float:
    """How many seconds of the Short the voice is planned to fill."""
    chosen = dict(params or DEFAULT_PARAMS)
    target = _float(chosen.get("target_duration_sec"), 28.0)
    return round(max(target * _float(chosen.get("narration_ratio"), 0.62), 0.0), 2)


def describe_params(params: Mapping[str, object]) -> str:
    """One log line: what this render will do differently."""
    return (
        f"{_float(params.get('target_duration_sec'), 28):g}s target, "
        f"hook={params.get('hook_style')} {_float(params.get('hook_duration_sec'), 2.4):g}s "
        f"(+{_float(params.get('hook_audio_boost_db'), 0):g}dB), "
        f"voice={params.get('tts_mode')} ({narration_word_budget(params)[0]}-"
        f"{narration_word_budget(params)[1]} words), "
        f"lang={params.get('language_mode')}, "
        f"pace={params.get('pace_mode')}, "
        f"drawing sound={_float(params.get('drawing_audio_volume'), 0):.2f}, "
        f"tempo x{_float(params.get('tempo'), 1):g}"
    )


# ---------------------------------------------------------------------------
# What the numbers say
# ---------------------------------------------------------------------------

def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _measured_rows(rows: Optional[List[dict]]) -> List[dict]:
    """Rows that describe a real upload (an id, and some numbers at all)."""
    if not rows:
        return []
    measured: List[dict] = []
    for row in rows:
        if not str(row.get("video_id") or "").strip():
            continue
        frame = dict(row)
        for column in ("views", "avg_view_percentage", "avg_view_duration_sec", "likes", "comments_count"):
            frame[column] = _to_float(frame.get(column), 0.0)
        if frame["views"] > 0 or frame["avg_view_percentage"] > 0:
            measured.append(frame)
    return measured


def summarise_performance(
    rows: Optional[List[dict]],
    recent: int = 8,
) -> Dict[str, object]:
    """The numbers the reflection reads, as plain JSON-friendly data."""
    rows = _measured_rows(rows)
    if not rows:
        return {"measured": 0}
    # ISO dates sort lexicographically, the same order sort_values gave.
    rows = sorted(rows, key=lambda row: str(row.get("date") or ""))
    rows = rows[-max(int(recent), 1):]

    def _row(item) -> Dict[str, object]:
        return {
            "video_id": str(item.get("video_id", "")),
            "title": str(item.get("title", ""))[:70],
            "published": str(item.get("date", "")),
            "views": int(item.get("views", 0) or 0),
            "retention_pct": round(float(item.get("avg_view_percentage", 0) or 0), 1),
            "avg_view_seconds": round(float(item.get("avg_view_duration_sec", 0) or 0), 1),
            "length_seconds": round(float(item.get("duration_sec", 0) or 0), 1),
            "likes": int(item.get("likes", 0) or 0),
            "comments": int(item.get("comments_count", 0) or 0),
            "hook_style": str(item.get("hook_type", "")),
            "language": str(item.get("language_mode", "")),
            "pace": str(item.get("pace_mode", "")),
            "tts_mode": str(item.get("tts_mode", "")),
        }

    records = [_row(item) for item in rows]
    retention = [record["retention_pct"] for record in records]
    by_hook: Dict[str, List[float]] = {}
    by_language: Dict[str, List[float]] = {}
    by_pace: Dict[str, List[float]] = {}
    for record in records:
        by_hook.setdefault(str(record["hook_style"] or "unknown"), []).append(record["retention_pct"])
        by_language.setdefault(str(record["language"] or "unknown"), []).append(record["retention_pct"])
        by_pace.setdefault(str(record["pace"] or "unknown"), []).append(record["retention_pct"])

    def _means(groups: Dict[str, List[float]]) -> Dict[str, float]:
        return {
            key: round(sum(values) / len(values), 1)
            for key, values in groups.items()
            if values
        }

    half = max(len(records) // 2, 1)
    older = retention[:-half] or retention[:1]
    newer = retention[-half:]
    best = max(records, key=lambda record: record["retention_pct"])
    worst = min(records, key=lambda record: record["retention_pct"])
    return {
        "measured": len(records),
        "avg_retention_pct": round(sum(retention) / len(retention), 1),
        "avg_view_seconds": round(
            sum(record["avg_view_seconds"] for record in records) / len(records), 1
        ),
        "avg_views": int(round(sum(record["views"] for record in records) / len(records))),
        "total_comments": int(sum(record["comments"] for record in records)),
        "trend_pct": round(sum(newer) / len(newer) - sum(older) / len(older), 1),
        "by_hook_style": _means(by_hook),
        "by_language": _means(by_language),
        "by_pace_mode": _means(by_pace),
        "best": best,
        "worst": worst,
        "recent": records,
    }


def review_prompt(
    summary: Mapping[str, object],
    params: Mapping[str, object],
    config: Optional[Mapping[str, object]] = None,
) -> str:
    """The reflection Gemini answers: what to change for the next Short, and why."""
    block = dict((config or {}).get("improvement") or {})
    audience = str(block.get("audience_note") or "").strip()
    audience_block = f"\nAudience note: {audience}\n" if audience else ""
    return f"""
You run the improvement loop of the YouTube Shorts channel CrayonJapan (oil pastel
making-of videos). Each Short is 9:16, shows a hand drawing one small everyday
motif with Sakura Cray-Pas oil pastels, and is posted at most once every 48 hours.
{audience_block}
Here is how the recent Shorts performed (retention_pct is the average percentage
of the video people watched; a Short whose viewers leave early shows a low value
and a short avg_view_seconds):

{json.dumps(summary, ensure_ascii=False, indent=2)}

The parameters the last Short was made with:

{json.dumps(params, ensure_ascii=False, indent=2)}

Decide the parameters for the NEXT Short. Rules:
- Change only what the numbers and the comments support, and say why.
- Keep it a controlled experiment: adjust one or two things, not everything.
- Shorter is not automatically better: compare retention against the length the
  viewer got through (avg_view_seconds vs length_seconds).
- A low retention with a long length means the opening or the pace is the
  problem, not only the duration.
- Reward comments and loops: a question the viewer can answer in one word works
  better than a generic "follow me".
- language_mode: "en" (English only). The channel is made for viewers overseas,
  so keep language_mode "en"; never switch it to "bilingual" or "ja".
- pace_mode: "timelapse" (the whole recording sped up) or "realtime_cuts"
  (motion-active moments at natural speed). Judge it by by_pace_mode, and hold
  one pace for at least 2 consecutive Shorts before switching: only propose a
  switch once each mode has 2 measured Shorts behind it.
- tts_mode: "short" (a few sharp lines), "full" (the voice runs throughout),
  "none" (drawing sound only).
- hook_style: "finish_flash_to_blank" (flash the finished artwork, then the
  blank page), "finish_preview" (the last seconds only), "text_only".
- Allowed ranges: target_duration_sec 20-32, hook_duration_sec 1.2-3.0,
  hook_flash_seconds 0-0.9, hook_audio_boost_db 0-12, finish_hold_seconds 1-5,
  drawing_audio_volume 0-1, narration_ratio 0.3-0.95, tempo 0.8-2.0.

Return strictly valid JSON, no markdown fences:
{{
  "diagnosis": "2-3 sentences: what the numbers say happened and why",
  "experiment": "one sentence: the single thing this next Short tests",
  "next_params": {{ "target_duration_sec": 26, "hook_style": "finish_flash_to_blank", ... }},
  "comment_reply_idea": "one optional idea for what to ask or answer in the comments"
}}
Only include the parameters you want to change in "next_params".
""".strip()


def parse_review(raw_text: str) -> Dict[str, object]:
    """Gemini's reflection as data; raises ValueError when it is not usable."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("review reply is not a JSON object")
    next_params = data.get("next_params") or {}
    if not isinstance(next_params, dict):
        raise ValueError("next_params is not an object")
    return {
        "diagnosis": str(data.get("diagnosis") or "").strip()[:600],
        "experiment": str(data.get("experiment") or "").strip()[:300],
        "comment_reply_idea": str(data.get("comment_reply_idea") or "").strip()[:300],
        "next_params": dict(next_params),
    }


def _ask_gemini(prompt: str, config: Optional[Mapping[str, object]]) -> str:
    """One text-only Gemini call (no images), on the same API version as the script."""
    import ai_writer

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    block = dict((config or {}).get("improvement") or {})
    model_name = str(block.get("review_model") or MODEL_NAME)
    client = ai_writer.create_client(api_key)
    last_error: Optional[BaseException] = None
    for name in ai_writer.model_candidates(model_name):
        try:
            response = client.models.generate_content(model=name, contents=[prompt])
        except Exception as exc:  # noqa: BLE001 - classified below
            if not ai_writer.is_retryable_model_error(exc):
                raise
            last_error = exc
            continue
        return getattr(response, "text", "") or ""
    raise RuntimeError(f"every model refused the review: {last_error}")


def decide_params(
    csv_path: str = PERFORMANCE_PATH,
    config: Optional[Mapping[str, object]] = None,
    client=None,
    log=print,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    """This render's parameters, and the reflection that chose them.

    Returns ``(params, review)``. ``review["status"]`` is ``gemini`` when the
    reflection ran, ``not-enough-data`` before any Short has numbers, or
    ``gemini-failed`` with the error - in both fallback cases the new defaults
    are used, which is exactly what the brief asks for.
    """
    base = config_params(config)
    block = dict((config or {}).get("improvement") or {})
    try:
        min_rows = max(int(block.get("min_rows", 2)), 1)
    except (TypeError, ValueError):
        min_rows = 2

    df = load_performance(csv_path)
    summary = summarise_performance(df)
    measured = int(summary.get("measured", 0) or 0)
    if measured < min_rows:
        log(
            f"[pdca] {measured} measured Short(s), fewer than {min_rows}: "
            "using the new defaults and skipping the reflection"
        )
        return base, {"status": "not-enough-data", "measured": measured}

    prompt = review_prompt(summary, base, config)
    try:
        raw = _ask_gemini(prompt, config)
        review = parse_review(raw)
    except Exception as exc:  # noqa: BLE001 - the render must go on
        log(f"[pdca] the reflection failed ({exc}); using the new defaults")
        return base, {"status": "gemini-failed", "error": str(exc), "measured": measured}

    params = validate_params(review.get("next_params"), base=base)
    # The channel is made for viewers overseas (2026-11-27): the reflection
    # may tune the render, but it must not move the language. That stays
    # whatever the config says - a human changes it in config.yaml, the loop
    # cannot switch it back (or away) on its own.
    if params.get("language_mode") != base.get("language_mode"):
        log(
            f"[pdca] keeping language_mode {base.get('language_mode')!r}: "
            "the reflection may not switch it"
        )
        params["language_mode"] = base.get("language_mode", "en")
    # The pace is a controlled experiment, held for MIN_PACE_RUN consecutive
    # Shorts before it may switch. The recent rows are the memory here - the
    # reflection's choice is not carried forward as the next run's base, so
    # the hold is counted from what was actually rendered, most recent first.
    # That covers the silent revert too: when the reflection says nothing
    # about the pace, the config default would flip it back, and the hold
    # keeps the running mode until it has had its 2 Shorts.
    wanted_pace = str(params.get("pace_mode") or "")
    recent = list(summary.get("recent") or [])
    last_pace = str((recent[-1] or {}).get("pace") or "") if recent else ""
    if wanted_pace and last_pace in PACE_MODES and wanted_pace != last_pace:
        run = 0
        for record in reversed(recent):
            if str(record.get("pace") or "") != last_pace:
                break
            run += 1
        if run < MIN_PACE_RUN:
            log(
                f"[pdca] keeping pace_mode {last_pace!r}: hold one pace for "
                f"{MIN_PACE_RUN} Shorts before switching (only {run} so far)"
            )
            params["pace_mode"] = last_pace
    review.update({"status": "gemini", "measured": measured, "summary": summary})
    log(f"[pdca] {review.get('diagnosis', '')}")
    log(f"[pdca] experiment: {review.get('experiment', '')}")
    changed = {
        key: (base.get(key), params.get(key))
        for key in params
        if params.get(key) != base.get(key)
    }
    if changed:
        log(
            "[pdca] changed "
            + ", ".join(f"{key}: {before} -> {after}" for key, (before, after) in changed.items())
        )
    else:
        log("[pdca] the reflection kept every parameter as it was")
    return params, review


def load_performance(path: str = PERFORMANCE_PATH) -> Optional[List[dict]]:
    """The performance log as rows, None when it is missing or unreadable.

    A file with only a header reads as an empty list, the same as the empty
    frame it used to be.
    """
    if not os.path.exists(path):
        return None
    try:
        return read_performance_csv(path)
    except Exception:  # noqa: BLE001 - a broken log is the same as no log
        return None


# ---------------------------------------------------------------------------
# State and history (mirrored to Drive by pdca_state.py)
# ---------------------------------------------------------------------------

def empty_state() -> Dict[str, object]:
    return {"version": 1, "params": {}, "review": {}, "history": []}


def load_state(path: str = STATE_PATH) -> Dict[str, object]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(state, dict):
        return empty_state()
    state.setdefault("version", 1)
    state.setdefault("params", {})
    state.setdefault("review", {})
    if not isinstance(state.get("history"), list):
        state["history"] = []
    return state


def save_state(state: Mapping[str, object], path: str = STATE_PATH) -> str:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def record_run(
    state: Dict[str, object],
    params: Mapping[str, object],
    review: Optional[Mapping[str, object]] = None,
    results: Optional[Sequence[Mapping[str, object]]] = None,
    at: Optional[str] = None,
) -> Dict[str, object]:
    """Remember what this run made, so the next reflection sees the history."""
    review = dict(review or {})
    videos = []
    for result in results or []:
        if str(result.get("status", "")) != "done":
            continue
        artifacts = result.get("artifacts") or {}
        videos.append(
            {
                "video_id": str(artifacts.get("video_id") or ""),
                "package": str(result.get("package") or ""),
                "name": str(result.get("name") or ""),
            }
        )
    state["params"] = dict(params)
    state["review"] = {
        "status": review.get("status", ""),
        "diagnosis": review.get("diagnosis", ""),
        "experiment": review.get("experiment", ""),
        "measured": review.get("measured", 0),
        "at": at or _now(),
    }
    history = list(state.get("history") or [])
    history.append(
        {
            "at": at or _now(),
            "params": dict(params),
            "review_status": review.get("status", ""),
            "diagnosis": review.get("diagnosis", ""),
            "experiment": review.get("experiment", ""),
            "videos": videos,
        }
    )
    state["history"] = history[-HISTORY_LIMIT:]
    return state


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "DEFAULT_PARAMS",
    "HOOK_STYLES",
    "LANGUAGE_MODES",
    "MIN_PACE_RUN",
    "PACE_MODES",
    "TTS_MODES",
    "STATE_PATH",
    "PERFORMANCE_PATH",
    "active_params",
    "clear_active_params",
    "config_params",
    "decide_params",
    "describe_params",
    "empty_state",
    "improvement_enabled",
    "load_performance",
    "load_state",
    "narrated_seconds",
    "narration_word_budget",
    "parse_review",
    "record_run",
    "review_prompt",
    "save_state",
    "set_active_params",
    "summarise_performance",
    "validate_params",
]
