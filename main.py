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
from ai_writer import ASMR_STYLE_KEY, choose_best_style, generate_making_script
from analytics_updater import CSV_COLUMNS
from metadata_writer import write_metadata_files
from video_processor import (
    DEFAULT_ARTWORK_OFFSET,
    build_project_hook_command,
    build_project_segment_command,
    build_vertical_timelapse,
    compute_project_plan,
    concat_segments,
    ensure_disk_space,
    extract_final_artwork,
    extract_keyframes,
    has_audio_stream,
    prepare_scanned_artwork,
    probe_duration,
    run_command,
)
from youtube_uploader import upload_short

CONFIG_PATH = "config.yaml"
CSV_PATH = "analytics/performance.csv"
INPUT_DIR = "input"
OUTPUT_DIR = "output"
RAW_LOCAL_EXTENSIONS = (".mp4", ".mov", ".avi")
HOOK_TYPE = "finish_preview_2.5s"
TTS_VOICE = "en-US-JennyNeural"
TTS_RATE = "+5%"
SUBTITLE_STYLE = (
    "Fontsize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "Outline=2,Alignment=2,MarginV=65"
)


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def should_keep_audio(style_key: str) -> bool:
    """The ASMR style keeps the real pastel-on-paper sound of the drawing."""
    return style_key == ASMR_STYLE_KEY


def build_subtitle_filter(srt_path: str) -> str:
    """Subtitles filter with the escaping FFmpeg expects on every platform."""
    escaped = srt_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"subtitles='{escaped}':force_style='{SUBTITLE_STYLE}'"


def build_mux_command(
    video_path: str,
    audio_path: str,
    srt_path: str,
    final_path: str,
    mix_original_audio: bool = False,
) -> List[str]:
    """Burn subtitles and lay the narration over the time-lapse.

    With `mix_original_audio` the original drawing sound (already speed-adjusted
    in the time-lapse) is mixed under the narration. Narration is padded with
    silence so ``-shortest`` ends on the video, never on a short voice track.
    """
    subtitles = build_subtitle_filter(srt_path)
    if mix_original_audio:
        filter_complex = (
            f"[0:v]{subtitles}[v];"
            "[0:a]volume=0.55[orig];"
            "[1:a]volume=1.0[narr];"
            "[narr]apad[narrpad];"
            "[orig][narrpad]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
        )
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        filter_complex = f"[0:v]{subtitles}[v];[1:a]apad[a]"
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
) -> None:
    """English narration plus word-level SRT subtitles via edge-tts."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
    submaker = edge_tts.SubMaker()
    with open(audio_path, "wb") as file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                feed_word_boundary(submaker, chunk)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(render_srt(submaker))


def mux_audio_and_subtitles(
    video_path: str,
    audio_path: str,
    srt_path: str,
    final_path: str,
    keep_original_audio: bool = False,
) -> str:
    """Render the upload-ready file."""
    import subprocess

    mix = bool(keep_original_audio) and has_audio_stream(video_path)
    subprocess.run(
        build_mux_command(video_path, audio_path, srt_path, final_path, mix_original_audio=mix),
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
    reference_scan: bool = False,
) -> dict:
    """Script, voice, subtitles, upload pack and (optionally) the upload."""
    audio_path = f"{output_dir}/{base_name}.mp3"
    srt_path = f"{output_dir}/{base_name}.srt"
    final_video = f"{output_dir}/{base_name}_final.mp4"
    keyframe_dir = f"{output_dir}/{base_name}_frames"

    print("[3/6] Extracting keyframes & writing the English script...")
    frames = extract_keyframes(raw_vertical, keyframe_dir)
    if reference_scan:
        # The finished scan is the strongest reference for colours and details,
        # so Gemini sees it first, next to the time-lapse keyframes.
        frames = [artwork_path] + frames
    script_data = generate_making_script(
        frames, style_key, config, reference_scan=reference_scan
    )

    print(f"[4/6] Generating English voiceover ({style_key})...")
    asyncio.run(synthesize_voice(script_data["narration"], audio_path, srt_path))

    print("[5/6] Burning subtitles and mixing audio...")
    keep_audio = should_keep_audio(style_key)
    mux_audio_and_subtitles(
        raw_vertical, audio_path, srt_path, final_video, keep_original_audio=keep_audio
    )

    print("[6/6] Writing the English metadata bundle...")
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
        "style_key": style_key,
        "vertical": raw_vertical,
        "audio": audio_path,
        "srt": srt_path,
        "final_video": final_video,
        "artwork": artwork_path,
        "metadata_text": metadata_paths["text"],
        "metadata_json": metadata_paths["json"],
        "script": script_data,
        "video_id": video_id,
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
    work_dir = source.work_dir or os.path.join(INPUT_DIR, base_name)
    os.makedirs(work_dir, exist_ok=True)

    raw_vertical = f"{output_dir}/{base_name}_vertical.mp4"
    artwork_path = f"{output_dir}/{base_name}_artwork.jpg"
    vertical_stem = os.path.splitext(raw_vertical)[0]

    clips = list(source.videos)
    style_key = choose_best_style(CSV_PATH, config)
    keep_audio = should_keep_audio(style_key)
    plan = compute_project_plan(
        [clip.duration_seconds for clip in clips],
        target_duration=float(config["video"]["target_duration_sec"]),
        hook_duration=float(config["video"]["hook_duration_sec"]),
    )

    print(
        f"[1/6] Editing project {source.name}: {len(clips)} clip(s), "
        f"{plan['total_duration']:.0f}s -> {plan['main_duration']:.0f}s (style={style_key})"
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
                runner(
                    build_project_hook_command(
                        local, hook_path,
                        hook_start=max(hook_start, 0.0),
                        hook_duration=hook_length,
                        keep_audio=keep_audio,
                        has_audio=audio,
                    )
                )
                hook_paths.append(hook_path)

            segment_path = f"{vertical_stem}_seg{index:02d}.mp4"
            runner(
                build_project_segment_command(
                    local, segment_path,
                    pts_multiplier=plan["pts_multiplier"],
                    keep_audio=keep_audio,
                    has_audio=audio,
                )
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

    print("[2/6] Concatenating the rendered segments (hook + clips)...")
    try:
        concat_segments(hook_paths + body_paths, raw_vertical, runner=runner)
    finally:
        for path in hook_paths + body_paths:
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
        reference_scan=reference_scan,
    )
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
