"""Render one sample Short from a local recording, without Gemini or YouTube.

The two external services are replaced by a canned script and a fixed parameter
set; FFmpeg and edge-tts are the real ones. This is how the layout of the hook,
the telop and the closing question is checked by eye before a render ships.

    python -m scripts.render_preview --input preview/sample.mp4 --language ja
    python -m scripts.render_preview --input preview/sample.mp4 --language bilingual

The finished MP4 lands in the output folder printed at the end; frames can be
cut from it with `ffmpeg -ss <seconds> -i <file> -frames:v 1 frame.png`.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main  # noqa: E402
import improvement  # noqa: E402

SCRIPT = {
    "title": "Turning a Pokemon Card into Thick Oil Pastel Art #Shorts",
    "hook_text": "A card found on the ground",
    "narration": (
        "This Pokemon card was lying on the pavement, and now it is on paper. "
        "I start with the pale gold border, one careful line at a time. "
        "The blue goes in short, buttery strokes, each layer deeper. "
        "Then the red symbol, pressed hard, and the black outline last."
    ),
    "description": (
        "A small Pokemon card drawn on paper with Sakura Cray-Pas Specialist oil "
        "pastels. #SakuraCrayPas #CrayPasSpecialist #OilPastelArt "
        "#JapaneseArtSupplies #OriginalPostcard #CrayonJapan"
    ),
    "tags": [
        "oil pastel", "pokemon card", "crayon art", "sakura cray pas",
        "art process", "asmr drawing", "japanese art supplies", "short video",
        "oil pastel drawing", "ポケモンカード",
    ],
    "title_ja": "落ちていたポケモンカードを油絵具で描いてみた #Shorts",
    "hook_ja": "道に落ちていたカードを描く",
    "narration_ja": (
        "道に落ちていたポケモンカードを、紙の上に描き直します。"
        "まずは淡い金色の縁から、一本ずつ丁寧に。"
        "青は短いバターのようなタッチで重ねて、層が増えるほど深くなります。"
        "最後に赤と黒の輪郭を入れると、カードが立ち上がりました。"
    ),
    "overseas_caption": "A Pokemon card redrawn on paper in thick, layered oil pastel.",
    "overseas_caption_ja": "紙の上に油絵具で描き直したポケモンカード。",
}


def canned_script(frames, style_key, config, reference_scan=False, params=None):
    """Stand-in for ai_writer.generate_making_script (no Gemini call)."""
    print(f"[preview] canned script for {len(frames)} frame(s); style={style_key}")
    return dict(SCRIPT)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Render one preview Short")
    parser.add_argument("--input", required=True, help="A local recording to render")
    parser.add_argument("--output", default="output_preview", help="Output root")
    parser.add_argument(
        "--language", default="bilingual", choices=list(improvement.LANGUAGE_MODES)
    )
    parser.add_argument("--duration", type=float, default=28.0)
    parser.add_argument(
        "--hook-style", default="finish_flash_to_blank", choices=list(improvement.HOOK_STYLES)
    )
    parser.add_argument("--tts-mode", default="short", choices=list(improvement.TTS_MODES))
    return parser.parse_args(argv)


def main_preview(argv=None) -> int:
    args = parse_args(argv)
    os.environ.setdefault("STYLE_OVERRIDE", "style_B_story")
    config = main.load_config(os.path.join(ROOT, "config.yaml"))
    params = improvement.validate_params(
        {
            "target_duration_sec": args.duration,
            "language_mode": args.language,
            "hook_style": args.hook_style,
            "tts_mode": args.tts_mode,
        }
    )
    improvement.set_active_params(params)
    main.generate_making_script = canned_script

    bundle = main.process_video_bundle(
        args.input, config, output_dir=args.output, upload=False
    )
    print(f"\n[preview] {bundle['final_video']}")
    print(f"[preview] telop  {bundle['telop']}")
    print(f"[preview] pack   {bundle['metadata_text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main_preview())
