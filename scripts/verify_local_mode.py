"""End-to-end check of the Windows local hub, with real FFmpeg and no Drive.

    python scripts/verify_local_mode.py --ffmpeg "$(command -v ffmpeg)"

It builds a throw-away base folder, drops one synthetic ``GO_`` artwork into
``01_RAW_VIDEO`` (two clips with audio + a scanned image), runs the same
``local_hub.sync_once`` a real slot runs, and checks the result: the package in
``02_READY_TO_POST`` with its four artifacts, a 1080x1920 Short of about the
target length, the source moved to ``03_ARCHIVE``, the state file recording it,
and a second scan that renders nothing.

Gemini, edge-tts and YouTube are replaced here (a canned script, ``tts_mode:
none`` and no upload) so the check needs no key, no network and no quota; FFmpeg
and the local file system are the real ones. Nothing outside the temporary
folder is touched.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import drive_sync  # noqa: E402
import improvement  # noqa: E402
import local_hub  # noqa: E402
import main  # noqa: E402

SCRIPT = {
    "title": "Sushi drawn in oil pastel #Shorts",
    "hook_text": "Watch this sushi appear",
    "narration": (
        "This is how a small oil pastel drawing is built, stroke by stroke. "
        "The first layer of colour goes down like a breath on the paper. "
        "Sakura Cray-Pas Specialist is soft enough to blend with a fingertip. "
        "One original postcard, drawn by hand and never repeated."
    ),
    "description": "A small original postcard. #SakuraCrayPas #OilPastelArt",
    "tags": ["oil pastel", "sakura cray pas", "crayon art"],
    "title_ja": "すしをオイルパステルで描く #Shorts",
    "hook_ja": "すしが色になっていく",
    "narration_ja": "小さなオイルパステル画が、一筆ずつ出来ていきます。",
    "overseas_caption": "Sushi drawn in creamy oil pastel",
    "overseas_caption_ja": "クレヨンで描くすし。",
}


def canned_script(frames, style_key, config, reference_scan=False, params=None):
    """Stand-in for ai_writer.generate_making_script (no Gemini call)."""
    del frames, style_key, config, reference_scan, params
    return dict(SCRIPT)


def run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{result.stderr[-2000:]}")
    return result


def build_clip(ffmpeg: str, path: str, seconds: int, size: str, pattern: str, tone: int):
    run([
        ffmpeg, "-y",
        "-f", "lavfi", "-i", f"{pattern}=size={size}:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", path,
    ])


def build_scan(path: str):
    from PIL import Image

    Image.new("RGB", (900, 1200), (243, 214, 222)).save(path, quality=90)


def probe(ffprobe: str, path: str, entry: str) -> str:
    result = run([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", f"stream={entry}", "-of", "default=nw=1:nk=1", path,
    ])
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'ok  ' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return condition


def verify(ffmpeg: str, ffprobe: str, keep: bool = False) -> int:
    tmp = tempfile.mkdtemp(prefix="crayonjapan-local-")
    base = os.path.join(tmp, "CrayonJapan")
    folders = local_hub.ensure_local_layout(
        local_hub.load_local_folders(base_dir=base, environ={})
    )
    print(f"1) building a throw-away base folder: {base}")
    project = os.path.join(folders.raw, "GO_20261121_sushi")
    os.makedirs(project)
    build_clip(ffmpeg, os.path.join(project, "part1.mp4"), 8, "640x480", "testsrc2", 440)
    build_clip(ffmpeg, os.path.join(project, "finish.mp4"), 6, "720x540", "smptebars", 330)
    build_scan(os.path.join(project, "scan.jpg"))
    # A real GO_ folder waits `local.go_min_age_seconds` after its rename; the
    # throw-away one is back-dated so the check does not sleep.
    stamp = os.path.getmtime(project) - 600
    os.utime(project, (stamp, stamp))
    print(f"     {len(os.listdir(project))} files in {local_hub.RAW_DIRNAME}")

    print("2) running one local slot (real FFmpeg, no Gemini, no upload)")
    improvement.clear_active_params()
    config = main.load_config(os.path.join(ROOT, "config.yaml"))
    params = improvement.validate_params(
        {"target_duration_sec": 28.0, "tts_mode": "none", "language_mode": "bilingual"}
    )
    improvement.set_active_params(params)
    main.generate_making_script = canned_script

    def pipeline(item):
        source = local_hub.source_for(item, folders)
        return main.process_project_bundle(
            source, config, output_dir=folders.output, upload=False
        )

    results = local_hub.sync_once(folders, pipeline, log=print)
    improvement.clear_active_params()

    print("3) checking the result...")
    ok = True
    ok &= check("one item was rendered", [r["status"] for r in results] == ["done"],
                str([r["status"] for r in results]))
    package_name = results[0].get("package", "") if results else ""
    package = os.path.join(folders.ready, package_name)
    ok &= check("the package folder exists and is named like the render",
                bool(package_name) and os.path.isdir(package), package_name)
    files = sorted(os.listdir(package)) if os.path.isdir(package) else []
    expected = sorted(
        f"20261121_sushi{ending}"
        for ending in ("_art_metadata.json", "_artwork.jpg", "_final.mp4", "_metadata.txt")
    )
    ok &= check("the four artifacts are in the package", files == expected, str(files))
    final = os.path.join(package, "20261121_sushi_final.mp4")
    ok &= check("the Short exists", os.path.exists(final))
    if os.path.exists(final):
        width, height = probe(ffprobe, final, "width"), probe(ffprobe, final, "height")
        ok &= check("the Short is 1080x1920 (9:16)", (width, height) == ("1080", "1920"),
                    f"{width}x{height}")
        duration = float(probe(ffprobe, final, "duration") or 0)
        # `duration` is not a stream field; fall back to the format duration
        if not duration:
            result = run([
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", final,
            ])
            duration = float(result.stdout.strip() or 0)
        ok &= check("the Short is about the target length", 20 <= duration <= 58,
                    f"{duration:.1f}s")
    ok &= check("the GO_ folder moved to 03_ARCHIVE",
                os.path.isdir(os.path.join(folders.archive, "GO_20261121_sushi")))
    ok &= check("the source is gone from 01_RAW_VIDEO", not os.path.exists(project))
    ok &= check("local_state.json records it", "done" in local_hub.state_summary(folders.state_path))
    ok &= check("the render leftovers were cleaned",
                not any(os.scandir(folders.output)), str(os.listdir(folders.output)))
    ok &= check("the scan became the artwork",
                "sushi" in " ".join(files))

    print("4) a second slot with nothing waiting...")
    again = local_hub.sync_once(folders, pipeline, log=print)
    ok &= check("the second slot renders nothing", again == [], str(again))

    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    if keep:
        print(f"kept: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Check the local hub end to end")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="path to ffmpeg")
    parser.add_argument("--ffprobe", default="", help="path to ffprobe (default: beside ffmpeg)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary base folder")
    return parser.parse_args(argv)


def main_cli(argv=None) -> int:
    args = parse_args(argv)
    ffprobe = args.ffprobe or os.path.join(os.path.dirname(args.ffmpeg), "ffprobe")
    return verify(args.ffmpeg, ffprobe or "ffprobe", keep=args.keep)


if __name__ == "__main__":
    sys.exit(main_cli())
