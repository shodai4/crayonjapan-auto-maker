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
