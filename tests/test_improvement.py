import csv
import json
import os
import tempfile
import unittest
from unittest import mock

import improvement


CONFIG = {
    "video": {"target_duration_sec": 28, "hook_duration_sec": 2.4},
    "improvement": {
        "enabled": True,
        "min_rows": 2,
        "audience_note": "89% of the viewers were in Japan.",
        "defaults": {"target_duration_sec": 27, "language_mode": "en"},
    },
    "telop": {"cta_text": "Follow for more oil pastel art!"},
}


def make_csv(path, rows):
    if not rows:
        open(path, "w", encoding="utf-8").close()
        return
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def rows(*specs):
    out = []
    for index, spec in enumerate(specs):
        out.append(
            {
                "video_id": f"v{index + 1}",
                "date": spec.get("date", f"2026-11-{10 + index:02d}"),
                "title": spec.get("title", f"Short {index + 1}"),
                "style_used": "style_B_story",
                "hook_type": spec.get("hook", "finish_preview"),
                "views": spec.get("views", 200),
                "avg_view_percentage": spec.get("retention", 40.0),
                "avg_view_duration_sec": spec.get("seconds", 12.0),
                "likes": spec.get("likes", 3),
                "comments_count": spec.get("comments", 0),
                "language_mode": spec.get("language", "en"),
                "pace_mode": spec.get("pace", "timelapse"),
                "tts_mode": "full",
                "duration_sec": spec.get("length", 46.0),
                "target_duration_sec": 45,
                "params_json": "{}",
                "top_feedback": "",
                "updated_at": "2026-11-20T00:00:00+00:00",
            }
        )
    return out


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def generate_content(self, model=None, contents=None):
        self.prompts.append((model, contents))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return FakeResponse(reply)


class FakeClient:
    def __init__(self, replies):
        self.models = FakeModels(replies)


class DefaultsTests(unittest.TestCase):
    def test_the_new_defaults_are_the_brief_s(self):
        params = improvement.validate_params()
        self.assertEqual(params["hook_style"], "finish_flash_to_blank")
        self.assertEqual(params["language_mode"], "en")
        self.assertEqual(params["tts_mode"], "short")
        self.assertTrue(20 <= float(params["target_duration_sec"]) <= 32)
        self.assertIn("次は何を描いてほしい", str(params["cta_question_ja"]))
        self.assertIn("What should I draw next", str(params["cta_question_en"]))
        self.assertGreater(float(params["drawing_audio_volume"]), 0)

    def test_config_defaults_win_over_the_module_defaults(self):
        params = improvement.config_params(CONFIG)
        self.assertEqual(params["target_duration_sec"], 27.0)
        self.assertEqual(params["hook_style"], "finish_flash_to_blank")

    def test_a_config_without_the_block_still_has_a_full_set(self):
        params = improvement.config_params({"video": {"target_duration_sec": 30}})
        self.assertEqual(params["target_duration_sec"], 30.0)
        self.assertEqual(params["language_mode"], "en")

    def test_numbers_are_clamped_not_rejected(self):
        params = improvement.validate_params(
            {"target_duration_sec": 90, "hook_duration_sec": -4, "tempo": 9}
        )
        self.assertEqual(params["target_duration_sec"], 32.0)
        self.assertEqual(params["hook_duration_sec"], 1.2)
        self.assertEqual(params["tempo"], 2.0)

    def test_choices_fall_back_to_the_base_value(self):
        base = improvement.validate_params({"hook_style": "text_only", "language_mode": "en"})
        params = improvement.validate_params(
            {"hook_style": "dance", "language_mode": "klingon"}, base=base
        )
        self.assertEqual(params["hook_style"], "text_only")
        self.assertEqual(params["language_mode"], "en")

    def test_broken_values_keep_the_default(self):
        params = improvement.validate_params(
            {"target_duration_sec": "long", "drawing_audio_volume": None, "cta_text": ""}
        )
        self.assertEqual(params["target_duration_sec"], 28.0)
        self.assertEqual(params["drawing_audio_volume"], 0.5)
        self.assertEqual(params["cta_text"], improvement.DEFAULT_PARAMS["cta_text"])

    def test_unknown_keys_are_dropped(self):
        params = improvement.validate_params({"watermark": "x", "api_key": "secret"})
        self.assertNotIn("watermark", params)
        self.assertNotIn("api_key", params)
        self.assertEqual(set(params), set(improvement.DEFAULT_PARAMS))

    def test_narration_budget_follows_the_target(self):
        short = improvement.narration_word_budget({"target_duration_sec": 20, "narration_ratio": 0.6})
        long = improvement.narration_word_budget({"target_duration_sec": 32, "narration_ratio": 0.8})
        self.assertLess(short[1], long[0])
        self.assertGreaterEqual(short[0], improvement.MIN_NARRATION_WORDS)
        self.assertLessEqual(long[1], improvement.MAX_NARRATION_WORDS)

    def test_describe_names_the_choices(self):
        line = improvement.describe_params(improvement.validate_params())
        for needle in ("hook=finish_flash_to_blank", "lang=en", "voice=short"):
            self.assertIn(needle, line)


class ActiveParamsTests(unittest.TestCase):
    def tearDown(self):
        improvement.clear_active_params()

    def test_without_a_pinned_set_the_config_is_used(self):
        self.assertEqual(improvement.active_params(CONFIG)["target_duration_sec"], 27.0)

    def test_a_pinned_set_is_used_and_cleared(self):
        improvement.set_active_params({"target_duration_sec": 24})
        self.assertEqual(improvement.active_params(CONFIG)["target_duration_sec"], 24.0)
        improvement.clear_active_params()
        self.assertEqual(improvement.active_params(CONFIG)["target_duration_sec"], 27.0)


class SummaryTests(unittest.TestCase):
    def test_unmeasured_rows_are_ignored(self):
        self.assertEqual(
            improvement.summarise_performance(rows({"retention": 0.0, "views": 0}))["measured"],
            0,
        )

    def test_the_summary_names_the_best_and_worst_short(self):
        summary = improvement.summarise_performance(
            rows(
                {"retention": 26.9, "title": "Pokemon card", "length": 46},
                {"retention": 50.0, "title": "Blue candy", "length": 83},
            )
        )
        self.assertEqual(summary["measured"], 2)
        self.assertEqual(summary["best"]["title"], "Blue candy")
        self.assertEqual(summary["worst"]["title"], "Pokemon card")
        self.assertAlmostEqual(summary["avg_retention_pct"], 38.45, places=1)
        self.assertEqual(summary["by_hook_style"]["finish_preview"], 38.5)

    def test_only_the_recent_rows_are_used(self):
        summary = improvement.summarise_performance(
            rows(*[{"retention": 10.0 + i} for i in range(12)]), recent=4
        )
        self.assertEqual(summary["measured"], 4)
        self.assertEqual(len(summary["recent"]), 4)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")

    def _decide(self, replies, config=CONFIG, client=None):
        make_csv(self.csv, rows({"retention": 26.9, "length": 46}, {"retention": 50.0, "length": 83}))
        fake = client if client is not None else FakeClient(replies)
        with mock.patch.object(improvement, "_ask_gemini", side_effect=lambda prompt, cfg: fake.models.generate_content(contents=[prompt]).text):
            return improvement.decide_params(self.csv, config, log=lambda *_: None)

    def test_the_prompt_carries_the_numbers_and_the_current_parameters(self):
        summary = improvement.summarise_performance(
            rows({"retention": 26.9, "title": "Pokemon card", "length": 46})
        )
        prompt = improvement.review_prompt(summary, improvement.validate_params(), CONFIG)
        self.assertIn("Pokemon", prompt)  # the row titles travel with the data
        self.assertIn("retention_pct", prompt)
        self.assertIn("89% of the viewers were in Japan", prompt)
        self.assertIn('"hook_style"', prompt)
        self.assertIn("finish_flash_to_blank", prompt)
        self.assertIn("Allowed ranges", prompt)

    def test_a_good_answer_becomes_the_next_parameters(self):
        answer = json.dumps(
            {
                "diagnosis": "Viewers left during the body of the long Short.",
                "experiment": "A 25 s cut with the flash first.",
                "next_params": {"target_duration_sec": 25, "hook_style": "finish_flash_to_blank"},
            }
        )
        params, review = self._decide([answer])
        self.assertEqual(review["status"], "gemini")
        self.assertEqual(params["target_duration_sec"], 25.0)
        self.assertEqual(params["hook_style"], "finish_flash_to_blank")
        self.assertEqual(params["language_mode"], "en")  # untouched

    def test_an_answer_outside_the_ranges_is_clamped(self):
        answer = json.dumps({"next_params": {"target_duration_sec": 70, "tempo": 0.1}})
        params, review = self._decide([answer])
        self.assertEqual(review["status"], "gemini")
        self.assertEqual(params["target_duration_sec"], 32.0)
        self.assertEqual(params["tempo"], 0.8)

    def test_a_markdown_fenced_answer_is_read(self):
        answer = "```json\n" + json.dumps({"next_params": {"target_duration_sec": 26}}) + "\n```"
        params, _ = self._decide([answer])
        self.assertEqual(params["target_duration_sec"], 26.0)

    def test_a_gemini_failure_falls_back_to_the_defaults(self):
        params, review = self._decide([RuntimeError("quota")])
        self.assertEqual(review["status"], "gemini-failed")
        self.assertIn("quota", review["error"])
        self.assertEqual(params, improvement.config_params(CONFIG))

    def test_a_broken_answer_falls_back_to_the_defaults(self):
        params, review = self._decide(["not json at all"])
        self.assertEqual(review["status"], "gemini-failed")
        self.assertEqual(params["target_duration_sec"], 27.0)

    def test_without_enough_data_no_gemini_call_is_made(self):
        make_csv(self.csv, rows())
        with mock.patch.object(improvement, "_ask_gemini") as ask:
            params, review = improvement.decide_params(self.csv, CONFIG, log=lambda *_: None)
        ask.assert_not_called()
        self.assertEqual(review["status"], "not-enough-data")
        self.assertEqual(params["target_duration_sec"], 27.0)

    def test_a_missing_csv_is_the_same_as_no_data(self):
        params, review = improvement.decide_params(
            os.path.join(self.tmp.name, "nope.csv"), CONFIG, log=lambda *_: None
        )
        self.assertEqual(review["status"], "not-enough-data")
        self.assertEqual(params["hook_style"], "finish_flash_to_blank")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "improvement_state.json")

    def test_a_missing_state_starts_empty(self):
        state = improvement.load_state(self.path)
        self.assertEqual(state["history"], [])
        self.assertEqual(state["params"], {})

    def test_a_broken_state_starts_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(improvement.load_state(self.path)["history"], [])

    def test_a_run_is_recorded_with_its_results(self):
        state = improvement.empty_state()
        params = improvement.validate_params()
        review = {"status": "gemini", "diagnosis": "d", "experiment": "e"}
        results = [
            {"status": "done", "name": "GO_a", "package": "a_20261125_1800",
             "artifacts": {"video_id": "abc"}},
            {"status": "failed", "name": "GO_b", "artifacts": {}},
        ]
        improvement.record_run(state, params, review, results)
        self.assertEqual(state["params"]["target_duration_sec"], 28.0)
        self.assertEqual(state["review"]["status"], "gemini")
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["videos"][0]["video_id"], "abc")
        self.assertEqual(len(state["history"][0]["videos"]), 1)

    def test_the_history_is_capped(self):
        state = improvement.empty_state()
        for _ in range(improvement.HISTORY_LIMIT + 5):
            improvement.record_run(state, improvement.validate_params(), {"status": "x"}, [])
        self.assertEqual(len(state["history"]), improvement.HISTORY_LIMIT)

    def test_the_state_round_trips(self):
        state = improvement.empty_state()
        improvement.record_run(state, improvement.validate_params({"tempo": 1.4}), {"status": "x"}, [])
        improvement.save_state(state, self.path)
        again = improvement.load_state(self.path)
        self.assertEqual(again["params"]["tempo"], 1.4)
        self.assertEqual(len(again["history"]), 1)


class DirectionLockTests(unittest.TestCase):
    """The overseas direction: the reflection cannot switch the language.

    The channel is made for viewers overseas (2026-11-27), so the reflection
    keeps language_mode "en". The lock lives in decide_params, not in the
    allowed values: a human can still choose a language in config.yaml, but
    the loop cannot move it on its own, in either direction.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")

    def _decide(self, replies, config=CONFIG, client=None):
        make_csv(self.csv, rows({"retention": 26.9, "length": 46}, {"retention": 50.0, "length": 83}))
        fake = client if client is not None else FakeClient(replies)
        with mock.patch.object(improvement, "_ask_gemini", side_effect=lambda prompt, cfg: fake.models.generate_content(contents=[prompt]).text):
            return improvement.decide_params(self.csv, config, log=lambda *_: None)

    def test_the_reflection_cannot_switch_the_language_to_bilingual(self):
        answer = json.dumps({"next_params": {"language_mode": "bilingual"}})
        params, review = self._decide([answer])
        self.assertEqual(review["status"], "gemini")
        self.assertEqual(params["language_mode"], "en")

    def test_the_reflection_cannot_switch_the_language_to_japanese(self):
        answer = json.dumps({"next_params": {"language_mode": "ja"}})
        params, _ = self._decide([answer])
        self.assertEqual(params["language_mode"], "en")

    def test_the_reflection_cannot_move_a_language_a_human_chose_either(self):
        config = {
            "video": {"target_duration_sec": 28, "hook_duration_sec": 2.4},
            "improvement": {"enabled": True, "min_rows": 2, "defaults": {"language_mode": "ja"}},
        }
        answer = json.dumps({"next_params": {"language_mode": "bilingual"}})
        params, _ = self._decide([answer], config=config)
        self.assertEqual(params["language_mode"], "ja")

    def test_the_review_prompt_keeps_the_language_english(self):
        summary = improvement.summarise_performance(rows({"retention": 40.0}))
        prompt = improvement.review_prompt(summary, improvement.validate_params(), {})
        self.assertIn('keep language_mode "en"', prompt)
        self.assertIn("overseas", prompt)
        self.assertIn('never switch it to "bilingual" or "ja"', prompt)
        self.assertNotIn("Most viewers are in Japan", prompt)


class PaceReflectionTests(unittest.TestCase):
    """The pace is a controlled experiment, not a flip-flop.

    performance.csv records which pace each Short used (same pattern as the
    language column), summarise_performance compares retention by pace, and
    decide_params holds one pace for 2 consecutive Shorts before it may
    switch - at one video a day a shorter hold is no experiment. The hold is
    counted from what was actually rendered, because the reflection's choice
    is not carried forward as the next run's base.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")

    def _decide(self, specs, replies, config=CONFIG):
        make_csv(self.csv, rows(*specs))
        fake = FakeClient(replies)
        with mock.patch.object(improvement, "_ask_gemini", side_effect=lambda prompt, cfg: fake.models.generate_content(contents=[prompt]).text):
            return improvement.decide_params(self.csv, config, log=lambda *_: None)

    def test_config_picks_up_the_video_pace_mode(self):
        config = {"video": {"pace_mode": "realtime_cuts"}}
        self.assertEqual(improvement.config_params(config)["pace_mode"], "realtime_cuts")

    def test_an_unknown_pace_falls_back_to_timelapse(self):
        config = {"video": {"pace_mode": "fast"}}
        self.assertEqual(improvement.config_params(config)["pace_mode"], "timelapse")

    def test_the_summary_compares_retention_by_pace(self):
        summary = improvement.summarise_performance(
            rows(
                {"retention": 30.0, "pace": "timelapse"},
                {"retention": 50.0, "pace": "timelapse"},
                {"retention": 60.0, "pace": "realtime_cuts"},
            )
        )
        self.assertEqual(summary["by_pace_mode"]["timelapse"], 40.0)
        self.assertEqual(summary["by_pace_mode"]["realtime_cuts"], 60.0)

    def test_a_switch_is_blocked_after_only_one_short_on_the_running_pace(self):
        answer = json.dumps({"next_params": {"pace_mode": "timelapse"}})
        params, review = self._decide(
            [{"retention": 40.0, "pace": "timelapse"}, {"retention": 55.0, "pace": "realtime_cuts"}],
            [answer],
        )
        self.assertEqual(review["status"], "gemini")
        self.assertEqual(params["pace_mode"], "realtime_cuts")

    def test_a_switch_is_allowed_after_two_shorts_on_the_running_pace(self):
        answer = json.dumps({"next_params": {"pace_mode": "timelapse"}})
        params, _ = self._decide(
            [{"retention": 55.0, "pace": "realtime_cuts"}, {"retention": 50.0, "pace": "realtime_cuts"}],
            [answer],
        )
        self.assertEqual(params["pace_mode"], "timelapse")

    def test_a_silent_reflection_does_not_revert_the_running_pace_early(self):
        answer = json.dumps({"next_params": {"target_duration_sec": 25}})
        params, _ = self._decide(
            [{"retention": 40.0, "pace": "timelapse"}, {"retention": 55.0, "pace": "realtime_cuts"}],
            [answer],
        )
        self.assertEqual(params["pace_mode"], "realtime_cuts")
        self.assertEqual(params["target_duration_sec"], 25.0)

    def test_old_rows_without_a_pace_column_do_not_block_a_switch(self):
        specs = [
            {"retention": 40.0, "date": "2026-11-10"},
            {"retention": 55.0, "date": "2026-11-11"},
        ]
        made = rows(*specs)
        for row in made:
            del row["pace_mode"]
        make_csv(self.csv, made)
        answer = json.dumps({"next_params": {"pace_mode": "realtime_cuts"}})
        fake = FakeClient([answer])
        with mock.patch.object(improvement, "_ask_gemini", side_effect=lambda prompt, cfg: fake.models.generate_content(contents=[prompt]).text):
            params, _ = improvement.decide_params(self.csv, CONFIG, log=lambda *_: None)
        self.assertEqual(params["pace_mode"], "realtime_cuts")

    def test_the_review_prompt_names_the_pace_and_the_hold(self):
        summary = improvement.summarise_performance(rows({"retention": 40.0}))
        prompt = improvement.review_prompt(summary, improvement.validate_params(), {})
        self.assertIn("pace_mode", prompt)
        self.assertIn("realtime_cuts", prompt)
        self.assertIn("by_pace_mode", prompt)
        self.assertIn("2 consecutive Shorts", prompt)

    def test_describe_names_the_pace(self):
        line = improvement.describe_params(improvement.validate_params({"pace_mode": "realtime_cuts"}))
        self.assertIn("pace=realtime_cuts", line)


if __name__ == "__main__":
    unittest.main()
