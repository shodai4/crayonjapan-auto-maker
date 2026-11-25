"""Local (Windows, Drive-free) folder hub: layout, queue, publish, archive."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import drive_sync
import local_hub


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def touch_old(path, seconds=600):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    os.utime(path, (stamp.timestamp(), stamp.timestamp()))


class BaseDirTests(unittest.TestCase):
    def test_windows_gets_a_short_root_folder(self):
        self.assertEqual(local_hub.default_base_dir("nt", home="C:\\Users\\a"), "C:\\CrayonJapan")

    def test_other_systems_get_a_home_folder(self):
        self.assertEqual(
            local_hub.default_base_dir("posix", home="/home/a"), "/home/a/CrayonJapan"
        )

    def test_the_argument_wins_over_the_environment_and_the_config(self):
        folders = local_hub.load_local_folders(
            base_dir="/tmp/from-arg",
            config={"local": {"base_dir": "/tmp/from-config"}},
            environ={"CRAYONJAPAN_HOME": "/tmp/from-env"},
        )
        self.assertEqual(folders.base, os.path.abspath("/tmp/from-arg"))

    def test_the_environment_wins_over_the_config(self):
        folders = local_hub.load_local_folders(
            config={"local": {"base_dir": "/tmp/from-config"}},
            environ={"CRAYONJAPAN_HOME": "/tmp/from-env"},
        )
        self.assertEqual(folders.base, os.path.abspath("/tmp/from-env"))

    def test_the_config_is_used_when_nothing_else_is_given(self):
        folders = local_hub.load_local_folders(
            config={"local": {"base_dir": "/tmp/from-config"}}, environ={}
        )
        self.assertEqual(folders.base, os.path.abspath("/tmp/from-config"))

    def test_a_missing_everything_falls_back_to_the_default(self):
        folders = local_hub.load_local_folders(config={}, environ={})
        self.assertEqual(folders.base, local_hub.default_base_dir())

    def test_a_quoted_windows_path_is_cleaned(self):
        folders = local_hub.load_local_folders(base_dir='"C:\\CrayonJapan"')
        self.assertEqual(folders.base, os.path.abspath("C:\\CrayonJapan"))

    def test_the_three_folders_and_the_state_file_live_under_the_base(self):
        folders = local_hub.load_local_folders(base_dir="C:\\CrayonJapan")
        self.assertEqual(
            os.path.basename(folders.raw), local_hub.RAW_DIRNAME
        )
        self.assertEqual(os.path.basename(folders.ready), local_hub.READY_DIRNAME)
        self.assertEqual(os.path.basename(folders.archive), local_hub.ARCHIVE_DIRNAME)
        self.assertEqual(
            os.path.basename(folders.state_path), local_hub.DEFAULT_STATE_NAME
        )
        self.assertEqual(
            os.path.dirname(folders.state_path), os.path.dirname(folders.raw)
        )

    def test_the_state_file_name_can_be_changed(self):
        folders = local_hub.load_local_folders(
            base_dir="/tmp/base", config={"local": {"state_file": "done.json"}}
        )
        self.assertEqual(os.path.basename(folders.state_path), "done.json")

    def test_the_work_output_holds_the_render_before_it_is_moved(self):
        folders = local_hub.load_local_folders(base_dir="/tmp/base")
        self.assertTrue(folders.output.startswith(folders.work))
        self.assertEqual(os.path.basename(folders.output), local_hub.OUTPUT_SUBDIR)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folders = local_hub.load_local_folders(
            base_dir=os.path.join(self.tmp.name, "CrayonJapan"), environ={}
        )

    def test_nothing_is_created_until_it_is_asked_for(self):
        self.assertFalse(os.path.isdir(self.folders.base))
        self.assertEqual(len(local_hub.missing_folders(self.folders)), 3)

    def test_ensure_creates_every_folder_used_by_a_run(self):
        local_hub.ensure_local_layout(self.folders)
        for path in (
            self.folders.raw,
            self.folders.ready,
            self.folders.archive,
            self.folders.work,
            self.folders.output,
            self.folders.logs,
        ):
            self.assertTrue(os.path.isdir(path), path)
        self.assertEqual(local_hub.missing_folders(self.folders), [])

    def test_ensure_is_idempotent(self):
        local_hub.ensure_local_layout(self.folders)
        local_hub.ensure_local_layout(self.folders)
        self.assertTrue(os.path.isdir(self.folders.raw))

    def test_the_layout_description_names_the_three_folders(self):
        text = local_hub.describe_layout(self.folders)
        for name in (local_hub.RAW_DIRNAME, local_hub.READY_DIRNAME, local_hub.ARCHIVE_DIRNAME):
            self.assertIn(name, text)

    def test_every_check_serialises_for_a_report(self):
        data = self.folders.as_dict()
        self.assertEqual(data["base"], self.folders.base)
        self.assertEqual(data["state"], self.folders.state_path)

    def test_the_task_name_comes_from_the_config(self):
        self.assertEqual(
            local_hub.local_task_name({"local": {"task_name": "My Job"}}), "My Job"
        )
        self.assertEqual(local_hub.local_task_name({}), "CrayonJapan Auto-Maker")


class ListItemsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folders = local_hub.ensure_local_layout(
            local_hub.load_local_folders(
                base_dir=os.path.join(self.tmp.name, "base"), environ={}
            )
        )

    def test_an_empty_folder_has_nothing_waiting(self):
        self.assertEqual(local_hub.list_local_items(self.folders), [])

    def test_a_missing_raw_folder_is_not_an_error(self):
        folders = local_hub.load_local_folders(
            base_dir=os.path.join(self.tmp.name, "not-created"), environ={}
        )
        self.assertEqual(local_hub.list_local_items(folders), [])

    def test_a_go_folder_is_a_project_with_its_clips_and_scan(self):
        folder = os.path.join(self.folders.raw, "GO_20261121_sushi")
        write(os.path.join(folder, "part2.mp4"))
        write(os.path.join(folder, "part10.mp4"))
        write(os.path.join(folder, "finish.mp4"))
        write(os.path.join(folder, "scan.jpg"))
        items = local_hub.list_local_items(self.folders)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item.is_project)
        self.assertEqual(item.name, "GO_20261121_sushi")
        self.assertEqual(item.stem, "20261121_sushi")
        # natural order, and the completion clip last
        self.assertEqual(
            [clip.name for clip in item.videos],
            ["part2.mp4", "part10.mp4", "finish.mp4"],
        )
        self.assertEqual([image.name for image in item.images], ["scan.jpg"])
        self.assertEqual(item.videos[0].id, os.path.join(folder, "part2.mp4"))
        self.assertGreater(item.videos[0].size, 0)

    def test_a_folder_without_the_go_prefix_is_still_being_filled(self):
        write(os.path.join(self.folders.raw, "20261121_sushi", "part1.mp4"))
        self.assertEqual(local_hub.list_local_items(self.folders), [])

    def test_the_go_prefix_is_case_insensitive(self):
        write(os.path.join(self.folders.raw, "go_cat", "part1.mp4"))
        items = local_hub.list_local_items(self.folders)
        self.assertEqual([item.stem for item in items], ["cat"])

    def test_a_loose_recording_is_the_single_video_flow(self):
        write(os.path.join(self.folders.raw, "drawing.mp4"))
        items = local_hub.list_local_items(self.folders)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0].is_project)
        self.assertEqual(items[0].kind, drive_sync.KIND_VIDEO)
        self.assertEqual(items[0].stem, "drawing")

    def test_other_files_and_folders_are_left_alone(self):
        write(os.path.join(self.folders.raw, "notes.txt"))
        write(os.path.join(self.folders.raw, "thumb.JPG"))
        os.makedirs(os.path.join(self.folders.raw, "GO_empty"))
        items = local_hub.list_local_items(self.folders)
        self.assertEqual([item.name for item in items], ["GO_empty"])
        self.assertEqual(items[0].videos, [])

    def test_the_queue_is_named_in_natural_order(self):
        for name in ("GO_20261122_cat", "GO_20261121_sushi", "GO_20261121_apple"):
            write(os.path.join(self.folders.raw, name, "part1.mp4"))
        items = local_hub.list_local_items(self.folders)
        self.assertEqual(
            [item.stem for item in items],
            ["20261121_apple", "20261121_sushi", "20261122_cat"],
        )

    def test_a_settled_item_is_renderable_but_a_fresh_one_is_not(self):
        write(os.path.join(self.folders.raw, "drawing.mp4"))
        item = local_hub.list_local_items(self.folders)[0]
        now = datetime.now(timezone.utc)
        self.assertFalse(
            local_hub.item_is_settled(item, now=now, min_age_seconds=60, go_min_age_seconds=5)
        )
        self.assertTrue(
            local_hub.item_is_settled(
                item, now=now + timedelta(seconds=120),
                min_age_seconds=60, go_min_age_seconds=5,
            )
        )

    def test_a_go_folder_only_waits_a_few_seconds(self):
        write(os.path.join(self.folders.raw, "GO_cat", "part1.mp4"))
        item = local_hub.list_local_items(self.folders)[0]
        now = datetime.now(timezone.utc)
        self.assertTrue(
            local_hub.item_is_settled(
                item, now=now + timedelta(seconds=10),
                min_age_seconds=60, go_min_age_seconds=5,
            )
        )


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folders = local_hub.ensure_local_layout(
            local_hub.load_local_folders(
                base_dir=os.path.join(self.tmp.name, "base"), environ={}
            )
        )
        folder = os.path.join(self.folders.raw, "GO_sushi")
        write(os.path.join(folder, "part1.mp4"), b"a" * 10)
        write(os.path.join(folder, "scan.jpg"), b"i" * 4)
        touch_old(folder)
        self.item = local_hub.list_local_items(self.folders)[0]

    def test_a_clip_length_is_read_when_the_hub_has_none(self):
        seen = []

        def prober(path):
            seen.append(path)
            return 12.5

        source = local_hub.source_for(self.item, self.folders, prober=prober)
        self.assertEqual([clip.duration_seconds for clip in source.videos], [12.5])
        self.assertEqual(len(seen), 1)
        self.assertTrue(source.is_project)

    def test_a_known_length_is_not_probed_again(self):
        item = drive_sync.RawItem(
            id=self.item.id,
            name=self.item.name,
            kind=self.item.kind,
            stem=self.item.stem,
            modified_time=self.item.modified_time,
            videos=[
                drive_sync.DriveFileRef(
                    id=os.path.join(self.item.id, "part1.mp4"), name="part1.mp4",
                    duration_seconds=30.0,
                )
            ],
            images=[],
        )
        source = local_hub.source_for(item, self.folders, prober=lambda path: 1.0)
        self.assertEqual(source.videos[0].duration_seconds, 30.0)

    def test_the_work_folder_is_unique_per_item(self):
        other = drive_sync.RawItem(
            id=self.item.id + "-second", name="GO_other", kind=self.item.kind,
            stem="other", modified_time=self.item.modified_time,
            videos=[], images=[],
        )
        first = local_hub.source_for(self.item, self.folders, prober=lambda path: 1.0)
        second = local_hub.source_for(other, self.folders, prober=lambda path: 1.0)
        self.assertNotEqual(first.work_dir, second.work_dir)

    def test_a_fetch_links_instead_of_copying(self):
        source = local_hub.source_for(self.item, self.folders, prober=lambda path: 1.0)
        clip = source.videos[0]
        target = os.path.join(source.work_dir, "local.mp4")
        source.fetch(clip.id, target)
        self.assertTrue(os.path.exists(target))
        self.assertEqual(os.path.getsize(target), 10)
        # deleting the work copy leaves the original where it is
        os.remove(target)
        self.assertTrue(os.path.exists(clip.id))

    def test_a_link_is_used_when_the_file_system_allows_it(self):
        source_path = write(os.path.join(self.tmp.name, "src.mp4"), b"abc")
        target = os.path.join(self.tmp.name, "link.mp4")
        local_hub.link_or_copy(source_path, target)
        self.assertEqual(os.path.getsize(target), 3)
        try:
            self.assertTrue(os.path.samefile(source_path, target))
        except OSError:  # a file system without inode identity
            pass

    def test_a_file_system_without_links_still_gets_the_file(self):
        source_path = write(os.path.join(self.tmp.name, "src2.mp4"), b"abcd")
        target = os.path.join(self.tmp.name, "copy.mp4")
        original_link = os.link

        def refuse(*args, **kwargs):
            raise OSError("no links here")

        os.link = refuse
        try:
            local_hub.link_or_copy(source_path, target)
        finally:
            os.link = original_link
        self.assertEqual(os.path.getsize(target), 4)
        self.assertFalse(os.path.samefile(source_path, target))

    def test_an_existing_destination_is_replaced(self):
        source_path = write(os.path.join(self.tmp.name, "src3.mp4"), b"xy")
        target = write(os.path.join(self.tmp.name, "old.mp4"), b"0123456789")
        local_hub.link_or_copy(source_path, target)
        self.assertEqual(os.path.getsize(target), 2)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folders = local_hub.ensure_local_layout(
            local_hub.load_local_folders(
                base_dir=os.path.join(self.tmp.name, "base"), environ={}
            )
        )

    def _artifacts(self, name="20261121_sushi", make=("final_video", "artwork", "metadata_text", "metadata_json")):
        source_dir = os.path.join(self.folders.output, name)
        mapping = {
            "final_video": "final.mp4",
            "artwork": "artwork.jpg",
            "metadata_text": "metadata.txt",
            "metadata_json": "art.json",
        }
        artifacts = {"output_folder": name, "base_name": "20261121_sushi", "video_id": "yt-1"}
        for key in make:
            artifacts[key] = write(os.path.join(source_dir, mapping[key]))
        return artifacts

    def test_the_four_artifacts_move_into_their_package(self):
        artifacts = self._artifacts()
        published = local_hub.move_artifacts(artifacts, self.folders, "20261121_sushi_20261121_1830")
        package = os.path.join(self.folders.ready, "20261121_sushi_20261121_1830")
        self.assertEqual(len(published), 4)
        self.assertEqual(
            sorted(os.listdir(package)),
            ["art.json", "artwork.jpg", "final.mp4", "metadata.txt"],
        )
        # moved, not copied
        self.assertFalse(os.path.exists(artifacts["final_video"]))

    def test_a_missing_artifact_is_reported_by_omission(self):
        artifacts = self._artifacts(make=("final_video",))
        artifacts["artwork"] = os.path.join(self.tmp.name, "gone.jpg")
        published = local_hub.move_artifacts(artifacts, self.folders, "pkg")
        self.assertEqual([os.path.basename(path) for path in published], ["final.mp4"])

    def test_an_artifact_already_in_place_is_not_moved_again(self):
        package = "pkg"
        target = write(os.path.join(self.folders.ready, package, "final.mp4"))
        published = local_hub.move_artifacts(
            {"final_video": target}, self.folders, package
        )
        self.assertEqual(published, [target])

    def test_an_existing_file_of_the_same_name_is_kept(self):
        package = "pkg"
        write(os.path.join(self.folders.ready, package, "final.mp4"), b"old")
        artifacts = self._artifacts(make=("final_video",))
        published = local_hub.move_artifacts(artifacts, self.folders, package)
        self.assertTrue(os.path.basename(published[0]).startswith("final"))
        self.assertNotEqual(os.path.basename(published[0]), "final.mp4")
        self.assertTrue(os.path.exists(os.path.join(self.folders.ready, package, "final.mp4")))

    def test_a_rendered_folder_moves_to_the_archive(self):
        folder = os.path.join(self.folders.raw, "GO_sushi")
        write(os.path.join(folder, "part1.mp4"))
        item = local_hub.list_local_items(self.folders)[0]
        archived = local_hub.archive_item(item, self.folders)
        self.assertTrue(os.path.isdir(archived))
        self.assertEqual(os.path.basename(archived), "GO_sushi")
        self.assertFalse(os.path.exists(folder))

    def test_keeping_the_source_leaves_it_in_the_raw_folder(self):
        folder = os.path.join(self.folders.raw, "GO_keep")
        write(os.path.join(folder, "part1.mp4"))
        item = local_hub.list_local_items(self.folders)[0]
        self.assertEqual(local_hub.archive_item(item, self.folders, archive=False), "")
        self.assertTrue(os.path.exists(folder))

    def test_two_renders_of_one_name_never_overwrite_each_other(self):
        first = write(os.path.join(self.folders.archive, "GO_sushi", "part1.mp4"))
        del first
        second = local_hub.unique_path(os.path.join(self.folders.archive, "GO_sushi"))
        self.assertTrue(second.endswith("GO_sushi_2"))

    def test_a_free_name_is_returned_unchanged(self):
        path = os.path.join(self.folders.archive, "GO_new")
        self.assertEqual(local_hub.unique_path(path), path)


class SyncOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folders = local_hub.ensure_local_layout(
            local_hub.load_local_folders(
                base_dir=os.path.join(self.tmp.name, "base"), environ={}
            )
        )
        self.now = datetime.now(timezone.utc) + timedelta(minutes=5)

    def _project(self, name="GO_20261121_sushi", clips=("part1.mp4", "part2.mp4")):
        folder = os.path.join(self.folders.raw, name)
        for clip in clips:
            write(os.path.join(folder, clip), b"v" * 8)
        write(os.path.join(folder, "scan.jpg"), b"i" * 4)
        touch_old(folder)
        return folder

    def _pipeline(self, calls):
        def pipeline(item):
            calls.append(item.name)
            package = f"{item.stem}_20261121_1830"
            source = os.path.join(self.folders.output, package)
            artifacts = {
                "base_name": item.stem,
                "output_folder": package,
                "video_id": "yt-1",
                "final_video": write(os.path.join(source, "final.mp4")),
                "artwork": write(os.path.join(source, "artwork.jpg")),
                "metadata_text": write(os.path.join(source, "metadata.txt")),
                "metadata_json": write(os.path.join(source, "art.json")),
            }
            return artifacts

        return pipeline

    def test_one_artwork_is_rendered_published_and_archived(self):
        folder = self._project()
        calls = []
        results = local_hub.sync_once(
            self.folders, self._pipeline(calls), now=self.now, log=lambda *_: None
        )
        self.assertEqual(calls, ["GO_20261121_sushi"])
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(results[0]["video_id"], "yt-1")
        self.assertEqual(results[0]["package"], "20261121_sushi_20261121_1830")
        package = os.path.join(self.folders.ready, "20261121_sushi_20261121_1830")
        self.assertEqual(len(os.listdir(package)), 4)
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.isdir(os.path.join(self.folders.archive, "GO_20261121_sushi")))

    def test_the_state_file_remembers_the_item(self):
        self._project()
        local_hub.sync_once(
            self.folders, self._pipeline([]), now=self.now, log=lambda *_: None
        )
        state = local_hub.load_state(self.folders.state_path)
        self.assertEqual(len(state["processed"]), 1)
        self.assertEqual(local_hub.state_summary(self.folders.state_path), {"done": 1})

    def test_a_processed_item_is_never_rendered_twice(self):
        self._project()
        local_hub.sync_once(
            self.folders, self._pipeline([]), now=self.now, log=lambda *_: None
        )
        calls = []
        results = local_hub.sync_once(
            self.folders, self._pipeline(calls), now=self.now, log=lambda *_: None
        )
        self.assertEqual(calls, [])
        self.assertEqual(results, [])

    def test_a_failed_render_stays_in_the_raw_folder(self):
        folder = self._project()

        def pipeline(item):
            raise RuntimeError("ffmpeg said no")

        results = local_hub.sync_once(
            self.folders, pipeline, now=self.now, log=lambda *_: None
        )
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("ffmpeg said no", results[0]["error"])
        self.assertTrue(os.path.exists(folder))
        self.assertEqual(local_hub.state_summary(self.folders.state_path), {})

    def test_a_render_without_artifacts_is_a_publish_failure(self):
        folder = self._project()
        results = local_hub.sync_once(
            self.folders, lambda item: {"output_folder": "pkg"}, now=self.now,
            log=lambda *_: None,
        )
        self.assertEqual(results[0]["status"], "publish_failed")
        self.assertTrue(os.path.exists(folder))

    def test_the_cap_leaves_the_rest_for_the_next_slot(self):
        for name in ("GO_20261121_a", "GO_20261122_b", "GO_20261123_c"):
            self._project(name)
        calls = []
        results = local_hub.sync_once(
            self.folders, self._pipeline(calls), now=self.now, max_items=1,
            log=lambda *_: None,
        )
        self.assertEqual(calls, ["GO_20261121_a"])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(local_hub.list_local_items(self.folders)), 2)

    def test_a_still_copying_recording_is_skipped(self):
        write(os.path.join(self.folders.raw, "drawing.mp4"))
        calls = []
        results = local_hub.sync_once(
            self.folders, self._pipeline(calls), now=datetime.now(timezone.utc),
            min_age_seconds=60, log=lambda *_: None,
        )
        self.assertEqual(calls, [])
        self.assertEqual(results, [])

    def test_an_empty_go_folder_is_skipped(self):
        os.makedirs(os.path.join(self.folders.raw, "GO_empty"))
        touch_old(os.path.join(self.folders.raw, "GO_empty"))
        calls = []
        local_hub.sync_once(
            self.folders, self._pipeline(calls), now=self.now, log=lambda *_: None
        )
        self.assertEqual(calls, [])

    def test_a_dry_run_renders_and_creates_nothing(self):
        self._project()
        calls = []
        results = local_hub.sync_once(
            self.folders, self._pipeline(calls), now=self.now, dry_run=True,
            log=lambda *_: None,
        )
        self.assertEqual(calls, [])
        self.assertEqual(results[0]["status"], "planned")
        self.assertEqual(results[0]["clips"], 2)
        self.assertEqual(results[0]["scans"], 1)
        self.assertEqual(os.listdir(self.folders.ready), [])
        self.assertFalse(os.path.exists(self.folders.state_path))

    def test_the_cap_is_respected_in_a_dry_run_too(self):
        for name in ("GO_a", "GO_b"):
            self._project(name)
        results = local_hub.sync_once(
            self.folders, self._pipeline([]), now=self.now, max_items=1,
            dry_run=True, log=lambda *_: None,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "planned")

    def test_keeping_the_raw_folder_is_honoured(self):
        folder = self._project()
        local_hub.sync_once(
            self.folders, self._pipeline([]), now=self.now,
            archive_after_render=False, log=lambda *_: None,
        )
        self.assertTrue(os.path.exists(folder))
        # still recorded, so a second slot does not render it again
        self.assertEqual(local_hub.state_summary(self.folders.state_path), {"done": 1})

    def test_the_render_leftovers_are_cleaned_up(self):
        self._project()
        local_hub.sync_once(
            self.folders, self._pipeline([]), now=self.now, log=lambda *_: None
        )
        self.assertEqual(os.listdir(self.folders.output), [])

    def test_the_leftovers_are_kept_when_asked(self):
        self._project()
        local_hub.sync_once(
            self.folders, self._pipeline([]), now=self.now, cleanup=False,
            log=lambda *_: None,
        )
        self.assertEqual(len(os.listdir(self.folders.output)), 1)

    def test_nothing_outside_the_work_area_is_ever_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = os.path.join(tmp, "keep")
            write(os.path.join(keep, "final.mp4"))
            removed = local_hub.cleanup_render_folder(
                {"final_video": os.path.join(keep, "final.mp4")},
                self.folders, log=lambda *_: None,
            )
            self.assertFalse(removed)
            self.assertTrue(os.path.exists(os.path.join(keep, "final.mp4")))

    def test_a_custom_state_path_is_used(self):
        self._project()
        state_path = os.path.join(self.tmp.name, "elsewhere", "state.json")
        local_hub.sync_once(
            self.folders, self._pipeline([]), now=self.now, state_path=state_path,
            log=lambda *_: None,
        )
        self.assertTrue(os.path.exists(state_path))
        self.assertFalse(os.path.exists(self.folders.state_path))

    def test_the_scan_never_crashes_on_an_unreadable_folder(self):
        local_hub.sync_once(
            local_hub.load_local_folders(
                base_dir=os.path.join(self.tmp.name, "gone"), environ={}
            ),
            self._pipeline([]), now=self.now, log=lambda *_: None,
        )


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folders = local_hub.ensure_local_layout(
            local_hub.load_local_folders(
                base_dir=os.path.join(self.tmp.name, "base"), environ={}
            )
        )

    def test_the_watcher_scans_on_its_interval_and_stops_on_the_cap(self):
        waits = []
        runs = local_hub.watch(
            self.folders, lambda item: {},
            interval_seconds=7, max_iterations=3,
            sleep=waits.append, log=lambda *_: None,
        )
        self.assertEqual(len(runs), 3)
        self.assertEqual(waits, [7, 7])

    def test_a_watch_cycle_renders_what_is_waiting(self):
        folder = os.path.join(self.folders.raw, "GO_a")
        write(os.path.join(folder, "part1.mp4"))
        touch_old(folder)
        calls = []

        def pipeline(item):
            calls.append(item.name)
            package = "a_pkg"
            source = os.path.join(self.folders.output, package)
            return {
                "output_folder": package,
                "final_video": write(os.path.join(source, "final.mp4")),
            }

        local_hub.watch(
            self.folders, pipeline, interval_seconds=0, max_iterations=1,
            sleep=lambda seconds: None, log=lambda *_: None,
        )
        self.assertEqual(calls, ["GO_a"])


class ReportTests(unittest.TestCase):
    def test_nothing_waiting(self):
        self.assertIn("Nothing", local_hub.format_results([]))

    def test_each_status_has_a_plain_label(self):
        text = local_hub.format_results(
            [
                {"name": "GO_a", "status": "done", "package": "a_1"},
                {"name": "GO_b", "status": "planned", "clips": 2, "scans": 1},
                {"name": "GO_c", "status": "failed", "error": "boom"},
            ]
        )
        self.assertIn("GO_a: rendered + published", text)
        self.assertIn("GO_b: would render (2 clip(s), 1 scan(s))", text)
        self.assertIn("GO_c: failed", text)

    def test_a_scan_log_is_appended_with_its_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "logs", "run_2026-11-26.log")
            local_hub.write_scan_log(path, "first")
            local_hub.write_scan_log(path, "second")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        self.assertEqual(text.count("====="), 4)
        self.assertIn("first", text)
        self.assertIn("second", text)

    def test_free_space_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = local_hub.free_space_bytes(tmp)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_free_space_of_a_missing_folder_reads_its_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = local_hub.free_space_bytes(os.path.join(tmp, "not", "yet"))
        self.assertIsNotNone(value)

    def test_json_output_is_stable(self):
        self.assertEqual(local_hub.dump_json({"b": 1, "a": 2}), '{"a": 2, "b": 1}')


if __name__ == "__main__":
    unittest.main()
