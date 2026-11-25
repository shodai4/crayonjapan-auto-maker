"""CrayonJapan Auto-Maker entry point.

Drop a raw drawing video into ./input and the pipeline renders a 9:16 English
YouTube Short, uploads it, and records it in analytics/performance.csv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Sequence

import pandas as pd
import yaml

import ai_writer
import doctor
import drive_sync
import improvement
import local_hub
import pdca_state
import telop
from ai_writer import (
    ASMR_STYLE_KEY,
    TRANSLATION_KEYS,
    choose_best_style,
    generate_making_script,
)
from analytics_updater import CSV_COLUMNS, update_performance_metrics
from metadata_writer import JST, ensure_shop_cta, write_metadata_files
from video_processor import (
    DEFAULT_ARTWORK_OFFSET,
    ZOOM_OUTRO_SECONDS,
    assemble_finale,
    build_vertical_timelapse,
    compute_finish_window,
    compute_hook_plan,
    compute_project_plan,
    concat_segments,
    ensure_disk_space,
    extract_final_artwork,
    extract_keyframes,
    has_audio_stream,
    prepare_scanned_artwork,
    probe_audio_seconds,
    probe_duration,
    render_project_segment,
    render_still_segment,
    run_command,
)
from youtube_uploader import get_latest_upload_time, upload_short

CONFIG_PATH = "config.yaml"
CSV_PATH = "analytics/performance.csv"
INPUT_DIR = "input"
# Local mirror of the Drive hub's 02_READY_TO_POST: the upload-ready package.
OUTPUT_DIR = "output"
# One render = one subfolder named ``{base_name}_{YYYYMMDD_HHMM}``, so the four
# artifacts of a Short never mix with the next one (and Drive gets the same
# folder, which is what the artist opens on their phone).
OUTPUT_STAMP_FORMAT = "%Y%m%d_%H%M"
RAW_LOCAL_EXTENSIONS = (".mp4", ".mov", ".avi")
HOOK_TYPE = "finish_preview_2.5s"
TTS_VOICE = "en-US-JennyNeural"
TTS_RATE = "+5%"
# A Japanese Short is spoken by a Japanese voice, a touch faster: the same
# message in fewer syllables, and the audience is in Japan.
TTS_VOICE_JA = "ja-JP-NanamiNeural"
TTS_RATE_JA = "+8%"
# edge-tts 7 defaults to SentenceBoundary, so the subtitle cues only arrive on
# the channel we ask for. Word timing matches the previous edge-tts 6 output
# (one cue per word); either channel is accepted so a version that ignores the
# request still fills the SRT instead of leaving it empty.
TTS_BOUNDARY = "WordBoundary"
SUBTITLE_CHUNK_TYPES = ("WordBoundary", "SentenceBoundary")
SUBTITLE_STYLE = (
    "Fontsize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "Outline=2,Alignment=2,MarginV=65"
)
# The telop document is the real on-screen design (see telop.py); the SRT is
# still written because it is the caption track and part of the upload pack.
DEFAULT_CTA_TEXT = telop.DEFAULT_CTA_TEXT


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def should_keep_audio(style_key: str) -> bool:
    """The ASMR style keeps the real pastel-on-paper sound of the drawing."""
    return style_key == ASMR_STYLE_KEY


def _escaped_filter_path(path: str) -> str:
    """A file path as an FFmpeg filter argument (Windows drive letters too)."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_subtitle_filter(srt_path: str) -> str:
    """Subtitles filter with the escaping FFmpeg expects on every platform."""
    escaped = _escaped_filter_path(srt_path)
    return f"subtitles='{escaped}':force_style='{SUBTITLE_STYLE}'"


def build_ass_filter(ass_path: str) -> str:
    """Burn the telop ASS document; its styles travel inside the file.

    ``fontsdir`` points libass at the bundled Japanese face, so kana and kanji
    render on the runner even though the workflow only installs a Latin font.
    """
    path = _escaped_filter_path(ass_path)
    fonts_dir = telop.japanese_fonts_dir()
    if not fonts_dir:
        return f"ass='{path}'"
    return f"ass=filename='{path}':fontsdir='{_escaped_filter_path(fonts_dir)}'"


def build_mux_command(
    video_path: str,
    audio_path: str,
    srt_path: str,
    final_path: str,
    mix_original_audio: bool = False,
    with_subtitles: bool = True,
    ass_path: str = "",
    original_volume: float = 0.55,
) -> List[str]:
    """Burn subtitles and lay the narration over the time-lapse.

    With `mix_original_audio` the original drawing sound (already speed-adjusted
    in the time-lapse) is mixed under the narration. Narration is padded with
    silence so ``-shortest`` ends on the video, never on a short voice track.
    Without `with_subtitles` (no cues came back from the voiceover) the picture
    is simply passed through, so a usable video is still produced.

    `ass_path` switches the burn from the plain SRT cues to the telop document
    (two-line cards, karaoke highlight, hook, title and closing CTA).
    """
    if ass_path:
        picture = f"[0:v]{build_ass_filter(ass_path)}[v]"
    elif with_subtitles:
        picture = f"[0:v]{build_subtitle_filter(srt_path)}[v]"
    else:
        picture = "[0:v]null[v]"
    try:
        level = min(max(float(original_volume), 0.0), 2.0)
    except (TypeError, ValueError):
        level = 0.55
    if mix_original_audio:
        filter_complex = (
            f"{picture};"
            f"[0:a]volume={level:.2f}[orig];"
            "[1:a]volume=1.0[narr];"
            "[narr]apad[narrpad];"
            "[orig][narrpad]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
        )
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        filter_complex = f"{picture};[1:a]apad[a]"
        maps = ["-map", "[v]", "-map", "[a]"]

    return [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        *maps,
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", final_path,
    ]


def feed_word_boundary(submaker, chunk: dict) -> None:
    """Hand one WordBoundary chunk to edge-tts 7.x (feed) or 6.x (create_sub).

    edge-tts 7.0.0 dropped ``SubMaker.create_sub`` and ``SubMaker.generate_subs``
    and the new API takes the chunk itself through ``feed()``; the requirements
    pin 7.x, but working with an older install must not crash either.
    """
    feed = getattr(submaker, "feed", None)
    if callable(feed):
        feed(chunk)
        return
    create_sub = getattr(submaker, "create_sub", None)
    if not callable(create_sub):
        raise RuntimeError(
            "edge-tts SubMaker offers neither feed() nor create_sub(); "
            "check the installed edge-tts version"
        )
    create_sub((chunk["offset"], chunk["duration"]), chunk["text"])


def render_srt(submaker) -> str:
    """The collected words as SRT text, on both edge-tts SubMaker APIs."""
    for name in ("get_srt", "generate_subs"):
        writer = getattr(submaker, name, None)
        if callable(writer):
            return writer()
    raise RuntimeError(
        "edge-tts SubMaker offers neither get_srt() nor generate_subs(); "
        "check the installed edge-tts version"
    )


async def synthesize_voice(
    text: str,
    audio_path: str,
    srt_path: str,
    voice: str = TTS_VOICE,
    rate: str = TTS_RATE,
    words_path: str = "",
) -> List[dict]:
    """English narration, word-level SRT cues and timings for the telop.

    ``words_path`` saves the boundaries as JSON; the ASS captions need the start
    and end of every spoken word, which the SRT does not carry in a form that
    survives rounding.
    """
    import edge_tts

    communicate = create_communicate(edge_tts, text, voice=voice, rate=rate)
    submaker = edge_tts.SubMaker()
    boundaries: List[dict] = []
    with open(audio_path, "wb") as file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                file.write(chunk["data"])
            elif chunk["type"] in SUBTITLE_CHUNK_TYPES:
                feed_word_boundary(submaker, chunk)
                boundaries.append({
                    "type": chunk["type"],
                    "offset": chunk.get("offset"),
                    "duration": chunk.get("duration"),
                    "text": chunk.get("text", ""),
                })
    srt_text = render_srt(submaker)
    if not srt_text.strip():
        print(
            "[voice] edge-tts returned no subtitle cues; "
            "the video will be rendered without burned-in subtitles"
        )
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt_text)

    # The boundaries carry no punctuation; the narration still has it, and the
    # caption needs it to know where a sentence (and so a card) ends.
    words = telop.attach_punctuation(
        telop.word_timings_from_boundaries(boundaries), text
    )
    if words_path:
        telop.write_word_timings(words_path, words)
    return words


def create_communicate(edge_tts, text: str, voice: str, rate: str):
    """A Communicate that reports word boundaries, on 7.x and 6.x alike."""
    try:
        return edge_tts.Communicate(
            text, voice=voice, rate=rate, boundary=TTS_BOUNDARY
        )
    except TypeError:
        # edge-tts 6 has no boundary option and always sends WordBoundary.
        return edge_tts.Communicate(text, voice=voice, rate=rate)


def subtitles_available(srt_path: str) -> bool:
    """True when there is at least one cue to burn into the picture."""
    try:
        return os.path.getsize(srt_path) > 0
    except OSError:
        return False


def _on_screen_lines(
    english: str,
    japanese: str,
    language_mode: str,
) -> tuple:
    """The primary and secondary on-screen line for a hook/title/ask.

    ``ja`` shows Japanese only, ``en`` English only, and ``bilingual`` shows the
    Japanese line large with the English one under it (the audience is mostly in
    Japan; the English stays so the overseas viewers are not shut out).
    """
    english = str(english or "").strip()
    japanese = str(japanese or "").strip()
    if language_mode == "ja":
        return (japanese or english, "")
    if language_mode == "bilingual":
        if japanese and english and japanese != english:
            return (japanese, english)
        return (japanese or english, "")
    return (english or japanese, "")


def write_telop(
    base_name: str,
    output_dir: str,
    script_data: dict,
    config: dict,
    video_seconds: Optional[float] = None,
    marked: Sequence[str] = (),
    params: Optional[dict] = None,
) -> str:
    """Lay out the ASS telop for one Short and return its path.

    Returns "" when ``telop.enabled`` is false, so the older plain-SRT look stays
    one config line away. The word timings come from the voice step; without
    them the hook, title and closing line are still burned, which is what a
    voice that answered without boundaries can offer.

    ``params`` decides the language of the on-screen text and the closing
    question; without it the original English-only look is kept.
    """
    settings = config.get("telop") or {}
    if settings.get("enabled", True) is False:
        print("      telop disabled in config.yaml; burning the plain SRT cues")
        return ""

    params = params or {}
    language_mode = str(params.get("language_mode") or "en")
    words = telop.load_word_timings(f"{output_dir}/{base_name}_words.json")
    series_label = str(settings.get("series_label") or config["brand"].get("name", ""))
    title = telop.clean_title(str(script_data.get("title", "")), series_label)
    title_en_short = telop.clean_title(str(script_data.get("title", "")), "")
    title_ja = telop.clean_title(str(script_data.get("title_ja", "")), "", max_chars=26)
    hook = telop.clean_hook(str(script_data.get("hook_text", "")))
    hook_ja = telop.clean_hook(str(script_data.get("hook_ja", "")))
    primary_hook, secondary_hook = _on_screen_lines(hook, hook_ja, language_mode)
    # The title is one line of its own: the series label prefixes the English
    # title, and a Japanese title is never prefixed (22 characters would leave
    # nothing but the label once the cut ran through it).
    if language_mode == "ja" or (language_mode == "bilingual" and title_ja):
        primary_title, secondary_title = title_ja, (
            title_en_short if language_mode == "bilingual" else ""
        )
    else:
        primary_title, secondary_title = title, ""
    cta_en = telop.clean_caption_text(
        str(params.get("cta_text") or settings.get("cta_text", DEFAULT_CTA_TEXT))
    )
    cta_ja = telop.clean_caption_text(str(params.get("cta_text_ja") or ""))
    if language_mode == "ja":
        primary_cta, secondary_cta = (cta_ja or cta_en), ""
    elif language_mode == "bilingual" and cta_ja:
        primary_cta, secondary_cta = cta_ja, cta_en
    else:
        primary_cta, secondary_cta = cta_en, ""
    ask_ja = str(params.get("cta_question_ja") or telop.DEFAULT_ASK_JA)
    ask_en = str(params.get("cta_question_en") or telop.DEFAULT_ASK_EN)
    primary_ask, secondary_ask = _on_screen_lines(ask_en, ask_ja, language_mode)
    font = str(settings.get("font") or "") or telop.detect_font()

    path = telop.write_telop_ass(
        f"{output_dir}/{base_name}.ass",
        words=words,
        hook_text=primary_hook,
        hook_text_secondary=secondary_hook,
        title=primary_title,
        title_secondary=secondary_title,
        cta_text=primary_cta,
        cta_secondary=secondary_cta,
        ask_text=primary_ask,
        ask_text_secondary=secondary_ask,
        video_seconds=video_seconds,
        font_name=font,
        marked=marked,
        karaoke=language_mode != "ja",
    )
    stats = telop.summarise(telop.plan_cards(words, marked=marked))
    print(
        f"      {int(stats['cards'])} caption card(s) in {font}, "
        f"longest line {int(stats['longest_line'])} chars, language={language_mode}"
    )
    return path


def mux_audio_and_subtitles(
    video_path: str,
    audio_path: str,
    srt_path: str,
    final_path: str,
    keep_original_audio: bool = False,
    ass_path: str = "",
    original_volume: float = 0.55,
) -> str:
    """Render the upload-ready file."""
    mix = bool(keep_original_audio) and has_audio_stream(video_path)
    with_subtitles = subtitles_available(srt_path)
    if ass_path and os.path.exists(ass_path):
        with_subtitles = True
    elif not with_subtitles:
        print("      no subtitle cues: rendering the video without burned subtitles")
    subprocess.run(
        build_mux_command(
            video_path, audio_path, srt_path, final_path,
            mix_original_audio=mix,
            with_subtitles=with_subtitles,
            ass_path=ass_path if os.path.exists(ass_path) else "",
            original_volume=original_volume,
        ),
        check=True,
    )
    return final_path


def append_performance_row(
    script_data: dict,
    style_key: str,
    video_id: str,
    csv_path: str = CSV_PATH,
    date: Optional[str] = None,
    hook_type: str = HOOK_TYPE,
    params: Optional[dict] = None,
    duration_sec: float = 0.0,
) -> pd.DataFrame:
    """Record the new Short so the next reflection knows what made it.

    Besides the identity of the upload, the row carries the parameters it was
    made with: without them the numbers cannot be attributed to a hook style, a
    language or a length, and the loop would only see noise.
    """
    params = params or {}
    row = {
        "video_id": video_id,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "title": script_data.get("title", ""),
        "style_used": style_key,
        "hook_type": str(params.get("hook_style") or hook_type),
        "views": 0,
        "avg_view_percentage": 0.0,
        "avg_view_duration_sec": 0.0,
        "likes": 0,
        "comments_count": 0,
        "language_mode": str(params.get("language_mode") or ""),
        "tts_mode": str(params.get("tts_mode") or ""),
        "duration_sec": round(float(duration_sec or 0.0), 3),
        "target_duration_sec": params.get("target_duration_sec", ""),
        "params_json": json.dumps(params, ensure_ascii=False, sort_keys=True) if params else "",
        "top_feedback": "",
        "updated_at": "",
    }
    new_row = pd.DataFrame([row])
    if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
        df = pd.read_csv(csv_path)
        df = pd.concat([df, new_row], ignore_index=True)
    else:
        df = new_row
    for column in CSV_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    df = df[CSV_COLUMNS]
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    df.to_csv(csv_path, index=False)
    return df


def get_output_dir(
    base_name: str,
    output_root: str = OUTPUT_DIR,
    now: Optional[datetime] = None,
) -> tuple[str, str]:
    """The per-render folder and its name: ``{base}_{YYYYMMDD_HHMM}``.

    Everything one drawing produces (vertical cut, voice, subtitles, final MP4,
    artwork JPEG, metadata pack) is written inside this folder instead of
    straight into ``output``, so a package is picked up in one piece and a
    second render can never overwrite the first. The name is JST, the timezone
    the artist reads the folder in, and the very same name is reused as the
    subfolder inside Drive's ``02_READY_TO_POST``.
    """
    cleaned = _clean_base_name(base_name)
    stamp = (now or datetime.now(JST)).strftime(OUTPUT_STAMP_FORMAT)
    folder = f"{cleaned}_{stamp}"
    path = os.path.join(output_root, folder)
    # Two drawings finished in the same minute still get one folder each, and
    # the name travels to Drive unchanged, so the pair always matches.
    suffix = 2
    while os.path.exists(path):
        folder = f"{cleaned}_{stamp}_{suffix}"
        path = os.path.join(output_root, folder)
        suffix += 1
    os.makedirs(path, exist_ok=True)
    return path, folder


def _clean_base_name(name: str) -> str:
    """Filename-safe output stem (``GO_20261121_sushi`` -> ``20261121_sushi``)."""
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(name or "").strip()
    ).strip("._-")
    return cleaned or "artwork"


def _clip_local_path(work_dir: str, index: int, ref: drive_sync.DriveFileRef) -> str:
    """Predictable, collision-free name for one downloaded clip/scan."""
    name = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_"
        for ch in os.path.basename(str(ref.name or ""))
    )
    return os.path.join(work_dir, f"{index:02d}_{name or 'clip.mp4'}")


def write_silent_audio(path: str, seconds: float = 0.5) -> str:
    """A short silent track, for a Short whose voice is switched off.

    The mux expects an audio input; ``apad`` then stretches this silence to the
    length of the picture, which is what leaves the drawing sound alone.
    """
    try:
        length = max(float(seconds or 0.5), 0.2)
    except (TypeError, ValueError):
        length = 0.5
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=stereo",
            "-t", f"{length:.2f}", "-c:a", "libmp3lame", "-q:a", "7", path,
        ],
        check=True,
    )
    return path


def _narrate(
    script_data: dict,
    audio_path: str,
    srt_path: str,
    words_path: str,
    params: dict,
    language_mode: str,
    tts_mode: str,
) -> tuple:
    """Voice the script in the run's language, or leave the drawing sound alone.

    Returns ``(words, marked)``: the timed words for the telop and the words the
    script emphasised. With ``tts_mode="none"`` a short silent track is written
    instead, so the mux still has an audio input to pad.
    """
    english, marked = telop.strip_emphasis_markers(str(script_data.get("narration") or ""))
    japanese, _ = telop.strip_emphasis_markers(str(script_data.get("narration_ja") or ""))
    if tts_mode == "none":
        write_silent_audio(audio_path)
        with open(srt_path, "w", encoding="utf-8") as handle:
            handle.write("")
        print("      voice off (drawing sound only); a silent track keeps the mux valid")
        return [], []

    spoken = english
    voice, rate = TTS_VOICE, TTS_RATE
    if language_mode == "ja":
        if japanese:
            spoken, voice, rate = japanese, TTS_VOICE_JA, TTS_RATE_JA
            marked = []  # the English emphasis markers do not exist in Japanese
        else:
            print("      no Japanese narration to speak; falling back to the English voice")
    words = asyncio.run(
        synthesize_voice(spoken, audio_path, srt_path, voice=voice, rate=rate, words_path=words_path)
    )
    if telop.contains_cjk(spoken):
        # Japanese boundaries arrive in sentence-sized chunks with timings that
        # do not add up (one 0.3 s span for a whole sentence), so the captions
        # are cut from the sentences themselves and spread over the measured
        # voice: predictable, one line per beat, in step with the audio.
        cues = telop.split_japanese_sentences(spoken)
        words = telop.spread_cues(cues, probe_audio_seconds(audio_path))
        if words:
            telop.write_word_timings(words_path, words)
            print(f"      {len(words)} Japanese cue(s) spread over the voice")
    return words, marked


def _script_for(
    frames: Sequence[str],
    style_key: str,
    config: dict,
    reference_scan: bool,
    params: dict,
    motif: str = "",
) -> dict:
    """The script for this Short, with the offline template as the floor.

    Gemini stopped being affordable for this channel, so a run may have no key
    or a key that answers 403 / 429 for the day. In that case the script is
    built from config.yaml and the artwork's own name (see
    ``ai_writer.fallback_script``) and the Short is still voiced, teloped,
    packed and uploaded - a missing script never costs the render or the post.
    """
    try:
        return generate_making_script(
            frames, style_key, config, reference_scan=reference_scan, params=params
        )
    except Exception as exc:  # noqa: BLE001 - the Short must still be made
        print(
            f"      no script from Gemini ({exc}); using the offline template "
            "script built from config.yaml and the artwork name"
        )
        return ai_writer.fallback_script(
            style_key, config, motif=motif, params=params, reason=str(exc)
        )


def _publish_bundle(
    base_name: str,
    raw_vertical: str,
    artwork_path: str,
    style_key: str,
    config: dict,
    output_dir: str,
    upload: bool,
    output_folder: str = "",
    reference_scan: bool = False,
    finale: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict:
    """Script, voice, subtitles, upload pack and (optionally) the upload.

    ``finale`` turns the hook/body/finish cut into the final picture: after the
    voice is measured, a slow zoom over the artwork is appended so the video
    ends with the narration instead of cutting it off.

    ``params`` is this run's parameter set (language, voice mode, closing
    question...); it defaults to the active set or config.yaml's defaults.
    """
    params = dict(params or improvement.active_params(config))
    language_mode = str(params.get("language_mode") or "en")
    tts_mode = str(params.get("tts_mode") or "short")
    drawing_volume = float(params.get("drawing_audio_volume") or 0.0)

    audio_path = f"{output_dir}/{base_name}.mp3"
    srt_path = f"{output_dir}/{base_name}.srt"
    words_path = f"{output_dir}/{base_name}_words.json"
    final_video = f"{output_dir}/{base_name}_final.mp4"
    keyframe_dir = f"{output_dir}/{base_name}_frames"

    print("[3/6] Extracting keyframes & writing the script...")
    # The finale is appended later, so the frames come from the cut that exists.
    frames_source = (finale or {}).get("base_video") or raw_vertical
    frames = extract_keyframes(frames_source, keyframe_dir)
    if reference_scan:
        # The finished scan is the strongest reference for colours and details,
        # so Gemini sees it first, next to the time-lapse keyframes.
        frames = [artwork_path] + frames
    script_data = _script_for(
        frames, style_key, config, reference_scan, params, motif=base_name
    )
    if str(script_data.get("script_source")) == ai_writer.SCRIPT_SOURCE_TEMPLATE:
        print(
            "      the upload pack uses the offline template script; "
            "set GEMINI_API_KEY in .env to have Gemini write it again"
        )
    # The shop link and the "original artwork" line are put in here, not left to
    # the model: the pack and the upload both read this same description.
    script_data["description"] = ensure_shop_cta(
        script_data.get("description"), config["brand"].get("shop_url", "")
    )

    print(f"[4/6] Voicing the script ({style_key}, lang={language_mode}, voice={tts_mode})...")
    words, marked_words = _narrate(
        script_data, audio_path, srt_path, words_path,
        params, language_mode, tts_mode,
    )

    vertical = raw_vertical
    finale_plan = None
    if finale:
        print("[4.5/6] Holding the finish slowly and zooming into the artwork...")
        finale_plan = assemble_finale(
            base_video=finale["base_video"],
            artwork_path=artwork_path,
            output_path=raw_vertical,
            target_total=float(finale["target_total"]),
            narration_seconds=probe_audio_seconds(audio_path) if tts_mode != "none" else None,
            runner=finale["runner"],
        )
        print(
            f"      cut {finale_plan['base_seconds']:.1f}s + zoom "
            f"{finale_plan['zoom_outro_seconds']:.1f}s = "
            f"{finale_plan['total_seconds']:.1f}s"
        )

    print("[5/6] Laying out the telop and mixing audio...")
    # The drawing sound is what sells the material, so it stays under the voice
    # (or alone, when the voice is off); ASMR keeps its louder bed.
    asmr = should_keep_audio(style_key)
    keep_audio = asmr or drawing_volume > 0
    original_volume = max(drawing_volume, 0.55) if asmr else drawing_volume
    video_seconds = float((finale_plan or {}).get("total_seconds") or 0.0)
    if video_seconds <= 0:
        video_seconds = probe_duration(vertical)
    telop_path = write_telop(
        base_name=base_name,
        output_dir=output_dir,
        script_data=script_data,
        config=config,
        video_seconds=video_seconds,
        marked=marked_words,
        params=params,
    )
    mux_audio_and_subtitles(
        vertical, audio_path, srt_path, final_video,
        keep_original_audio=keep_audio,
        ass_path=telop_path,
        original_volume=original_volume,
    )

    print("[6/6] Writing the metadata bundle...")
    missing_ja = [key for key in TRANSLATION_KEYS if not script_data.get(key)]
    if missing_ja:
        print(
            f"      Gemini sent no {', '.join(missing_ja)}; "
            "the pack marks that translation as missing"
        )
    if not script_data.get("overseas_caption"):
        # The block itself is always written; only its subject falls back.
        print(
            "      Gemini sent no overseas caption sentence; "
            "the TikTok / Instagram / Pinterest block uses the hook as its subject"
        )
    metadata_paths = write_metadata_files(
        script_data,
        output_dir=output_dir,
        base_name=base_name,
        artwork_path=artwork_path,
        video_path=final_video,
        style_key=style_key,
        material_name=str(config["brand"].get("material_name", "")),
        shop_url=str(config["brand"].get("shop_url", "")),
    )

    duration_sec = probe_duration(final_video)
    video_id = ""
    privacy = str(config["youtube"]["default_privacy"])
    if upload:
        print("      Uploading to the CrayonJapan YouTube channel...")
        video_id = upload_short(
            final_video,
            script_data,
            privacy_status=privacy,
            category_id=str(config["youtube"].get("category_id", "22")),
            default_language=str(config["youtube"].get("default_language", "en")),
        )
        append_performance_row(
            script_data, style_key, video_id, params=params, duration_sec=duration_sec
        )
        print(f"Completed! Video ID: {video_id} (privacy={privacy})")
    else:
        print("Completed without a YouTube upload (--no-upload); metadata is ready to paste.")

    return {
        "base_name": base_name,
        "output_dir": output_dir,
        "output_folder": output_folder,
        "style_key": style_key,
        "vertical": raw_vertical,
        "audio": audio_path,
        "srt": srt_path,
        "words": words_path,
        "telop": telop_path,
        "final_video": final_video,
        "artwork": artwork_path,
        "metadata_text": metadata_paths["text"],
        "metadata_json": metadata_paths["json"],
        "script": script_data,
        "video_id": video_id,
        "finale": finale_plan,
        "duration_sec": duration_sec,
        "params": params,
    }


def process_video_bundle(
    input_path: str,
    config: Optional[dict] = None,
    output_dir: str = OUTPUT_DIR,
    upload: bool = True,
    artwork_offset_seconds: float = DEFAULT_ARTWORK_OFFSET,
    base_name: Optional[str] = None,
) -> dict:
    """Render one raw drawing into the full upload bundle.

    Returns every path the Drive sync needs, so nothing has to be guessed from
    file names: the finished MP4, the finished-artwork JPEG and the English
    metadata (.txt + .json) all live in ``output_dir``.
    """
    config = config or load_config()
    params = improvement.active_params(config)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)

    base_name = _clean_base_name(
        base_name if base_name is not None else os.path.splitext(os.path.basename(input_path))[0]
    )
    # The whole package of this drawing goes into its own named folder.
    output_dir, output_folder = get_output_dir(base_name, output_dir)
    raw_vertical = f"{output_dir}/{base_name}_vertical.mp4"
    artwork_path = f"{output_dir}/{base_name}_artwork.jpg"

    # The style is chosen first: the ASMR style needs the original audio kept.
    style_key = choose_best_style(CSV_PATH, config)
    drawing_volume = float(params.get("drawing_audio_volume") or 0.0)
    keep_audio = should_keep_audio(style_key) or drawing_volume > 0

    print(f"[1/6] Cutting the finished artwork out of the drawing...")
    # The artwork comes first, because the opening flash is made of it.
    extract_final_artwork(
        input_path,
        artwork_path,
        offset_seconds=artwork_offset_seconds,
    )

    print(
        f"[2/6] Editing vertical timelapse: {input_path} (style={style_key}, "
        f"{improvement.describe_params(params)})"
    )
    build_vertical_timelapse(
        input_path,
        raw_vertical,
        target_duration=float(params["target_duration_sec"]),
        hook_duration=float(params["hook_duration_sec"]),
        keep_audio=keep_audio,
        hook_style=str(params["hook_style"]),
        flash_image=artwork_path,
        flash_seconds=float(params["hook_flash_seconds"]),
        volume_db=float(params["hook_audio_boost_db"]),
    )

    return dict(
        {"input": input_path},
        **_publish_bundle(
            base_name=base_name,
            raw_vertical=raw_vertical,
            artwork_path=artwork_path,
            style_key=style_key,
            config=config,
            output_dir=output_dir,
            upload=upload,
            output_folder=output_folder,
            params=params,
        ),
    )


def process_project_bundle(
    source: "drive_sync.ProjectSource",
    config: Optional[dict] = None,
    output_dir: str = OUTPUT_DIR,
    upload: bool = True,
    artwork_offset_seconds: float = DEFAULT_ARTWORK_OFFSET,
    keep_local_raw: bool = False,
    runner=run_command,
) -> dict:
    """Render a ``GO_`` project folder: several clips + an optional scan.

    Disk-safe by design: each clip is downloaded, immediately rendered into its
    own short 1080x1920 segment, and deleted again, so a 3-6 GB hour-long
    recording never leaves a second multi-GB intermediate on the runner.
    """
    config = config or load_config()
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)

    base_name = _clean_base_name(source.stem)
    # Same package folder as the single-video flow: one drawing, one folder.
    output_dir, output_folder = get_output_dir(base_name, output_dir)
    work_dir = source.work_dir or os.path.join(INPUT_DIR, base_name)
    os.makedirs(work_dir, exist_ok=True)

    raw_vertical = f"{output_dir}/{base_name}_vertical.mp4"
    artwork_path = f"{output_dir}/{base_name}_artwork.jpg"
    vertical_stem = os.path.splitext(raw_vertical)[0]

    clips = list(source.videos)
    style_key = choose_best_style(CSV_PATH, config)
    params = improvement.active_params(config)
    drawing_volume = float(params.get("drawing_audio_volume") or 0.0)
    keep_audio = should_keep_audio(style_key) or drawing_volume > 0
    target_total = float(params["target_duration_sec"])
    # The opening: a flash of the finished piece, then the tail at natural
    # speed, or (text_only) no opening footage at all.
    hook = compute_hook_plan(
        str(params["hook_style"]),
        float(params["hook_duration_sec"]),
        float(params["hook_flash_seconds"]),
    )
    # The cut leaves room for the finale: the finish shot replayed slowly, then a
    # closing zoom over the artwork. Both are only appended after the voice is
    # measured, so their length can follow the narration.
    finish_window = compute_finish_window(
        clips[-1].duration_seconds if clips else None,
        hold_seconds=float(params["finish_hold_seconds"]),
    )
    timelapse_target = target_total - finish_window["screen_seconds"] - ZOOM_OUTRO_SECONDS
    plan = compute_project_plan(
        [clip.duration_seconds for clip in clips],
        target_duration=timelapse_target,
        hook_duration=float(hook["total"]),
        tempo=float(params["tempo"]),
    )

    print(
        f"[1/6] Editing project {source.name}: {len(clips)} clip(s), "
        f"{plan['total_duration']:.0f}s -> {plan['main_duration']:.0f}s "
        f"+ {hook['flash']:.1f}s flash + {hook['tail']:.1f}s hook "
        f"+ {finish_window['screen_seconds']:.1f}s finish + zoom "
        f"(style={style_key}; {improvement.describe_params(params)})"
    )

    # The scan comes first (it is small): if it is there the artwork is settled
    # before an hour of rendering, and the clips no longer need to be kept for
    # their last frame.
    reference_scan = False
    if source.images:
        scan = source.images[0]
        local_scan = _clip_local_path(work_dir, len(clips), scan)
        ensure_disk_space(work_dir, scan.size)
        print(f"      preparing the scanned artwork: {scan.name}")
        source.fetch(scan.id, local_scan)
        try:
            prepare_scanned_artwork(local_scan, artwork_path)
            reference_scan = True
        except Exception as exc:  # a broken scan must not lose the render
            print(f"      scan unusable ({exc}); falling back to the last frame")
        finally:
            if not keep_local_raw and os.path.exists(local_scan):
                os.remove(local_scan)

    hook_paths: List[str] = []
    body_paths: List[str] = []
    finish_paths: List[str] = []
    for index, clip in enumerate(clips):
        local = _clip_local_path(work_dir, index, clip)
        ensure_disk_space(work_dir, clip.size)
        print(f"      downloading clip {index + 1}/{len(clips)}: {clip.name}")
        source.fetch(clip.id, local)
        try:
            audio = has_audio_stream(local)
            is_last = index == len(clips) - 1
            if is_last and not reference_scan:
                # The opening flash is made of the finished piece, so the
                # artwork has to exist before the first segment is rendered.
                print("      no scan in the folder: taking the artwork from the last clip...")
                extract_final_artwork(
                    local, artwork_path, offset_seconds=artwork_offset_seconds
                )
            if is_last:
                # Hook start comes from the real tail of the last clip, so a
                # slightly different metadata duration cannot overrun it.
                duration = probe_duration(local)
                if hook["flash"] > 0 and os.path.exists(artwork_path):
                    flash_path = f"{vertical_stem}_flash.mp4"
                    render_still_segment(
                        artwork_path, flash_path, hook["flash"],
                        with_audio=keep_audio, runner=runner,
                    )
                    hook_paths.append(flash_path)
                if hook["tail"] > 0:
                    hook_length = min(float(hook["tail"]), max(duration, 0.1))
                    hook_start = min(plan["hook_start"], max(duration - hook_length, 0.0))
                    hook_path = f"{vertical_stem}_hook.mp4"
                    render_project_segment(
                        local, hook_path,
                        keep_audio=keep_audio,
                        has_audio=audio,
                        runner=runner,
                        seek=max(hook_start, 0.0),
                        length=hook_length,
                        duration=hook_length,
                        volume_db=float(params["hook_audio_boost_db"]),
                    )
                    hook_paths.append(hook_path)

                # Finale, part one: the same last moment, played slowly, so the
                # finished piece is actually looked at before the zoom begins.
                finish = compute_finish_window(
                    duration, hold_seconds=float(params["finish_hold_seconds"])
                )
                finish_path = f"{vertical_stem}_finish.mp4"
                render_project_segment(
                    local, finish_path,
                    pts_multiplier=finish["pts_multiplier"],
                    keep_audio=keep_audio,
                    has_audio=audio,
                    runner=runner,
                    seek=finish["start"],
                    length=finish["source_seconds"],
                    duration=finish["screen_seconds"],
                )
                finish_paths.append(finish_path)

            segment_path = f"{vertical_stem}_seg{index:02d}.mp4"
            render_project_segment(
                local, segment_path,
                pts_multiplier=plan["pts_multiplier"],
                keep_audio=keep_audio,
                has_audio=audio,
                runner=runner,
                # The planned length bounds a silent bed for a clip with no sound.
                duration=plan["segment_durations"][index],
            )
            body_paths.append(segment_path)
        finally:
            if not keep_local_raw and os.path.exists(local):
                os.remove(local)  # the original still lives in Drive until archived

    print("[2/6] Concatenating the rendered segments (hook + clips + finish)...")
    base_video = f"{vertical_stem}_base.mp4"
    try:
        concat_segments(hook_paths + body_paths + finish_paths, base_video, runner=runner)
    finally:
        for path in hook_paths + body_paths + finish_paths:
            if os.path.exists(path):
                os.remove(path)

    bundle = _publish_bundle(
        base_name=base_name,
        raw_vertical=raw_vertical,
        artwork_path=artwork_path,
        style_key=style_key,
        config=config,
        output_dir=output_dir,
        upload=upload,
        output_folder=output_folder,
        reference_scan=reference_scan,
        finale={"base_video": base_video, "target_total": target_total, "runner": runner},
        params=params,
    )
    if not keep_local_raw and os.path.exists(base_video):
        os.remove(base_video)  # only the final, zoom-finished Short is kept
    if not keep_local_raw:
        shutil.rmtree(work_dir, ignore_errors=True)
    return dict(
        {
            "input": "",
            "project": source.name,
            "clip_count": len(clips),
            "artwork_from_scan": reference_scan,
            "plan": plan,
        },
        **bundle,
    )


def process_new_video(input_path: str, config: Optional[dict] = None) -> str:
    """Run the full pipeline for one raw video and return the YouTube video id."""
    bundle = process_video_bundle(input_path, config)
    return bundle["video_id"]


DEFAULT_MIN_INTERVAL_HOURS = 48.0
DEFAULT_MAX_VIDEOS_PER_RUN = 1
# ``latest=None`` means "the channel has no upload yet"; only this sentinel
# means "ask the channel".
_UNKNOWN = object()


def schedule_settings(config: Optional[dict]) -> dict:
    """The posting-rhythm block of config.yaml, with safe defaults.

    ``enabled`` defaults to false so a config without the block keeps the old
    "render everything waiting" behaviour; the shipped config turns it on.
    """
    raw = (config or {}).get("schedule") or {}
    try:
        min_hours = float(raw.get("min_interval_hours", DEFAULT_MIN_INTERVAL_HOURS))
    except (TypeError, ValueError):
        min_hours = DEFAULT_MIN_INTERVAL_HOURS
    try:
        per_run = int(raw.get("max_videos_per_run", DEFAULT_MAX_VIDEOS_PER_RUN))
    except (TypeError, ValueError):
        per_run = DEFAULT_MAX_VIDEOS_PER_RUN
    return {
        "enabled": bool(raw.get("enabled", False)),
        "min_interval_hours": max(min_hours, 0.0),
        "max_videos_per_run": max(per_run, 1),
    }


def _as_utc(moment: datetime) -> datetime:
    """A naive timestamp is read as UTC; an aware one is converted to it."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def post_interval_state(
    config: Optional[dict],
    now: Optional[datetime] = None,
    latest=_UNKNOWN,
    loader=None,
    log=print,
) -> dict:
    """May this run make and publish a Short, judged by the channel itself?

    The newest video on the YouTube channel is the one source of truth for
    "when did we last post" (``02_READY_TO_POST`` can hold a package from an
    earlier test run and a Drive file time is only an upload). Returns
    ``{allowed, reason, hours_elapsed, hours_left, settings}``. When the channel
    cannot be read the run is held: posting blind could break the interval the
    artist asked for, while a held run only waits for the next trigger.
    """
    settings = schedule_settings(config)
    state = {
        "allowed": True,
        "reason": "schedule disabled",
        "hours_elapsed": None,
        "hours_left": None,
        "settings": settings,
    }
    if not settings["enabled"]:
        return state

    read = loader or get_latest_upload_time
    try:
        newest = read() if latest is _UNKNOWN else latest
    except Exception as exc:  # auth error, quota, network, unreadable timestamp
        log(
            f"[schedule] the newest upload on the channel could not be read ({exc}); "
            "holding this run so nothing is posted without knowing the interval"
        )
        return {**state, "allowed": False, "reason": "unknown-latest-upload"}

    if newest is None:
        log("[schedule] the channel has no upload yet: the first Short may go out now")
        return {**state, "reason": "no-upload-yet"}

    reference = _as_utc(now or datetime.now(timezone.utc))
    elapsed = (reference - _as_utc(newest)).total_seconds() / 3600.0
    left = settings["min_interval_hours"] - elapsed
    if left > 0:
        log(
            f"[schedule] the newest upload is {elapsed:.1f}h old; the interval is "
            f"{settings['min_interval_hours']:g}h, so {left:.1f}h to go. Nothing is "
            "rendered or posted this run (the GO_ folders stay in 01_RAW_VIDEO)."
        )
        return {
            **state,
            "allowed": False,
            "reason": "too-soon",
            "hours_elapsed": elapsed,
            "hours_left": left,
        }

    log(
        f"[schedule] the newest upload is {elapsed:.1f}h old: the "
        f"{settings['min_interval_hours']:g}h interval has passed, so one Short "
        "goes out this run"
    )
    return {**state, "reason": "interval-passed", "hours_elapsed": elapsed}


def _collect_settings(config: Optional[dict]) -> dict:
    """How stale a row has to be before it is worth asking YouTube again."""
    block = dict(((config or {}).get("improvement") or {}).get("collect") or {})
    def _number(key, default, cast):
        try:
            return cast(block.get(key, default))
        except (TypeError, ValueError):
            return default
    return {
        "min_age_hours": max(_number("min_age_hours", 24, float), 0.0),
        "max_rows": max(_number("max_rows", 8, int), 1),
        "refresh_hours": max(_number("refresh_hours", 6.0, float), 0.0),
    }


def learning_prepare(
    config: dict,
    service=None,
    folders: Optional["drive_sync.DriveFolders"] = None,
    csv_path: str = CSV_PATH,
    log=print,
) -> tuple:
    """Restore the history, collect the numbers, decide this run's parameters.

    The order is the PDCA cycle the brief asks for and it matters: the CSV has to
    come back from Drive before the numbers are collected, and the numbers have
    to be collected before the reflection reads them. Every step degrades to the
    safe defaults instead of stopping the render.

    Without ``service``/``folders`` (the local PC mode) there is no Drive to
    restore from: the history in ``analytics/`` on this PC is the only copy and
    is already local, so the restore step is simply skipped.
    """
    if not improvement.improvement_enabled(config):
        log("[pdca] improvement disabled in config.yaml; using the config defaults")
        return improvement.config_params(config), {"status": "disabled"}

    if service is None or folders is None:
        log("[pdca] local mode: the learning history stays in analytics/ on this PC")
    else:
        try:
            pdca_state.restore_all(service, folders, csv_path=csv_path, log=log)
        except Exception as exc:  # noqa: BLE001 - a render must still happen
            log(f"[pdca] could not restore the history from Drive: {exc}")

    settings = _collect_settings(config)
    try:
        update_performance_metrics(
            csv_path,
            min_age_hours=settings["min_age_hours"],
            max_rows=settings["max_rows"],
            refresh_hours=settings["refresh_hours"],
        )
    except Exception as exc:  # noqa: BLE001 - YouTube being unavailable is not fatal
        log(f"[pdca] could not collect the numbers this run: {exc}")

    try:
        params, review = improvement.decide_params(csv_path, config, log=log)
    except Exception as exc:  # noqa: BLE001 - the render is the point of the run
        log(f"[pdca] the reflection could not run ({exc}); using the config defaults")
        params = improvement.config_params(config)
        review = {"status": "gemini-failed", "error": str(exc)}
    improvement.set_active_params(params)
    log(f"[pdca] this run: {improvement.describe_params(params)}")
    return params, review


def learning_finish(
    results: Sequence[dict],
    service=None,
    folders: Optional["drive_sync.DriveFolders"] = None,
    params: Optional[dict] = None,
    review: Optional[dict] = None,
    csv_path: str = CSV_PATH,
    log=print,
) -> None:
    """Record what this run made and put the history back in Drive."""
    params = dict(params or improvement.active_params())
    try:
        state = improvement.load_state()
        improvement.record_run(state, params, review, results)
        improvement.save_state(state)
    except Exception as exc:  # noqa: BLE001 - the Shorts are already out
        log(f"[pdca] could not record this run locally: {exc}")
        return
    done = [result for result in results if str(result.get("status")) == "done"]
    if not done:
        log("[pdca] nothing was published this run; the history was left as it was")
        return
    if service is None or folders is None:
        log("[pdca] local mode: the history was saved in analytics/ on this PC")
        return
    try:
        pdca_state.backup_all(service, folders, csv_path=csv_path, log=log)
    except Exception as exc:  # noqa: BLE001 - the Shorts are already out
        log(f"[pdca] could not save the history to Drive: {exc}")


def run_drive_mode(args, config: dict) -> int:
    """Phase 2: Google Drive folder in, upload-ready package out."""
    schedule = schedule_settings(config)
    ignore_schedule = getattr(args, "ignore_schedule", False) is True
    # A run that does not upload (`--no-upload`) posts nothing, so the interval
    # has nothing to protect: it would only stop the artist's package from being
    # made and checked on the phone.
    publishes = not bool(getattr(args, "no_upload", False))
    gated = args.drive_once and schedule["enabled"] and not ignore_schedule
    max_items: Optional[int] = None

    if gated and publishes:
        # The gate runs before Drive is even touched: a run that may not post
        # has nothing to render, so it must not download, cut or upload either.
        gate = post_interval_state(config, log=print)
        if not gate["allowed"]:
            if gate["reason"] == "unknown-latest-upload":
                return 1
            return 0
        max_items = schedule["max_videos_per_run"]
    elif gated and not publishes:
        print(
            "[schedule] --no-upload: nothing is posted, so the posting interval is "
            "not checked and every waiting item is rendered into 02_READY_TO_POST"
        )
    elif args.drive_once and schedule["enabled"] and ignore_schedule:
        print("[schedule] --ignore-schedule: the posting interval is not checked this run")
    if args.drive_watch and schedule["enabled"] and not ignore_schedule:
        print(
            "[schedule] the interval gate belongs to --drive-once (the CI path); "
            "this local watcher renders whatever is waiting"
        )

    try:
        folders = drive_sync.load_drive_folders()
    except drive_sync.MissingFolderIdError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    service = drive_sync.build_drive_service()
    ignore_pdca = bool(getattr(args, "ignore_pdca", False))
    params: Optional[dict] = None
    review: Optional[dict] = None
    if publishes and not ignore_pdca:
        params, review = learning_prepare(config, service, folders)
    elif publishes:
        print("[pdca] --ignore-pdca: rendering with the config defaults, no collection")
    else:
        improvement.clear_active_params()

    def pipeline(source: "drive_sync.ProjectSource") -> dict:
        if source.is_project:
            return process_project_bundle(
                source,
                config,
                upload=not args.no_upload,
                artwork_offset_seconds=args.artwork_offset,
            )
        if not source.videos:
            raise RuntimeError(f"{source.name} has no video to render")
        # Loose file in 01_RAW_VIDEO: the original single-video flow.
        clip = source.videos[0]
        os.makedirs(source.work_dir, exist_ok=True)
        local = _clip_local_path(source.work_dir, 0, clip)
        ensure_disk_space(source.work_dir, clip.size)
        print(f"      downloading {clip.name}")
        source.fetch(clip.id, local)
        try:
            return dict(
                {"file": clip.name},
                **process_video_bundle(
                    local,
                    config,
                    upload=not args.no_upload,
                    artwork_offset_seconds=args.artwork_offset,
                    base_name=source.stem,
                ),
            )
        finally:
            if os.path.exists(local):
                os.remove(local)

    sync_kwargs = {
        "download_dir": INPUT_DIR,
        "state_path": args.drive_state,
        "min_age_seconds": args.drive_min_age,
        "go_min_age_seconds": args.drive_go_min_age,
        "max_items": max_items,
    }

    if args.drive_once:
        results: List[dict] = []
        try:
            results = drive_sync.sync_once(service, folders, pipeline, **sync_kwargs)
        finally:
            if publishes and params is not None:
                learning_finish(results, service, folders, params, review)
            improvement.clear_active_params()
        done = [r for r in results if r["status"] == "done"]
        print(f"[drive] {len(done)} item(s) processed, {len(results) - len(done)} failed/skipped.")
        return 0 if len(done) == len(results) else 1

    interval = args.drive_interval or float(
        os.environ.get("DRIVE_WATCH_INTERVAL_SECONDS", drive_sync.DEFAULT_INTERVAL_SECONDS)
    )
    print(
        f"Watching Drive {drive_sync.RAW_FOLDER_ENV} every {interval:g}s "
        f"(Ctrl+C to stop)..."
    )
    try:
        drive_sync.watch(
            service, folders, pipeline,
            interval_seconds=interval,
            **sync_kwargs,
        )
    except KeyboardInterrupt:
        print("\nStopped watching Drive.")
    return 0


def local_settings(config: Optional[dict]) -> dict:
    """The ``local`` block of config.yaml, with safe defaults.

    Only numbers and switches live here; the three folder names are fixed by the
    hub, because they are the artist's habit and the README's promise.
    """
    block = dict(((config or {}).get("local") or {}))

    def _number(key, default, cast):
        try:
            return cast(block.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "base_dir": str(block.get("base_dir") or "").strip(),
        "min_age_seconds": max(_number("min_age_seconds", local_hub.DEFAULT_MIN_AGE_SECONDS, int), 0),
        "go_min_age_seconds": max(
            _number("go_min_age_seconds", local_hub.DEFAULT_GO_FOLDER_MIN_AGE_SECONDS, int), 0
        ),
        "scan_interval_seconds": max(
            _number("scan_interval_seconds", local_hub.DEFAULT_SCAN_INTERVAL_SECONDS, float), 1.0
        ),
        "archive_after_render": bool(block.get("archive_after_render", True)),
    }


def local_folders_for(args, config: dict) -> "local_hub.LocalFolders":
    """The base folder for this run: ``--local-base``, the env, config, default."""
    return local_hub.load_local_folders(
        base_dir=getattr(args, "local_base", "") or None, config=config
    )


def run_local_init(args, config: dict) -> int:
    """Create the folder layout (``--local-init``), then show it."""
    folders = local_folders_for(args, config)
    local_hub.ensure_local_layout(folders)
    print(f"Local mode is ready in: {folders.base}")
    print(local_hub.describe_layout(folders))
    print(
        f"\nDrop one artwork into {local_hub.RAW_DIRNAME} as a folder named "
        "GO_<name> (several clips + an optional scan.jpg), then run run_local.bat."
    )
    return 0


def run_doctor(args, config: dict) -> int:
    """``--doctor``: is this PC able to render and upload at all?"""
    folders = local_folders_for(args, config)
    checks = doctor.run_checks(config=config, folders=folders)
    print(f"CrayonJapan local setup check - base folder: {folders.base}")
    print(doctor.format_checks(checks))
    code = doctor.exit_code(checks)
    if code == 0:
        print("\nReady: a publishing run needs nothing else on this PC.")
    else:
        print("\nNot ready yet: fix the FAIL lines above, then run --doctor again.")
    return code


def run_auth(config: dict) -> int:
    """``--auth``: walk the first browser sign-in and keep ``token.json``.

    The render needs three YouTube scopes (upload, read, analytics). This is the
    one interactive step of a local setup and it is deliberately its own
    command: a scheduled run can then start unattended with the saved token.
    """
    import youtube_uploader

    print("Opening the browser for the YouTube sign-in of the CrayonJapan channel...")
    print("Choose the Google account that owns the channel and allow upload, read and analytics.")
    try:
        credentials = youtube_uploader.load_credentials(force_authorize=True)
    except FileNotFoundError as exc:
        print(f"\n{exc}", file=sys.stderr)
        print(
            "Create a Desktop OAuth client in Google Cloud Console (YouTube Data API v3 "
            "and YouTube Analytics API enabled) and save it as client_secret.json.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - the browser flow can fail in many ways
        print(f"\nThe sign-in did not finish: {exc}", file=sys.stderr)
        return 1

    try:
        service = youtube_uploader.get_youtube_service(credentials)
        channel = service.channels().list(part="snippet", mine=True).execute()
        items = (channel or {}).get("items") or []
        title = str(((items[0] if items else {}).get("snippet") or {}).get("title") or "")
    except Exception as exc:  # noqa: BLE001 - a saved token is still useful
        print(f"Signed in, but the channel could not be read: {exc}")
        print("The token was saved as token.json; --doctor shows the rest.")
        return 0

    print(f"Signed in as: {title or '(no channel name returned)'}")
    print("token.json saved. An unattended run can now upload without a browser.")
    return 0


def run_local_mode(args, config: dict) -> int:
    """Windows local mode: folder in, Short out, no Google Drive anywhere.

    The three Drive folders of the CI path become three folders on this PC, and
    the posting rhythm is the one ``schedule`` already describes: a publishing
    run first reads the newest upload on the channel, renders at most one
    waiting artwork and posts it. Nothing about the render changes - the same
    project plan, the same telop, the same pack.
    """
    settings = local_settings(config)
    folders = local_folders_for(args, config)
    dry_run = bool(getattr(args, "dry_run", False))
    publishes = not bool(getattr(args, "no_upload", False))
    ignore_schedule = bool(getattr(args, "ignore_schedule", False))
    schedule = schedule_settings(config)
    local_once = bool(getattr(args, "local_once", False))

    print(f"[local] base folder: {folders.base}")
    if dry_run:
        print("[local] --dry-run: nothing is rendered, uploaded or moved")
        for path in local_hub.missing_folders(folders):
            print(f"[local] folder not created yet: {path}")

    gated = local_once and schedule["enabled"] and not ignore_schedule and publishes
    max_items: Optional[int] = None
    if gated and not dry_run:
        gate = post_interval_state(config, log=print)
        if not gate["allowed"]:
            # Nothing may be posted, so nothing is rendered either: a run inside
            # the interval must not read, cut or upload a drawing.
            return 1 if gate["reason"] == "unknown-latest-upload" else 0
        max_items = schedule["max_videos_per_run"]
    elif gated and dry_run:
        max_items = schedule["max_videos_per_run"]
    elif local_once and schedule["enabled"] and ignore_schedule:
        print("[schedule] --ignore-schedule: the posting interval is not checked this run")
    elif local_once and schedule["enabled"] and not publishes:
        print(
            "[schedule] --no-upload: nothing is posted, so the posting interval is "
            f"not checked and every waiting item is rendered into {local_hub.READY_DIRNAME}"
        )
    elif getattr(args, "local_watch", False) and schedule["enabled"] and not ignore_schedule:
        print(
            "[schedule] the interval gate belongs to the scheduled run (--local-once); "
            "this watcher renders whatever is waiting"
        )

    if dry_run:
        items = local_hub.list_local_items(folders)
        if not items:
            print(f"[dry-run] nothing is waiting in {local_hub.RAW_DIRNAME}")
        else:
            print(f"[dry-run] {len(items)} item(s) waiting in {local_hub.RAW_DIRNAME}:")
            for item in items:  # every item, not just the head of the queue
                kind = "project" if item.is_project else "single clip"
                scan = f", {len(item.images)} scan(s)" if item.images else ""
                print(f"  - {item.name}: {kind}, {len(item.videos)} clip(s){scan}")
        if gated:
            gate = post_interval_state(config, log=print)
            if gate["allowed"]:
                print(
                    f"[dry-run] the posting gate is open ({gate['reason']}): "
                    f"a real run would render {min(len(items), max_items)} and post it"
                )
            else:
                left = gate.get("hours_left")
                print(
                    "[dry-run] the posting gate is closed "
                    f"({gate['reason']}{', %.1f h to go' % left if left is not None else ''}): "
                    "this slot would render and post nothing, and the GO_ folders would wait"
                )
        elif items:
            print(
                f"[dry-run] a real run would render all {len(items)} of them, since "
                "nothing is posted or the posting rhythm is off"
            )
        print("[dry-run] no file was created, changed or uploaded")
        return 0

    local_hub.ensure_local_layout(folders)

    ignore_pdca = bool(getattr(args, "ignore_pdca", False))
    params: Optional[dict] = None
    review: Optional[dict] = None
    if publishes and not ignore_pdca:
        # No Drive in local mode: the history in analytics/ is the only copy.
        params, review = learning_prepare(config, None, None)
    elif publishes:
        print("[pdca] --ignore-pdca: rendering with the config defaults, no collection")
    else:
        improvement.clear_active_params()

    def pipeline(item: "drive_sync.RawItem") -> dict:
        if item.is_project:
            source = local_hub.source_for(item, folders)
            return process_project_bundle(
                source,
                config,
                output_dir=folders.output,
                upload=not args.no_upload,
                artwork_offset_seconds=args.artwork_offset,
            )
        # A loose recording is rendered straight from where it lies: nothing is
        # copied, and the original is only ever moved to 03_ARCHIVE afterwards.
        return dict(
            {"file": item.name},
            **process_video_bundle(
                item.id,
                config,
                output_dir=folders.output,
                upload=not args.no_upload,
                artwork_offset_seconds=args.artwork_offset,
                base_name=item.stem,
            ),
        )

    sync_kwargs = {
        "state_path": getattr(args, "local_state", "") or folders.state_path,
        "min_age_seconds": (
            args.local_min_age
            if getattr(args, "local_min_age", None) is not None
            else settings["min_age_seconds"]
        ),
        "go_min_age_seconds": (
            args.local_go_min_age
            if getattr(args, "local_go_min_age", None) is not None
            else settings["go_min_age_seconds"]
        ),
        "archive_after_render": settings["archive_after_render"],
        "max_items": max_items,
    }

    if local_once:
        results: List[dict] = []
        try:
            results = local_hub.sync_once(folders, pipeline, **sync_kwargs)
        finally:
            if publishes and params is not None:
                learning_finish(results, None, None, params, review)
            improvement.clear_active_params()
        print(local_hub.format_results(results))
        done = [r for r in results if r.get("status") == "done"]
        log_path = os.path.join(
            folders.logs, f"run_{datetime.now(JST).strftime('%Y-%m-%d')}.log"
        )
        try:
            local_hub.write_scan_log(log_path, local_hub.format_results(results))
        except OSError:  # a log that cannot be written must not fail the run
            pass
        print(
            f"[local] {len(done)} item(s) rendered and published, "
            f"{len(results) - len(done)} failed."
        )
        return 0 if len(done) == len(results) else 1

    interval = args.local_interval or settings["scan_interval_seconds"]
    print(
        f"Watching {folders.raw} every {interval:g}s (Ctrl+C to stop)..."
    )
    try:
        local_hub.watch(
            folders, pipeline,
            interval_seconds=interval,
            **sync_kwargs,
        )
    except KeyboardInterrupt:
        print("\nStopped watching the local folder.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CrayonJapan making-of Short pipeline")
    parser.add_argument(
        "--input",
        help="Process this video file once and exit (default: watch ./input)",
    )
    parser.add_argument(
        "--drive-once",
        action="store_true",
        help="Scan DRIVE_RAW_FOLDER_ID once, render and drop the package in Drive",
    )
    parser.add_argument(
        "--drive-watch",
        action="store_true",
        help="Keep scanning the Drive raw folder on an interval",
    )
    parser.add_argument(
        "--drive-interval",
        type=float,
        default=None,
        help=f"Seconds between Drive scans (default {drive_sync.DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--drive-min-age",
        type=int,
        default=drive_sync.DEFAULT_MIN_AGE_SECONDS,
        help="Skip videos modified more recently than this many seconds (default 60)",
    )
    parser.add_argument(
        "--drive-go-min-age",
        type=int,
        default=drive_sync.DEFAULT_GO_FOLDER_MIN_AGE_SECONDS,
        help=(
            "Seconds to wait after a folder was renamed to GO_ before rendering it "
            f"(default {drive_sync.DEFAULT_GO_FOLDER_MIN_AGE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--drive-state",
        default=drive_sync.DEFAULT_STATE_PATH,
        help="Processed-file log so the same drawing is never rendered twice",
    )
    parser.add_argument(
        "--ignore-schedule",
        action="store_true",
        help=(
            "Post even when the newest upload is younger than "
            "schedule.min_interval_hours (emergency test runs only)"
        ),
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Render and write the metadata but skip the YouTube upload",
    )
    parser.add_argument(
        "--ignore-pdca",
        action="store_true",
        help=(
            "Skip the collect/reflect steps and render with config.yaml's "
            "improvement defaults (a manual, comparable run)"
        ),
    )
    parser.add_argument(
        "--local-once",
        action="store_true",
        help=(
            "Windows local mode: scan the local 01_RAW_VIDEO once, render one "
            "waiting artwork into 02_READY_TO_POST, post it and archive it"
        ),
    )
    parser.add_argument(
        "--local-watch",
        action="store_true",
        help="Keep scanning the local 01_RAW_VIDEO on an interval",
    )
    parser.add_argument(
        "--local-base",
        default="",
        help=(
            "Base folder of local mode (default: CRAYONJAPAN_HOME, else "
            "local.base_dir in config.yaml, else C:\\CrayonJapan)"
        ),
    )
    parser.add_argument(
        "--local-state",
        default="",
        help="Processed-item log of local mode (default: <base>\\local_state.json)",
    )
    parser.add_argument(
        "--local-interval",
        type=float,
        default=None,
        help=(
            "Seconds between local scans for --local-watch "
            f"(default {local_hub.DEFAULT_SCAN_INTERVAL_SECONDS})"
        ),
    )
    parser.add_argument(
        "--local-min-age",
        type=int,
        default=None,
        help=(
            "Seconds a loose recording has to sit still before it is rendered "
            f"(default {local_hub.DEFAULT_MIN_AGE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--local-go-min-age",
        type=int,
        default=None,
        help=(
            "Seconds to wait after a folder was renamed to GO_ "
            f"(default {local_hub.DEFAULT_GO_FOLDER_MIN_AGE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--local-init",
        action="store_true",
        help="Create the local folder layout (01_RAW_VIDEO, 02_READY_TO_POST, 03_ARCHIVE)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Check FFmpeg, the Python packages, the folders and the credentials",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With --local-once/--local-watch: show the folder layout, the posting "
            "gate and what would be rendered, without rendering, uploading or moving"
        ),
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Open the browser once for the YouTube sign-in and save token.json",
    )
    parser.add_argument(
        "--artwork-offset",
        type=float,
        default=DEFAULT_ARTWORK_OFFSET,
        help="Seconds before the end to grab the finished artwork (default 0 = last frame)",
    )
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    config = load_config()

    if args.doctor:
        return run_doctor(args, config)

    if args.auth:
        return run_auth(config)

    if args.local_init:
        return run_local_init(args, config)

    if args.input:
        process_new_video(args.input, config)
        return 0

    if args.local_once or args.local_watch or (args.dry_run and not args.drive_once and not args.drive_watch):
        if args.dry_run and not (args.local_once or args.local_watch):
            # `--dry-run` on its own describes the local slot that run_local.bat
            # would take, so it is read as a local one-shot.
            args.local_once = True
        return run_local_mode(args, config)

    if args.drive_once or args.drive_watch:
        return run_drive_mode(args, config)

    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class VideoDropHandler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory or not event.src_path.lower().endswith(RAW_LOCAL_EXTENSIONS):
                return
            time.sleep(3)  # let the copy finish before reading the file
            try:
                process_video_bundle(
                    event.src_path, config,
                    upload=not args.no_upload,
                    artwork_offset_seconds=args.artwork_offset,
                )
            except Exception as exc:  # keep watching after a failed video
                print(f"Failed to process {event.src_path}: {exc}", file=sys.stderr)

    os.makedirs(INPUT_DIR, exist_ok=True)
    print("Watching ./input folder for new CrayonJapan drawing videos...")
    observer = Observer()
    observer.schedule(VideoDropHandler(), path=INPUT_DIR, recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
