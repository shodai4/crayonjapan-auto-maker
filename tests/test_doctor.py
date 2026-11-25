"""--doctor: the pre-flight checks of a Windows local run."""

import os
import tempfile
import unittest
from types import SimpleNamespace

import doctor
import local_hub


def fake_result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class PythonCheckTests(unittest.TestCase):
    def test_a_modern_python_passes(self):
        check = doctor.check_python((3, 12))
        self.assertEqual(check.state, doctor.STATE_OK)

    def test_the_oldest_supported_python_passes(self):
        self.assertEqual(doctor.check_python((3, 10)).state, doctor.STATE_OK)

    def test_an_old_python_fails_with_a_hint(self):
        check = doctor.check_python((3, 8))
        self.assertEqual(check.state, doctor.STATE_FAIL)
        self.assertIn("3.10", check.hint)


class FfmpegCheckTests(unittest.TestCase):
    def test_a_missing_ffmpeg_fails(self):
        check = doctor.check_ffmpeg(which=lambda name: None)
        self.assertEqual(check.state, doctor.STATE_FAIL)
        self.assertIn("PATH", check.detail)

    def test_a_working_ffmpeg_passes_with_its_version(self):
        check = doctor.check_ffmpeg(
            which=lambda name: "/usr/bin/ffmpeg",
            runner=lambda *args, **kwargs: fake_result(
                stdout="ffmpeg version 7.0.2-static Copyright\n"
            ),
        )
        self.assertEqual(check.state, doctor.STATE_OK)
        self.assertIn("7.0.2", check.detail)

    def test_an_old_ffmpeg_fails(self):
        check = doctor.check_ffmpeg(
            which=lambda name: "ffmpeg",
            runner=lambda *args, **kwargs: fake_result(stdout="ffmpeg version 4.4.2\n"),
        )
        self.assertEqual(check.state, doctor.STATE_FAIL)
        self.assertIn("older", check.detail)

    def test_an_ffmpeg_that_cannot_run_fails(self):
        def explode(*args, **kwargs):
            raise OSError("permission denied")

        check = doctor.check_ffmpeg(which=lambda name: "ffmpeg", runner=explode)
        self.assertEqual(check.state, doctor.STATE_FAIL)

    def test_a_missing_ffprobe_is_only_a_warning(self):
        self.assertEqual(doctor.check_ffprobe(which=lambda name: None).state, doctor.STATE_WARN)
        self.assertEqual(doctor.check_ffprobe(which=lambda name: "/x").state, doctor.STATE_OK)

    def test_the_filters_and_the_encoder_are_probed(self):
        seen = []

        def runner(cmd, **kwargs):
            seen.append(cmd[-1])
            return fake_result(0)

        check = doctor.check_ffmpeg_filters(which=lambda name: "ffmpeg", runner=runner)
        self.assertEqual(check.state, doctor.STATE_OK)
        self.assertEqual(seen, ["filter=ass", "filter=subtitles", "encoder=libx264"])

    def test_a_build_without_libass_fails(self):
        def runner(cmd, **kwargs):
            return fake_result(1 if cmd[-1] == "filter=ass" else 0)

        check = doctor.check_ffmpeg_filters(which=lambda name: "ffmpeg", runner=runner)
        self.assertEqual(check.state, doctor.STATE_FAIL)
        self.assertIn("ass", check.detail)

    def test_without_ffmpeg_the_filter_check_is_skipped(self):
        check = doctor.check_ffmpeg_filters(which=lambda name: None)
        self.assertEqual(check.state, doctor.STATE_WARN)
        self.assertIn("skipped", check.detail)


class CredentialCheckTests(unittest.TestCase):
    def test_the_bundled_japanese_font_is_required_for_a_clean_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = doctor.check_font(tmp)
            self.assertEqual(check.state, doctor.STATE_WARN)
            os.makedirs(os.path.join(tmp, "assets", "fonts"))
            open(os.path.join(tmp, "assets", "fonts", "NotoSansJP-Bold.otf"), "w").close()
            self.assertEqual(doctor.check_font(tmp).state, doctor.STATE_OK)

    def test_a_missing_key_only_warns_because_the_template_takes_over(self):
        check = doctor.check_gemini_key(env={})
        self.assertEqual(check.state, doctor.STATE_WARN)
        self.assertIn("template", check.detail)

    def test_a_key_is_reported_as_set_without_printing_it(self):
        check = doctor.check_gemini_key(env={"GEMINI_API_KEY": "secret-value"})
        self.assertEqual(check.state, doctor.STATE_OK)
        self.assertNotIn("secret-value", check.detail)

    def test_a_blank_key_counts_as_missing(self):
        self.assertEqual(
            doctor.check_gemini_key(env={"GEMINI_API_KEY": "   "}).state, doctor.STATE_WARN
        )

    def test_the_oauth_client_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(doctor.check_client_secret(tmp).state, doctor.STATE_FAIL)
            open(os.path.join(tmp, "client_secret.json"), "w").close()
            self.assertEqual(doctor.check_client_secret(tmp).state, doctor.STATE_OK)

    def test_a_saved_token_is_enough_for_the_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "token.json"), "w").close()
            self.assertEqual(doctor.check_youtube_token(tmp, env={}).state, doctor.STATE_OK)

    def test_a_token_in_the_environment_counts_too(self):
        check = doctor.check_youtube_token(
            tempfile.gettempdir(), env={"YOUTUBE_TOKEN_JSON": "{}"}
        )
        self.assertEqual(check.state, doctor.STATE_OK)

    def test_without_a_token_the_first_run_signs_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = doctor.check_youtube_token(tmp, env={})
        self.assertEqual(check.state, doctor.STATE_WARN)
        self.assertIn("--auth", check.hint)


class FolderCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _folders(self, name="base"):
        return local_hub.load_local_folders(
            base_dir=os.path.join(self.tmp.name, name), environ={}
        )

    def test_an_existing_base_folder_passes(self):
        folders = local_hub.ensure_local_layout(self._folders())
        self.assertEqual(doctor.check_base_folder(folders).state, doctor.STATE_OK)

    def test_a_base_folder_that_can_be_created_only_warns(self):
        check = doctor.check_base_folder(self._folders())
        self.assertEqual(check.state, doctor.STATE_WARN)
        self.assertIn("--local-init", check.hint)

    def test_a_base_folder_under_a_missing_parent_fails(self):
        folders = local_hub.load_local_folders(
            base_dir=os.path.join(self.tmp.name, "no-such-drive", "CrayonJapan"), environ={}
        )
        self.assertEqual(doctor.check_base_folder(folders).state, doctor.STATE_FAIL)

    def test_free_space_is_reported_in_gb(self):
        folders = local_hub.ensure_local_layout(self._folders())
        check = doctor.check_free_space(folders)
        self.assertIn(check.state, (doctor.STATE_OK, doctor.STATE_WARN))
        self.assertIn("GB", check.detail)

    def test_pending_work_is_listed(self):
        folders = local_hub.ensure_local_layout(self._folders())
        self.assertIn("none yet", doctor.check_pending(folders).detail)
        os.makedirs(os.path.join(folders.raw, "GO_a"))
        open(os.path.join(folders.raw, "GO_a", "part1.mp4"), "w").close()
        self.assertIn("1 waiting", doctor.check_pending(folders).detail)

    def test_a_long_queue_is_shortened(self):
        folders = local_hub.ensure_local_layout(self._folders())
        for name in ("GO_a", "GO_b", "GO_c", "GO_d"):
            os.makedirs(os.path.join(folders.raw, name))
            open(os.path.join(folders.raw, name, "part1.mp4"), "w").close()
        self.assertIn("+1 more", doctor.check_pending(folders).detail)

    def test_the_shipped_schedule_is_described(self):
        import main

        config = main.load_config(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "config.yaml"))
        check = doctor.check_schedule(config)
        self.assertEqual(check.state, doctor.STATE_OK)
        self.assertIn("48 h", check.detail)


class ReportTests(unittest.TestCase):
    def test_the_report_lines_carry_a_label_and_a_hint(self):
        checks = [
            doctor.Check("FFmpeg", doctor.STATE_OK, "7.0"),
            doctor.Check("OAuth client", doctor.STATE_FAIL, "missing", "create one"),
        ]
        text = doctor.format_checks(checks)
        self.assertIn("[OK  ] FFmpeg", text)
        self.assertIn("[FAIL] OAuth client", text)
        self.assertIn("-> create one", text)

    def test_a_hint_is_not_printed_for_a_passing_check(self):
        text = doctor.format_checks([doctor.Check("x", doctor.STATE_OK, "fine", "ignore me")])
        self.assertNotIn("ignore me", text)

    def test_the_exit_code_says_whether_a_run_can_work(self):
        self.assertEqual(doctor.exit_code([doctor.Check("a", doctor.STATE_OK, "")]), 0)
        self.assertEqual(doctor.exit_code([doctor.Check("a", doctor.STATE_WARN, "")]), 0)
        self.assertEqual(doctor.exit_code([doctor.Check("a", doctor.STATE_FAIL, "")]), 1)
        self.assertFalse(doctor.ready([doctor.Check("a", doctor.STATE_FAIL, "")]))
        self.assertTrue(doctor.ready([doctor.Check("a", doctor.STATE_WARN, "")]))

    def test_every_check_runs_without_a_network_or_a_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            folders = local_hub.ensure_local_layout(
                local_hub.load_local_folders(base_dir=os.path.join(tmp, "base"), environ={})
            )
            checks = doctor.run_checks(
                config={"local": {"base_dir": folders.base}, "schedule": {"enabled": True}},
                folders=folders,
                project_root=tmp,
                env={},
                which=lambda name: None,
                runner=lambda *args, **kwargs: fake_result(1),
                version=(3, 12),
            )
        names = [check.name for check in checks]
        self.assertIn("Python", names)
        self.assertIn("FFmpeg", names)
        self.assertIn("Base folder", names)
        self.assertIn("Posting rhythm", names)
        # no ffmpeg and no client_secret.json in an empty root: not ready
        self.assertEqual(doctor.exit_code(checks), 1)


if __name__ == "__main__":
    unittest.main()
