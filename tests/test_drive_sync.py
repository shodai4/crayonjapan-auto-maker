import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import drive_sync as ds

FOLDERS = ds.DriveFolders(raw="raw-folder", ready="ready-folder", archive="archive-folder")
NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)


def modified(age_seconds=3600):
    return (NOW - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")


def video_info(file_id="file1", name="drawing.mp4", age_seconds=3600):
    return {"id": file_id, "name": name, "mimeType": "video/mp4", "modifiedTime": modified(age_seconds)}


def ref(file_id="v1", name="part1.mp4", size=1024, duration=120.0, age_seconds=3600):
    return ds.DriveFileRef(
        id=file_id, name=name, size=size,
        modified_time=modified(age_seconds), duration_seconds=duration,
    )


def video_item(file_id="file1", name="drawing.mp4", age_seconds=3600, duration=120.0):
    """A loose video dropped straight into 01_RAW_VIDEO."""
    return ds.RawItem(
        id=file_id,
        name=name,
        kind=ds.KIND_VIDEO,
        stem=os.path.splitext(name)[0],
        modified_time=modified(age_seconds),
        videos=[ref(file_id, name, duration=duration)],
    )


def project_item(
    item_id="folder1",
    name="GO_20261121_sushi",
    age_seconds=3600,
    videos=None,
    images=None,
):
    return ds.RawItem(
        id=item_id,
        name=name,
        kind=ds.KIND_PROJECT,
        stem=ds.strip_go_prefix(name),
        modified_time=modified(age_seconds),
        videos=videos if videos is not None else [ref("v1", "part1.mp4"), ref("v2", "part2.mp4")],
        images=images if images is not None else [],
    )


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


def service_for_children(children_by_parent):
    """Fake Drive that answers each `'<id>' in parents` query from a dict."""
    service = mock.MagicMock()

    def listing(**kwargs):
        query = str(kwargs.get("q", ""))
        for parent, children in children_by_parent.items():
            if f"'{parent}' in parents" in query:
                return _Response({"files": children})
        return _Response({"files": []})

    service.files.return_value.list.side_effect = listing
    return service


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

    def test_loose_video_still_waits_a_minute(self):
        self.assertFalse(ds.item_is_settled(video_item(age_seconds=30), now=NOW))
        self.assertTrue(ds.item_is_settled(video_item(age_seconds=90), now=NOW))

    def test_go_folder_only_waits_a_few_seconds_after_the_rename(self):
        self.assertFalse(ds.item_is_settled(project_item(age_seconds=1), now=NOW))
        self.assertTrue(ds.item_is_settled(project_item(age_seconds=30), now=NOW))
        self.assertTrue(ds.item_is_settled(project_item(age_seconds=30), now=NOW, min_age_seconds=600))


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


class ReadyPackageFolderTests(unittest.TestCase):
    def test_an_existing_folder_of_that_name_is_reused(self):
        service = service_for_children({
            "ready-folder": [
                {"id": "pkg1", "name": "20261121_sushi_20261121_1830",
                 "mimeType": ds.FOLDER_MIME_TYPE},
            ],
        })
        creator = mock.MagicMock()
        info = ds.ensure_subfolder(
            service, "ready-folder", "20261121_sushi_20261121_1830", creator=creator
        )
        self.assertEqual(info["id"], "pkg1")
        creator.assert_not_called()

    def test_a_new_package_folder_is_created_once(self):
        service = service_for_children({})
        creator = mock.MagicMock(return_value={"id": "pkg-new"})
        info = ds.ensure_subfolder(service, "ready-folder", "20261121_sushi_20261121_1830", creator=creator)
        self.assertEqual(info["id"], "pkg-new")
        creator.assert_called_once_with(service, "20261121_sushi_20261121_1830", "ready-folder")

    def test_the_lookup_asks_only_for_folders_and_escapes_the_name(self):
        service = service_for_children({})
        creator = mock.MagicMock(return_value={"id": "pkg-new"})
        ds.ensure_subfolder(service, "ready-folder", "it's a folder", creator=creator)
        query = service.files.return_value.list.call_args.kwargs["q"]
        self.assertIn(f"mimeType = '{ds.FOLDER_MIME_TYPE}'", query)
        self.assertIn("name = 'it\\'s a folder'", query)

    def test_create_folder_carries_the_folder_mime_type_and_parent(self):
        service = mock.MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {"id": "f-new"}
        created = ds.create_folder(service, "20261121_sushi_20261121_1830", "ready-folder")
        self.assertEqual(created["id"], "f-new")
        kwargs = service.files.return_value.create.call_args.kwargs
        self.assertEqual(kwargs["body"], {
            "name": "20261121_sushi_20261121_1830",
            "mimeType": ds.FOLDER_MIME_TYPE,
            "parents": ["ready-folder"],
        })

    def test_the_package_name_is_the_pipeline_output_folder(self):
        self.assertEqual(
            ds.ready_package_name({"output_folder": "20261121_sushi_20261121_1830"}),
            "20261121_sushi_20261121_1830",
        )

    def test_without_a_named_folder_the_stem_and_the_scan_time_are_used(self):
        self.assertEqual(
            ds.ready_package_name({"base_name": "20261121_sushi"}, now=NOW),
            "20261121_sushi_20261121_1200",
        )
        self.assertEqual(ds.ready_package_name({}, now=NOW), "artwork_20261121_1200")


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


class GoFolderNamingTests(unittest.TestCase):
    def test_only_go_folders_are_picked_up(self):
        for name in ("GO_20261121_sushi", "go_sakura", "Go_cat"):
            self.assertTrue(ds.is_go_folder_name(name), name)
        for name in ("20261121_sushi", "GONZO", "my_GO_folder", ""):
            self.assertFalse(ds.is_go_folder_name(name), name)

    def test_the_prefix_is_stripped_from_the_output_stem(self):
        self.assertEqual(ds.strip_go_prefix("GO_20261121_sushi"), "20261121_sushi")
        self.assertEqual(ds.strip_go_prefix("go_20261121_sushi"), "20261121_sushi")
        self.assertEqual(ds.strip_go_prefix("GO_  spaced  "), "spaced")
        # A bare GO_ keeps its own name instead of becoming an empty stem.
        self.assertEqual(ds.strip_go_prefix("GO_"), "GO_")
        self.assertEqual(ds.strip_go_prefix("sushi"), "sushi")

    def test_natural_sort_keeps_part_numbers_in_order(self):
        names = ["part10.mp4", "part2.mp4", "part1.mp4"]
        self.assertEqual(
            sorted(names, key=ds.natural_sort_key),
            ["part1.mp4", "part2.mp4", "part10.mp4"],
        )

    def test_completion_clip_sorts_after_the_drawing_clips(self):
        names = ["finish.mp4", "part10.mp4", "part2.mp4", "09_final_touch.MOV"]
        self.assertEqual(
            sorted(names, key=ds.natural_sort_key),
            ["part2.mp4", "part10.mp4", "09_final_touch.MOV", "finish.mp4"],
        )

    def test_image_and_video_names_are_told_apart(self):
        for name in ("scan.jpg", "SCAN.JPEG", "art.png"):
            self.assertTrue(ds.is_raw_image_name(name), name)
        for name in ("clip.mp4", "notes.txt", ""):
            self.assertFalse(ds.is_raw_image_name(name), name)


class PendingItemTests(unittest.TestCase):
    def test_go_folder_and_loose_video_are_both_listed(self):
        service = service_for_children({
            "raw-folder": [
                {"id": "f1", "name": "GO_20261121_sushi", "mimeType": ds.FOLDER_MIME_TYPE,
                 "modifiedTime": modified(3600)},
                {"id": "f2", "name": "uploading_sakura", "mimeType": ds.FOLDER_MIME_TYPE,
                 "modifiedTime": modified(3600)},
                {"id": "v9", "name": "old_drawing.mp4", "mimeType": "video/mp4",
                 "modifiedTime": modified(3600)},
                {"id": "t1", "name": "notes.txt", "mimeType": "text/plain",
                 "modifiedTime": modified(3600)},
            ],
            "f1": [
                {"id": "v10", "name": "part10.mp4", "mimeType": "video/mp4", "size": "2048",
                 "modifiedTime": modified(3600),
                 "videoMediaMetadata": {"durationMillis": "90000"}},
                {"id": "v2", "name": "part2.mp4", "mimeType": "video/mp4", "size": "1024",
                 "modifiedTime": modified(3600),
                 "videoMediaMetadata": {"durationMillis": "30000"}},
                {"id": "v3", "name": "finish.mp4", "mimeType": "video/mp4",
                 "modifiedTime": modified(3600)},
                {"id": "i1", "name": "scan.png", "mimeType": "image/png", "size": "5000",
                 "modifiedTime": modified(3600)},
                {"id": "i2", "name": "scan_close.jpg", "mimeType": "image/jpeg",
                 "modifiedTime": modified(3600)},
            ],
        })

        items = ds.list_pending_raw_items(service, "raw-folder")

        self.assertEqual([item.name for item in items], ["GO_20261121_sushi", "old_drawing.mp4"])
        project, loose = items
        self.assertEqual(project.kind, ds.KIND_PROJECT)
        self.assertEqual(project.stem, "20261121_sushi")
        self.assertEqual([clip.name for clip in project.videos], ["part2.mp4", "part10.mp4", "finish.mp4"])
        self.assertEqual([scan.name for scan in project.images], ["scan.png", "scan_close.jpg"])
        self.assertEqual([clip.duration_seconds for clip in project.videos], [30.0, 90.0, None])
        self.assertEqual(project.videos[0].size, 1024)
        self.assertEqual(loose.kind, ds.KIND_VIDEO)
        self.assertEqual(loose.stem, "old_drawing")
        self.assertEqual([clip.name for clip in loose.videos], ["old_drawing.mp4"])

    def test_a_go_folder_without_a_video_is_still_reported(self):
        service = service_for_children({
            "raw-folder": [{"id": "f1", "name": "GO_empty", "mimeType": ds.FOLDER_MIME_TYPE,
                            "modifiedTime": modified(3600)}],
            "f1": [],
        })
        items = ds.list_pending_raw_items(service, "raw-folder")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].videos, [])

    def test_nothing_waiting_means_an_empty_list(self):
        self.assertEqual(ds.list_pending_raw_items(service_for_children({}), "raw-folder"), [])

    def test_the_listing_asks_for_the_clip_durations(self):
        service = service_for_children({"raw-folder": []})
        ds.list_pending_raw_items(service, "raw-folder")
        fields = service.files.return_value.list.call_args.kwargs["fields"]
        self.assertIn("videoMediaMetadata(durationMillis", fields)


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
        self.sources = []

    def _pipeline(self, artifacts=None, video_id="yt-123"):
        def pipeline(source):
            self.sources.append(source)
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

    def _run(self, items=None, pipeline=None, keep_local_raw=True, **kwargs):
        items = [video_item()] if items is None else items
        download_dir = os.path.join(self.tmp.name, "input")

        def downloader(svc, fid, path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"raw")
            return path

        download = mock.MagicMock(side_effect=downloader)
        upload = mock.MagicMock(side_effect=lambda svc, path, folder: {"id": f"up-{os.path.basename(path)}"})
        mover = mock.MagicMock(return_value={"id": "file1"})
        self.folder_creator = mock.MagicMock(return_value={"id": "pkg-folder"})
        with mock.patch.object(ds, "list_pending_raw_items", return_value=items):
            results = ds.sync_once(
                object(), FOLDERS, pipeline or self._pipeline(),
                download_dir=download_dir,
                state_path=self.state_path,
                now=NOW,
                downloader=download,
                uploader=upload,
                mover=mover,
                folder_creator=self.folder_creator,
                log=self.logs.append,
                keep_local_raw=keep_local_raw,
                **kwargs,
            )
        return results, download, upload, mover

    def test_full_cycle_uploads_a_package_and_archives_the_source(self):
        results, download, upload, mover = self._run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(results[0]["video_id"], "yt-123")
        self.assertEqual(len(upload.call_args_list), 4)
        # The four artifacts go together into one package folder of 02_READY_TO_POST.
        self.assertEqual(results[0]["package"], "drawing_20261121_1200")
        self.folder_creator.assert_called_once_with(
            mock.ANY, FOLDERS.ready, "drawing_20261121_1200"
        )
        for call in upload.call_args_list:
            self.assertEqual(call.args[2], "pkg-folder")
        mover.assert_called_once_with(mock.ANY, "file1", FOLDERS.archive, FOLDERS.raw)
        download.assert_not_called()  # the pipeline pulls its own clips

        state = ds.load_state(self.state_path)
        self.assertTrue(ds.is_processed(state, "file1"))
        self.assertEqual(len(state["processed"]["file1"]["uploaded"]), 4)

    def test_the_pipeline_names_the_package_folder(self):
        pipeline = self._pipeline({"output_folder": "20261121_sushi_20261121_1830"})
        results, _, upload, _ = self._run(pipeline=pipeline)
        self.assertEqual(results[0]["package"], "20261121_sushi_20261121_1830")
        self.folder_creator.assert_called_once_with(
            mock.ANY, FOLDERS.ready, "20261121_sushi_20261121_1830"
        )
        for call in upload.call_args_list:
            self.assertEqual(call.args[2], "pkg-folder")

    def test_the_pipeline_receives_what_it_needs_to_download_a_clip(self):
        self._run()
        source = self.sources[0]
        self.assertEqual(source.item_id, "file1")
        self.assertEqual(source.stem, "drawing")
        self.assertEqual(source.kind, ds.KIND_VIDEO)
        self.assertEqual([clip.name for clip in source.videos], ["drawing.mp4"])
        self.assertTrue(callable(source.fetch))
        dest = os.path.join(source.work_dir, "clip.mp4")
        self.assertEqual(source.fetch("file1", dest), dest)
        with open(dest, "rb") as handle:
            self.assertEqual(handle.read(), b"raw")

    def test_a_project_folder_is_archived_as_a_whole(self):
        item = project_item(images=[ref("i1", "scan.png", duration=None)])
        results, _, upload, mover = self._run(items=[item])
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(results[0]["kind"], ds.KIND_PROJECT)
        mover.assert_called_once_with(mock.ANY, "folder1", FOLDERS.archive, FOLDERS.raw)
        source = self.sources[0]
        self.assertEqual(source.stem, "20261121_sushi")
        self.assertEqual(len(source.videos), 2)
        self.assertEqual(len(source.images), 1)
        self.assertEqual(upload.call_count, 4)

    def test_second_scan_does_not_render_the_same_item_again(self):
        self._run()
        results, download, _, _ = self._run()
        self.assertEqual(results, [])
        download.assert_not_called()
        self.assertEqual(len(self.sources), 1)

    def test_fresh_upload_is_skipped_until_it_settles(self):
        results, download, _, mover = self._run(items=[video_item(age_seconds=5)])
        self.assertEqual(results, [])
        download.assert_not_called()
        mover.assert_not_called()
        self.assertTrue(any("still uploading" in line for line in self.logs))

    def test_a_go_folder_without_a_video_is_skipped(self):
        results, download, upload, mover = self._run(items=[project_item(videos=[])])
        self.assertEqual(results, [])
        download.assert_not_called()
        upload.assert_not_called()
        mover.assert_not_called()
        self.assertTrue(any("no video" in line for line in self.logs))

    def test_pipeline_failure_keeps_the_item_in_the_raw_folder(self):
        def boom(source):
            raise RuntimeError("ffmpeg exploded")

        results, _, upload, mover = self._run(pipeline=boom)
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("ffmpeg exploded", results[0]["error"])
        upload.assert_not_called()
        mover.assert_not_called()
        self.assertFalse(ds.is_processed(ds.load_state(self.state_path), "file1"))

    def test_publish_failure_is_reported_and_not_marked_done(self):
        upload = mock.MagicMock(return_value={"id": "up"})
        mover = mock.MagicMock(side_effect=ds.DriveSyncError("drive said no"))
        with mock.patch.object(ds, "list_pending_raw_items", return_value=[video_item()]):
            results = ds.sync_once(
                object(), FOLDERS, self._pipeline(),
                state_path=self.state_path, now=NOW,
                uploader=upload, mover=mover,
                folder_creator=mock.MagicMock(return_value={"id": "pkg-folder"}),
                log=self.logs.append,
            )
        self.assertEqual(results[0]["status"], "publish_failed")

    def test_max_items_renders_one_and_leaves_the_rest_in_the_raw_folder(self):
        items = [
            project_item("folder1", "GO_20261121_sushi"),
            project_item("folder2", "GO_20261122_waffle"),
            project_item("folder3", "GO_20261123_pokemon"),
        ]
        results, _, _, mover = self._run(items=items, max_items=1)

        self.assertEqual([result["file_id"] for result in results], ["folder1"])
        self.assertEqual([source.item_id for source in self.sources], ["folder1"])
        mover.assert_called_once_with(mock.ANY, "folder1", FOLDERS.archive, FOLDERS.raw)
        self.assertTrue(any("2 item(s) stay" in line for line in self.logs))
        state = ds.load_state(self.state_path)
        self.assertTrue(ds.is_processed(state, "folder1"))
        self.assertFalse(ds.is_processed(state, "folder2"))
        self.assertFalse(ds.is_processed(state, "folder3"))

    def test_a_skipped_item_does_not_use_up_the_one_video_cap(self):
        items = [
            project_item("folder0", "GO_20261120_just_renamed", age_seconds=1),
            project_item("folder1", "GO_20261121_sushi"),
        ]
        results, _, _, mover = self._run(items=items, max_items=1)

        self.assertEqual([result["file_id"] for result in results], ["folder1"])
        mover.assert_called_once_with(mock.ANY, "folder1", FOLDERS.archive, FOLDERS.raw)

    def test_without_a_cap_every_waiting_item_is_rendered(self):
        items = [
            project_item("folder1", "GO_20261121_sushi"),
            project_item("folder2", "GO_20261122_waffle"),
        ]
        results, _, _, mover = self._run(items=items)

        self.assertEqual([result["file_id"] for result in results], ["folder1", "folder2"])
        self.assertEqual(mover.call_count, 2)
        self.assertFalse(any("stopping after" in line for line in self.logs))
        self.assertEqual(len(results[0]["uploaded"]), 4)
        self.assertFalse(ds.is_processed(ds.load_state(self.state_path), "file1"))

    def test_missing_artifacts_are_not_uploaded(self):
        pipeline = self._pipeline({"artwork": os.path.join(self.tmp.name, "missing.jpg")})
        _, _, upload, _ = self._run(pipeline=pipeline)
        self.assertEqual(upload.call_count, 3)

    def test_the_work_directory_is_removed_after_success(self):
        def pipeline(source):
            os.makedirs(source.work_dir, exist_ok=True)
            with open(os.path.join(source.work_dir, "raw.mp4"), "wb") as handle:
                handle.write(b"big")
            return self._pipeline()(source)

        self._run(pipeline=pipeline, keep_local_raw=False)
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "input")), [])

    def test_keep_local_raw_leaves_the_work_directory(self):
        def pipeline(source):
            os.makedirs(source.work_dir, exist_ok=True)
            with open(os.path.join(source.work_dir, "raw.mp4"), "wb") as handle:
                handle.write(b"big")
            return self._pipeline()(source)

        self._run(pipeline=pipeline, keep_local_raw=True)
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "input")), ["file1_drawing"])

    def test_one_failing_item_does_not_stop_the_next(self):
        calls = []

        def pipeline(source):
            calls.append(source.name)
            if source.name.endswith("bad.mp4"):
                raise RuntimeError("bad clip")
            return self._pipeline()(source)

        results, _, _, _ = self._run(
            items=[video_item("bad", "bad.mp4"), video_item("good", "good.mp4")],
            pipeline=pipeline,
        )
        self.assertEqual([result["status"] for result in results], ["failed", "done"])
        self.assertEqual(len(calls), 2)


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
