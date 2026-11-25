"""The Windows entry points and the Japanese walk-through that ships with them."""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BATCH_FILES = ("setup_local.bat", "run_local.bat", os.path.join("scripts", "register_local_task.bat"))
DOC = os.path.join("docs", "SETUP_WINDOWS_LOCAL.md")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def read(*parts, binary=False):
    path = os.path.join(ROOT, *parts)
    if binary:
        with open(path, "rb") as handle:
            return handle.read()
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class BatchFileTests(unittest.TestCase):
    def test_every_entry_point_exists(self):
        for name in BATCH_FILES:
            with self.subTest(name=name):
                self.assertTrue(os.path.exists(os.path.join(ROOT, name)))

    def test_the_batch_files_are_plain_ascii_with_windows_line_endings(self):
        for name in BATCH_FILES:
            with self.subTest(name=name):
                data = read(name, binary=True)
                # cmd.exe reads a .bat in the OEM code page: Japanese text would
                # show as mojibake, so the messages stay English on purpose.
                data.decode("ascii")
                # a lone LF would leave a line break cmd does not expect
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

    def test_every_batch_file_starts_with_echo_off_and_a_rem_block(self):
        for name in BATCH_FILES:
            with self.subTest(name=name):
                text = read(name)
                self.assertTrue(text.startswith("@echo off"))
                self.assertIn("rem ", text)

    def test_setup_installs_the_requirements_and_checks_the_pc(self):
        text = read("setup_local.bat")
        for needle in (
            "-m venv",
            "pip install -r requirements.txt",
            ".env.example",
            "--local-init",
            "--doctor",
            "pause",
        ):
            self.assertIn(needle, text)

    def test_setup_stops_on_a_failure_instead_of_carrying_on(self):
        text = read("setup_local.bat")
        self.assertIn("goto :fail", text)
        self.assertIn(":fail", text)
        self.assertIn("exit /b 1", text)

    def test_run_local_passes_every_argument_through(self):
        text = read("run_local.bat")
        self.assertIn("main.py", text)
        self.assertIn("%*", text)
        self.assertIn("exit /b %CODE%", text)
        self.assertIn("setup_local.bat", text)  # tells you where to start if .venv is missing

    def test_run_local_logs_into_the_base_folder(self):
        self.assertIn("local_hub.load_local_folders().logs", read("run_local.bat"))

    def test_the_task_registration_is_daily_and_removable(self):
        text = read(os.path.join("scripts", "register_local_task.bat"))
        self.assertIn("schtasks /Create", text)
        self.assertIn("/SC DAILY", text)
        self.assertIn("/ST %AT%", text)
        self.assertIn("/delete", text)
        self.assertIn("schtasks /Delete", text)
        self.assertIn("local.task_name", text)

    def test_the_batch_files_hold_no_credentials(self):
        for name in BATCH_FILES:
            with self.subTest(name=name):
                text = read(name)
                self.assertIsNone(EMAIL.search(text))
                self.assertNotIn("AIza", text)


class WindowsDocTests(unittest.TestCase):
    def setUp(self):
        self.text = read(DOC)

    def test_the_walk_through_exists_in_japanese(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, DOC)))
        self.assertGreater(len(self.text), 2000)
        self.assertIn("フォルダ", self.text)

    def test_it_names_the_three_folders_and_the_signal(self):
        for needle in ("01_RAW_VIDEO", "02_READY_TO_POST", "03_ARCHIVE", "GO_", "scan.jpg"):
            self.assertIn(needle, self.text)

    def test_it_covers_the_setup_steps(self):
        for needle in (
            "setup_local.bat",
            "client_secret.json",
            "YouTube Data API v3",
            "YouTube Analytics API",
            "--auth",
            "--doctor",
            "FFmpeg",
        ):
            self.assertIn(needle, self.text)

    def test_it_documents_the_commands_and_the_schedule(self):
        for needle in ("--local-once", "--dry-run", "--no-upload", "register_local_task.bat", "22:00"):
            self.assertIn(needle, self.text)

    def test_it_explains_that_gemini_is_optional(self):
        self.assertIn("GEMINI_API_KEY", self.text)
        self.assertIn("テンプレート台本", self.text)

    def test_it_says_not_to_run_both_hubs_at_once(self):
        self.assertIn("どちらか一方", self.text)

    def test_it_holds_no_address_or_drive_id(self):
        self.assertIsNone(EMAIL.search(self.text))
        self.assertIsNone(re.search(r"drive\.google\.com/drive/folders/\S", self.text))

    def test_the_readme_points_at_it(self):
        readme = read("README.md")
        self.assertIn("docs/SETUP_WINDOWS_LOCAL.md", readme)
        self.assertIn("--local-once", readme)
        self.assertIn("setup_local.bat", readme)
        self.assertIn("01_RAW_VIDEO", readme)


class ConfigTests(unittest.TestCase):
    def test_the_local_block_documents_the_folder_and_the_ages(self):
        import main

        config = main.load_config(os.path.join(ROOT, "config.yaml"))
        block = config["local"]
        self.assertEqual(block["base_dir"], "")
        self.assertEqual(block["min_age_seconds"], 60)
        self.assertEqual(block["go_min_age_seconds"], 5)
        self.assertTrue(block["archive_after_render"])
        self.assertTrue(block["task_name"])

    def test_the_local_block_is_commented_for_the_artist(self):
        text = read("config.yaml")
        start = text.index("local:")
        block = text[start:text.index("telop:")]
        self.assertIn("#", block)
        self.assertIn("CRAYONJAPAN_HOME", block)


if __name__ == "__main__":
    unittest.main()
