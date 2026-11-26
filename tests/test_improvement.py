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
        "defaults": {"target_duration_sec": 27, "language_mode": "bilingual"},
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
        self.assertEqual(params["language_mode"], "bilingual")
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
        self.assertEqual(params["language_mode"], "bilingual")

    def test_numbers_are_clamped_not_rejected(self):
        params = improvement.validate_params(
            {"target_duration_sec": 90, "hook_duration_sec": -4, "tempo": 9}
        )
        self.assertEqual(params["target_duration_sec"], 32.0)
        self.assertEqual(params["hook_duration_sec"], 1.2)
        self.assertEqual(params["tempo"], 2.0)

    def test_choices_fall_back_to_the_base_value(self):
        base = improvement.validate_params({"hook_style": "text_only", "language_mode": "ja"})
        params = improvement.validate_params(
            {"hook_style": "dance", "language_mode": "klingon"}, base=base
        )
        self.assertEqual(params["hook_style"], "text_only")
        self.assertEqual(params["language_mode"], "ja")

    def test_broken_values_keep_the_default(self):
        params = improvement.validate_params(
            {"target_duration_sec": "long", "drawing_audio_volume": None, "cta_text": ""}
        )
        self.assertEqual(params["target_duration_sec"], 28.0)
        self.assertEqual(params["drawing_audio_volume"], 0.35)
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
        for needle in ("hook=finish_flash_to_blank", "lang=bilingual", "voice=short"):
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
        self.assertEqual(params["language_mode"], "bilingual")  # untouched

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


if __name__ == "__main__":
    unittest.main()
