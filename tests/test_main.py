import json
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import drive_sync
import main
from analytics_updater import CSV_COLUMNS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigTests(unittest.TestCase):
    def test_config_contents(self):
        config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.assertEqual(config["brand"]["name"], "CrayonJapan")
        self.assertEqual(config["brand"]["shop_url"], "https://crayonjapan.base.shop/")
        self.assertEqual(config["video"]["width"], 1080)
        self.assertEqual(config["video"]["height"], 1920)
        self.assertEqual(config["video"]["aspect_ratio"], "9:16")
        self.assertEqual(config["youtube"]["default_privacy"], "unlisted")
        weights = sum(style["weight"] for style in config["evolution"]["styles"].values())
        self.assertAlmostEqual(weights, 1.0, places=6)

    def test_only_asmr_keeps_audio(self):
        self.assertTrue(main.should_keep_audio("style_C_asmr"))
        self.assertFalse(main.should_keep_audio("style_A_material"))
        self.assertFalse(main.should_keep_audio("style_B_story"))


class SubtitleTests(unittest.TestCase):
    def test_filter_has_style_and_escaping(self):
        vf = main.build_subtitle_filter(os.path.join("output", "clip.srt"))
        self.assertIn("subtitles='", vf)
        self.assertIn("Fontsize=18", vf)
        self.assertIn("Alignment=2", vf)

    def test_windows_path_is_escaped(self):
        vf = main.build_subtitle_filter("C:\\work\\clip.srt")
        self.assertIn("C\\:/work/clip.srt", vf)
        self.assertNotIn("C\\work", vf)


class MuxCommandTests(unittest.TestCase):
    def test_narration_only_command(self):
        cmd = main.build_mux_command("v.mp4", "a.mp3", "s.srt", "final.mp4")
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("apad", fc)
        self.assertNotIn("amix", fc)
        self.assertIn("-shortest", cmd)
        self.assertIn("aac", cmd)
        self.assertEqual(cmd[-1], "final.mp4")

    def test_mix_command_mixes_original_drawing_sound(self):
        cmd = main.build_mux_command(
            "v.mp4", "a.mp3", "s.srt", "final.mp4", mix_original_audio=True
        )
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("amix=inputs=2", fc)
        self.assertIn("normalize=0", fc)
        self.assertIn("volume=0.55", fc)
        self.assertIn("[orig]", fc)
        self.assertIn("[v]", fc)
        self.assertIn("[a]", fc)

    def test_mux_uses_mix_only_when_video_has_audio(self):
        with mock.patch.object(main, "has_audio_stream", return_value=True), \
                mock.patch("subprocess.run") as run:
            main.mux_audio_and_subtitles("v.mp4", "a.mp3", "s.srt", "f.mp4", keep_original_audio=True)
        cmd = run.call_args[0][0]
        self.assertIn("amix", cmd[cmd.index("-filter_complex") + 1])

        with mock.patch.object(main, "has_audio_stream", return_value=False), \
                mock.patch("subprocess.run") as run:
            main.mux_audio_and_subtitles("v.mp4", "a.mp3", "s.srt", "f.mp4", keep_original_audio=True)
        cmd = run.call_args[0][0]
        self.assertNotIn("amix", cmd[cmd.index("-filter_complex") + 1])


class PerformanceRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "analytics", "performance.csv")

    SCRIPT = {"title": "Oil pastel postcard #Shorts", "description": "d"}

    def test_creates_csv_with_header_and_row(self):
        df = main.append_performance_row(self.SCRIPT, "style_B_story", "vid1", self.csv)
        self.assertEqual(list(df.columns), CSV_COLUMNS)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.loc[0, "style_used"], "style_B_story")
        self.assertEqual(df.loc[0, "hook_type"], main.HOOK_TYPE)
        self.assertEqual(float(df.loc[0, "avg_view_percentage"]), 0.0)
        self.assertTrue(os.path.exists(self.csv))

    def test_appends_without_losing_previous_rows(self):
        main.append_performance_row(self.SCRIPT, "style_A_material", "vid1", self.csv)
        df = main.append_performance_row(self.SCRIPT, "style_C_asmr", "vid2", self.csv)
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df["video_id"]), ["vid1", "vid2"])

        saved = pd.read_csv(self.csv)
        self.assertEqual(list(saved["video_id"]), ["vid1", "vid2"])
        self.assertEqual(list(saved.columns), CSV_COLUMNS)

    def test_records_given_date(self):
        df = main.append_performance_row(
            self.SCRIPT, "style_A_material", "vid1", self.csv, date="2026-11-20"
        )
        self.assertEqual(df.loc[0, "date"], "2026-11-20")


SCRIPT = {
    "title": "Oil pastel postcard #Shorts",
    "hook_text": "One tiny postcard",
    "narration": "This postcard started as an empty sheet.",
    "description": "Original postcard at CrayonJapan.",
    "tags": ["oil pastel", "postcard"],
}


def fake_artwork(video_path, output_image_path, offset_seconds=0.0):
    os.makedirs(os.path.dirname(output_image_path) or ".", exist_ok=True)
    with open(output_image_path, "wb") as handle:
        handle.write(b"jpeg")
    return output_image_path


class ProcessVideoBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.out = os.path.join(self.tmp.name, "output")

    def _run(self, upload=False, **patches):
        with mock.patch.object(main, "choose_best_style", return_value="style_B_story"), \
                mock.patch.object(main, "build_vertical_timelapse") as timelapse, \
                mock.patch.object(main, "extract_final_artwork", side_effect=fake_artwork) as artwork, \
                mock.patch.object(main, "extract_keyframes", return_value=["kf.jpg"]), \
                mock.patch.object(main, "generate_making_script", return_value=dict(SCRIPT)) as script, \
                mock.patch.object(main, "synthesize_voice", new=mock.MagicMock()) as narration, \
                mock.patch.object(main.asyncio, "run") as voice, \
                mock.patch.object(main, "mux_audio_and_subtitles"), \
                mock.patch.object(main, "upload_short", return_value="yt-1") as upload_short, \
                mock.patch.object(main, "append_performance_row") as row:
            bundle = main.process_video_bundle(
                "input/clip.mp4", self.config, output_dir=self.out, upload=upload
            )
        return bundle, timelapse, artwork, voice, upload_short, row

    def test_bundle_returns_every_artifact_path(self):
        bundle, timelapse, artwork, voice, upload_short, row = self._run()
        self.assertEqual(bundle["base_name"], "clip")
        self.assertEqual(bundle["style_key"], "style_B_story")
        self.assertEqual(bundle["final_video"], os.path.join(self.out, "clip_final.mp4"))
        self.assertEqual(bundle["artwork"], os.path.join(self.out, "clip_artwork.jpg"))
        self.assertTrue(os.path.exists(bundle["artwork"]))
        self.assertTrue(os.path.exists(bundle["metadata_text"]))
        self.assertTrue(os.path.exists(bundle["metadata_json"]))
        timelapse.assert_called_once()
        voice.assert_called_once()

    def test_artwork_comes_from_the_raw_recording_not_the_vertical_crop(self):
        _, _, artwork, _, _, _ = self._run()
        self.assertEqual(artwork.call_args.args[0], "input/clip.mp4")
        self.assertEqual(artwork.call_args.args[1], os.path.join(self.out, "clip_artwork.jpg"))

    def test_metadata_bundle_holds_the_english_upload_pack(self):
        bundle, _, _, _, _, _ = self._run()
        with open(bundle["metadata_json"], "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        self.assertEqual(metadata["artwork_title"], SCRIPT["title"])
        self.assertEqual(metadata["style"], "style_B_story")
        self.assertEqual(metadata["material"], "Sakura Cray-Pas Specialist")
        self.assertEqual(metadata["shop_url"], "https://crayonjapan.base.shop/")
        self.assertEqual(metadata["artwork_image"], "clip_artwork.jpg")
        self.assertEqual(metadata["short_video"], "clip_final.mp4")

    def test_no_upload_skips_youtube_and_the_csv_row(self):
        bundle, _, _, _, upload_short, row = self._run(upload=False)
        upload_short.assert_not_called()
        row.assert_not_called()
        self.assertEqual(bundle["video_id"], "")

    def test_upload_records_the_video_id(self):
        bundle, _, _, _, upload_short, row = self._run(upload=True)
        upload_short.assert_called_once()
        row.assert_called_once()
        self.assertEqual(bundle["video_id"], "yt-1")

    def test_process_new_video_still_returns_the_video_id(self):
        with mock.patch.object(main, "process_video_bundle", return_value={"video_id": "yt-9"}):
            self.assertEqual(main.process_new_video("input/clip.mp4", self.config), "yt-9")


class DriveModeTests(unittest.TestCase):
    def test_missing_folder_ids_exit_with_a_hint(self):
        args = mock.Mock(no_upload=True, artwork_offset=0.0, drive_state="s.json",
                         drive_min_age=60, drive_once=True, drive_interval=None)
        config = main.load_config(os.path.join(ROOT, "config.yaml"))
        with mock.patch.object(drive_sync, "load_drive_folders", side_effect=drive_sync.MissingFolderIdError("DRIVE_RAW_FOLDER_ID is not set")), \
                mock.patch.object(drive_sync, "build_drive_service") as build_service:
            self.assertEqual(main.run_drive_mode(args, config), 2)
        build_service.assert_not_called()

    def test_drive_once_passes_the_pipeline_and_reports(self):
        args = mock.Mock(no_upload=True, artwork_offset=1.5, drive_state="s.json",
                         drive_min_age=30, drive_once=True, drive_interval=None)
        config = main.load_config(os.path.join(ROOT, "config.yaml"))
        folders = drive_sync.DriveFolders("raw", "ready", "archive")
        with mock.patch.object(drive_sync, "load_drive_folders", return_value=folders), \
                mock.patch.object(drive_sync, "build_drive_service", return_value=object()), \
                mock.patch.object(drive_sync, "sync_once", return_value=[{"status": "done"}]) as sync, \
                mock.patch.object(main, "process_video_bundle") as bundle:
            self.assertEqual(main.run_drive_mode(args, config), 0)

            pipeline = sync.call_args.args[2]
            pipeline("input/clip.mp4")
            bundle.assert_called_once_with(
                "input/clip.mp4", config, upload=False, artwork_offset_seconds=1.5
            )
            self.assertEqual(sync.call_args.kwargs["min_age_seconds"], 30)


if __name__ == "__main__":
    unittest.main()