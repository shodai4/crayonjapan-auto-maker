import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "auto-edit.yml")
GAS_SCRIPT = os.path.join(ROOT, "gas", "Code_drive_watch.gs")

DRIVE_FOLDER_ID = re.compile(r"\b1[A-Za-z0-9_-]{20,}\b")
API_KEY = re.compile(r"AIza[0-9A-Za-z_-]{10,}")


def read_workflow():
    with open(WORKFLOW, "r", encoding="utf-8") as handle:
        return handle.read()


class AutoEditWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.document = yaml.safe_load(read_workflow())
        # `on` is parsed as a boolean key by YAML 1.1; accept both spellings.
        self.triggers = self.document.get("on", self.document.get(True))

    def test_both_entry_points_are_wired(self):
        self.assertIn("repository_dispatch", self.triggers)
        self.assertIn("workflow_dispatch", self.triggers)
        self.assertIn("raw_video_uploaded", self.triggers["repository_dispatch"]["types"])

    def test_runs_on_the_free_ubuntu_runner_with_a_time_limit(self):
        job = self.document["jobs"]["edit"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertIn("timeout-minutes", job)

    def test_renders_one_video_at_a_time(self):
        self.assertEqual(self.document["concurrency"]["group"], "crayonjapan-auto-edit")
        self.assertFalse(self.document["concurrency"]["cancel-in-progress"])

    def test_workflow_can_only_read_the_repository(self):
        self.assertEqual(self.document["permissions"], {"contents": "read"})

    def test_installs_ffmpeg_and_the_opencv_libraries(self):
        steps = self.document["jobs"]["edit"]["steps"]
        apt = [step for step in steps if "apt-get install" in str(step.get("run", ""))]
        self.assertEqual(len(apt), 1)
        command = apt[0]["run"]
        for package in ("ffmpeg", "libgl1", "libglib2.0-0"):
            self.assertIn(package, command)

    def test_runs_tests_then_the_drive_once_pipeline(self):
        runs = [str(step.get("run", "")) for step in self.document["jobs"]["edit"]["steps"]]
        joined = "\n".join(runs)
        self.assertIn("unittest discover -s tests -t .", joined)
        # `--no-upload` may or may not be there: the workflow is edited in the
        # browser separate from this code, and with the posting schedule the
        # run is meant to publish.
        self.assertIn("python main.py --drive-once", joined)
        self.assertLess(
            joined.index("unittest discover"),
            joined.index("python main.py --drive-once"),
        )

    def test_secrets_cover_the_api_key_and_the_three_folders(self):
        text = read_workflow()
        for name in (
            "GEMINI_API_KEY",
            "DRIVE_RAW_FOLDER_ID",
            "DRIVE_READY_FOLDER_ID",
            "DRIVE_ARCHIVE_FOLDER_ID",
        ):
            self.assertIn("secrets." + name, text)

    def test_dispatched_token_is_used_and_masked(self):
        text = read_workflow()
        self.assertIn("github.event.client_payload.drive_token", text)
        self.assertIn("::add-mask::", text)

    def test_never_prints_a_credential(self):
        text = read_workflow()
        for line in text.splitlines():
            stripped = line.strip()
            if any(word in stripped for word in ("GDRIVE_ACCESS_TOKEN", "GDRIVE_TOKEN_JSON")):
                self.assertNotIn("echo $", stripped)
        self.assertIsNone(API_KEY.search(text))
        self.assertIsNone(DRIVE_FOLDER_ID.search(text))


class GasScriptTests(unittest.TestCase):
    """The Apps Script half lives in the repository for review; it holds no IDs."""

    def setUp(self):
        with open(GAS_SCRIPT, "r", encoding="utf-8") as handle:
            self.text = handle.read()

    def test_triggers_the_workflow_and_guards_against_double_dispatch(self):
        for needle in (
            "/dispatches",
            "raw_video_uploaded",
            "PropertiesService.getScriptProperties",
            "ScriptApp.getOAuthToken",
            "installRawVideoTrigger",
        ):
            self.assertIn(needle, self.text)

    def test_reads_the_folder_and_repository_from_properties(self):
        for name in ("RAW_FOLDER_ID", "GITHUB_REPO", "GITHUB_TOKEN"):
            self.assertIn(name, self.text)

    def test_holds_no_folder_id_api_key_or_address(self):
        self.assertIsNone(API_KEY.search(self.text))
        self.assertIsNone(DRIVE_FOLDER_ID.search(self.text))
        self.assertNotIn("@", self.text)

    def test_watches_go_folders_as_well_as_loose_videos(self):
        self.assertIn("/^GO_/i", self.text)
        self.assertIn("getFolders()", self.text)
        self.assertIn("FOLDER_MIN_AGE_MS", self.text)
        self.assertIn("GO_FOLDER_PATTERN.test(sub.getName())", self.text)
        self.assertIn("VIDEO_EXTENSION.test(file.getName())", self.text)

    def test_the_dispatch_says_what_kind_of_item_it_is(self):
        self.assertIn("item_type", self.text)
        self.assertIn("kind: 'folder'", self.text)
        self.assertIn("kind: 'file'", self.text)

    def test_the_trigger_never_throws_when_unconfigured(self):
        start = self.text.index("function watchRawVideo")
        body = self.text[start:self.text.index("function collectWaitingItems")]
        self.assertIn("console.error", body)
        self.assertIn("return;", body)
        self.assertNotIn("throw new Error", body)

    def test_a_failed_dispatch_is_logged_and_retried_not_thrown(self):
        body = self.text[self.text.index("function watchRawVideo"):self.text.index("function collectWaitingItems")]
        self.assertIn("try {", body)
        self.assertIn("catch (err)", body)
        self.assertIn("dispatch failed", body)
        # dispatching is inside the try, and the log is only written after it
        self.assertLess(body.index("dispatchToGitHub(waiting[0]"), body.index("catch (err)"))
        self.assertLess(body.index("catch (err)"), body.index("writeDispatchLog"))

    def test_describe_lists_folders_and_files(self):
        self.assertIn("describeRawFolder", self.text)
        describe = self.text[self.text.index("function describeRawFolder"):]
        self.assertIn("getFolders()", describe)
        self.assertIn("getFiles()", describe)
        self.assertIn("go=", describe)


if __name__ == "__main__":
    unittest.main()
