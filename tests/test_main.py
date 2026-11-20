import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

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


if __name__ == "__main__":
    unittest.main()
