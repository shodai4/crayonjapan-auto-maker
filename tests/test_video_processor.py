import os
import tempfile
import unittest
from unittest import mock

import video_processor as vp


class AtempoTests(unittest.TestCase):
    def _factors(self, speed):
        chain = vp.build_atempo_filter(speed)
        factors = []
        for part in chain.split(","):
            self.assertTrue(part.startswith("atempo="), chain)
            factors.append(float(part.split("=", 1)[1]))
        return factors

    def test_single_stage_when_in_range(self):
        self.assertEqual(len(self._factors(1.25)), 1)
        self.assertAlmostEqual(self._factors(0.75)[0], 0.75)

    def test_chain_product_equals_speed_and_stages_in_range(self):
        for speed in [0.25, 0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 3.4, 7.5, 10.0, 10.703, 25.0, 100.0]:
            factors = self._factors(speed)
            for factor in factors:
                self.assertGreaterEqual(factor, vp.ATEMPO_MIN - 1e-9)
                self.assertLessEqual(factor, vp.ATEMPO_MAX + 1e-9)
            product = 1.0
            for factor in factors:
                product *= factor
            self.assertAlmostEqual(product, speed, delta=max(speed, 1.0) * 1e-9)

    def test_rejects_invalid_speed(self):
        for bad in [0, -1, 0.0, float("nan"), float("inf"), "fast", None]:
            with self.assertRaises(ValueError):
                vp.build_atempo_filter(bad)


class TimelapsePlanTests(unittest.TestCase):
    def test_normal_recording(self):
        plan = vp.compute_timelapse_plan(380.0, target_duration=38.0, hook_duration=2.5)
        self.assertAlmostEqual(plan["hook_start"], 376.0)
        self.assertAlmostEqual(plan["hook_duration"], 2.5)
        self.assertAlmostEqual(plan["main_duration"], 35.5)
        self.assertAlmostEqual(plan["speed_factor"], 380.0 / 35.5)
        self.assertAlmostEqual(plan["pts_multiplier"], 35.5 / 380.0)

    def test_short_recording_is_not_slowed_down(self):
        plan = vp.compute_timelapse_plan(5.0, target_duration=38.0, hook_duration=2.5)
        self.assertAlmostEqual(plan["main_duration"], 35.5)
        self.assertLess(plan["speed_factor"], 1.0)
        self.assertAlmostEqual(plan["pts_multiplier"], 1.0)
        self.assertAlmostEqual(plan["hook_start"], 1.0)

    def test_min_main_duration_floor_applies(self):
        plan = vp.compute_timelapse_plan(120.0, target_duration=5.0, hook_duration=2.5)
        self.assertAlmostEqual(plan["main_duration"], vp.MIN_MAIN_DURATION)

    def test_short_source_keeps_audio_at_natural_speed(self):
        plan = vp.compute_timelapse_plan(20.0, target_duration=38.0, hook_duration=2.5)
        self.assertAlmostEqual(plan["pts_multiplier"], 1.0)
        self.assertAlmostEqual(plan["audio_speed_factor"], 1.0)

    def test_zero_duration_is_clamped(self):
        plan = vp.compute_timelapse_plan(0)
        self.assertGreaterEqual(plan["orig_duration"], 1.0)


class CommandTests(unittest.TestCase):
    def test_hook_is_muted_by_default(self):
        cmd = vp.build_hook_command("in.mp4", "hook.mp4", 10.0, 2.5)
        self.assertIn("-an", cmd)
        self.assertNotIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-vf") + 1], vp.CROP_FILTER)
        self.assertEqual(cmd[-1], "hook.mp4")
        self.assertIn("libx264", cmd)

    def test_hook_keeps_audio_on_request(self):
        cmd = vp.build_hook_command("in.mp4", "hook.mp4", 10.0, 2.5, keep_audio=True)
        self.assertNotIn("-an", cmd)
        self.assertIn("aac", cmd)

    def test_main_is_sped_up_and_muted_by_default(self):
        cmd = vp.build_main_command("in.mp4", "main.mp4", 0.1, 10.0)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertTrue(vf.startswith("setpts=0.100000*PTS,"))
        self.assertTrue(vf.endswith(vp.CROP_FILTER))
        self.assertIn("-an", cmd)
        self.assertNotIn("-af", cmd)

    def test_main_applies_atempo_chain_when_keeping_audio(self):
        cmd = vp.build_main_command("in.mp4", "main.mp4", 0.1, 10.0, keep_audio=True)
        self.assertNotIn("-an", cmd)
        af = cmd[cmd.index("-af") + 1]
        self.assertEqual(af, vp.build_atempo_filter(10.0))
        self.assertIn("aac", cmd)

    def test_short_source_audio_is_not_slowed_down(self):
        cmd = vp.build_main_command("in.mp4", "main.mp4", 1.0, 0.5634, keep_audio=True)
        af = cmd[cmd.index("-af") + 1]
        self.assertEqual(af, vp.build_atempo_filter(1.0))
        self.assertNotIn("atempo=0.563400", af)

    def test_concat_list_uses_absolute_paths(self):
        content = vp.build_concat_list_content("hook.mp4", "main.mp4")
        self.assertEqual(
            content,
            f"file '{os.path.abspath('hook.mp4')}'\nfile '{os.path.abspath('main.mp4')}'\n",
        )
        cmd = vp.build_concat_command("list.txt", "out.mp4")
        self.assertIn("concat", cmd)
        self.assertIn("copy", cmd)


class ProjectPlanTests(unittest.TestCase):
    def test_segments_split_the_main_duration_proportionally(self):
        plan = vp.compute_project_plan([600.0, 600.0, 30.0], target_duration=38.0, hook_duration=2.5)
        self.assertEqual(plan["clip_count"], 3)
        self.assertAlmostEqual(plan["total_duration"], 1230.0)
        self.assertAlmostEqual(plan["main_duration"], 35.5)
        self.assertAlmostEqual(sum(plan["segment_durations"]), plan["main_duration"], places=6)
        # Proportional: the two long clips share most of the Short.
        self.assertAlmostEqual(
            plan["segment_durations"][0] / plan["segment_durations"][2], 600.0 / 30.0
        )
        self.assertFalse(plan["estimated"])

    def test_every_clip_shares_one_time_lapse_speed(self):
        plan = vp.compute_project_plan([300.0, 900.0], target_duration=38.0, hook_duration=2.5)
        pts = plan["pts_multiplier"]
        self.assertAlmostEqual(plan["segment_durations"][0], 300.0 * pts)
        self.assertAlmostEqual(plan["segment_durations"][1], 900.0 * pts)
        self.assertAlmostEqual(pts, 35.5 / 1200.0)

    def test_the_hook_is_the_tail_of_the_last_clip(self):
        plan = vp.compute_project_plan([120.0, 60.0], target_duration=38.0, hook_duration=2.5)
        self.assertAlmostEqual(plan["hook_start"], 56.0)
        self.assertAlmostEqual(plan["hook_duration"], 2.5)

    def test_a_last_clip_shorter_than_the_hook_is_not_overrun(self):
        plan = vp.compute_project_plan([120.0, 2.0], target_duration=38.0, hook_duration=2.5)
        self.assertAlmostEqual(plan["hook_duration"], 2.0)
        self.assertAlmostEqual(plan["hook_start"], 0.0)

    def test_unknown_durations_are_estimated_and_flagged(self):
        plan = vp.compute_project_plan([600.0, None], target_duration=38.0, hook_duration=2.5)
        self.assertTrue(plan["estimated"])
        self.assertAlmostEqual(plan["total_duration"], 1200.0)
        self.assertAlmostEqual(plan["segment_durations"][0], plan["segment_durations"][1])

    def test_a_short_project_is_not_slowed_down(self):
        plan = vp.compute_project_plan([5.0], target_duration=38.0, hook_duration=2.5)
        self.assertAlmostEqual(plan["pts_multiplier"], 1.0)
        self.assertAlmostEqual(plan["segment_durations"][0], 5.0)

    def test_no_clips_is_rejected(self):
        with self.assertRaises(ValueError):
            vp.compute_project_plan([])


class ProjectCommandTests(unittest.TestCase):
    def test_fit_filter_letterboxes_without_cropping(self):
        vf = vp.build_fit_filter()
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", vf)
        self.assertIn("pad=1080:1920", vf)
        self.assertIn("fps=30", vf)
        self.assertIn("setsar=1", vf)
        self.assertNotIn("crop", vf)

    def test_segment_speeds_the_clip_up_and_mutes_by_default(self):
        cmd = vp.build_segment_video_command("in.mp4", "seg.mp4", 0.05)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertTrue(vf.startswith("setpts=0.050000*PTS,"))
        self.assertIn("force_original_aspect_ratio=decrease", vf)
        self.assertIn("-an", cmd)
        self.assertNotIn("-af", cmd)
        self.assertNotIn("anullsrc", cmd)
        self.assertIn("yuv420p", cmd)
        self.assertEqual(cmd[-1], "seg.mp4")

    def test_hook_segment_is_not_sped_up(self):
        cmd = vp.build_segment_video_command("last.mp4", "hook.mp4", seek=56.0, length=2.5)
        self.assertIn("-ss", cmd)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "56.000")
        self.assertEqual(cmd[cmd.index("-t") + 1], "2.500")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertNotIn("setpts", vf)
        self.assertIn("fps=30", vf)
        self.assertIn("-an", cmd)

    def test_a_clip_whose_speed_is_unknown_keeps_natural_speed(self):
        for bad in (0, -1, float("nan"), "fast", None):
            with self.subTest(bad=bad):
                cmd = vp.build_segment_video_command("in.mp4", "seg.mp4", bad)
                self.assertNotIn("setpts", cmd[cmd.index("-vf") + 1])


class SegmentRenderTests(unittest.TestCase):
    """ASMR segments are three safe passes: picture, sound, then a join.

    A single command with `-af "...apad"` plus `-shortest` stayed on frame=0
    for an hour (2026-11-21), so the split is a regression guard, not a detail.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "seg.mp4")
        self.commands = []

    def _render(self, **kwargs):
        def runner(cmd):
            self.commands.append(list(cmd))
            target = cmd[-1]
            if isinstance(target, str) and target.endswith(".mp4"):
                with open(target, "wb") as handle:
                    handle.write(b"segment")
        return vp.render_project_segment("in.mp4", self.out, runner=runner, **kwargs)

    def test_asmr_segment_is_picture_sound_and_join(self):
        result = self._render(pts_multiplier=0.05, keep_audio=True, has_audio=True)
        self.assertEqual(result, self.out)
        self.assertEqual(len(self.commands), 3)
        picture, sound, join = self.commands

        vf = picture[picture.index("-vf") + 1]
        self.assertTrue(vf.startswith("setpts=0.050000*PTS,"))
        self.assertIn("force_original_aspect_ratio=decrease", vf)
        self.assertIn("-an", picture)
        self.assertNotIn("-af", picture)

        self.assertIn("-vn", sound)
        self.assertEqual(sound[sound.index("-af") + 1], vp.build_atempo_filter(20.0))
        self.assertIn("0:a:0", sound)
        self.assertIn("aac", sound)

        self.assertEqual(join[join.index("-c:v") + 1], "copy")
        self.assertIn("aac", join)
        self.assertIn("-shortest", join)
        self.assertEqual(join[-1], self.out)

    def test_no_call_ever_combines_apad_with_shortest(self):
        self._render(pts_multiplier=0.02, keep_audio=True, has_audio=True)
        for cmd in self.commands:
            self.assertFalse(any("apad" in str(part) for part in cmd), cmd)
            if "-shortest" in cmd:
                self.assertNotIn("-af", cmd)

    def test_a_silent_clip_gets_a_bounded_silent_bed_when_audio_is_kept(self):
        self._render(pts_multiplier=0.05, keep_audio=True, has_audio=False, duration=30.0)
        picture, sound, join = self.commands
        self.assertTrue(any("anullsrc" in str(part) for part in sound))
        self.assertIn("0:a:0", sound)
        # An endless bed would never finish on its own, so it is cut to length.
        self.assertEqual(sound[sound.index("-t") + 1], "30.000")
        self.assertNotIn("-af", sound)  # a silent bed only has to be long enough
        self.assertIn("-shortest", join)

    def test_a_hook_without_sound_gets_the_hook_length_as_its_bed(self):
        self._render(keep_audio=True, has_audio=False, seek=56.0, length=2.5)
        sound = self.commands[1]
        self.assertEqual(sound[sound.index("-t") + 1], "2.500")

    def test_the_join_temporaries_are_removed(self):
        self._render(pts_multiplier=0.05, keep_audio=True, has_audio=True)
        self.assertEqual(os.listdir(self.tmp.name), ["seg.mp4"])

    def test_a_muted_segment_is_still_a_single_pass(self):
        self._render(pts_multiplier=0.05, keep_audio=False, has_audio=True)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("-an", self.commands[0])
        self.assertNotIn("-vn", self.commands[0])

    def test_the_hook_is_cut_at_natural_speed_and_keeps_its_sound(self):
        self._render(keep_audio=True, has_audio=True, seek=56.0, length=2.5)
        picture, sound, join = self.commands
        self.assertEqual(picture[picture.index("-ss") + 1], "56.000")
        self.assertEqual(picture[picture.index("-t") + 1], "2.500")
        self.assertNotIn("setpts", picture[picture.index("-vf") + 1])
        self.assertEqual(sound[sound.index("-ss") + 1], "56.000")
        self.assertEqual(sound[sound.index("-t") + 1], "2.500")
        self.assertNotIn("-af", sound)  # natural speed needs no atempo
        self.assertIn("-shortest", join)

    def test_concat_list_holds_every_segment_in_order(self):
        content = vp.build_segments_concat_list_content(["hook.mp4", "seg0.mp4", "seg1.mp4"])
        lines = content.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].endswith("hook.mp4'"))
        self.assertTrue(lines[2].endswith("seg1.mp4'"))


class ConcatSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "final.mp4")
        self.commands = []

    def test_runs_one_concat_and_cleans_the_list_file(self):
        segments = []
        for index in range(2):
            path = os.path.join(self.tmp.name, f"seg{index}.mp4")
            with open(path, "wb") as handle:
                handle.write(b"x")
            segments.append(path)

        def runner(cmd):
            self.commands.append(list(cmd))
            with open(self.out, "wb") as handle:
                handle.write(b"joined")

        vp.concat_segments(segments, self.out, runner=runner)
        self.assertEqual(len(self.commands), 1)
        cmd = self.commands[0]
        self.assertIn("concat", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("copy", cmd)
        self.assertEqual(cmd[-1], self.out)
        self.assertEqual(
            [name for name in os.listdir(self.tmp.name) if name.endswith(".txt")], []
        )

    def test_an_empty_concat_is_rejected(self):
        with self.assertRaises(ValueError):
            vp.concat_segments([], self.out)

    def test_a_concat_that_produces_nothing_raises(self):
        with self.assertRaises(RuntimeError):
            vp.concat_segments(["seg.mp4"], self.out, runner=lambda cmd: None)


class FinalePlanTests(unittest.TestCase):
    """The closing slow finish + zoom: lengths must always fit a Short."""

    def test_the_finish_shot_slows_the_tail_of_the_last_clip(self):
        window = vp.compute_finish_window(240.0)
        self.assertAlmostEqual(window["source_seconds"], 4.0)
        self.assertAlmostEqual(window["start"], 236.0)
        self.assertAlmostEqual(window["screen_seconds"], 4.0 / 0.75, places=3)
        self.assertAlmostEqual(window["pts_multiplier"], 1.333333, places=6)

    def test_a_clip_shorter_than_the_hold_is_shown_from_its_start(self):
        window = vp.compute_finish_window(2.0)
        self.assertAlmostEqual(window["source_seconds"], 2.0)
        self.assertAlmostEqual(window["start"], 0.0)

    def test_an_unknown_length_still_gives_a_usable_window(self):
        for bad in (None, 0, -5, "fast", float("nan")):
            with self.subTest(bad=bad):
                window = vp.compute_finish_window(bad)
                self.assertGreater(window["source_seconds"], 0)
                self.assertGreater(window["screen_seconds"], 0)

    def test_the_planned_total_is_kept_when_the_voice_fits(self):
        self.assertAlmostEqual(vp.compute_finale_total(45.0, 38.0), 45.0)
        self.assertAlmostEqual(vp.compute_finale_total(45.0, None), 45.0)

    def test_a_longer_voice_extends_the_picture(self):
        self.assertAlmostEqual(vp.compute_finale_total(45.0, 47.0), 48.0)

    def test_the_total_never_passes_the_shorts_limit(self):
        self.assertAlmostEqual(vp.compute_finale_total(45.0, 70.0), vp.SHORTS_MAX_SECONDS)

    def test_the_zoom_fills_the_gap_up_to_its_range(self):
        self.assertAlmostEqual(vp.compute_zoom_outro_seconds(41.0, 45.0), 4.0)
        self.assertAlmostEqual(vp.compute_zoom_outro_seconds(44.0, 45.0), vp.ZOOM_OUTRO_MIN_SECONDS)
        self.assertAlmostEqual(vp.compute_zoom_outro_seconds(30.0, 45.0), vp.ZOOM_OUTRO_MAX_SECONDS)


class ZoomOutroCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_scan_is_fitted_so_the_zoom_never_crops_it(self):
        vf = vp.build_zoom_outro_filter(4.0)
        inner_w = int(1080 / vp.ZOOM_OUTRO_END) // 2 * 2
        inner_h = int(1920 / vp.ZOOM_OUTRO_END) // 2 * 2
        self.assertIn(f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease", vf)
        self.assertIn("pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=white", vf)
        self.assertIn("zoompan=z='1.0000+0.3000*on/119'", vf)
        self.assertIn("s=1080x1920", vf)
        self.assertIn("fps=30", vf)
        self.assertTrue(vf.endswith("format=yuv420p"))

    def test_the_command_carries_a_silent_bed_for_the_concat(self):
        cmd = vp.build_zoom_outro_command("art.jpg", "zoom.mp4", duration=4.0)
        self.assertEqual(cmd[cmd.index("-i") + 1], "art.jpg")
        self.assertTrue(any("anullsrc" in str(part) for part in cmd))
        self.assertIn("48000", " ".join(cmd))
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "120")
        self.assertIn("libx264", cmd)
        self.assertIn("aac", cmd)
        self.assertIn("-shortest", cmd)
        self.assertEqual(cmd[-1], "zoom.mp4")

    def test_a_longer_zoom_renders_more_frames(self):
        cmd = vp.build_zoom_outro_command("art.jpg", "zoom.mp4", duration=6.0)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "180")

    def test_create_zoom_outro_reports_an_empty_render(self):
        with self.assertRaises(RuntimeError):
            vp.create_zoom_outro("art.jpg", os.path.join(self.tmp.name, "z.mp4"),
                                 runner=lambda cmd: None)

    def test_assemble_finale_appends_the_zoom_and_cleans_up(self):
        base = os.path.join(self.tmp.name, "cut_base.mp4")
        with open(base, "wb") as handle:
            handle.write(b"cut")
        artwork = os.path.join(self.tmp.name, "art_artwork.jpg")
        with open(artwork, "wb") as handle:
            handle.write(b"jpeg")
        out = os.path.join(self.tmp.name, "cut_vertical.mp4")
        commands = []

        def runner(cmd):
            commands.append(list(cmd))
            target = cmd[-1]
            if isinstance(target, str) and target.endswith(".mp4"):
                with open(target, "wb") as handle:
                    handle.write(b"segment")

        with mock.patch.object(vp, "probe_duration", return_value=41.0):
            plan = vp.assemble_finale(
                base, artwork, out, target_total=45.0, narration_seconds=44.5, runner=runner
            )

        self.assertEqual(plan["zoom_outro"], 1)
        self.assertAlmostEqual(plan["zoom_outro_seconds"], 4.5)
        self.assertAlmostEqual(plan["total_seconds"], 45.5)
        self.assertEqual(len(commands), 2)  # the zoom render, then one concat
        self.assertIn("zoompan", " ".join(commands[0]))
        self.assertIn("concat", commands[1])
        self.assertTrue(os.path.exists(out))
        self.assertEqual(
            [name for name in os.listdir(self.tmp.name) if name.endswith("_zoom.mp4")], []
        )

    def test_a_narration_that_cannot_fit_is_reported(self):
        base = os.path.join(self.tmp.name, "cut_base.mp4")
        with open(base, "wb") as handle:
            handle.write(b"cut")
        artwork = os.path.join(self.tmp.name, "art_artwork.jpg")
        with open(artwork, "wb") as handle:
            handle.write(b"jpeg")
        out = os.path.join(self.tmp.name, "cut_vertical.mp4")

        def runner(cmd):
            target = cmd[-1]
            if isinstance(target, str) and target.endswith(".mp4"):
                with open(target, "wb") as handle:
                    handle.write(b"segment")

        with mock.patch.object(vp, "probe_duration", return_value=41.0), \
                mock.patch("builtins.print") as printed:
            plan = vp.assemble_finale(
                base, artwork, out, target_total=45.0, narration_seconds=70.0, runner=runner
            )
        self.assertAlmostEqual(plan["zoom_outro_seconds"], vp.ZOOM_OUTRO_MAX_SECONDS)
        self.assertTrue(
            any("last words will be cut" in str(call.args[0]) for call in printed.call_args_list)
        )

    def test_a_missing_scan_falls_back_to_the_cut(self):
        base = os.path.join(self.tmp.name, "cut_base.mp4")
        with open(base, "wb") as handle:
            handle.write(b"cut")
        out = os.path.join(self.tmp.name, "cut_vertical.mp4")
        with mock.patch.object(vp, "probe_duration", return_value=41.0):
            plan = vp.assemble_finale(
                base, os.path.join(self.tmp.name, "missing.jpg"), out,
                target_total=45.0, runner=lambda cmd: None,
            )
        self.assertEqual(plan["zoom_outro"], 0)
        self.assertTrue(os.path.exists(out))


class DiskGuardTests(unittest.TestCase):
    def test_enough_space_passes(self):
        usage = mock.Mock(free=10 * 1024 ** 3)
        with mock.patch("shutil.disk_usage", return_value=usage):
            vp.ensure_disk_space(".", 6 * 1024 ** 3)

    def test_a_clip_that_would_fill_the_disk_is_refused(self):
        usage = mock.Mock(free=4 * 1024 ** 3)
        with mock.patch("shutil.disk_usage", return_value=usage):
            with self.assertRaises(RuntimeError) as ctx:
                vp.ensure_disk_space(".", 6 * 1024 ** 3)
        self.assertIn("free disk", str(ctx.exception))

    def test_unknown_size_is_not_blocked(self):
        usage = mock.Mock(free=0)
        with mock.patch("shutil.disk_usage", return_value=usage):
            vp.ensure_disk_space(".", 0)


class ScannedArtworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_large_scan_is_shrunk_uncropped_to_the_artwork_jpeg(self):
        import cv2
        import numpy as np

        source = os.path.join(self.tmp.name, "scan.png")
        target = os.path.join(self.tmp.name, "out_artwork.jpg")
        canvas = np.full((2400, 1600, 3), 200, dtype=np.uint8)
        cv2.imwrite(source, canvas)

        vp.prepare_scanned_artwork(source, target)

        written = cv2.imread(target)
        self.assertEqual(written.shape[0], 1620)
        self.assertEqual(written.shape[1], 1080)
        self.assertGreater(os.path.getsize(target), 0)

    def test_a_small_scan_is_not_upscaled(self):
        import cv2
        import numpy as np

        source = os.path.join(self.tmp.name, "small.jpg")
        target = os.path.join(self.tmp.name, "small_artwork.jpg")
        cv2.imwrite(source, np.full((300, 400, 3), 120, dtype=np.uint8))

        vp.prepare_scanned_artwork(source, target)
        self.assertEqual(cv2.imread(target).shape[:2], (300, 400))

    def test_a_transparent_png_is_flattened_onto_white(self):
        import cv2
        import numpy as np

        source = os.path.join(self.tmp.name, "clear.png")
        target = os.path.join(self.tmp.name, "clear_artwork.jpg")
        canvas = np.zeros((100, 100, 4), dtype=np.uint8)
        cv2.imwrite(source, canvas)

        vp.prepare_scanned_artwork(source, target)
        written = cv2.imread(target)
        self.assertEqual(written.shape[2], 3)
        self.assertGreater(int(written.mean()), 240)

    def test_an_unreadable_scan_raises(self):
        with self.assertRaises(RuntimeError):
            vp.prepare_scanned_artwork(
                os.path.join(self.tmp.name, "missing.jpg"),
                os.path.join(self.tmp.name, "out.jpg"),
            )


class BuildTimelapseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "clip_vertical.mp4")
        self.commands = []

        def runner(cmd):
            self.commands.append(list(cmd))

        self.runner = runner

    def _run(self, keep_audio, has_audio):
        with mock.patch.object(vp, "probe_duration", return_value=380.0), \
                mock.patch.object(vp, "has_audio_stream", return_value=has_audio):
            return vp.build_vertical_timelapse(
                "input/clip.mp4", self.out,
                target_duration=38.0, hook_duration=2.5,
                keep_audio=keep_audio, runner=self.runner,
            )

    def test_default_render_is_muted(self):
        result = self._run(keep_audio=False, has_audio=True)
        self.assertEqual(result, self.out)
        self.assertEqual(len(self.commands), 3)
        self.assertIn("-an", self.commands[0])
        self.assertIn("-an", self.commands[1])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "clip_vertical_list.txt")))

    def test_asmr_render_keeps_audio(self):
        self._run(keep_audio=True, has_audio=True)
        self.assertNotIn("-an", self.commands[0])
        self.assertNotIn("-an", self.commands[1])
        self.assertIn("-af", self.commands[1])

    def test_missing_source_audio_falls_back_to_muted(self):
        self._run(keep_audio=True, has_audio=False)
        self.assertIn("-an", self.commands[0])
        self.assertIn("-an", self.commands[1])

    def test_temp_files_are_removed(self):
        self._run(keep_audio=True, has_audio=True)
        leftovers = [name for name in os.listdir(self.tmp.name) if name != "clip_vertical.mp4"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()


class OpeningHookTests(unittest.TestCase):
    """Requirement 1: the finished piece flashes first, then the blank page."""

    def test_the_flash_style_splits_the_hook_into_flash_and_tail(self):
        plan = vp.compute_hook_plan("finish_flash_to_blank", 2.4, 0.4)
        self.assertAlmostEqual(plan["flash"], 0.4)
        self.assertAlmostEqual(plan["tail"], 2.0)
        self.assertAlmostEqual(plan["total"], 2.4)

    def test_the_previous_look_keeps_the_whole_tail(self):
        plan = vp.compute_hook_plan("finish_preview", 2.4, 0.4)
        self.assertEqual(plan["flash"], 0.0)
        self.assertAlmostEqual(plan["tail"], 2.4)

    def test_text_only_leaves_the_opening_to_the_time_lapse(self):
        plan = vp.compute_hook_plan("text_only", 2.4, 0.4)
        self.assertEqual(plan["total"], 0.0)
        self.assertEqual(plan["tail"], 0.0)

    def test_the_hook_never_runs_past_three_seconds(self):
        self.assertLessEqual(vp.compute_hook_plan("finish_preview", 9.0)["total"], 3.0)

    def test_the_tail_keeps_room_to_read_as_the_finished_piece(self):
        plan = vp.compute_hook_plan("finish_flash_to_blank", 2.0, 1.9)
        self.assertGreaterEqual(plan["tail"], vp.MIN_HOOK_TAIL_SECONDS)
        self.assertAlmostEqual(plan["flash"] + plan["tail"], 2.0)

    def test_an_unknown_style_falls_back_to_the_previous_look(self):
        self.assertEqual(vp.compute_hook_plan("bogus", 2.4)["style"], "finish_preview")

    def test_the_flash_is_a_still_on_the_shared_canvas(self):
        cmd = vp.build_still_segment_command("/tmp/art.jpg", "flash.mp4", 0.4)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("force_original_aspect_ratio=decrease", vf)
        self.assertIn("color=white", vf)
        self.assertIn("fade=t=in", vf)
        self.assertEqual(cmd[cmd.index("-loop") + 1], "1")
        self.assertIn("anullsrc", " ".join(cmd))
        self.assertIn("libx264", cmd)

    def test_a_muted_flash_has_no_audio_stream(self):
        cmd = vp.build_still_segment_command("/tmp/art.jpg", "flash.mp4", 0.4, with_audio=False)
        self.assertIn("-an", cmd)
        self.assertNotIn("anullsrc", " ".join(cmd))

    def test_the_hook_audio_can_be_boosted(self):
        cmd = vp.build_hook_command("in.mp4", "hook.mp4", 10.0, 2.0, keep_audio=True, volume_db=6)
        self.assertIn("volume=6.0dB", cmd[cmd.index("-af") + 1])

    def test_the_segment_audio_carries_the_boost_after_the_speed_change(self):
        cmd = vp.build_segment_audio_command(
            "in.mp4", "a.m4a", speed_factor=2.0, volume_db=6,
        )
        af = cmd[cmd.index("-af") + 1]
        self.assertTrue(af.startswith(vp.build_atempo_filter(2.0)))
        self.assertIn("volume=6.0dB", af)

    def test_no_boost_means_no_volume_filter(self):
        cmd = vp.build_segment_audio_command("in.mp4", "a.m4a", speed_factor=2.0)
        af = cmd[cmd.index("-af") + 1]
        self.assertNotIn("volume=", af)


class TempoTests(unittest.TestCase):
    """Requirement 2: the body can be played faster than the target implies."""

    def plan(self, tempo):
        return vp.compute_project_plan(
            [100.0], target_duration=24.0, hook_duration=2.0, tempo=tempo
        )

    def test_tempo_one_keeps_the_planned_length(self):
        plan = self.plan(1.0)
        self.assertAlmostEqual(plan["main_duration"], 22.0, places=3)

    def test_a_faster_tempo_shortens_the_body(self):
        slow = self.plan(1.0)
        fast = self.plan(1.5)
        self.assertAlmostEqual(fast["main_duration"], slow["main_duration"] / 1.5, places=3)
        self.assertLess(fast["pts_multiplier"], slow["pts_multiplier"])

    def test_a_broken_tempo_is_ignored(self):
        for value in (0, -3, "fast", None):
            self.assertAlmostEqual(self.plan(value)["main_duration"], 22.0, places=3)

    def test_the_tempo_is_clamped(self):
        self.assertAlmostEqual(self.plan(9.0)["tempo"], vp.TEMPO_MAX)

    def test_the_finish_hold_follows_the_parameter(self):
        short = vp.compute_finish_window(60.0, hold_seconds=2.0)
        long = vp.compute_finish_window(60.0, hold_seconds=4.0)
        self.assertLess(short["screen_seconds"], long["screen_seconds"])
        self.assertAlmostEqual(short["source_seconds"], 2.0)
