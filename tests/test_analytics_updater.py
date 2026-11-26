import csv
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

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
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        return list(reader), fieldnames


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

        rows = au.update_performance_metrics(
            self.csv, youtube=youtube, analytics=analytics, today=TODAY
        )

        self.assertEqual(int(rows[0]["views"]), 1200)
        self.assertEqual(int(rows[0]["likes"]), 34)
        self.assertEqual(int(rows[0]["comments_count"]), 5)
        self.assertAlmostEqual(float(rows[0]["avg_view_percentage"]), 62.54)
        self.assertAlmostEqual(float(rows[1]["avg_view_percentage"]), 41.0)
        self.assertEqual(int(rows[1]["likes"]), 0)

        saved, _ = read_csv(self.csv)
        self.assertAlmostEqual(float(saved[0]["avg_view_percentage"]), 62.54)

    def test_analytics_failure_keeps_existing_values(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({"v1": {"viewCount": "7"}})
        analytics = FakeAnalytics(values={"v2": 48.0}, failing={"v1"})

        with mock.patch("sys.stderr"):
            rows = au.update_performance_metrics(
                self.csv, youtube=youtube, analytics=analytics, today=TODAY
            )

        self.assertAlmostEqual(float(rows[0]["avg_view_percentage"]), 55.0)
        self.assertEqual(int(rows[0]["views"]), 7)
        self.assertAlmostEqual(float(rows[1]["avg_view_percentage"]), 48.0)

    def test_empty_analytics_rows_keep_existing_values(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({})
        analytics = FakeAnalytics(values={}, empty={"v1", "v2"})

        with mock.patch("sys.stderr"):
            rows = au.update_performance_metrics(
                self.csv, youtube=youtube, analytics=analytics, today=TODAY
            )

        self.assertAlmostEqual(float(rows[0]["avg_view_percentage"]), 55.0)
        self.assertAlmostEqual(float(rows[1]["avg_view_percentage"]), 0.0)

    def test_analytics_client_failure_does_not_abort_run(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({"v1": {"viewCount": "3"}})

        with mock.patch("sys.stderr"), \
                mock.patch.object(
                    au, "get_youtube_analytics_service",
                    side_effect=RuntimeError("consent required"),
                ):
            rows = au.update_performance_metrics(self.csv, youtube=youtube, today=TODAY)

        self.assertEqual(int(rows[0]["views"]), 3)
        self.assertAlmostEqual(float(rows[0]["avg_view_percentage"]), 55.0)

    def test_analytics_can_be_skipped(self):
        make_csv(self.csv, self._rows())
        youtube = FakeYouTube({"v1": {"viewCount": "3"}})
        analytics = FakeAnalytics(values={"v1": 90.0})

        rows = au.update_performance_metrics(
            self.csv, youtube=youtube, analytics=analytics, with_analytics=False, today=TODAY
        )

        self.assertEqual(analytics.queries, [])
        self.assertAlmostEqual(float(rows[0]["avg_view_percentage"]), 55.0)

    def test_empty_csv_is_reported(self):
        make_csv(self.csv, [])
        rows = au.update_performance_metrics(
            self.csv, youtube=FakeYouTube({}), analytics=FakeAnalytics(values={}), today=TODAY
        )
        self.assertEqual(rows, [])

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
        # One request asks for both numbers the loop learns from.
        self.assertIn("averageViewPercentage", queries["video==v1"]["metrics"])
        self.assertIn("averageViewDuration", queries["video==v1"]["metrics"])

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


class MetricTests(unittest.TestCase):
    """Requirement 5: the loop needs seconds watched and the Short's length."""

    def test_iso_duration_is_read(self):
        self.assertAlmostEqual(au.parse_iso_duration("PT46S"), 46.0)
        self.assertAlmostEqual(au.parse_iso_duration("PT1M3S"), 63.0)
        self.assertAlmostEqual(au.parse_iso_duration("PT1H2M3.5S"), 3723.5)
        self.assertIsNone(au.parse_iso_duration("46"))
        self.assertIsNone(au.parse_iso_duration(None))

    def test_metric_values_are_named_by_the_column_headers(self):
        response = {
            "columnHeaders": [
                {"name": "averageViewPercentage"},
                {"name": "averageViewDuration"},
            ],
            "rows": [[26.9, 12.4]],
        }
        values = au.extract_metric_values(response)
        self.assertAlmostEqual(values["averageViewPercentage"], 26.9)
        self.assertAlmostEqual(values["averageViewDuration"], 12.4)

    def test_a_reply_without_headers_falls_back_to_the_percentage(self):
        response = {"rows": [["2026-11-01", "2026-11-20", 61.4]]}
        self.assertEqual(au.extract_metric_values(response), {})
        self.assertAlmostEqual(au.extract_average_view_percentage(response), 61.4)

    def test_both_metrics_are_asked_for_in_one_request(self):
        class Recorder:
            def __init__(self):
                self.queries = []

            def reports(self):
                outer = self

                class Reports:
                    def query(self, **kwargs):
                        outer.queries.append(kwargs)

                        class R:
                            def execute(self_inner):
                                return {
                                    "columnHeaders": [
                                        {"name": "averageViewPercentage"},
                                        {"name": "averageViewDuration"},
                                    ],
                                    "rows": [[30.0, 9.0]],
                                }

                        return R()

                return Reports()

        analytics = Recorder()
        values = au.fetch_view_metrics(analytics, "v1", "2026-11-01", "2026-11-20")
        self.assertEqual(len(analytics.queries), 1)
        self.assertIn("averageViewPercentage", analytics.queries[0]["metrics"])
        self.assertIn("averageViewDuration", analytics.queries[0]["metrics"])
        self.assertAlmostEqual(values["averageViewDuration"], 9.0)

    def test_video_details_carry_the_length(self):
        class Videos:
            def __init__(self):
                self.parts = []

            def list(self, part=None, id=None):
                self.parts.append(part)
                return FakeRequest({
                    "items": [
                        {
                            "id": "v1",
                            "statistics": {"viewCount": "10"},
                            "contentDetails": {"duration": "PT28S"},
                        }
                    ]
                })

        class YouTube:
            def __init__(self):
                self._videos = Videos()

            def videos(self):
                return self._videos

        youtube = YouTube()
        details = au.fetch_video_details(youtube, ["v1"])
        self.assertIn("contentDetails", youtube._videos.parts[0])
        self.assertAlmostEqual(details["v1"]["duration_sec"], 28.0)
        self.assertEqual(details["v1"]["statistics"]["viewCount"], "10")


class RefreshSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")

    def frame(self):
        rows = [
            {"video_id": "old", "date": "2026-11-01", "views": 1000, "avg_view_percentage": 50.0,
             "updated_at": "2026-11-20T00:00:00+00:00"},
            {"video_id": "fresh", "date": "2026-11-19", "views": 5, "avg_view_percentage": 0.0,
             "updated_at": ""},
            {"video_id": "never", "date": "2026-11-10", "views": 0, "avg_view_percentage": 0.0,
             "updated_at": ""},
        ]
        return au._ensure_columns(rows)

    def test_without_limits_every_row_is_refreshed(self):
        rows = self.frame()
        self.assertEqual(au.rows_to_refresh(rows, today=TODAY), list(range(len(rows))))

    def test_a_freshly_measured_row_is_left_alone(self):
        rows = self.frame()
        now = dt.datetime(2026, 11, 20, 3, tzinfo=dt.timezone.utc)
        picked = au.rows_to_refresh(rows, today=TODAY, min_age_hours=24, refresh_hours=6, now=now)
        self.assertNotIn(0, picked)  # measured two hours ago
        self.assertIn(1, picked)     # never measured
        self.assertIn(2, picked)

    def test_a_short_younger_than_the_age_limit_is_skipped_when_measured(self):
        rows = self.frame()
        rows[1]["views"] = 500
        rows[1]["avg_view_percentage"] = 40.0
        rows[1]["date"] = "2026-11-20"
        now = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc)
        picked = au.rows_to_refresh(rows, today=TODAY, min_age_hours=24, now=now)
        self.assertNotIn(1, picked)

    def test_the_cap_keeps_the_newest_rows(self):
        rows = self.frame()
        picked = au.rows_to_refresh(
            rows, today=TODAY, min_age_hours=0, max_rows=2,
            now=dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(len(picked), 2)
        self.assertNotIn(0, picked)  # the oldest of the three

    def test_the_update_records_the_seconds_and_the_timestamp(self):
        make_csv(self.csv, [
            {"video_id": "v1", "date": "2026-11-01", "title": "A", "style_used": "s",
             "hook_type": "h", "views": 0, "avg_view_percentage": 0.0, "likes": 0,
             "comments_count": 0, "top_feedback": ""},
        ])

        class Analytics:
            def reports(self):
                class Reports:
                    def query(self, **kwargs):
                        return FakeRequest({
                            "columnHeaders": [
                                {"name": "averageViewPercentage"},
                                {"name": "averageViewDuration"},
                            ],
                            "rows": [[26.9, 12.4]],
                        })

                return Reports()

        class YouTube:
            def videos(self):
                class Videos:
                    def list(self, part=None, id=None):
                        return FakeRequest({
                            "items": [{
                                "id": "v1",
                                "statistics": {"viewCount": "268", "likeCount": "4",
                                               "commentCount": "0"},
                                "contentDetails": {"duration": "PT46S"},
                            }]
                        })

                return Videos()

        rows = au.update_performance_metrics(
            self.csv, youtube=YouTube(), analytics=Analytics(), today=TODAY,
        )
        self.assertAlmostEqual(float(rows[0]["avg_view_percentage"]), 26.9)
        self.assertAlmostEqual(float(rows[0]["avg_view_duration_sec"]), 12.4)
        self.assertAlmostEqual(float(rows[0]["duration_sec"]), 46.0)
        self.assertTrue(str(rows[0]["updated_at"]).startswith("20"))

    def test_the_new_columns_are_added_to_an_old_file(self):
        make_csv(self.csv, [
            {"video_id": "v1", "date": "2026-11-01", "title": "A", "style_used": "s",
             "hook_type": "h", "views": 1, "avg_view_percentage": 2.0, "likes": 0,
             "comments_count": 0, "top_feedback": ""},
        ])
        rows = au.update_performance_metrics(
            self.csv, youtube=FakeYouTube({}), analytics=FakeAnalytics(values={}),
            with_analytics=False, today=TODAY,
        )
        for column in ("avg_view_duration_sec", "language_mode", "tts_mode",
                       "duration_sec", "target_duration_sec", "params_json", "updated_at"):
            self.assertIn(column, rows[0])
