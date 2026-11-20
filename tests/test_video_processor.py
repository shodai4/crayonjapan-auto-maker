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
        cmd = vp.build_project_segment_command("in.mp4", "seg.mp4", 0.05)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertTrue(vf.startswith("setpts=0.050000*PTS,"))
        self.assertIn("force_original_aspect_ratio=decrease", vf)
        self.assertIn("-an", cmd)
        self.assertNotIn("-af", cmd)
        self.assertNotIn("anullsrc", cmd)
        self.assertIn("yuv420p", cmd)
        self.assertEqual(cmd[-1], "seg.mp4")

    def test_asmr_segment_keeps_and_stretches_the_original_audio(self):
        cmd = vp.build_project_segment_command("in.mp4", "seg.mp4", 0.05, keep_audio=True, has_audio=True)
        af = cmd[cmd.index("-af") + 1]
        self.assertEqual(af, f"{vp.build_atempo_filter(20.0)},apad")
        self.assertIn("0:a:0", cmd)
        self.assertNotIn("-an", cmd)
        self.assertIn("aac", cmd)
        self.assertIn("48000", cmd)
        self.assertIn("-shortest", cmd)

    def test_a_silent_clip_gets_a_silent_track_when_audio_is_kept(self):
        cmd = vp.build_project_segment_command("in.mp4", "seg.mp4", 0.05, keep_audio=True, has_audio=False)
        self.assertTrue(any("anullsrc" in part for part in cmd))
        self.assertIn("1:a:0", cmd)
        self.assertIn("-shortest", cmd)

    def test_hook_segment_is_not_sped_up(self):
        cmd = vp.build_project_hook_command("last.mp4", "hook.mp4", 56.0, 2.5)
        self.assertIn("-ss", cmd)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "56.000")
        self.assertEqual(cmd[cmd.index("-t") + 1], "2.500")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertNotIn("setpts", vf)
        self.assertIn("fps=30", vf)
        self.assertIn("-an", cmd)

    def test_hook_keeps_audio_for_asmr(self):
        cmd = vp.build_project_hook_command("last.mp4", "hook.mp4", 0.0, 2.5, keep_audio=True, has_audio=True)
        self.assertNotIn("-an", cmd)
        self.assertIn("apad", cmd[cmd.index("-af") + 1])

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
