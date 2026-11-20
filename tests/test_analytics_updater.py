import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import analytics_updater as au
from youtube_uploader import build_analytics_query

COLUMNS = au.CSV_COLUMNS
TODAY = dt.date(2026, 11, 20)


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeVideos:
    def __init__(self, stats):
        self.stats = stats
        self.batches = []

    def list(self, part=None, id=None):
        self.batches.append(id.split(",") if id else [])
        items = [
            {"id": vid, "statistics": self.stats[vid]}
            for vid in (id.split(",") if id else [])
            if vid in self.stats
        ]
        return FakeRequest({"items": items})


class FakeYouTube:
    def __init__(self, stats):
        self._videos = FakeVideos(stats)

    def videos(self):
        return self._videos


class FakeReports:
    def __init__(self, values, failing=(), empty=(), recorder=None):
        self.values = values
        self.failing = set(failing)
        self.empty = set(empty)
        self.recorder = recorder if recorder is not None else []
        self.error = None

    def query(self, **kwargs):
        self.recorder.append(kwargs)
        vid = kwargs["filters"].split("==")[1]
        if vid in self.failing:
            raise RuntimeError("quota exceeded")
        if vid in self.empty:
            return FakeRequest({"rows": []})
        return FakeRequest({"rows": [[kwargs["startDate"], kwargs["endDate"], self.values[vid]]]})


class FakeAnalytics:
    def __init__(self, **kwargs):
        self._reports = FakeReports(**kwargs)
        self.queries = self._reports.recorder

    def reports(self):
        return self._reports


def make_csv(path, rows):
    pd.DataFrame(rows, columns=COLUMNS).to_csv(path, index=False)


class UpdatePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")

    def _rows(self):
        return [
            {
                "video_id": "v1", "date": "2026-11-01", "title": "A", "style_used": "style_B_story",
                "hook_type": "finish_preview_2.5s", "views": 0, "avg_view_percentage": 55.0,
                "likes": 0, "comments_count": 0, "top_feedback": "",
            },
            {
                "video_id": "v2", "date": "2026-11-10", "title": "B", "style_used": "style_C_asmr",
                "hook_type": "finish_preview_2.5s", "views": 0, "avg_view_percentage": 0.0,
                "likes": 0, "comments_count": 0, "top_feedback": "",
            },
        ]

    def test_updates_stats_and_average_view_percentage(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({
            "v1": {"viewCount": "1200", "likeCount": "34", "commentCount": "5"},
            "v2": {"viewCount": "10"},
        })
        analytics = FakeAnalytics(values={"v1": 62.536, "v2": 41.0})

        df = au.update_performance_metrics(
            self.csv, youtube=youtube, analytics=analytics, today=TODAY
        )

        self.assertEqual(int(df.loc[0, "views"]), 1200)
        self.assertEqual(int(df.loc[0, "likes"]), 34)
        self.assertEqual(int(df.loc[0, "comments_count"]), 5)
        self.assertAlmostEqual(float(df.loc[0, "avg_view_percentage"]), 62.54)
        self.assertAlmostEqual(float(df.loc[1, "avg_view_percentage"]), 41.0)
        self.assertEqual(int(df.loc[1, "likes"]), 0)

        saved = pd.read_csv(self.csv)
        self.assertAlmostEqual(float(saved.loc[0, "avg_view_percentage"]), 62.54)

    def test_analytics_failure_keeps_existing_values(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({"v1": {"viewCount": "7"}})
        analytics = FakeAnalytics(values={"v2": 48.0}, failing={"v1"})

        with mock.patch("sys.stderr"):
            df = au.update_performance_metrics(
                self.csv, youtube=youtube, analytics=analytics, today=TODAY
            )

        self.assertAlmostEqual(float(df.loc[0, "avg_view_percentage"]), 55.0)
        self.assertEqual(int(df.loc[0, "views"]), 7)
        self.assertAlmostEqual(float(df.loc[1, "avg_view_percentage"]), 48.0)

    def test_empty_analytics_rows_keep_existing_values(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({})
        analytics = FakeAnalytics(values={}, empty={"v1", "v2"})

        with mock.patch("sys.stderr"):
            df = au.update_performance_metrics(
                self.csv, youtube=youtube, analytics=analytics, today=TODAY
            )

        self.assertAlmostEqual(float(df.loc[0, "avg_view_percentage"]), 55.0)
        self.assertAlmostEqual(float(df.loc[1, "avg_view_percentage"]), 0.0)

    def test_analytics_client_failure_does_not_abort_run(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({"v1": {"viewCount": "3"}})

        with mock.patch("sys.stderr"), \
                mock.patch.object(
                    au, "get_youtube_analytics_service",
                    side_effect=RuntimeError("consent required"),
                ):
            df = au.update_performance_metrics(self.csv, youtube=youtube, today=TODAY)

        self.assertEqual(int(df.loc[0, "views"]), 3)
        self.assertAlmostEqual(float(df.loc[0, "avg_view_percentage"]), 55.0)

    def test_analytics_can_be_skipped(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({"v1": {"viewCount": "3"}})
        analytics = FakeAnalytics(values={"v1": 90.0})

        df = au.update_performance_metrics(
            self.csv, youtube=youtube, analytics=analytics, with_analytics=False, today=TODAY
        )

        self.assertEqual(analytics.queries, [])
        self.assertAlmostEqual(float(df.loc[0, "avg_view_percentage"]), 55.0)

    def test_empty_csv_is_reported(self):
        make_csv(self.csv, [])
        df = au.update_performance_metrics(
            self.csv, youtube=FakeYouTube({}), analytics=FakeAnalytics(values={}), today=TODAY
        )
        self.assertTrue(df.empty)

    def test_query_uses_upload_date_and_today(self):
        make_csv(self.csv, self._rows())
        analytics = FakeAnalytics(values={"v1": 60.0, "v2": 60.0})

        au.update_performance_metrics(
            self.csv, youtube=FakeYouTube({}), analytics=analytics, today=TODAY
        )

        queries = {q["filters"]: q for q in analytics.queries}
        self.assertEqual(queries["video==v1"]["startDate"], "2026-11-01")
        self.assertEqual(queries["video==v1"]["endDate"], "2026-11-20")
        self.assertEqual(queries["video==v1"]["ids"], "channel==MINE")
        self.assertEqual(queries["video==v1"]["metrics"], "averageViewPercentage")

    def test_future_date_is_clamped_to_today(self):
        rows = self._rows()
        rows[0]["date"] = "2027-01-01"
        make_csv(self.csv, rows)
        analytics = FakeAnalytics(values={"v1": 60.0})

        au.update_performance_metrics(
            self.csv, youtube=FakeYouTube({}), analytics=analytics, today=TODAY
        )

        start = analytics.queries[0]["startDate"]
        self.assertLessEqual(start, "2026-11-20")


class FetchTests(unittest.TestCase):
    def test_list_requests_are_batched_by_fifty(self):
        ids = [f"v{i}" for i in range(60)]
        youtube = FakeYouTube({vid: {"viewCount": "1"} for vid in ids})

        stats = au.fetch_video_statistics(youtube, ids)

        self.assertEqual([len(batch) for batch in youtube.videos().batches], [50, 10])
        self.assertEqual(len(stats), 60)

    def test_chunked_helper(self):
        self.assertEqual(list(au.chunked(list(range(5)), 2)), [[0, 1], [2, 3], [4]])

    def test_extract_average_view_percentage_variants(self):
        self.assertIsNone(au.extract_average_view_percentage(None))
        self.assertIsNone(au.extract_average_view_percentage({}))
        self.assertIsNone(au.extract_average_view_percentage({"rows": []}))
        self.assertEqual(
            au.extract_average_view_percentage({"rows": [["2026-11-01", "2026-11-20", 61.4]]}),
            61.4,
        )
        self.assertIsNone(
            au.extract_average_view_percentage({"rows": [["2026-11-01", "2026-11-20", "n/a"]]})
        )

    def test_fetch_returns_none_on_api_error(self):
        with mock.patch("sys.stderr"):
            self.assertIsNone(
                au.fetch_avg_view_percentage(
                    FakeAnalytics(values={}, failing={"v1"}), "v1", "2026-11-01", "2026-11-20"
                )
            )

    def test_analytics_query_params(self):
        params = build_analytics_query("abc123", "2026-11-01", "2026-11-20")
        self.assertEqual(params["ids"], "channel==MINE")
        self.assertEqual(params["filters"], "video==abc123")
        self.assertEqual(params["metrics"], "averageViewPercentage")
        self.assertEqual(params["startDate"], "2026-11-01")
        self.assertEqual(params["endDate"], "2026-11-20")


if __name__ == "__main__":
    unittest.main()
