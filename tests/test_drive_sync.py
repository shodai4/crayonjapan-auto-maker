import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import drive_sync as ds

FOLDERS = ds.DriveFolders(raw="raw-folder", ready="ready-folder", archive="archive-folder")
NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)


def video_info(file_id="file1", name="drawing.mp4", age_seconds=3600):
    modified = (NOW - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")
    return {"id": file_id, "name": name, "mimeType": "video/mp4", "modifiedTime": modified}


def fake_service(pages):
    service = mock.MagicMock()
    service.files.return_value.list.return_value.execute.side_effect = pages
    return service


class FolderConfigTests(unittest.TestCase):
    def test_reads_the_three_ids_from_the_environment(self):
        env = {
            ds.RAW_FOLDER_ENV: " raw-1 ",
            ds.READY_FOLDER_ENV: "ready-1",
            ds.ARCHIVE_FOLDER_ENV: "archive-1",
        }
        folders = ds.load_drive_folders(env)
        self.assertEqual(folders, ds.DriveFolders(raw="raw-1", ready="ready-1", archive="archive-1"))

    def test_missing_id_names_the_variable(self):
        with self.assertRaises(ds.MissingFolderIdError) as ctx:
            ds.load_drive_folders({ds.RAW_FOLDER_ENV: "raw-1", ds.READY_FOLDER_ENV: "ready-1"})
        self.assertIn(ds.ARCHIVE_FOLDER_ENV, str(ctx.exception))

    def test_blank_values_are_treated_as_missing(self):
        env = {ds.RAW_FOLDER_ENV: "  ", ds.READY_FOLDER_ENV: "ready-1", ds.ARCHIVE_FOLDER_ENV: "a"}
        with self.assertRaises(ds.MissingFolderIdError):
            ds.load_drive_folders(env)

    def test_missing_folder_error_is_a_drive_sync_error(self):
        self.assertTrue(issubclass(ds.MissingFolderIdError, ds.DriveSyncError))


class FileNameTests(unittest.TestCase):
    def test_accepts_supported_video_extensions_case_insensitively(self):
        for name in ["a.mp4", "B.MOV", "c.Avi"]:
            self.assertTrue(ds.is_raw_video_name(name), name)

    def test_rejects_other_files(self):
        for name in ["notes.txt", "clip.webm", "", None]:
            self.assertFalse(ds.is_raw_video_name(name))

    def test_mime_types_for_the_artifacts(self):
        self.assertEqual(ds.guess_mime_type("clip_final.mp4"), "video/mp4")
        self.assertEqual(ds.guess_mime_type("clip_artwork.jpg"), "image/jpeg")
        self.assertEqual(ds.guess_mime_type("clip_metadata.txt"), "text/plain")
        self.assertEqual(ds.guess_mime_type("clip_art_metadata.json"), "application/json")
        self.assertEqual(ds.guess_mime_type("weird.bin"), "application/octet-stream")


class ListRawVideosTests(unittest.TestCase):
    def test_queries_the_folder_and_keeps_only_videos(self):
        service = fake_service([{"files": [
            video_info("v1", "drawing.mp4"),
            {"id": "t1", "name": "notes.txt", "mimeType": "text/plain"},
            {"id": "f1", "name": "sub", "mimeType": ds.FOLDER_MIME_TYPE},
            video_info("v2", "second.MOV"),
        ]}])
        videos = ds.list_raw_videos(service, "raw-folder")
        self.assertEqual([v["id"] for v in videos], ["v1", "v2"])

        kwargs = service.files.return_value.list.call_args.kwargs
        self.assertIn("'raw-folder' in parents", kwargs["q"])
        self.assertIn("trashed = false", kwargs["q"])
        self.assertIn("id,name,mimeType", kwargs["fields"])

    def test_follows_page_tokens(self):
        service = fake_service([
            {"files": [video_info("v1", "a.mp4")], "nextPageToken": "token-2"},
            {"files": [video_info("v2", "b.mp4")]},
        ])
        videos = ds.list_raw_videos(service, "raw-folder")
        self.assertEqual([v["id"] for v in videos], ["v1", "v2"])
        calls = service.files.return_value.list.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].kwargs["pageToken"], "token-2")


class SettledTests(unittest.TestCase):
    def test_old_file_is_settled(self):
        self.assertTrue(ds.is_settled(video_info(age_seconds=600), now=NOW, min_age_seconds=60))

    def test_fresh_upload_waits_for_the_next_scan(self):
        self.assertFalse(ds.is_settled(video_info(age_seconds=5), now=NOW, min_age_seconds=60))

    def test_zero_min_age_accepts_everything(self):
        self.assertTrue(ds.is_settled(video_info(age_seconds=0), now=NOW, min_age_seconds=0))

    def test_missing_timestamp_is_settled(self):
        self.assertTrue(ds.is_settled({"id": "v1"}, now=NOW))

    def test_naive_now_is_read_as_utc(self):
        info = video_info(age_seconds=600)
        self.assertTrue(ds.is_settled(info, now=datetime(2026, 11, 21, 12, 0, 0)))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_writes_the_streamed_bytes_and_leaves_no_partial_file(self):
        dest = os.path.join(self.tmp.name, "input", "clip.mp4")
        calls = []

        def fetcher(file_id, handle):
            calls.append(file_id)
            handle.write(b"video-bytes")

        ds.download_video(object(), "file1", dest, fetcher=fetcher)
        self.assertEqual(calls, ["file1"])
        with open(dest, "rb") as handle:
            self.assertEqual(handle.read(), b"video-bytes")
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_empty_download_raises_and_is_cleaned_up(self):
        dest = os.path.join(self.tmp.name, "clip.mp4")
        with self.assertRaises(ds.DriveSyncError):
            ds.download_video(object(), "file1", dest, fetcher=lambda fid, handle: None)
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))


class UploadAndMoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_upload_bodies_carry_name_parent_and_mime_type(self):
        path = os.path.join(self.tmp.name, "clip_artwork.jpg")
        with open(path, "wb") as handle:
            handle.write(b"jpeg")
        service = mock.MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {"id": "new1"}

        created = ds.upload_file(service, path, "ready-folder", media_factory=lambda p: ("media", p))

        self.assertEqual(created["id"], "new1")
        kwargs = service.files.return_value.create.call_args.kwargs
        self.assertEqual(kwargs["body"], {
            "name": "clip_artwork.jpg",
            "parents": ["ready-folder"],
            "mimeType": "image/jpeg",
        })
        self.assertEqual(kwargs["media_body"], ("media", path))

    def test_upload_rejects_a_missing_file(self):
        with self.assertRaises(ds.DriveSyncError):
            ds.upload_file(mock.MagicMock(), os.path.join(self.tmp.name, "gone.txt"), "ready")

    def test_move_reparents_the_file(self):
        service = mock.MagicMock()
        service.files.return_value.update.return_value.execute.return_value = {"id": "f1"}
        ds.move_file(service, "f1", "archive-folder", "raw-folder")
        kwargs = service.files.return_value.update.call_args.kwargs
        self.assertEqual(kwargs["fileId"], "f1")
        self.assertEqual(kwargs["addParents"], "archive-folder")
        self.assertEqual(kwargs["removeParents"], "raw-folder")

    def test_move_without_the_old_parent_still_adds(self):
        service = mock.MagicMock()
        service.files.return_value.update.return_value.execute.return_value = {"id": "f1"}
        ds.move_file(service, "f1", "archive-folder")
        kwargs = service.files.return_value.update.call_args.kwargs
        self.assertNotIn("removeParents", kwargs)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "drive_state.json")

    def test_missing_state_file_is_empty(self):
        self.assertEqual(ds.load_state(self.path)["processed"], {})

    def test_corrupt_state_file_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(ds.load_state(self.path)["processed"], {})

    def test_round_trip_marks_a_file_processed(self):
        state = ds.load_state(self.path)
        ds.mark_processed(state, "f1", name="a.mp4", video_id="yt1", uploaded=["u1"])
        ds.save_state(state, self.path)

        reloaded = ds.load_state(self.path)
        self.assertTrue(ds.is_processed(reloaded, "f1"))
        self.assertEqual(reloaded["processed"]["f1"]["video_id"], "yt1")
        self.assertEqual(reloaded["processed"]["f1"]["uploaded"], ["u1"])
        self.assertFalse(ds.is_processed(reloaded, "f2"))


class SyncOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "drive_state.json")
        self.artifacts = {}
        for name in ("clip_final.mp4", "clip_artwork.jpg", "clip_metadata.txt", "clip_art_metadata.json"):
            path = os.path.join(self.tmp.name, name)
            with open(path, "wb") as handle:
                handle.write(b"x")
            self.artifacts[name] = path
        self.logs = []

    def _pipeline(self, artifacts=None, video_id="yt-123"):
        def pipeline(local_path):
            self.pipeline_input = local_path
            return dict(
                {
                    "final_video": self.artifacts["clip_final.mp4"],
                    "artwork": self.artifacts["clip_artwork.jpg"],
                    "metadata_text": self.artifacts["clip_metadata.txt"],
                    "metadata_json": self.artifacts["clip_art_metadata.json"],
                    "video_id": video_id,
                },
                **(artifacts or {}),
            )
        return pipeline

    def _run(self, videos=None, pipeline=None, **kwargs):
        videos = [video_info()] if videos is None else videos
        download_dir = os.path.join(self.tmp.name, "input")

        def downloader(svc, fid, path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"raw")
            return path

        download = mock.MagicMock(side_effect=downloader)
        upload = mock.MagicMock(side_effect=lambda svc, path, folder: {"id": f"up-{os.path.basename(path)}"})
        mover = mock.MagicMock(return_value={"id": "file1"})
        with mock.patch.object(ds, "list_raw_videos", return_value=videos):
            results = ds.sync_once(
                object(), FOLDERS, pipeline or self._pipeline(),
                download_dir=download_dir,
                state_path=self.state_path,
                now=NOW,
                downloader=download,
                uploader=upload,
                mover=mover,
                log=self.logs.append,
                **kwargs,
            )
        return results, download, upload, mover

    def test_full_cycle_uploads_a_package_and_archives_the_source(self):
        results, download, upload, mover = self._run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(results[0]["video_id"], "yt-123")
        download.assert_called_once()
        self.assertEqual(upload.call_count, 4)
        for call in upload.call_args_list:
            self.assertEqual(call.args[2], FOLDERS.ready)
        mover.assert_called_once_with(mock.ANY, "file1", FOLDERS.archive, FOLDERS.raw)

        state = ds.load_state(self.state_path)
        self.assertTrue(ds.is_processed(state, "file1"))
        self.assertEqual(len(state["processed"]["file1"]["uploaded"]), 4)

    def test_second_scan_does_not_render_the_same_file_again(self):
        self._run()
        results, download, _, _ = self._run()
        self.assertEqual(results, [])
        download.assert_not_called()

    def test_fresh_upload_is_skipped_until_it_settles(self):
        results, download, _, mover = self._run(videos=[video_info(age_seconds=5)])
        self.assertEqual(results, [])
        download.assert_not_called()
        mover.assert_not_called()
        self.assertTrue(any("still uploading" in line for line in self.logs))

    def test_pipeline_failure_keeps_the_file_in_the_raw_folder(self):
        def boom(local_path):
            raise RuntimeError("ffmpeg exploded")

        results, _, upload, mover = self._run(pipeline=boom)
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("ffmpeg exploded", results[0]["error"])
        upload.assert_not_called()
        mover.assert_not_called()
        self.assertFalse(ds.is_processed(ds.load_state(self.state_path), "file1"))

    def test_publish_failure_is_reported_and_not_marked_done(self):
        download = mock.MagicMock(side_effect=lambda svc, fid, path: path)
        upload = mock.MagicMock(return_value={"id": "up"})
        mover = mock.MagicMock(side_effect=ds.DriveSyncError("drive said no"))
        with mock.patch.object(ds, "list_raw_videos", return_value=[video_info()]):
            results = ds.sync_once(
                object(), FOLDERS, self._pipeline(),
                state_path=self.state_path, now=NOW,
                downloader=download, uploader=upload, mover=mover,
                log=self.logs.append,
            )
        self.assertEqual(results[0]["status"], "publish_failed")
        self.assertEqual(len(results[0]["uploaded"]), 4)
        self.assertFalse(ds.is_processed(ds.load_state(self.state_path), "file1"))

    def test_missing_artifacts_are_not_uploaded(self):
        pipeline = self._pipeline({"artwork": os.path.join(self.tmp.name, "missing.jpg")})
        _, _, upload, _ = self._run(pipeline=pipeline)
        self.assertEqual(upload.call_count, 3)

    def test_local_copy_can_be_dropped_after_success(self):
        _, _, _, _ = self._run(keep_local_raw=False)
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "input")), [])


class WatchTests(unittest.TestCase):
    def test_scans_the_configured_number_of_times(self):
        sleeps = []
        with mock.patch.object(ds, "sync_once", return_value=[]) as sync:
            runs = ds.watch(
                object(), FOLDERS, lambda path: {},
                interval_seconds=7,
                max_iterations=3,
                sleep=sleeps.append,
                log=lambda line: None,
            )
        self.assertEqual(len(runs), 3)
        self.assertEqual(sync.call_count, 3)
        self.assertEqual(sleeps, [7, 7])

    def test_scan_errors_do_not_stop_the_watcher(self):
        with mock.patch.object(ds, "sync_once", side_effect=ds.DriveSyncError("quota")):
            runs = ds.watch(
                object(), FOLDERS, lambda path: {},
                max_iterations=2, sleep=lambda seconds: None, log=lambda line: None,
            )
        self.assertEqual(runs, [[], []])


def _has_google_auth():
    try:
        import google.oauth2.credentials  # noqa: F401
    except ImportError:  # pragma: no cover - depends on the install
        return False
    return True


AUTHORIZED_USER = {
    "token": "old-access-token",
    "refresh_token": "refresh-token",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "scopes": ["https://www.googleapis.com/auth/drive"],
}


class CiCredentialTests(unittest.TestCase):
    """The credentials CI hands in instead of a local drive_token.json."""

    def test_no_environment_credential_means_none(self):
        self.assertIsNone(ds.load_credentials_from_env({}))
        self.assertIsNone(
            ds.load_credentials_from_env({ds.ACCESS_TOKEN_ENV: "  ", ds.TOKEN_JSON_ENV: ""})
        )

    def test_invalid_json_names_the_variable(self):
        with self.assertRaises(ds.DriveSyncError) as ctx:
            ds.load_credentials_from_env({ds.TOKEN_JSON_ENV: "{not json"})
        self.assertIn(ds.TOKEN_JSON_ENV, str(ctx.exception))

    def test_json_without_refresh_token_is_rejected(self):
        with self.assertRaises(ds.DriveSyncError) as ctx:
            ds.load_credentials_from_env({ds.TOKEN_JSON_ENV: json.dumps({"token": "t"})})
        self.assertIn("refresh_token", str(ctx.exception))

    @unittest.skipUnless(_has_google_auth(), "google-auth is not installed")
    def test_access_token_is_used_as_is(self):
        creds = ds.load_credentials_from_env({ds.ACCESS_TOKEN_ENV: " token-123 "})
        self.assertEqual(creds.token, "token-123")
        self.assertIsNone(creds.refresh_token)

    @unittest.skipUnless(_has_google_auth(), "google-auth is not installed")
    def test_authorized_user_json_keeps_its_refresh_token(self):
        creds = ds.load_credentials_from_env({ds.TOKEN_JSON_ENV: json.dumps(AUTHORIZED_USER)})
        self.assertEqual(creds.refresh_token, "refresh-token")
        self.assertIn("https://www.googleapis.com/auth/drive", creds.scopes)

    @unittest.skipUnless(_has_google_auth(), "google-auth is not installed")
    def test_dispatched_access_token_wins_over_the_stored_json(self):
        creds = ds.load_credentials_from_env(
            {ds.ACCESS_TOKEN_ENV: "fresh", ds.TOKEN_JSON_ENV: json.dumps(AUTHORIZED_USER)}
        )
        self.assertEqual(creds.token, "fresh")

    def test_build_drive_service_prefers_the_environment(self):
        sentinel = object()
        with mock.patch.object(ds, "load_credentials_from_env", return_value=sentinel) as load:
            with mock.patch("googleapiclient.discovery.build") as build:
                ds.build_drive_service()
        load.assert_called_once_with()
        self.assertIs(build.call_args.kwargs["credentials"], sentinel)


if __name__ == "__main__":
    unittest.main()
