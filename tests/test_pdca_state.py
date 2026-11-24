import os
import tempfile
import unittest
from unittest import mock

import drive_sync
import improvement
import pdca_state


FOLDERS = drive_sync.DriveFolders(raw="raw-id", ready="ready-id", archive="archive-id")


class StateFolderTests(unittest.TestCase):
    def test_the_dedicated_folder_wins(self):
        self.assertEqual(
            pdca_state.state_folder_id(FOLDERS, env={"DRIVE_STATE_FOLDER_ID": "state-id"}),
            "state-id",
        )

    def test_without_a_dedicated_folder_the_archive_is_used(self):
        self.assertEqual(pdca_state.state_folder_id(FOLDERS, env={}), "archive-id")
        # The state files must never land beside the raw uploads.
        self.assertNotEqual(pdca_state.state_folder_id(FOLDERS, env={}), "raw-id")


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "performance.csv")
        self.logs = []

    def test_a_missing_remote_file_keeps_the_local_copy(self):
        with mock.patch.object(drive_sync, "find_child_by_name", return_value=None):
            restored = pdca_state.restore_file(
                object(), "folder", self.path, "performance.csv", log=self.logs.append
            )
        self.assertFalse(restored)
        self.assertTrue(any("no performance.csv" in line for line in self.logs))

    def test_a_found_file_is_downloaded_over_the_local_copy(self):
        def fake_download(service, file_id, path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("video_id\nabc\n")
            return path

        with mock.patch.object(
            drive_sync, "find_child_by_name", return_value={"id": "f1", "name": "performance.csv"}
        ):
            restored = pdca_state.restore_file(
                object(), "folder", self.path, "performance.csv",
                downloader=fake_download, log=self.logs.append,
            )
        self.assertTrue(restored)
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertIn("abc", handle.read())

    def test_a_download_error_keeps_the_run_alive(self):
        def broken(service, file_id, path):
            raise RuntimeError("network down")

        with mock.patch.object(drive_sync, "find_child_by_name", return_value={"id": "f1"}):
            restored = pdca_state.restore_file(
                object(), "folder", self.path, "performance.csv",
                downloader=broken, log=self.logs.append,
            )
        self.assertFalse(restored)
        self.assertTrue(any("network down" in line for line in self.logs))

    def test_no_folder_means_no_call(self):
        with mock.patch.object(drive_sync, "find_child_by_name") as finder:
            self.assertFalse(
                pdca_state.restore_file(object(), "", self.path, "performance.csv")
            )
        finder.assert_not_called()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "performance.csv")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("video_id\n")

    def test_the_local_file_is_pushed_with_its_name(self):
        calls = []

        def fake_upload(service, path, folder):
            calls.append((path, folder))
            return {"id": "f9"}

        saved = pdca_state.backup_file(
            object(), "folder", self.path, "performance.csv",
            uploader=fake_upload, log=lambda *_: None,
        )
        self.assertEqual(saved["id"], "f9")
        self.assertEqual(calls, [(self.path, "folder")])

    def test_an_upload_error_does_not_raise(self):
        def broken(service, path, folder):
            raise RuntimeError("quota")

        logs = []
        saved = pdca_state.backup_file(
            object(), "folder", self.path, "performance.csv",
            uploader=broken, log=logs.append,
        )
        self.assertIsNone(saved)
        self.assertTrue(any("quota" in line for line in logs))

    def test_a_missing_local_file_is_skipped(self):
        saved = pdca_state.backup_file(
            object(), "folder", os.path.join(self.tmp.name, "gone.csv"), "gone.csv"
        )
        self.assertIsNone(saved)


class AllTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")
        self.state = os.path.join(self.tmp.name, "improvement_state.json")
        for path in (self.csv, self.state):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")

    def test_restore_covers_both_files(self):
        names = []

        def fake_restore(service, folder_id, path, name, **kwargs):
            names.append(name)
            return True

        with mock.patch.object(pdca_state, "restore_file", side_effect=fake_restore):
            result = pdca_state.restore_all(
                object(), FOLDERS, csv_path=self.csv, state_path=self.state
            )
        self.assertEqual(names, [pdca_state.PERFORMANCE_NAME, pdca_state.STATE_NAME])
        self.assertEqual(result, {"performance": True, "state": True})

    def test_backup_covers_both_files(self):
        names = []

        def fake_backup(service, folder_id, path, name, **kwargs):
            names.append(name)
            return {"id": name}

        with mock.patch.object(pdca_state, "backup_file", side_effect=fake_backup):
            pdca_state.backup_all(object(), FOLDERS, csv_path=self.csv, state_path=self.state)
        self.assertEqual(names, [pdca_state.PERFORMANCE_NAME, pdca_state.STATE_NAME])

    def test_the_names_match_the_files_the_loop_reads(self):
        self.assertEqual(pdca_state.PERFORMANCE_NAME, os.path.basename(improvement.PERFORMANCE_PATH))
        self.assertEqual(pdca_state.STATE_NAME, os.path.basename(improvement.STATE_PATH))


if __name__ == "__main__":
    unittest.main()
