import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pandas as pd

import drive_sync
import improvement
import main
import pdca_state
import telop
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
        self.assertEqual(config["youtube"]["default_privacy"], "public")
        self.assertTrue(config["schedule"]["enabled"])
        self.assertEqual(config["schedule"]["min_interval_hours"], 48)
        self.assertEqual(config["schedule"]["max_videos_per_run"], 1)
        weights = sum(style["weight"] for style in config["evolution"]["styles"].values())
        self.assertAlmostEqual(weights, 1.0, places=6)

    def test_only_asmr_keeps_audio(self):
        self.assertTrue(main.should_keep_audio("style_C_asmr"))
        self.assertFalse(main.should_keep_audio("style_A_material"))
        self.assertFalse(main.should_keep_audio("style_B_story"))


class OutputFolderTests(unittest.TestCase):
    def test_the_package_folder_is_the_artwork_name_plus_the_jst_minute(self):
        jst = timezone(timedelta(hours=9))
        with tempfile.TemporaryDirectory() as tmp:
            path, name = main.get_output_dir(
                "20261121_sushi", tmp, now=datetime(2026, 11, 21, 18, 30, tzinfo=jst)
            )
            self.assertEqual(name, "20261121_sushi_20261121_1830")
            self.assertEqual(path, os.path.join(tmp, name))
            self.assertTrue(os.path.isdir(path))

    def test_a_second_render_in_the_same_minute_gets_its_own_folder(self):
        jst = timezone(timedelta(hours=9))
        now = datetime(2026, 11, 21, 18, 30, tzinfo=jst)
        with tempfile.TemporaryDirectory() as tmp:
            first, first_name = main.get_output_dir("20261121_sushi", tmp, now=now)
            second, second_name = main.get_output_dir("20261121_sushi", tmp, now=now)
            self.assertNotEqual(first, second)
            self.assertEqual(first_name, "20261121_sushi_20261121_1830")
            self.assertEqual(second_name, "20261121_sushi_20261121_1830_2")
            self.assertTrue(os.path.isdir(second))

    def test_an_unsafe_name_becomes_a_plain_folder_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, name = main.get_output_dir(" GO / sushi ", tmp)
            self.assertNotIn(" ", name)
            self.assertNotIn("/", name)
            self.assertTrue(name.startswith("GO"))
            self.assertRegex(name, r"_\d{8}_\d{4}$")

    def test_an_empty_name_falls_back_to_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, name = main.get_output_dir("", tmp)
            self.assertTrue(name.startswith("artwork_"))


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

    def test_without_cues_the_narration_is_still_muxed(self):
        cmd = main.build_mux_command("v.mp4", "a.mp3", "s.srt", "final.mp4", with_subtitles=False)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertNotIn("subtitles=", fc)
        self.assertIn("apad", fc)
        self.assertIn("-shortest", cmd)

    def test_an_empty_srt_is_not_burned(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        empty = os.path.join(tmp.name, "empty.srt")
        with open(empty, "w", encoding="utf-8"):
            pass
        self.assertFalse(main.subtitles_available(empty))
        with mock.patch("subprocess.run") as run:
            main.mux_audio_and_subtitles("v.mp4", "a.mp3", empty, "f.mp4")
        cmd = run.call_args[0][0]
        self.assertNotIn("subtitles=", cmd[cmd.index("-filter_complex") + 1])

    def test_a_filled_srt_is_burned(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        srt = os.path.join(tmp.name, "cues.srt")
        with open(srt, "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\nWait\n\n")
        self.assertTrue(main.subtitles_available(srt))
        with mock.patch("subprocess.run") as run:
            main.mux_audio_and_subtitles("v.mp4", "a.mp3", srt, "f.mp4")
        cmd = run.call_args[0][0]
        self.assertIn("subtitles=", cmd[cmd.index("-filter_complex") + 1])

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


class TelopIntegrationTests(unittest.TestCase):
    """The ASS telop replaces the plain SRT burn, and the SRT is still written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.out = self.tmp.name

    def _words(self):
        return [
            {"text": "The", "start": 0.4, "end": 0.7},
            {"text": "secret", "start": 0.75, "end": 1.2},
            {"text": "layer", "start": 1.25, "end": 1.7},
        ]

    def test_the_telop_is_written_next_to_the_voice(self):
        telop.write_word_timings(os.path.join(self.out, "clip_words.json"), self._words())
        path = main.write_telop(
            base_name="clip",
            output_dir=self.out,
            script_data=SCRIPT,
            config=self.config,
            video_seconds=12.0,
        )
        self.assertEqual(path, os.path.join(self.out, "clip.ass"))
        with open(path, "r", encoding="utf-8") as handle:
            ass = handle.read()
        self.assertIn("Style: Telop", ass)
        self.assertIn("One tiny", ass)                    # the hook (cleaned)
        self.assertIn("postcard", ass)
        self.assertIn("CrayonJapan", ass)                 # the fixed title
        # the closing line wraps into two lines, so check both halves
        self.assertIn("Follow for more", ass)
        self.assertIn("oil pastel art!", ass)

    def test_switching_the_telop_off_leaves_the_plain_srt_path(self):
        config = dict(self.config)
        config["telop"] = dict(config.get("telop") or {}, enabled=False)
        self.assertEqual(
            main.write_telop("clip", self.out, SCRIPT, config, video_seconds=9.0), ""
        )

    def test_the_second_config_override_names_the_font(self):
        config = dict(self.config)
        config["telop"] = dict(config.get("telop") or {}, font="DejaVu Sans")
        main.write_telop("clip", self.out, SCRIPT, config, video_seconds=9.0)
        with open(os.path.join(self.out, "clip.ass"), "r", encoding="utf-8") as handle:
            self.assertIn("Style: Telop,DejaVu Sans,", handle.read())

    def test_the_mux_burns_the_ass_when_it_is_given(self):
        cmd = main.build_mux_command(
            "v.mp4", "a.mp3", "s.srt", "final.mp4", ass_path="clip.ass"
        )
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("ass=filename='clip.ass'", fc)
        self.assertNotIn("subtitles=", fc)
        self.assertIn("apad", fc)

    def test_the_ass_filter_escapes_a_windows_path(self):
        vf = main.build_ass_filter("C:\\work\\clip.ass")
        self.assertIn("ass=filename='C\\:/work/clip.ass'", vf)
        # The bundled Japanese face travels with the repository, so libass is
        # pointed at its folder explicitly.
        self.assertIn("fontsdir=", vf)

    def test_an_ass_file_is_preferred_over_the_srt(self):
        ass_path = os.path.join(self.tmp.name, "clip.ass")
        with open(ass_path, "w", encoding="utf-8") as handle:
            handle.write("[Events]\n")
        with mock.patch("subprocess.run") as run:
            main.mux_audio_and_subtitles("v.mp4", "a.mp3", "s.srt", "f.mp4", ass_path=ass_path)
        cmd = run.call_args[0][0]
        self.assertIn("ass=", cmd[cmd.index("-filter_complex") + 1])

    def test_a_missing_ass_falls_back_to_the_srt(self):
        srt = os.path.join(self.tmp.name, "cues.srt")
        with open(srt, "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\nWait\n\n")
        with mock.patch("subprocess.run") as run:
            main.mux_audio_and_subtitles(
                "v.mp4", "a.mp3", srt, "f.mp4", ass_path="/nope/missing.ass"
            )
        cmd = run.call_args[0][0]
        self.assertIn("subtitles=", cmd[cmd.index("-filter_complex") + 1])


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
    "title_ja": "オイルパステルのポストカード #Shorts",
    "hook_ja": "ちいさなポストカード1枚",
    "narration_ja": "このポストカードは、まっ白な紙から始まりました。",
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
        package = bundle["output_dir"]
        self.assertEqual(os.path.dirname(package), self.out)
        self.assertRegex(bundle["output_folder"], r"^clip_\d{8}_\d{4}$")
        self.assertEqual(bundle["output_folder"], os.path.basename(package))
        self.assertEqual(bundle["final_video"], os.path.join(package, "clip_final.mp4"))
        self.assertEqual(bundle["artwork"], os.path.join(package, "clip_artwork.jpg"))
        self.assertTrue(os.path.exists(bundle["artwork"]))
        self.assertTrue(os.path.exists(bundle["metadata_text"]))
        self.assertTrue(os.path.exists(bundle["metadata_json"]))
        timelapse.assert_called_once()
        voice.assert_called_once()

    def test_artwork_comes_from_the_raw_recording_not_the_vertical_crop(self):
        bundle, _, artwork, _, _, _ = self._run()
        self.assertEqual(artwork.call_args.args[0], "input/clip.mp4")
        self.assertEqual(
            artwork.call_args.args[1], os.path.join(bundle["output_dir"], "clip_artwork.jpg")
        )

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

    def test_metadata_text_pack_carries_the_japanese_narration(self):
        bundle, _, _, _, _, _ = self._run()
        with open(bundle["metadata_text"], "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("=== NARRATION (EN) ===", text)
        self.assertIn(SCRIPT["narration"], text)
        self.assertIn("=== NARRATION (JA) ===", text)
        self.assertIn(SCRIPT["narration_ja"], text)
        self.assertIn(SCRIPT["title_ja"], text)
        self.assertIn(SCRIPT["hook_ja"], text)

    def test_the_pack_carries_the_overseas_sns_captions_without_gemini_fields(self):
        """The script has no overseas sentence here: the block is still built."""
        bundle, _, _, _, _, _ = self._run()
        with open(bundle["metadata_text"], "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("=== OVERSEAS SNS CAPTIONS (TikTok / Instagram Reels / Pinterest) ===", text)
        self.assertIn("=== OVERSEAS SNS CAPTIONS (JA / 日本語訳) ===", text)
        for keyword in ("Oil Pastel", "Sakura Cray-Pas"):
            self.assertIn(keyword, text)
        for tag in ("#oilpastel", "#sakuracraypas", "#crayonart", "#artprocess"):
            self.assertIn(tag, text)
        self.assertIn("--- Pinterest ---", text)
        self.assertIn("https://crayonjapan.base.shop/", text)

    def test_the_upload_description_carries_the_shop_call_to_action(self):
        _, _, _, _, upload_short, _ = self._run(upload=True)
        description = upload_short.call_args.args[1]["description"]
        self.assertIn("Original artwork available at:", description)
        self.assertIn("https://crayonjapan.base.shop/", description)
        self.assertIn("1点ものの原画はこちらで販売中です", description)

    def test_the_pack_description_carries_the_shop_call_to_action(self):
        bundle, _, _, _, _, _ = self._run()
        with open(bundle["metadata_text"], "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Original artwork available at:", text)
        self.assertIn("1点ものの原画はこちらで販売中です", text)

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

    def test_each_render_gets_its_own_package_folder(self):
        first, _, _, _, _, _ = self._run()
        second, _, _, _, _, _ = self._run()
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertEqual(os.path.dirname(first["output_dir"]), self.out)
        self.assertEqual(os.path.dirname(second["output_dir"]), self.out)
        # The second render did not write over the first one's package.
        self.assertTrue(os.path.exists(first["metadata_text"]))
        self.assertTrue(os.path.exists(second["metadata_text"]))

    def test_process_new_video_still_returns_the_video_id(self):
        with mock.patch.object(main, "process_video_bundle", return_value={"video_id": "yt-9"}):
            self.assertEqual(main.process_new_video("input/clip.mp4", self.config), "yt-9")


class ProcessProjectBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.out = os.path.join(self.tmp.name, "output")
        self.work = os.path.join(self.tmp.name, "work")
        self.fetched = []
        self.runner_cmds = []

    def _clip(self, file_id, name, duration=120.0, size=1024):
        return drive_sync.DriveFileRef(id=file_id, name=name, size=size, duration_seconds=duration)

    def _source(self, videos=None, images=None, stem="20261121_sushi"):
        clips = videos if videos is not None else [
            self._clip("v1", "part1.mp4", 120.0),
            self._clip("v2", "part2.mp4", 240.0),
        ]

        def fetch(file_id, path):
            self.fetched.append((file_id, path))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"raw-bytes")
            return path

        return drive_sync.ProjectSource(
            item_id="folder1",
            name=f"GO_{stem}",
            stem=stem,
            kind=drive_sync.KIND_PROJECT,
            work_dir=self.work,
            videos=clips,
            images=images or [],
            fetch=fetch,
        )

    def _runner(self, cmd):
        self.runner_cmds.append(list(cmd))
        if "concat" in cmd:
            list_path = cmd[cmd.index("-i") + 1]
            if os.path.exists(list_path):
                with open(list_path, "r", encoding="utf-8") as handle:
                    self.concat_order = [
                        line.rsplit("/", 1)[-1].rstrip("'")
                        for line in handle.read().splitlines()
                    ]
        target = cmd[-1]
        if isinstance(target, str) and target.endswith(".mp4"):
            with open(target, "wb") as handle:
                handle.write(b"segment")

    def _run(self, source, keep_local_raw=False):
        def fake_artwork(video_path, output_path, offset_seconds=0.0):
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "wb") as handle:
                handle.write(b"jpeg")
            self.artwork_source = video_path
            return output_path

        def fake_scan(image_path, output_path):
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "wb") as handle:
                handle.write(b"scan-jpeg")
            self.scan_source = image_path
            return output_path

        with (
            mock.patch.object(main, "choose_best_style", return_value="style_B_story"),
            mock.patch.object(main, "has_audio_stream", return_value=True),
            mock.patch.object(
                main, "probe_duration",
                side_effect=lambda path: 120.0 if path.endswith("part1.mp4") else 240.0,
            ),
            mock.patch.object(main, "extract_final_artwork", side_effect=fake_artwork),
            mock.patch.object(main, "prepare_scanned_artwork", side_effect=fake_scan),
            mock.patch.object(main, "extract_keyframes", return_value=[]) as keyframes,
            mock.patch.object(main, "generate_making_script", return_value=dict(SCRIPT)) as script,
            mock.patch.object(main.asyncio, "run"),
            mock.patch.object(main, "probe_audio_seconds", return_value=38.0) as audio_length,
            mock.patch.object(main, "assemble_finale", side_effect=self._finale) as finale,
            mock.patch.object(main, "mux_audio_and_subtitles"),
            mock.patch.object(main, "append_performance_row"),
            mock.patch.object(main, "ensure_disk_space"),
        ):
            bundle = main.process_project_bundle(
                source, self.config, output_dir=self.out, upload=False,
                keep_local_raw=keep_local_raw, runner=self._runner,
            )
        self.keyframes_source = keyframes.call_args.args[0]
        self.finale_call = finale.call_args
        self.audio_length = audio_length
        return bundle, script

    def test_output_names_come_from_the_folder_without_the_go_prefix(self):
        bundle, _ = self._run(self._source())
        package = bundle["output_dir"]
        self.assertEqual(bundle["base_name"], "20261121_sushi")
        self.assertRegex(bundle["output_folder"], r"^20261121_sushi_\d{8}_\d{4}$")
        self.assertEqual(os.path.dirname(package), self.out)
        self.assertEqual(bundle["final_video"], os.path.join(package, "20261121_sushi_final.mp4"))
        self.assertEqual(bundle["artwork"], os.path.join(package, "20261121_sushi_artwork.jpg"))
        self.assertEqual(bundle["metadata_text"], os.path.join(package, "20261121_sushi_metadata.txt"))
        self.assertEqual(bundle["clip_count"], 2)

    def _finale(self, base_video, artwork_path, output_path, target_total,
                narration_seconds=None, runner=None):
        self.finale_kwargs = {
            "base_video": base_video,
            "artwork_path": artwork_path,
            "output_path": output_path,
            "target_total": target_total,
            "narration_seconds": narration_seconds,
        }
        with open(output_path, "wb") as handle:
            handle.write(b"zoomed")
        return {
            "base_seconds": 41.0,
            "zoom_outro_seconds": 4.0,
            "total_seconds": 45.0,
            "zoom_outro": 1,
        }

    def test_the_hook_is_cut_from_the_last_clip_and_comes_first(self):
        self._run(self._source())
        self.assertIn("concat", self.runner_cmds[-1])
        # flash of the finished piece -> its last seconds -> the drawing -> finish
        self.assertEqual(len(self.concat_order), 5)
        self.assertIn("flash", self.concat_order[0])
        self.assertIn("hook", self.concat_order[1])
        self.assertIn("seg00", self.concat_order[2])
        self.assertIn("seg01", self.concat_order[3])
        self.assertIn("finish", self.concat_order[4])

    def test_the_cut_keeps_room_for_the_slow_finish_and_the_zoom(self):
        params = improvement.config_params(self.config)
        bundle, _ = self._run(self._source())
        plan = bundle["plan"]
        finish_screen = params["finish_hold_seconds"] / 0.75
        hook_total = max(
            params["hook_duration_sec"] - params["hook_flash_seconds"], 0.0
        ) + params["hook_flash_seconds"]
        self.assertAlmostEqual(
            plan["main_duration"],
            params["target_duration_sec"] - finish_screen - 4.0 - hook_total,
            places=3,
        )
        self.assertAlmostEqual(
            plan["main_duration"] + hook_total + finish_screen + 4.0,
            params["target_duration_sec"],
            places=3,
        )

    def test_the_last_moment_is_replayed_slowly_before_the_zoom(self):
        params = improvement.config_params(self.config)
        self._run(self._source())
        # Two seeks: the hook tail and the finish shot, both from the tail.
        cuts = [
            cmd for cmd in self.runner_cmds
            if "-ss" in cmd and "-vf" in cmd and "concat" not in cmd
        ]
        self.assertEqual(len(cuts), 2)
        finish = next(cmd for cmd in cuts if "setpts" in cmd[cmd.index("-vf") + 1])
        hold = params["finish_hold_seconds"]
        self.assertAlmostEqual(float(finish[finish.index("-ss") + 1]), 240.0 - hold, places=3)
        self.assertAlmostEqual(float(finish[finish.index("-t") + 1]), hold, places=3)
        vf = finish[finish.index("-vf") + 1]
        self.assertTrue(vf.startswith("setpts=1.333333*PTS,"))
        self.assertTrue(finish[finish.index("-i") + 1].endswith("part2.mp4"))

    def test_the_zoom_is_sized_from_the_measured_narration(self):
        params = improvement.config_params(self.config)
        bundle, _ = self._run(self._source())
        self.assertEqual(
            self.finale_kwargs["base_video"],
            os.path.join(bundle["output_dir"], "20261121_sushi_vertical_base.mp4"),
        )
        self.assertEqual(self.finale_kwargs["output_path"], bundle["vertical"])
        self.assertEqual(self.finale_kwargs["artwork_path"], bundle["artwork"])
        self.assertAlmostEqual(
            self.finale_kwargs["target_total"], params["target_duration_sec"]
        )
        self.assertAlmostEqual(self.finale_kwargs["narration_seconds"], 38.0)
        self.assertEqual(self.keyframes_source, self.finale_kwargs["base_video"])
        self.assertFalse(os.path.exists(self.finale_kwargs["base_video"]))
        self.assertEqual(bundle["finale"]["zoom_outro"], 1)

    def test_each_clip_is_downloaded_once_and_deleted_again(self):
        self._run(self._source())
        self.assertEqual([call[0] for call in self.fetched], ["v1", "v2"])
        for _, path in self.fetched:
            self.assertFalse(os.path.exists(path), path)
        self.assertFalse(os.path.exists(self.work))

    def test_a_scanned_image_becomes_the_artwork_and_the_gemini_reference(self):
        scan = self._clip("i1", "scan.jpg", duration=None, size=500)
        bundle, script = self._run(self._source(images=[scan]))
        self.assertTrue(bundle["artwork_from_scan"])
        self.assertEqual([call[0] for call in self.fetched], ["i1", "v1", "v2"])
        self.assertTrue(self.scan_source.endswith("scan.jpg"))
        with open(bundle["artwork"], "rb") as handle:
            self.assertEqual(handle.read(), b"scan-jpeg")
        frames = script.call_args.args[0]
        self.assertEqual(frames[0], bundle["artwork"])
        self.assertTrue(script.call_args.kwargs["reference_scan"])

    def test_without_a_scan_the_artwork_comes_from_the_last_clip(self):
        bundle, script = self._run(self._source())
        self.assertFalse(bundle["artwork_from_scan"])
        self.assertTrue(self.artwork_source.endswith("part2.mp4"))
        self.assertFalse(script.call_args.kwargs["reference_scan"])

    def test_clip_proportions_follow_their_lengths(self):
        bundle, _ = self._run(self._source())
        plan = bundle["plan"]
        self.assertAlmostEqual(
            plan["segment_durations"][1] / plan["segment_durations"][0], 2.0
        )
        self.assertAlmostEqual(sum(plan["segment_durations"]), plan["main_duration"])

    def test_keep_local_raw_keeps_the_downloaded_clips(self):
        self._run(self._source(), keep_local_raw=True)
        self.assertEqual(len(os.listdir(self.work)), 2)

    def test_a_shorter_final_clip_does_not_get_a_longer_hook(self):
        videos = [self._clip("v1", "part1.mp4", 120.0), self._clip("v2", "finish.mp4", 2.0)]
        with mock.patch.object(main, "probe_duration", return_value=2.0):
            self._run(self._source(videos=videos))
        hook = next(cmd for cmd in self.runner_cmds if "-ss" in cmd)
        self.assertEqual(hook[hook.index("-t") + 1], "2.000")
        self.assertEqual(hook[hook.index("-ss") + 1], "0.000")


class DriveModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))

    def _source(self, kind="video", videos=None, images=None, name="drawing.mp4"):
        work_dir = os.path.join(self.tmp.name, "work")
        writes = []

        def fetch(file_id, path):
            writes.append((file_id, path))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"raw")
            return path

        clips = videos if videos is not None else [drive_sync.DriveFileRef(id="v1", name=name)]
        return drive_sync.ProjectSource(
            item_id="item1",
            name=("GO_20261121_sushi" if kind == "project" else name),
            stem=("20261121_sushi" if kind == "project" else os.path.splitext(name)[0]),
            kind=kind,
            work_dir=work_dir,
            videos=clips,
            images=images or [],
            fetch=fetch,
        ), writes

    def test_missing_folder_ids_exit_with_a_hint(self):
        args = mock.Mock(no_upload=True, artwork_offset=0.0, drive_state="s.json",
                         drive_min_age=60, drive_once=True, drive_interval=None,
                         drive_watch=False, ignore_schedule=True)
        with mock.patch.object(drive_sync, "load_drive_folders", side_effect=drive_sync.MissingFolderIdError("DRIVE_RAW_FOLDER_ID is not set")), \
                mock.patch.object(drive_sync, "build_drive_service") as build_service:
            self.assertEqual(main.run_drive_mode(args, self.config), 2)
        build_service.assert_not_called()

    def test_drive_once_passes_the_pipeline_and_reports(self):
        args = mock.Mock(no_upload=True, artwork_offset=1.5, drive_state="s.json",
                         drive_min_age=30, drive_once=True, drive_interval=None,
                         drive_watch=False, ignore_schedule=True)
        folders = drive_sync.DriveFolders("raw", "ready", "archive")
        source, writes = self._source()
        with mock.patch.object(drive_sync, "load_drive_folders", return_value=folders), \
                mock.patch.object(drive_sync, "build_drive_service", return_value=object()), \
                mock.patch.object(drive_sync, "sync_once", return_value=[{"status": "done"}]) as sync, \
                mock.patch.object(main, "process_video_bundle") as bundle, \
                mock.patch.object(main, "ensure_disk_space"):
            self.assertEqual(main.run_drive_mode(args, self.config), 0)

            pipeline = sync.call_args.args[2]
            pipeline(source)
            bundle.assert_called_once_with(
                writes[0][1], self.config, upload=False,
                artwork_offset_seconds=1.5, base_name="drawing",
            )
            self.assertEqual(writes[0][0], "v1")
            self.assertFalse(os.path.exists(writes[0][1]))  # raw copy removed again
            self.assertEqual(sync.call_args.kwargs["min_age_seconds"], 30)

    def test_a_project_source_goes_to_the_project_pipeline(self):
        args = mock.Mock(no_upload=True, artwork_offset=0.0, drive_state="s.json",
                         drive_min_age=30, drive_once=True, drive_interval=None,
                         drive_watch=False, ignore_schedule=True)
        folders = drive_sync.DriveFolders("raw", "ready", "archive")
        source, writes = self._source(kind="project")
        with mock.patch.object(drive_sync, "load_drive_folders", return_value=folders), \
                mock.patch.object(drive_sync, "build_drive_service", return_value=object()), \
                mock.patch.object(drive_sync, "sync_once", return_value=[{"status": "done"}]) as sync, \
                mock.patch.object(main, "process_project_bundle", return_value={}) as project:
            main.run_drive_mode(args, self.config)
            pipeline = sync.call_args.args[2]
            pipeline(source)
        project.assert_called_once_with(
            source, self.config, upload=False, artwork_offset_seconds=0.0
        )
        self.assertEqual(writes, [])


class ScheduleSettingsTests(unittest.TestCase):
    def test_the_shipped_config_posts_one_short_every_48_hours(self):
        settings = main.schedule_settings(main.load_config(os.path.join(ROOT, "config.yaml")))
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["min_interval_hours"], 48.0)
        self.assertEqual(settings["max_videos_per_run"], 1)

    def test_a_config_without_the_block_keeps_rendering_everything(self):
        self.assertEqual(
            main.schedule_settings({}),
            {"enabled": False, "min_interval_hours": 48.0, "max_videos_per_run": 1},
        )

    def test_broken_numbers_fall_back_instead_of_crashing(self):
        settings = main.schedule_settings(
            {"schedule": {"enabled": True, "min_interval_hours": "soon", "max_videos_per_run": "one"}}
        )
        self.assertEqual(settings["min_interval_hours"], 48.0)
        self.assertEqual(settings["max_videos_per_run"], 1)

    def test_a_zero_or_negative_cap_still_makes_one_video(self):
        for value in (0, -3):
            self.assertEqual(
                main.schedule_settings({"schedule": {"max_videos_per_run": value}})[
                    "max_videos_per_run"
                ],
                1,
            )

    def test_the_numbers_may_arrive_as_text(self):
        settings = main.schedule_settings(
            {"schedule": {"enabled": True, "min_interval_hours": "36", "max_videos_per_run": "2"}}
        )
        self.assertEqual(settings["min_interval_hours"], 36.0)
        self.assertEqual(settings["max_videos_per_run"], 2)


class PostIntervalTests(unittest.TestCase):
    NOW = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.logs = []

    def _state(self, latest=main._UNKNOWN, now=None, config=None, loader=None):
        return main.post_interval_state(
            config or self.config,
            now=now or self.NOW,
            latest=latest,
            loader=loader,
            log=self.logs.append,
        )

    def test_47_hours_after_the_last_post_is_too_soon(self):
        state = self._state(self.NOW - timedelta(hours=47))
        self.assertFalse(state["allowed"])
        self.assertEqual(state["reason"], "too-soon")
        self.assertAlmostEqual(state["hours_elapsed"], 47.0)
        self.assertAlmostEqual(state["hours_left"], 1.0)
        self.assertTrue(any("Nothing is rendered or posted" in line for line in self.logs))

    def test_the_first_hour_is_too_soon(self):
        state = self._state(self.NOW - timedelta(minutes=30))
        self.assertFalse(state["allowed"])
        self.assertAlmostEqual(state["hours_left"], 47.5)

    def test_exactly_48_hours_lets_one_short_go(self):
        state = self._state(self.NOW - timedelta(hours=48))
        self.assertTrue(state["allowed"])
        self.assertEqual(state["reason"], "interval-passed")

    def test_a_long_gap_lets_one_short_go(self):
        state = self._state(self.NOW - timedelta(days=4, hours=2))
        self.assertTrue(state["allowed"])
        self.assertAlmostEqual(state["hours_elapsed"], 98.0)

    def test_a_channel_with_no_upload_lets_the_first_short_go(self):
        state = self._state(None)
        self.assertTrue(state["allowed"])
        self.assertEqual(state["reason"], "no-upload-yet")

    def test_the_channel_reader_is_used_when_no_time_is_handed_in(self):
        state = self._state(main._UNKNOWN, loader=lambda: self.NOW - timedelta(hours=10))
        self.assertFalse(state["allowed"])
        self.assertAlmostEqual(state["hours_left"], 38.0)

    def test_an_unreadable_channel_holds_the_run(self):
        def boom():
            raise RuntimeError("401 invalid_credentials")

        state = self._state(main._UNKNOWN, loader=boom)
        self.assertFalse(state["allowed"])
        self.assertEqual(state["reason"], "unknown-latest-upload")
        self.assertTrue(any("401 invalid_credentials" in line for line in self.logs))

    def test_a_disabled_schedule_never_holds_the_run(self):
        config = dict(self.config)
        config["schedule"] = {"enabled": False, "min_interval_hours": 48}
        state = self._state(self.NOW - timedelta(minutes=5), config=config)
        self.assertTrue(state["allowed"])
        self.assertEqual(state["reason"], "schedule disabled")

    def test_a_naive_clock_is_read_as_utc(self):
        state = self._state((self.NOW - timedelta(hours=12)).replace(tzinfo=None))
        self.assertFalse(state["allowed"])
        self.assertAlmostEqual(state["hours_left"], 36.0)

    def test_the_interval_can_be_measured_from_an_aware_jst_clock(self):
        jst = timezone(timedelta(hours=9))
        now_jst = self.NOW.astimezone(jst)
        state = self._state(self.NOW - timedelta(hours=50), now=now_jst)
        self.assertTrue(state["allowed"])
        self.assertAlmostEqual(state["hours_elapsed"], 50.0)


class DriveOnceScheduleTests(unittest.TestCase):
    def setUp(self):
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.folders = drive_sync.DriveFolders("raw", "ready", "archive")

    def _args(self, **overrides):
        values = dict(
            no_upload=False, artwork_offset=0.0, drive_state="s.json",
            drive_min_age=60, drive_go_min_age=5, drive_once=True,
            drive_interval=None, drive_watch=False, ignore_schedule=False,
        )
        values.update(overrides)
        return mock.Mock(**values)

    def _drive(self, args, gate=None, sync_results=None):
        gate = gate if gate is not None else {
            "allowed": True,
            "reason": "interval-passed",
            "hours_elapsed": 50.0,
            "hours_left": None,
            "settings": main.schedule_settings(self.config),
        }
        with mock.patch.object(main, "post_interval_state", return_value=gate) as gate_mock, \
                mock.patch.object(drive_sync, "load_drive_folders", return_value=self.folders) as folders, \
                mock.patch.object(drive_sync, "build_drive_service", return_value=object()), \
                mock.patch.object(drive_sync, "watch", return_value=[]) as watch, \
                mock.patch.object(
                    drive_sync, "sync_once", return_value=sync_results or [{"status": "done"}]
                ) as sync:
            code = main.run_drive_mode(args, self.config)
        return code, gate_mock, folders, sync, watch

    def test_too_soon_exits_cleanly_without_touching_drive(self):
        code, gate, folders, sync, _ = self._drive(
            self._args(),
            gate={"allowed": False, "reason": "too-soon", "hours_left": 1.0},
        )
        self.assertEqual(code, 0)
        gate.assert_called_once()
        folders.assert_not_called()
        sync.assert_not_called()

    def test_an_unknown_latest_upload_fails_the_run_instead_of_posting(self):
        code, gate, folders, sync, _ = self._drive(
            self._args(),
            gate={"allowed": False, "reason": "unknown-latest-upload"},
        )
        self.assertEqual(code, 1)
        folders.assert_not_called()
        sync.assert_not_called()

    def test_the_interval_lets_one_video_through_per_run(self):
        code, _, _, sync, _ = self._drive(self._args())
        self.assertEqual(code, 0)
        self.assertEqual(sync.call_args.kwargs["max_items"], 1)

    def test_ignore_schedule_skips_the_gate_and_the_cap(self):
        code, gate, _, sync, _ = self._drive(self._args(ignore_schedule=True))
        self.assertEqual(code, 0)
        gate.assert_not_called()
        self.assertIsNone(sync.call_args.kwargs["max_items"])

    def test_a_disabled_schedule_is_not_even_asked(self):
        config = dict(self.config)
        config["schedule"] = {"enabled": False}
        self.config = config
        code, gate, _, sync, _ = self._drive(self._args())
        self.assertEqual(code, 0)
        gate.assert_not_called()
        self.assertIsNone(sync.call_args.kwargs["max_items"])

    def test_the_watcher_is_not_capped(self):
        code, gate, _, sync, watch = self._drive(self._args(drive_once=False, drive_watch=True))
        self.assertEqual(code, 0)
        gate.assert_not_called()
        sync.assert_not_called()
        watch.assert_called_once()
        self.assertIsNone(watch.call_args.kwargs["max_items"])

    def test_a_render_only_run_is_not_gated(self):
        code, gate, _, sync, _ = self._drive(self._args(no_upload=True))
        self.assertEqual(code, 0)
        gate.assert_not_called()
        self.assertIsNone(sync.call_args.kwargs["max_items"])

    def test_the_ignore_schedule_flag_reaches_drive_mode(self):
        with mock.patch.object(main, "run_drive_mode", return_value=0) as drive, \
                mock.patch.object(main, "load_config", return_value=self.config), \
                mock.patch("dotenv.load_dotenv"):
            main.main(["--drive-once", "--ignore-schedule"])
        args = drive.call_args.args[0]
        self.assertTrue(args.drive_once)
        self.assertTrue(args.ignore_schedule)


if __name__ == "__main__":
    unittest.main()

class RunParameterTests(unittest.TestCase):
    """Requirements 1, 2, 3 and 5: the run's parameters reach the render."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.out = os.path.join(self.tmp.name, "output")
        improvement.clear_active_params()

    def tearDown(self):
        improvement.clear_active_params()

    def _run(self, upload=False, script=None):
        with mock.patch.object(main, "choose_best_style", return_value="style_B_story"), \
                mock.patch.object(main, "build_vertical_timelapse") as timelapse, \
                mock.patch.object(main, "extract_final_artwork", side_effect=fake_artwork), \
                mock.patch.object(main, "extract_keyframes", return_value=["kf.jpg"]), \
                mock.patch.object(
                    main, "generate_making_script", return_value=dict(script or SCRIPT)
                ) as gemini, \
                mock.patch.object(main, "synthesize_voice") as voice, \
                mock.patch.object(main.asyncio, "run") as ran, \
                mock.patch.object(main, "probe_audio_seconds", return_value=14.0), \
                mock.patch.object(main, "probe_duration", return_value=28.0), \
                mock.patch.object(main, "mux_audio_and_subtitles") as mux, \
                mock.patch.object(main, "upload_short", return_value="yt-9"), \
                mock.patch.object(main, "append_performance_row") as row:
            bundle = main.process_video_bundle(
                "input/clip.mp4", self.config, output_dir=self.out, upload=upload
            )
        return bundle, timelapse, gemini, voice, ran, mux, row

    def test_the_short_is_rendered_at_the_new_length_and_hook(self):
        bundle, timelapse, _, _, _, _, _ = self._run()
        kwargs = timelapse.call_args.kwargs
        self.assertAlmostEqual(kwargs["target_duration"], 28.0)
        self.assertEqual(kwargs["hook_style"], "finish_flash_to_blank")
        self.assertAlmostEqual(kwargs["hook_duration"], 2.4)
        self.assertAlmostEqual(kwargs["flash_seconds"], 0.4)
        self.assertEqual(bundle["params"]["language_mode"], "bilingual")

    def test_a_pinned_parameter_set_changes_the_render(self):
        improvement.set_active_params(
            {
                "target_duration_sec": 24,
                "hook_style": "text_only",
                "language_mode": "ja",
                "tts_mode": "none",
                "drawing_audio_volume": 0.9,
            }
        )
        _, timelapse, _, voice, ran, mux, _ = self._run()
        kwargs = timelapse.call_args.kwargs
        self.assertAlmostEqual(kwargs["target_duration"], 24.0)
        self.assertEqual(kwargs["hook_style"], "text_only")
        # tts_mode none: no edge-tts call, and a silent track keeps the mux valid
        ran.assert_not_called()
        self.assertFalse(voice.called)
        self.assertTrue(os.path.exists(self._last_audio()))
        self.assertAlmostEqual(mux.call_args.kwargs["original_volume"], 0.9)

    def _last_audio(self):
        for name in os.listdir(self.out):
            folder = os.path.join(self.out, name)
            if os.path.isdir(folder):
                for item in os.listdir(folder):
                    if item.endswith(".mp3"):
                        return os.path.join(folder, item)
        return os.path.join(self.out, "missing.mp3")

    def test_a_japanese_short_is_spoken_by_the_japanese_voice(self):
        improvement.set_active_params({"language_mode": "ja", "tts_mode": "short"})
        _, _, _, voice, ran, _, _ = self._run()
        self.assertTrue(ran.called)
        args, kwargs = voice.call_args.args, voice.call_args.kwargs
        self.assertEqual(kwargs["voice"], main.TTS_VOICE_JA)
        self.assertIn("まっ白な紙", args[0])

    def test_the_english_mode_keeps_the_english_voice(self):
        improvement.set_active_params({"language_mode": "en"})
        _, _, _, voice, _, _, _ = self._run()
        self.assertEqual(voice.call_args.kwargs["voice"], main.TTS_VOICE)
        self.assertIn("postcard", voice.call_args.args[0])

    def test_the_youtube_row_records_what_the_short_was_made_with(self):
        improvement.set_active_params({"hook_style": "finish_flash_to_blank", "language_mode": "ja"})
        _, _, _, _, _, _, row = self._run(upload=True)
        kwargs = row.call_args.kwargs
        self.assertEqual(kwargs["params"]["language_mode"], "ja")
        self.assertAlmostEqual(kwargs["duration_sec"], 28.0)

    def test_the_prompt_is_given_the_run_parameters(self):
        improvement.set_active_params({"target_duration_sec": 24, "language_mode": "ja"})
        _, _, gemini, _, _, _, _ = self._run()
        self.assertEqual(gemini.call_args.kwargs["params"]["language_mode"], "ja")

    def test_a_japanese_script_on_screen_uses_the_japanese_hook(self):
        improvement.set_active_params({"language_mode": "ja"})
        bundle, _, _, _, _, _, _ = self._run()
        with open(bundle["telop"], "r", encoding="utf-8") as handle:
            ass = handle.read()
        self.assertIn("ちいさな", ass)
        self.assertIn("次は何を描いてほしい", ass)   # the closing question
        self.assertIn("Noto Sans CJK JP", ass)

    def test_the_english_mode_keeps_the_english_hook_on_screen(self):
        improvement.set_active_params({"language_mode": "en"})
        bundle, _, _, _, _, _, _ = self._run()
        with open(bundle["telop"], "r", encoding="utf-8") as handle:
            ass = handle.read()
        self.assertIn("One tiny", ass)
        self.assertIn("postcard", ass)
        self.assertIn("What should I draw", ass)
        self.assertNotIn("次は何を描いてほしい", ass)

    def test_the_bilingual_mode_shows_both_and_never_drops_the_shop_link(self):
        bundle, _, _, _, _, _, _ = self._run(upload=True)
        with open(bundle["telop"], "r", encoding="utf-8") as handle:
            ass = handle.read()
        self.assertIn("ちいさな", ass)
        self.assertIn("One tiny", ass)
        self.assertIn("crayonjapan.base.shop", bundle["script"]["description"])
        self.assertTrue(
            any("crayonjapan.base.shop" in str(value) for value in bundle["script"].values())
        )


class SilentTrackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_silent_track_is_written_with_ffmpeg(self):
        path = os.path.join(self.tmp.name, "silence.mp3")
        with mock.patch.object(main.subprocess, "run") as run:
            main.write_silent_audio(path, 0.5)
        cmd = run.call_args.args[0]
        self.assertIn("anullsrc", " ".join(cmd))
        self.assertEqual(run.call_args.kwargs.get("check"), True)


class LearningLoopTests(unittest.TestCase):
    """Requirement 5: collect -> reflect -> render -> save, end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.folders = drive_sync.DriveFolders("raw", "ready", "archive")
        improvement.clear_active_params()

    def tearDown(self):
        improvement.clear_active_params()

    def test_prepare_restores_collects_and_pins_the_parameters(self):
        order = []

        def fake_restore(service, folders, **kwargs):
            order.append("restore")
            return {}

        def fake_update(*args, **kwargs):
            order.append(("collect", kwargs["min_age_hours"], kwargs["max_rows"]))
            return None

        def fake_decide(csv_path, config, log=print):
            order.append("reflect")
            return {"target_duration_sec": 26.0}, {"status": "gemini"}

        with mock.patch.object(pdca_state, "restore_all", side_effect=fake_restore), \
                mock.patch.object(main, "update_performance_metrics", side_effect=fake_update), \
                mock.patch.object(improvement, "decide_params", side_effect=fake_decide):
            params, review = main.learning_prepare(
                self.config, object(), self.folders, csv_path=self.csv, log=lambda *_: None
            )

        self.assertEqual([step if not isinstance(step, tuple) else step[0] for step in order],
                         ["restore", "collect", "reflect"])
        self.assertEqual(order[1][1], 24)
        self.assertEqual(order[1][2], 8)
        self.assertEqual(review["status"], "gemini")
        self.assertEqual(improvement.active_params(self.config)["target_duration_sec"], 26.0)

    def test_a_broken_restore_or_collect_does_not_stop_the_run(self):
        logs = []
        with mock.patch.object(pdca_state, "restore_all", side_effect=RuntimeError("drive")), \
                mock.patch.object(main, "update_performance_metrics", side_effect=RuntimeError("quota")), \
                mock.patch.object(
                    improvement, "decide_params",
                    return_value=(improvement.config_params(self.config), {"status": "not-enough-data"}),
                ):
            params, review = main.learning_prepare(
                self.config, object(), self.folders, csv_path=self.csv, log=logs.append
            )
        self.assertEqual(review["status"], "not-enough-data")
        self.assertTrue(any("drive" in line for line in logs))
        self.assertTrue(any("quota" in line for line in logs))

    def test_a_disabled_improvement_block_uses_the_config_defaults(self):
        config = dict(self.config)
        config["improvement"] = dict(config["improvement"], enabled=False)
        with mock.patch.object(pdca_state, "restore_all") as restore:
            params, review = main.learning_prepare(
                config, object(), self.folders, csv_path=self.csv, log=lambda *_: None
            )
        restore.assert_not_called()
        self.assertEqual(review["status"], "disabled")
        self.assertEqual(params["language_mode"], "bilingual")

    def test_finish_writes_the_history_and_pushes_it_to_drive(self):
        results = [
            {"status": "done", "name": "GO_a", "package": "a_1",
             "artifacts": {"video_id": "yt-1"}},
        ]
        with mock.patch.object(pdca_state, "backup_all") as backup:
            main.learning_finish(
                results, object(), self.folders,
                params=improvement.validate_params(),
                review={"status": "gemini", "diagnosis": "d"},
                csv_path=self.csv,
            )
        backup.assert_called_once()
        state = improvement.load_state()
        self.assertEqual(state["review"]["status"], "gemini")
        self.assertEqual(state["history"][-1]["videos"][0]["video_id"], "yt-1")

    def test_finish_without_a_publication_keeps_drive_untouched(self):
        with mock.patch.object(pdca_state, "backup_all") as backup:
            main.learning_finish(
                [{"status": "failed"}], object(), self.folders,
                params=improvement.validate_params(), review={}, csv_path=self.csv,
            )
        backup.assert_not_called()
