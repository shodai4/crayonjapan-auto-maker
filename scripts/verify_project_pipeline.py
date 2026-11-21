#!/usr/bin/env python3
"""End-to-end check of the GO_ project path with a real FFmpeg and OpenCV.

Nothing here talks to Drive, Gemini or YouTube: two synthetic drawing clips
(different resolution, one with sound, one silent) and a scanned PNG stand in
for the phone upload, and a fake Drive answers the folder listing while a local
copy stands in for the download.

    python3 scripts/verify_project_pipeline.py --ffmpeg "$(command -v ffmpeg)"

It verifies that
  * both clips become one 1080x1920 / 30 fps Short of the target length,
  * the silent clip did not break the concatenation (ASMR keeps audio),
  * the hook + segments are joined, and the raw clips are deleted again,
  * the scan becomes the artwork JPEG (uncropped, 1080 wide),
  * drive_sync finds the GO_ folder, uploads four artifacts, archives the folder
    and records it in the state file.

The script is a manual check, not part of the unittest suite (CI has no ffmpeg
until the workflow installs it, and this one takes a few seconds of CPU).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import cv2  # noqa: E402

import drive_sync as ds  # noqa: E402
import main  # noqa: E402
import video_processor as vp  # noqa: E402

SCRIPT = {
    "title": "One-of-1 oil pastel postcard #Shorts",
    "hook_text": "One tiny postcard",
    "narration": "This postcard started as an empty sheet of paper.",
    "description": "Original postcard at CrayonJapan.",
    "tags": ["oil pastel", "postcard"],
    # Stand-in for the Japanese fields Gemini returns with every script.
    "title_ja": "世界に1枚のオイルパステルポストカード #Shorts",
    "hook_ja": "ちいさなポストカード",
    "narration_ja": "このポストカードは、まっ白な紙から始まりました。",
}

FFPROBE_SHIM = '''#!/usr/bin/env python3
"""Minimal ffprobe for the verification run (duration + audio stream only)."""
import re
import subprocess
import sys

args = sys.argv[1:]
path = args[-1]
info = subprocess.run(
    [%(ffmpeg)r, "-hide_banner", "-i", path],
    capture_output=True, text=True,
).stderr
if "-select_streams" in args:
    print("0" if re.search(r"Stream #\\d+:\\d+.*Audio:", info) else "")
else:
    match = re.search(r"Duration: (\\d+):(\\d+):(\\d+\\.\\d+)", info)
    if match:
        hours, minutes, seconds = match.groups()
        print(f"{int(hours) * 3600 + int(minutes) * 60 + float(seconds):.3f}")
    else:
        print("0")
'''


def run(cmd, **kwargs):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def make_clip(path, width, height, seconds, with_audio, ffmpeg):
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=30:duration={seconds}",
    ]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-shortest"]
    cmd.append(path)
    run(cmd)


def make_scan(path, width=1600, height=1200):
    canvas = np.full((height, width, 3), 236, dtype=np.uint8)
    cv2.circle(canvas, (width // 2, height // 2), min(width, height) // 3, (60, 90, 200), -1)
    cv2.rectangle(
        canvas,
        (width // 10, height // 10),
        (width // 2, height // 3),
        (40, 180, 120),
        -1,
    )
    cv2.imwrite(path, canvas)


def probe_video(path, ffmpeg):
    cap = cv2.VideoCapture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    return {"fps": fps, "frames": frames, "width": width, "height": height,
            "duration": (frames / fps) if fps else 0.0}


class FakeDriveFiles:
    """Answers the one query drive_sync makes, from dictionaries."""

    def __init__(self, children_by_parent):
        self.children_by_parent = children_by_parent

    def list(self, **kwargs):
        query = str(kwargs.get("q", ""))
        payload = {"files": []}
        for parent, children in self.children_by_parent.items():
            if f"'{parent}' in parents" in query:
                payload = {"files": children}
                break
        return _Response(payload)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeDrive:
    def __init__(self, children_by_parent):
        self._files = FakeDriveFiles(children_by_parent)

    def files(self):
        return self._files


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' - ' + detail) if detail else ''}")
    return bool(condition)


def main_check(ffmpeg):
    tmp = tempfile.mkdtemp(prefix="crayonjapan-verify-")
    failures = []
    try:
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        # `ffmpeg` on PATH: video_processor calls it by name.
        os.symlink(os.path.abspath(ffmpeg), os.path.join(bin_dir, "ffmpeg"))
        ffmpeg = os.path.join(bin_dir, "ffmpeg")
        shim = os.path.join(bin_dir, "ffprobe")
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write(FFPROBE_SHIM % {"ffmpeg": ffmpeg})
        os.chmod(shim, 0o755)
        env_path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["PATH"] = env_path

        clips_dir = os.path.join(tmp, "clips")
        os.makedirs(clips_dir)
        clip_a = os.path.join(clips_dir, "part1.mp4")
        clip_b = os.path.join(clips_dir, "finish.mp4")
        scan = os.path.join(clips_dir, "scan.png")

        print("1) building the synthetic artwork (2 clips + 1 scan)...")
        make_clip(clip_a, 720, 1280, 60, with_audio=True, ffmpeg=ffmpeg)
        make_clip(clip_b, 1920, 1080, 30, with_audio=False, ffmpeg=ffmpeg)
        make_scan(scan)
        for path in (clip_a, clip_b, scan):
            print(f"     {os.path.basename(path)}: {os.path.getsize(path) / 1024:.0f} KB")

        folders = ds.DriveFolders(raw="raw-folder", ready="ready-folder", archive="archive-folder")
        root_children = [
            {"id": "proj1", "name": "GO_20261121_sushi", "mimeType": ds.FOLDER_MIME_TYPE,
             "modifiedTime": "2026-11-21T03:00:00Z"},
            {"id": "uploading", "name": "20261122_cat", "mimeType": ds.FOLDER_MIME_TYPE,
             "modifiedTime": "2026-11-21T03:00:00Z"},
        ]
        project_children = [
            {"id": "v1", "name": "part1.mp4", "mimeType": "video/mp4", "size": str(os.path.getsize(clip_a)),
             "modifiedTime": "2026-11-21T03:00:00Z",
             "videoMediaMetadata": {"durationMillis": "60000"}},
            {"id": "v2", "name": "finish.mp4", "mimeType": "video/mp4", "size": str(os.path.getsize(clip_b)),
             "modifiedTime": "2026-11-21T03:00:00Z",
             "videoMediaMetadata": {"durationMillis": "30000"}},
            {"id": "scan1", "name": "scan.png", "mimeType": "image/png", "size": str(os.path.getsize(scan)),
             "modifiedTime": "2026-11-21T03:00:00Z"},
        ]
        service = FakeDrive({"raw-folder": root_children, "proj1": project_children})

        sources = {clip_a: "v1", clip_b: "v2", scan: "scan1"}
        by_id = {value: key for key, value in sources.items()}
        uploaded = []
        moved = []

        def downloader(svc, file_id, path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copyfile(by_id[file_id], path)
            return path

        def uploader(svc, path, folder_id):
            uploaded.append((os.path.basename(path), folder_id))
            return {"id": f"up-{os.path.basename(path)}"}

        def mover(svc, file_id, new_parent, old_parent=None):
            moved.append((file_id, new_parent, old_parent))
            return {"id": file_id}

        out_dir = os.path.join(tmp, "output")
        work_root = os.path.join(tmp, "input")
        state_path = os.path.join(tmp, "drive_state.json")
        config = main.load_config(os.path.join(ROOT, "config.yaml"))

        print("2) running the Drive scan -> project render (ASMR style, real FFmpeg)...")
        with (
            mock.patch.object(main, "choose_best_style", return_value="style_C_asmr"),
            mock.patch.object(main, "generate_making_script", return_value=dict(SCRIPT)),
            mock.patch.object(main.asyncio, "run", new=mock.MagicMock()),
            mock.patch.object(main, "mux_audio_and_subtitles"),
        ):
            def pipeline(source):
                result = main.process_project_bundle(
                    source, config, output_dir=out_dir, upload=False, runner=vp.run_command,
                )
                # stand in for the narration mux so a *_final.mp4 exists
                shutil.copyfile(result["vertical"], result["final_video"])
                return result

            results = ds.sync_once(
                service, folders, pipeline,
                download_dir=work_root,
                state_path=state_path,
                min_age_seconds=0,
                go_min_age_seconds=0,
                keep_local_raw=False,
                now=datetime(2026, 11, 21, 4, 0, 0, tzinfo=timezone.utc),
                downloader=downloader,
                uploader=uploader,
                mover=mover,
                log=lambda line: print(f"     {line}"),
            )

        print("3) checking the result...")
        failures.append(not check("one item rendered", len(results) == 1 and results[0]["status"] == "done",
                                  str([r.get("status") for r in results])))
        base = "20261121_sushi"
        vertical = os.path.join(out_dir, f"{base}_vertical.mp4")
        final = os.path.join(out_dir, f"{base}_final.mp4")
        artwork = os.path.join(out_dir, f"{base}_artwork.jpg")

        info = probe_video(vertical, ffmpeg)
        failures.append(not check("vertical is 1080x1920", (info["width"], info["height"]) == (1080, 1920),
                                  f"{info['width']}x{info['height']}"))
        failures.append(not check("vertical is ~30 fps", abs(info["fps"] - 30) < 0.5, f"{info['fps']:.2f}"))
        target = float(config["video"]["target_duration_sec"])
        failures.append(not check(f"vertical is close to {target:.0f}s (hook + main)",
                                  abs(info["duration"] - target) <= 4.0, f"{info['duration']:.1f}s"))
        failures.append(not check("concat kept the audio track (ASMR)", vp.has_audio_stream(vertical)))

        artwork_img = cv2.imread(artwork)
        failures.append(not check("artwork comes from the scan", artwork_img is not None))
        if artwork_img is not None:
            height, width = artwork_img.shape[:2]
            failures.append(not check("artwork is 1080 wide and uncropped", (width, height) == (1080, 810),
                                      f"{width}x{height}"))

        failures.append(not check("no raw clip left on disk",
                                  not os.path.exists(os.path.join(work_root, "proj1_20261121_sushi"))
                                  or not any(
                                      name.endswith((".mp4", ".mov", ".avi"))
                                      for name in os.listdir(os.path.join(work_root, "proj1_20261121_sushi"))
                                  )))
        biggest = max(
            (os.path.getsize(os.path.join(out_dir, name)) for name in os.listdir(out_dir)
             if os.path.isfile(os.path.join(out_dir, name))),
            default=0,
        )
        failures.append(not check("no multi-hundred-MB intermediate in output/",
                                  biggest < 120 * 1024 * 1024, f"largest {biggest / 1024 / 1024:.1f} MB"))

        names = sorted(name for name, _ in uploaded)
        failures.append(not check("four artifacts uploaded to 02_READY_TO_POST",
                                  names == [f"{base}_art_metadata.json", f"{base}_artwork.jpg",
                                            f"{base}_final.mp4", f"{base}_metadata.txt"], str(names)))
        failures.append(not check("the GO_ folder itself was archived",
                                  moved == [("proj1", "archive-folder", "raw-folder")], str(moved)))
        failures.append(not check("state records the archived folder",
                                  ds.is_processed(ds.load_state(state_path), "proj1")))
        failures.append(not check("outputs use the prefix-free stem",
                                  os.path.exists(final) and os.path.exists(artwork)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return 0 if not any(failures) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="End-to-end check of the GO_ project pipeline")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg",
                        help="path to the ffmpeg binary (default: $PATH)")
    args = parser.parse_args()
    if not os.path.exists(args.ffmpeg):
        print(f"ffmpeg not found: {args.ffmpeg}", file=sys.stderr)
        raise SystemExit(2)
    code = main_check(args.ffmpeg)
    print("\nRESULT:", "PASS" if code == 0 else "FAIL")
    raise SystemExit(code)
