"""CrayonJapan Auto-Maker entry point.

Drop a raw drawing video into ./input and the pipeline renders a 9:16 English
YouTube Short, uploads it, and records it in analytics/performance.csv.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from datetime import datetime
from typing import List, Optional, Sequence

import pandas as pd
import yaml

import drive_sync
import telop
from ai_writer import (
    ASMR_STYLE_KEY,
    TRANSLATION_KEYS,
    choose_best_style,
    generate_making_script,
)
from analytics_updater import CSV_COLUMNS
from metadata_writer import JST, write_metadata_files
from video_processor import (
    DEFAULT_ARTWORK_OFFSET,
    ZOOM_OUTRO_SECONDS,
    assemble_finale,
    build_vertical_timelapse,
    compute_finish_window,
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
    run_command,
)
from youtube_uploader import upload_short

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
    """Burn the telop ASS document; its styles travel inside the file."""
    return f"ass='{_escaped_filter_path(ass_path)}'"


def build_mux_command(
    video_path: str,
    audio_path: str,
    srt_path: str,
    final_path: str,
    mix_original_audio: bool = False,
    with_subtitles: bool = True,
    ass_path: str = "",
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
    if mix_original_audio:
        filter_complex = (
            f"{picture};"
            "[0:a]volume=0.55[orig];"
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


def write_telop(
    base_name: str,
    output_dir: str,
    script_data: dict,
    config: dict,
    video_seconds: Optional[float] = None,
    marked: Sequence[str] = (),
) -> str:
    """Lay out the ASS telop for one Short and return its path.

    Returns "" when ``telop.enabled`` is false, so the older plain-SRT look stays
    one config line away. The word timings come from the voice step; without
    them the hook, title and closing line are still burned, which is what a
    voice that answered without boundaries can offer.
    """
    settings = config.get("telop") or {}
    if settings.get("enabled", True) is False:
        print("      telop disabled in config.yaml; burning the plain SRT cues")
        return ""

    words = telop.load_word_timings(f"{output_dir}/{base_name}_words.json")
    series_label = str(settings.get("series_label") or config["brand"].get("name", ""))
    title = telop.clean_title(str(script_data.get("title", "")), series_label)
    hook = telop.clean_hook(str(script_data.get("hook_text", "")))
    cta = telop.clean_caption_text(str(settings.get("cta_text", DEFAULT_CTA_TEXT)))
    font = str(settings.get("font") or "") or telop.detect_font()

    path = telop.write_telop_ass(
        f"{output_dir}/{base_name}.ass",
        words=words,
        hook_text=hook,
        title=title,
        cta_text=cta,
        video_seconds=video_seconds,
        font_name=font,
        marked=marked,
    )
    stats = telop.summarise(telop.plan_cards(words, marked=marked))
    print(
        f"      {int(stats['cards'])} caption card(s) in {font}, "
        f"longest line {int(stats['longest_line'])} chars"
    )
    return path


def mux_audio_and_subtitles(
    video_path: str,
    audio_path: str,
    srt_path: str,
    final_path: str,
    keep_original_audio: bool = False,
    ass_path: str = "",
) -> str:
    """Render the upload-ready file."""
    import subprocess

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
) -> pd.DataFrame:
    """Record the new Short so the style evolution has data to learn from."""
    new_row = pd.DataFrame([{
        "video_id": video_id,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "title": script_data.get("title", ""),
        "style_used": style_key,
        "hook_type": hook_type,
        "views": 0,
        "avg_view_percentage": 0.0,
        "likes": 0,
        "comments_count": 0,
        "top_feedback": "",
    }])
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
) -> dict:
    """Script, voice, subtitles, upload pack and (optionally) the upload.

    ``finale`` turns the hook/body/finish cut into the final picture: after the
    voice is measured, a slow zoom over the artwork is appended so the video
    ends with the narration instead of cutting it off.
    """
    audio_path = f"{output_dir}/{base_name}.mp3"
    srt_path = f"{output_dir}/{base_name}.srt"
    words_path = f"{output_dir}/{base_name}_words.json"
    final_video = f"{output_dir}/{base_name}_final.mp4"
    keyframe_dir = f"{output_dir}/{base_name}_frames"

    print("[3/6] Extracting keyframes & writing the English script...")
    # The finale is appended later, so the frames come from the cut that exists.
    frames_source = (finale or {}).get("base_video") or raw_vertical
    frames = extract_keyframes(frames_source, keyframe_dir)
    if reference_scan:
        # The finished scan is the strongest reference for colours and details,
        # so Gemini sees it first, next to the time-lapse keyframes.
        frames = [artwork_path] + frames
    script_data = generate_making_script(
        frames, style_key, config, reference_scan=reference_scan
    )

    print(f"[4/6] Generating English voiceover ({style_key})...")
    # **word** markers are the script's own emphasis: spoken without them,
    # highlighted on screen instead.
    narration, marked_words = telop.strip_emphasis_markers(script_data["narration"])
    asyncio.run(
        synthesize_voice(narration, audio_path, srt_path, words_path=words_path)
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
            narration_seconds=probe_audio_seconds(audio_path),
            runner=finale["runner"],
        )
        print(
            f"      cut {finale_plan['base_seconds']:.1f}s + zoom "
            f"{finale_plan['zoom_outro_seconds']:.1f}s = "
            f"{finale_plan['total_seconds']:.1f}s"
        )

    print("[5/6] Laying out the telop and mixing audio...")
    keep_audio = should_keep_audio(style_key)
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
    )
    mux_audio_and_subtitles(
        vertical, audio_path, srt_path, final_video,
        keep_original_audio=keep_audio,
        ass_path=telop_path,
    )

    print("[6/6] Writing the English metadata bundle...")
    missing_ja = [key for key in TRANSLATION_KEYS if not script_data.get(key)]
    if missing_ja:
        print(
            f"      Gemini sent no {', '.join(missing_ja)}; "
            "the pack marks that translation as missing"
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
        append_performance_row(script_data, style_key, video_id)
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
    keep_audio = should_keep_audio(style_key)

    print(f"[1/6] Editing vertical timelapse: {input_path} (style={style_key})")
    build_vertical_timelapse(
        input_path,
        raw_vertical,
        target_duration=config["video"]["target_duration_sec"],
        hook_duration=config["video"]["hook_duration_sec"],
        keep_audio=keep_audio,
    )

    print("[2/6] Cutting the finished artwork out of the drawing...")
    extract_final_artwork(
        input_path,
        artwork_path,
        offset_seconds=artwork_offset_seconds,
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
    keep_audio = should_keep_audio(style_key)
    target_total = float(config["video"]["target_duration_sec"])
    hook_seconds = float(config["video"]["hook_duration_sec"])
    # The cut leaves room for the finale: the finish shot replayed slowly, then a
    # closing zoom over the artwork. Both are only appended after the voice is
    # measured, so their length can follow the narration.
    finish_window = compute_finish_window(clips[-1].duration_seconds if clips else None)
    timelapse_target = target_total - finish_window["screen_seconds"] - ZOOM_OUTRO_SECONDS
    plan = compute_project_plan(
        [clip.duration_seconds for clip in clips],
        target_duration=timelapse_target,
        hook_duration=hook_seconds,
    )

    print(
        f"[1/6] Editing project {source.name}: {len(clips)} clip(s), "
        f"{plan['total_duration']:.0f}s -> {plan['main_duration']:.0f}s "
        f"+ {finish_window['screen_seconds']:.1f}s finish + zoom "
        f"(style={style_key})"
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
            if is_last:
                # Hook start comes from the real tail of the last clip, so a
                # slightly different metadata duration cannot overrun it.
                duration = probe_duration(local)
                hook_length = min(plan["hook_duration"], max(duration, 0.1))
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
                )
                hook_paths.append(hook_path)

                # Finale, part one: the same last moment, played slowly, so the
                # finished piece is actually looked at before the zoom begins.
                finish = compute_finish_window(duration)
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

            if is_last and not reference_scan:
                print("      no scan in the folder: taking the artwork from the last clip...")
                extract_final_artwork(
                    local, artwork_path, offset_seconds=artwork_offset_seconds
                )
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


def run_drive_mode(args, config: dict) -> int:
    """Phase 2: Google Drive folder in, upload-ready package out."""
    try:
        folders = drive_sync.load_drive_folders()
    except drive_sync.MissingFolderIdError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    service = drive_sync.build_drive_service()

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
    }

    if args.drive_once:
        results = drive_sync.sync_once(service, folders, pipeline, **sync_kwargs)
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
        "--no-upload",
        action="store_true",
        help="Render and write the metadata but skip the YouTube upload",
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

    if args.input:
        process_new_video(args.input, config)
        return 0

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
