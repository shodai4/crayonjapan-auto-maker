"""main.py wiring of the Windows local mode (--local-once and friends)."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import drive_sync
import improvement
import local_hub
import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LocalSettingsTests(unittest.TestCase):
    def test_the_shipped_config_has_the_local_block(self):
        settings = main.local_settings(main.load_config(os.path.join(ROOT, "config.yaml")))
        self.assertEqual(settings["min_age_seconds"], 60)
        self.assertEqual(settings["go_min_age_seconds"], 5)
        self.assertTrue(settings["archive_after_render"])

    def test_a_config_without_the_block_keeps_safe_defaults(self):
        settings = main.local_settings({})
        self.assertEqual(settings["scan_interval_seconds"], 60.0)
        self.assertEqual(settings["min_age_seconds"], 60)

    def test_broken_numbers_fall_back(self):
        settings = main.local_settings(
            {"local": {"min_age_seconds": "soon", "scan_interval_seconds": None}}
        )
        self.assertEqual(settings["min_age_seconds"], 60)
        self.assertEqual(settings["scan_interval_seconds"], 60.0)

    def test_a_negative_age_is_read_as_zero(self):
        self.assertEqual(
            main.local_settings({"local": {"min_age_seconds": -5}})["min_age_seconds"], 0
        )

    def test_the_interval_is_never_zero(self):
        self.assertGreaterEqual(
            main.local_settings({"local": {"scan_interval_seconds": 0}})["scan_interval_seconds"], 1.0
        )


class LocalModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "CrayonJapan")
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        improvement.clear_active_params()
        self.addCleanup(improvement.clear_active_params)

    def _args(self, **overrides):
        values = dict(
            no_upload=False, artwork_offset=0.0, ignore_schedule=False, ignore_pdca=True,
            local_once=True, local_watch=False, local_base=self.base, local_state="",
            local_interval=None, local_min_age=None, local_go_min_age=None,
            dry_run=False,
        )
        values.update(overrides)
        return mock.Mock(**values)

    def _gate(self, allowed=True, reason="interval-passed"):
        return {
            "allowed": allowed, "reason": reason, "hours_elapsed": 50.0,
            "hours_left": None, "settings": main.schedule_settings(self.config),
        }

    def _run(self, args, gate=None, sync_results=None):
        gate = gate if gate is not None else self._gate()
        with mock.patch.object(main, "post_interval_state", return_value=gate) as gate_mock, \
                mock.patch.object(main, "learning_prepare", return_value=({}, {})) as prepare, \
                mock.patch.object(main, "learning_finish") as finish, \
                mock.patch.object(
                    local_hub, "sync_once", return_value=sync_results or [{"status": "done"}]
                ) as sync, \
                mock.patch.object(local_hub, "watch", return_value=[]) as watch:
            code = main.run_local_mode(args, self.config)
        return code, gate_mock, prepare, finish, sync, watch

    def test_the_folder_layout_is_created_before_the_scan(self):
        code, _, _, _, sync, _ = self._run(self._args())
        self.assertEqual(code, 0)
        for path in (local_hub.RAW_DIRNAME, local_hub.READY_DIRNAME, local_hub.ARCHIVE_DIRNAME):
            self.assertTrue(os.path.isdir(os.path.join(self.base, path)))
        sync.assert_called_once()

    def test_a_run_inside_the_interval_renders_nothing(self):
        code, gate, prepare, _, sync, _ = self._run(
            self._args(), gate=self._gate(False, "too-soon")
        )
        self.assertEqual(code, 0)
        gate.assert_called_once()
        sync.assert_not_called()
        prepare.assert_not_called()
        self.assertFalse(os.path.isdir(os.path.join(self.base, local_hub.READY_DIRNAME)))

    def test_an_unreadable_channel_holds_the_run_with_an_error(self):
        code, _, _, _, sync, _ = self._run(
            self._args(), gate=self._gate(False, "unknown-latest-upload")
        )
        self.assertEqual(code, 1)
        sync.assert_not_called()

    def test_the_slot_is_capped_to_one_short(self):
        _, _, _, _, sync, _ = self._run(self._args())
        self.assertEqual(sync.call_args.kwargs["max_items"], 1)

    def test_a_render_only_run_is_not_gated(self):
        _, gate, _, _, sync, _ = self._run(self._args(no_upload=True))
        gate.assert_not_called()
        self.assertIsNone(sync.call_args.kwargs["max_items"])

    def test_ignore_schedule_skips_the_gate(self):
        _, gate, _, _, sync, _ = self._run(self._args(ignore_schedule=True))
        gate.assert_not_called()
        self.assertIsNone(sync.call_args.kwargs["max_items"])

    def test_the_learning_steps_run_without_drive(self):
        _, _, prepare, finish, _, _ = self._run(self._args(ignore_pdca=False))
        prepare.assert_called_once()
        # no Drive service and no Drive folders: local mode
        self.assertEqual(prepare.call_args.args[1:], (None, None))
        finish.assert_called_once()

    def test_a_render_only_run_skips_the_learning_steps(self):
        _, _, prepare, finish, _, _ = self._run(self._args(no_upload=True, ignore_pdca=False))
        prepare.assert_not_called()
        finish.assert_not_called()

    def test_a_failed_item_makes_the_run_fail(self):
        code, _, _, _, _, _ = self._run(
            self._args(), sync_results=[{"status": "done"}, {"status": "failed"}]
        )
        self.assertEqual(code, 1)

    def test_the_scan_writes_its_own_daily_log(self):
        self._run(self._args())
        logs = os.listdir(os.path.join(self.base, local_hub.LOGS_DIRNAME))
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].startswith("run_"))

    def test_the_state_file_defaults_to_the_base_folder(self):
        _, _, _, _, sync, _ = self._run(self._args())
        self.assertTrue(
            sync.call_args.kwargs["state_path"].endswith(local_hub.DEFAULT_STATE_NAME)
        )

    def test_the_state_file_can_be_given(self):
        other = os.path.join(self.tmp.name, "state.json")
        _, _, _, _, sync, _ = self._run(self._args(local_state=other))
        self.assertEqual(sync.call_args.kwargs["state_path"], other)

    def test_the_cli_ages_reach_the_scan(self):
        _, _, _, _, sync, _ = self._run(self._args(local_min_age=3, local_go_min_age=1))
        self.assertEqual(sync.call_args.kwargs["min_age_seconds"], 3)
        self.assertEqual(sync.call_args.kwargs["go_min_age_seconds"], 1)

    def test_the_config_ages_are_used_when_the_cli_has_none(self):
        config = dict(self.config)
        config["local"] = dict(config["local"], min_age_seconds=90, go_min_age_seconds=9)
        self.config = config
        _, _, _, _, sync, _ = self._run(self._args())
        self.assertEqual(sync.call_args.kwargs["min_age_seconds"], 90)
        self.assertEqual(sync.call_args.kwargs["go_min_age_seconds"], 9)

    def test_the_watcher_is_not_capped(self):
        _, gate, _, _, sync, watch = self._run(self._args(local_once=False, local_watch=True))
        gate.assert_not_called()
        sync.assert_not_called()
        watch.assert_called_once()
        self.assertIsNone(watch.call_args.kwargs["max_items"])

    def test_the_watcher_uses_the_configured_interval(self):
        config = dict(self.config)
        config["local"] = dict(config["local"], scan_interval_seconds=15)
        self.config = config
        _, _, _, _, _, watch = self._run(self._args(local_once=False, local_watch=True))
        self.assertEqual(watch.call_args.kwargs["interval_seconds"], 15)

    def test_the_watcher_stops_cleanly_on_ctrl_c(self):
        with mock.patch.object(local_hub, "watch", side_effect=KeyboardInterrupt):
            code = main.run_local_mode(self._args(local_once=False, local_watch=True), self.config)
        self.assertEqual(code, 0)


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "CrayonJapan")
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        improvement.clear_active_params()
        self.addCleanup(improvement.clear_active_params)

    def _args(self, **overrides):
        values = dict(
            no_upload=False, artwork_offset=0.0, ignore_schedule=False, ignore_pdca=False,
            local_once=True, local_watch=False, local_base=self.base, local_state="",
            local_interval=None, local_min_age=None, local_go_min_age=None, dry_run=True,
        )
        values.update(overrides)
        return mock.Mock(**values)

    def _gate(self, allowed=True, reason="interval-passed", hours_left=None):
        return {
            "allowed": allowed, "reason": reason, "hours_elapsed": 50.0,
            "hours_left": hours_left, "settings": main.schedule_settings(self.config),
        }

    def test_nothing_is_created_rendered_or_uploaded(self):
        with mock.patch.object(main, "post_interval_state", return_value=self._gate()), \
                mock.patch.object(main, "learning_prepare") as prepare, \
                mock.patch.object(local_hub, "sync_once") as sync:
            code = main.run_local_mode(self._args(), self.config)
        self.assertEqual(code, 0)
        sync.assert_not_called()
        prepare.assert_not_called()
        self.assertFalse(os.path.exists(self.base))

    def test_the_waiting_items_are_described(self):
        folder = os.path.join(self.base, local_hub.RAW_DIRNAME, "GO_20261121_sushi")
        os.makedirs(folder)
        open(os.path.join(folder, "part1.mp4"), "w").close()
        open(os.path.join(folder, "scan.jpg"), "w").close()
        with mock.patch.object(main, "post_interval_state", return_value=self._gate()), \
                mock.patch("builtins.print") as printed:
            code = main.run_local_mode(self._args(), self.config)
        self.assertEqual(code, 0)
        text = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("GO_20261121_sushi", text)
        self.assertIn("1 scan(s)", text)
        self.assertIn("would render", text)
        self.assertIn("no file was created", text)

    def test_a_slot_inside_the_interval_says_so(self):
        with mock.patch.object(
            main, "post_interval_state",
            return_value=self._gate(False, "too-soon", hours_left=12.5),
        ), mock.patch("builtins.print") as printed:
            code = main.run_local_mode(self._args(), self.config)
        self.assertEqual(code, 0)
        text = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("12.5 h to go", text)
        self.assertIn("would render and post nothing", text)

    def test_a_dry_run_does_not_need_a_gate_with_no_upload(self):
        with mock.patch.object(main, "post_interval_state") as gate, \
                mock.patch("builtins.print"):
            code = main.run_local_mode(self._args(no_upload=True), self.config)
        self.assertEqual(code, 0)
        gate.assert_not_called()


class PipelineWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "CrayonJapan")
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))
        self.folders = local_hub.ensure_local_layout(
            local_hub.load_local_folders(base_dir=self.base, environ={})
        )

    def _args(self, **overrides):
        values = dict(
            no_upload=True, artwork_offset=1.5, ignore_schedule=True, ignore_pdca=True,
            local_once=True, local_watch=False, local_base=self.base, local_state="",
            local_interval=None, local_min_age=None, local_go_min_age=None, dry_run=False,
        )
        values.update(overrides)
        return mock.Mock(**values)

    def test_a_project_folder_goes_to_the_project_pipeline(self):
        folder = os.path.join(self.folders.raw, "GO_20261121_sushi")
        os.makedirs(folder)
        open(os.path.join(folder, "part1.mp4"), "w").close()
        item = local_hub.list_local_items(self.folders)[0]
        with mock.patch.object(main, "process_project_bundle", return_value={}) as project, \
                mock.patch.object(local_hub, "sync_once") as sync:
            main.run_local_mode(self._args(), self.config)
            pipeline = sync.call_args.args[1]
            pipeline(item)
        self.assertEqual(project.call_args.args[0].stem, "20261121_sushi")
        self.assertEqual(project.call_args.kwargs["output_dir"], self.folders.output)
        self.assertEqual(project.call_args.kwargs["artwork_offset_seconds"], 1.5)
        self.assertTrue(project.call_args.kwargs["upload"] is False)

    def test_a_loose_recording_goes_to_the_single_video_pipeline_where_it_lies(self):
        path = os.path.join(self.folders.raw, "drawing.mp4")
        open(path, "w").close()
        item = local_hub.list_local_items(self.folders)[0]
        with mock.patch.object(main, "process_video_bundle", return_value={}) as single, \
                mock.patch.object(local_hub, "sync_once") as sync:
            main.run_local_mode(self._args(), self.config)
            sync.call_args.args[1](item)
        self.assertEqual(single.call_args.args[0], path)
        self.assertEqual(single.call_args.kwargs["base_name"], "drawing")
        self.assertEqual(single.call_args.kwargs["output_dir"], self.folders.output)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "CrayonJapan")
        self.config = main.load_config(os.path.join(ROOT, "config.yaml"))

    def _main(self, argv):
        with mock.patch.object(main, "load_config", return_value=self.config), \
                mock.patch("dotenv.load_dotenv"):
            return main.main(argv)

    def test_local_once_reaches_the_local_mode(self):
        with mock.patch.object(main, "run_local_mode", return_value=0) as run:
            self._main(["--local-once", "--local-base", self.base])
        args = run.call_args.args[0]
        self.assertTrue(args.local_once)
        self.assertEqual(args.local_base, self.base)

    def test_local_watch_reaches_the_local_mode(self):
        with mock.patch.object(main, "run_local_mode", return_value=0) as run:
            self._main(["--local-watch", "--local-interval", "5"])
        self.assertTrue(run.call_args.args[0].local_watch)
        self.assertEqual(run.call_args.args[0].local_interval, 5.0)

    def test_a_lone_dry_run_reads_as_a_local_one_shot(self):
        with mock.patch.object(main, "run_local_mode", return_value=0) as run:
            self._main(["--dry-run"])
        self.assertTrue(run.call_args.args[0].local_once)

    def test_dry_run_with_drive_mode_stays_a_drive_run(self):
        with mock.patch.object(main, "run_drive_mode", return_value=0) as run:
            self._main(["--drive-once", "--dry-run"])
        self.assertTrue(run.call_args.args[0].dry_run)

    def test_local_init_creates_the_folder_layout(self):
        code = self._main(["--local-init", "--local-base", self.base])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.base, local_hub.RAW_DIRNAME)))

    def test_doctor_returns_its_exit_code(self):
        with mock.patch.object(main.doctor, "run_checks", return_value=[
            main.doctor.Check("FFmpeg", main.doctor.STATE_FAIL, "missing")
        ]):
            code = self._main(["--doctor", "--local-base", self.base])
        self.assertEqual(code, 1)

    def test_doctor_is_clean_with_a_prepared_pc(self):
        with mock.patch.object(main.doctor, "run_checks", return_value=[
            main.doctor.Check("FFmpeg", main.doctor.STATE_OK, "7.0")
        ]):
            self.assertEqual(self._main(["--doctor", "--local-base", self.base]), 0)

    def test_auth_without_a_client_secret_explains_what_is_missing(self):
        with mock.patch(
            "youtube_uploader.load_credentials",
            side_effect=FileNotFoundError("client_secret.json not found"),
        ), mock.patch("builtins.print") as printed:
            code = self._main(["--auth"])
        self.assertEqual(code, 1)
        text = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("client_secret.json", text)

    def test_auth_reports_the_channel_it_signed_in_as(self):
        service = mock.Mock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "CrayonJapan"}}]
        }
        with mock.patch("youtube_uploader.load_credentials", return_value=object()), \
                mock.patch("youtube_uploader.get_youtube_service", return_value=service), \
                mock.patch("builtins.print") as printed:
            code = self._main(["--auth"])
        self.assertEqual(code, 0)
        text = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("CrayonJapan", text)


if __name__ == "__main__":
    unittest.main()
