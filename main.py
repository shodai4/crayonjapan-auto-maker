"""CrayonJapan Auto-Maker entry point.

Drop a raw drawing video into ./input and the pipeline renders a 9:16 English
YouTube Short, uploads it, and records it in analytics/performance.csv.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from typing import List, Optional

import pandas as pd
import yaml

from ai_writer import ASMR_STYLE_KEY, choose_best_style, generate_making_script
from analytics_updater import CSV_COLUMNS
from video_processor import build_vertical_timelapse, extract_keyframes, has_audio_stream
from youtube_uploader import upload_short

CONFIG_PATH = "config.yaml"
CSV_PATH = "analytics/performance.csv"
INPUT_DIR = "input"
OUTPUT_DIR = "output"
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
                submaker.create_sub((chunk["offset"], chunk["duration"]), chunk["text"])
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(submaker.generate_subs())


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


def process_new_video(input_path: str, config: Optional[dict] = None) -> str:
    """Run the full pipeline for one raw video and return the YouTube video id."""
    config = config or load_config()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    raw_vertical = f"{OUTPUT_DIR}/{base_name}_vertical.mp4"
    audio_path = f"{OUTPUT_DIR}/{base_name}.mp3"
    srt_path = f"{OUTPUT_DIR}/{base_name}.srt"
    final_video = f"{OUTPUT_DIR}/{base_name}_final.mp4"
    keyframe_dir = f"{OUTPUT_DIR}/{base_name}_frames"

    # The style is chosen first: the ASMR style needs the original audio kept.
    style_key = choose_best_style(CSV_PATH, config)
    keep_audio = should_keep_audio(style_key)

    print(f"[1/5] Editing vertical timelapse: {input_path} (style={style_key})")
    build_vertical_timelapse(
        input_path,
        raw_vertical,
        target_duration=config["video"]["target_duration_sec"],
        hook_duration=config["video"]["hook_duration_sec"],
        keep_audio=keep_audio,
    )

    print("[2/5] Extracting keyframes & writing the English script...")
    frames = extract_keyframes(raw_vertical, keyframe_dir)
    script_data = generate_making_script(frames, style_key, config)

    print(f"[3/5] Generating English voiceover ({style_key})...")
    asyncio.run(synthesize_voice(script_data["narration"], audio_path, srt_path))

    print("[4/5] Burning subtitles and mixing audio...")
    mux_audio_and_subtitles(
        raw_vertical, audio_path, srt_path, final_video, keep_original_audio=keep_audio
    )

    print("[5/5] Uploading to the CrayonJapan YouTube channel...")
    privacy = config["youtube"]["default_privacy"]
    video_id = upload_short(
        final_video,
        script_data,
        privacy_status=privacy,
        category_id=str(config["youtube"].get("category_id", "22")),
        default_language=str(config["youtube"].get("default_language", "en")),
    )

    append_performance_row(script_data, style_key, video_id)
    print(f"Completed! Video ID: {video_id} (privacy={privacy})")
    return video_id


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CrayonJapan making-of Short pipeline")
    parser.add_argument(
        "--input",
        help="Process this video file once and exit (default: watch ./input)",
    )
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    config = load_config()

    if args.input:
        process_new_video(args.input, config)
        return 0

    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class VideoDropHandler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory or not event.src_path.lower().endswith((".mp4", ".mov")):
                return
            time.sleep(3)  # let the copy finish before reading the file
            try:
                process_new_video(event.src_path, config)
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
