import csv
import json
import os
import tempfile
import unittest
from unittest import mock

import ai_writer
from main import load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = load_config(os.path.join(ROOT, "config.yaml"))


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["video_id", "style_used", "avg_view_percentage"]
        )
        writer.writeheader()
        writer.writerows(rows)


STATS_ROWS = [
    {"video_id": "v1", "style_used": "style_A_material", "avg_view_percentage": 41.0},
    {"video_id": "v2", "style_used": "style_B_story", "avg_view_percentage": 72.0},
    {"video_id": "v3", "style_used": "style_B_story", "avg_view_percentage": 68.5},
    {"video_id": "v4", "style_used": "style_C_asmr", "avg_view_percentage": 33.0},
    {"video_id": "v5", "style_used": "style_A_material", "avg_view_percentage": 50.0},
]


class ChooseStyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")
        # These tests describe the automatic pick, so a one-run STYLE_OVERRIDE
        # from the environment must not leak into them.
        patcher = mock.patch.dict(os.environ, {"STYLE_OVERRIDE": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_csv_returns_a_known_style(self):
        style = ai_writer.choose_best_style(self.csv, CONFIG)
        self.assertIn(style, CONFIG["evolution"]["styles"])

    def test_less_than_three_rows_explores(self):
        write_csv(self.csv, STATS_ROWS[:2])
        with mock.patch.object(ai_writer.random, "choice", return_value="style_A_material"):
            self.assertEqual(ai_writer.choose_best_style(self.csv, CONFIG), "style_A_material")

    def test_best_performing_style_wins(self):
        write_csv(self.csv, STATS_ROWS)
        with mock.patch.object(ai_writer.random, "random", return_value=0.9):
            self.assertEqual(ai_writer.choose_best_style(self.csv, CONFIG), "style_B_story")

    def test_exploration_rate_forces_random_pick(self):
        write_csv(self.csv, STATS_ROWS)
        with mock.patch.object(ai_writer.random, "random", return_value=0.01), \
                mock.patch.object(ai_writer.random, "choice", return_value="style_C_asmr"):
            self.assertEqual(ai_writer.choose_best_style(self.csv, CONFIG), "style_C_asmr")

    def test_nan_scores_are_ignored(self):
        rows = [
            {"video_id": "v1", "style_used": "style_A_material", "avg_view_percentage": 20.0},
            {"video_id": "v2", "style_used": "style_A_material", "avg_view_percentage": 30.0},
            {"video_id": "v3", "style_used": "style_B_story", "avg_view_percentage": None},
            {"video_id": "v4", "style_used": "style_B_story", "avg_view_percentage": None},
        ]
        write_csv(self.csv, rows)
        with mock.patch.object(ai_writer.random, "random", return_value=0.9):
            self.assertEqual(ai_writer.choose_best_style(self.csv, CONFIG), "style_A_material")

    def test_unknown_style_names_are_not_chosen(self):
        rows = STATS_ROWS + [
            {"video_id": "v9", "style_used": "style_Z_old", "avg_view_percentage": 99.0},
        ]
        write_csv(self.csv, rows)
        with mock.patch.object(ai_writer.random, "random", return_value=0.9):
            self.assertIn(
                ai_writer.choose_best_style(self.csv, CONFIG), CONFIG["evolution"]["styles"]
            )


class StyleOverrideTests(unittest.TestCase):
    """STYLE_OVERRIDE pins one style for a single run and is otherwise inert."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, "performance.csv")

    def test_a_known_style_wins_over_the_learned_pick(self):
        write_csv(self.csv, STATS_ROWS)
        with mock.patch.dict(os.environ, {"STYLE_OVERRIDE": "style_C_asmr"}):
            self.assertEqual(ai_writer.choose_best_style(self.csv, CONFIG), "style_C_asmr")

    def test_surrounding_spaces_are_trimmed(self):
        with mock.patch.dict(os.environ, {"STYLE_OVERRIDE": "  style_B_story  "}):
            self.assertEqual(ai_writer.choose_best_style(self.csv, CONFIG), "style_B_story")

    def test_an_unknown_name_falls_back_to_the_usual_pick(self):
        write_csv(self.csv, STATS_ROWS)
        with mock.patch.dict(os.environ, {"STYLE_OVERRIDE": "style_Z_typo"}), \
                mock.patch.object(ai_writer.random, "random", return_value=0.9):
            self.assertEqual(ai_writer.choose_best_style(self.csv, CONFIG), "style_B_story")

    def test_unset_or_blank_keeps_the_usual_pick(self):
        write_csv(self.csv, STATS_ROWS)
        for value in ("", "   "):
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {"STYLE_OVERRIDE": value}), \
                    mock.patch.object(ai_writer.random, "random", return_value=0.9):
                self.assertEqual(ai_writer.choose_best_style(self.csv, CONFIG), "style_B_story")

    def test_the_helper_reads_the_environment_it_is_given(self):
        self.assertEqual(
            ai_writer.requested_style_override(CONFIG, {"STYLE_OVERRIDE": "style_A_material"}),
            "style_A_material",
        )
        self.assertIsNone(ai_writer.requested_style_override(CONFIG, {}))
        self.assertIsNone(ai_writer.requested_style_override(CONFIG, {"STYLE_OVERRIDE": "nope"}))


class ParseScriptJsonTests(unittest.TestCase):
    VALID = {
        "title": "Oil pastel postcard #Shorts",
        "hook_text": "One minute drawing",
        "narration": "Soft pastel layers build a tiny sunset.",
        "description": "Shop link: https://crayonjapan.base.shop/",
        "tags": [f"tag{i}" for i in range(14)],
    }

    def test_parses_plain_json(self):
        import json

        data = ai_writer.parse_script_json(json.dumps(self.VALID))
        # "#Shorts" is stripped on the way in: the upload title never carries it.
        self.assertEqual(data["title"], "Oil pastel postcard")
        self.assertEqual(len(data["tags"]), 10)

    def test_parses_fenced_json(self):
        import json

        raw = "```json\n" + json.dumps(self.VALID) + "\n```"
        data = ai_writer.parse_script_json(raw)
        self.assertEqual(data["hook_text"], self.VALID["hook_text"])

    def test_parses_fenced_json_without_language(self):
        import json

        raw = "```\n" + json.dumps(self.VALID) + "\n```"
        self.assertTrue(ai_writer.parse_script_json(raw)["title"])

    def test_comma_separated_tags_string(self):
        import json

        payload = dict(self.VALID, tags="oil pastel, crayon, asmr")
        data = ai_writer.parse_script_json(json.dumps(payload))
        self.assertEqual(data["tags"], ["oil pastel", "crayon", "asmr"])

    def test_missing_key_raises(self):
        import json

        payload = dict(self.VALID)
        payload.pop("narration")
        with self.assertRaises(ValueError):
            ai_writer.parse_script_json(json.dumps(payload))

    def test_non_object_json_raises(self):
        with self.assertRaises(ValueError):
            ai_writer.parse_script_json("[1, 2, 3]")

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            ai_writer.parse_script_json("not json at all")

    def test_empty_reply_raises(self):
        with self.assertRaises(ValueError):
            ai_writer.parse_script_json("")


class PromptTests(unittest.TestCase):
    def test_material_style_prompt_contains_brand_context(self):
        prompt = ai_writer.build_script_prompt("style_A_material", CONFIG)
        self.assertIn(CONFIG["brand"]["material_name"], prompt)
        self.assertIn(CONFIG["brand"]["shop_url"], prompt)
        self.assertIn(CONFIG["evolution"]["styles"]["style_A_material"]["description"], prompt)
        self.assertNotIn("the drawing sound carries the video", prompt)

    def test_asmr_prompt_talks_across_the_short_while_the_drawing_sounds(self):
        prompt = ai_writer.build_script_prompt("style_C_asmr", CONFIG)
        self.assertIn("the drawing sound stays in front", prompt)
        self.assertIn(f"{ai_writer.ASMR_NARRATION_MIN_WORDS}-{ai_writer.ASMR_NARRATION_MAX_WORDS} words", prompt)
        self.assertIn("never a long silence", prompt)

    def test_a_normal_style_asks_for_a_full_script(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG)
        self.assertIn(f"{ai_writer.NARRATION_MIN_WORDS}-", prompt)
        self.assertIn(f"{ai_writer.NARRATION_MAX_WORDS} words", prompt)
        self.assertIn("0-3 s", prompt)
        self.assertIn("3-35 s", prompt)
        self.assertIn("no long pauses", prompt)

    def test_the_prompt_carries_the_artist_context_from_the_config(self):
        for style in CONFIG["evolution"]["styles"]:
            with self.subTest(style=style):
                prompt = ai_writer.build_script_prompt(style, CONFIG)
                for note in CONFIG["brand"]["artist_notes"]:
                    self.assertIn(note, prompt)
                self.assertIn("never invent a fact", prompt)

    def test_every_style_asks_for_the_hook_sensory_words_and_the_loop(self):
        """The hook names the motif and the payoff; the ending echoes the opening."""
        for style in CONFIG["evolution"]["styles"]:
            with self.subTest(style=style):
                # The prompt wraps sentences across lines; read it as one line.
                prompt = " ".join(ai_writer.build_script_prompt(style, CONFIG).split())
                self.assertIn("Hook (0-3s)", prompt)
                self.assertIn("names the motif and the payoff", prompt)
                self.assertIn("same words as the on-screen hook text", prompt)
                self.assertIn("first frame", prompt)
                self.assertIn("Sensory Language", prompt)
                for word in ("creamy", "buttery", "vibrant", "crisp"):
                    self.assertIn(word, prompt)
                self.assertIn("Loop-friendly Ending", prompt)
                self.assertIn("echoes the first line", prompt)
                self.assertIn("naturally leads back to the beginning", prompt)
                self.assertIn("satisfying punchline", prompt)
                # Generic curiosity-gap lines name nothing: the first seconds
                # must say what is being drawn.
                self.assertNotIn("The secret to realistic texture is", prompt)
                self.assertNotIn("You won't believe how this layer transforms", prompt)

    def test_the_narration_rule_keeps_the_hook_sensory_words_and_the_loop(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG)
        narration_line = next(
            line for line in prompt.splitlines() if line.startswith('3. "narration"')
        )
        self.assertIn("name the motif and the payoff", narration_line)
        self.assertIn("sensory words", narration_line)
        self.assertIn("leads back to the opening", narration_line)
        self.assertIn("one true detail", narration_line)
        self.assertIn("Hold the reveal", narration_line)


class JapaneseTranslationTests(unittest.TestCase):
    """Gemini returns the Japanese the artist checks before the Short is posted."""

    REPLY = {
        "title": "Painting a Cyndaquil Card #Shorts",
        "hook_text": "Found a fire-type treasure today!",
        "narration": "This tiny card began as a blank sheet of paper.",
        "description": "Shop: https://crayonjapan.base.shop/",
        "tags": ["pokemon", "oil pastel"],
        "title_ja": "  ヒノアラシのカードを描いてみた #Shorts  ",
        "hook_ja": "今日、ほのおタイプの宝物を見つけたよ！",
        "narration_ja": "この小さなカードは、まっ白な紙から始まりました。",
    }

    def test_every_style_asks_for_the_three_japanese_fields(self):
        for style in CONFIG["evolution"]["styles"]:
            with self.subTest(style=style):
                prompt = ai_writer.build_script_prompt(style, CONFIG)
                for key in ai_writer.TRANSLATION_KEYS:
                    self.assertIn(f'"{key}"', prompt)
                self.assertIn("translation only", prompt)

    def test_the_prompt_keeps_the_earlier_script_rules(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG)
        self.assertIn(f"{ai_writer.NARRATION_MIN_WORDS}-", prompt)
        self.assertIn("never invent a fact", prompt)

    def test_parse_keeps_the_japanese_fields_and_trims_them(self):
        data = ai_writer.parse_script_json(json.dumps(self.REPLY))
        self.assertEqual(data["title_ja"], "ヒノアラシのカードを描いてみた #Shorts")
        self.assertEqual(data["hook_ja"], self.REPLY["hook_ja"])
        self.assertEqual(data["narration_ja"], self.REPLY["narration_ja"])
        self.assertEqual(data["narration"], self.REPLY["narration"])

    def test_the_key_names_of_geminis_own_proposal_are_accepted(self):
        reply = dict(self.REPLY)
        reply.pop("title_ja")
        reply["script_ja"] = reply.pop("narration_ja")
        reply["hook"] = reply.pop("hook_text")
        reply["script"] = reply.pop("narration")
        data = ai_writer.parse_script_json(json.dumps(reply))
        self.assertEqual(data["narration_ja"], self.REPLY["narration_ja"])
        self.assertEqual(data["hook_text"], self.REPLY["hook_text"])
        self.assertEqual(data["narration"], self.REPLY["narration"])
        self.assertEqual(data["title_ja"], "")

    def test_a_reply_without_japanese_still_parses(self):
        reply = {key: self.REPLY[key] for key in ai_writer.REQUIRED_SCRIPT_KEYS}
        data = ai_writer.parse_script_json(json.dumps(reply))
        for key in ai_writer.TRANSLATION_KEYS:
            self.assertEqual(data[key], "")

    def test_blank_or_non_text_translations_become_empty(self):
        reply = dict(self.REPLY, title_ja="   ", hook_ja=None, narration_ja=["x"])
        data = ai_writer.parse_script_json(json.dumps(reply))
        for key in ai_writer.TRANSLATION_KEYS:
            self.assertEqual(data[key], "")


class OverseasCaptionTests(unittest.TestCase):
    """Gemini's one sentence for the TikTok / Instagram / Pinterest block."""

    REPLY = dict(
        JapaneseTranslationTests.REPLY,
        overseas_caption="A Cyndaquil card turned into soft pastel flames",
        overseas_caption_ja="ヒノアラシのカードを、やわらかなパステルの炎にしました。",
    )

    def test_every_style_asks_for_the_overseas_sentence_and_its_japanese(self):
        for style in CONFIG["evolution"]["styles"]:
            with self.subTest(style=style):
                prompt = ai_writer.build_script_prompt(style, CONFIG)
                for key in ai_writer.OVERSEAS_KEYS:
                    self.assertIn(f'"{key}"', prompt)
                self.assertIn("TikTok / Instagram Reels / Pinterest", prompt)

    def test_the_prompt_asks_for_the_two_search_keywords(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG)
        self.assertIn("Oil pastel", prompt)
        self.assertIn("do not describe a person or a camera", prompt)

    def test_parse_keeps_the_overseas_fields_and_trims_them(self):
        reply = dict(self.REPLY, overseas_caption="  A pastel flame  ", overseas_caption_ja=" パステルの炎 ")
        data = ai_writer.parse_script_json(json.dumps(reply))
        self.assertEqual(data["overseas_caption"], "A pastel flame")
        self.assertEqual(data["overseas_caption_ja"], "パステルの炎")

    def test_a_reply_without_them_still_parses(self):
        reply = {key: self.REPLY[key] for key in ai_writer.REQUIRED_SCRIPT_KEYS}
        data = ai_writer.parse_script_json(json.dumps(reply))
        for key in ai_writer.OVERSEAS_KEYS:
            self.assertEqual(data[key], "")

    def test_the_naming_slip_of_geminis_own_reply_is_accepted(self):
        reply = dict(self.REPLY)
        reply.pop("overseas_caption")
        reply["overseas_text"] = "A pastel flame on paper"
        data = ai_writer.parse_script_json(json.dumps(reply))
        self.assertEqual(data["overseas_caption"], "A pastel flame on paper")

    def test_blank_or_non_text_values_become_empty(self):
        reply = dict(self.REPLY, overseas_caption="   ", overseas_caption_ja=["x"])
        data = ai_writer.parse_script_json(json.dumps(reply))
        for key in ai_writer.OVERSEAS_KEYS:
            self.assertEqual(data[key], "")


class _FakeApiError(Exception):
    """Stands in for google.genai.errors.APIError without the SDK."""

    def __init__(self, code, status):
        super().__init__(f"{code} {status}")
        self.code = code
        self.status = status


class ModelCandidateTests(unittest.TestCase):
    def test_the_primary_model_comes_first(self):
        names = ai_writer.model_candidates("the-primary")
        self.assertEqual(names[0], "the-primary")
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(names[1:])

    def test_the_default_chain_starts_with_model_name(self):
        self.assertEqual(ai_writer.model_candidates()[0], ai_writer.MODEL_NAME)

    def test_a_fallback_is_not_repeated_when_it_is_the_primary(self):
        names = ai_writer.model_candidates(ai_writer.FALLBACK_MODELS[0])
        self.assertEqual(names.count(ai_writer.FALLBACK_MODELS[0]), 1)

    def test_retired_quota_and_overload_errors_are_retryable(self):
        for code, status in (
            (404, "NOT_FOUND"),
            (429, "RESOURCE_EXHAUSTED"),
            (503, "UNAVAILABLE"),
            (500, "INTERNAL"),
        ):
            with self.subTest(code=code):
                self.assertTrue(ai_writer.is_retryable_model_error(_FakeApiError(code, status)))

    def test_a_status_name_alone_is_enough(self):
        self.assertTrue(
            ai_writer.is_retryable_model_error(_FakeApiError(None, "RESOURCE_EXHAUSTED"))
        )

    def test_a_rejected_request_is_not_retryable(self):
        for code, status in ((400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED")):
            with self.subTest(code=code):
                self.assertFalse(ai_writer.is_retryable_model_error(_FakeApiError(code, status)))

    def test_a_plain_exception_is_not_retryable(self):
        self.assertFalse(ai_writer.is_retryable_model_error(ValueError("boom")))


class _FakeModels:
    def __init__(self, replies, calls):
        self._replies = replies
        self._calls = calls

    def generate_content(self, model, contents):
        self._calls.append((model, contents))
        reply = self._replies[model]
        if isinstance(reply, Exception):
            raise reply
        return type("Reply", (), {"text": reply})()


class _FakeClient:
    def __init__(self, replies, calls):
        self.models = _FakeModels(replies, calls)


VALID_SCRIPT = {
    "title": "Tiny pastel cat #Shorts",
    "hook_text": "Three strokes only",
    "narration": "A one-of-a-kind postcard.",
    "description": "Shop: {shop}",
    "tags": ["oil pastel", "japan"],
}


class GenerateScriptFallbackTests(unittest.TestCase):
    def _run(self, replies, model_name=None):
        calls = []
        kwargs = {"model_name": model_name} if model_name else {}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                mock.patch.object(ai_writer, "create_client", return_value=_FakeClient(replies, calls)) as factory:
            data = ai_writer.generate_making_script([], "style_A_material", CONFIG, **kwargs)
        return data, calls, factory

    def test_the_japanese_fields_reach_the_caller(self):
        script = dict(
            VALID_SCRIPT,
            title_ja="ちいさなパステルのねこ #Shorts",
            hook_ja="三筆だけ",
            narration_ja="世界に1枚だけのポストカードです。",
        )
        data, _, _ = self._run({ai_writer.MODEL_NAME: json.dumps(script)})
        self.assertEqual(data["title_ja"], script["title_ja"])
        self.assertEqual(data["hook_ja"], script["hook_ja"])
        self.assertEqual(data["narration_ja"], script["narration_ja"])

    def test_a_reply_without_japanese_still_returns_a_script(self):
        data, _, _ = self._run({ai_writer.MODEL_NAME: json.dumps(VALID_SCRIPT)})
        self.assertEqual(data["title"], "Tiny pastel cat")
        for key in ai_writer.TRANSLATION_KEYS:
            self.assertEqual(data[key], "")

    def test_a_working_primary_model_is_used_once(self):
        data, calls, _ = self._run({ai_writer.MODEL_NAME: json.dumps(VALID_SCRIPT)})
        self.assertEqual(data["title"], "Tiny pastel cat")
        self.assertEqual([name for name, _ in calls], [ai_writer.MODEL_NAME])

    def test_a_retired_model_falls_back_to_the_next_one(self):
        fallback = ai_writer.FALLBACK_MODELS[0]
        replies = {
            ai_writer.MODEL_NAME: _FakeApiError(404, "NOT_FOUND"),
            fallback: json.dumps(VALID_SCRIPT),
        }
        data, calls, _ = self._run(replies)
        self.assertEqual(data["title"], "Tiny pastel cat")
        self.assertEqual([name for name, _ in calls], [ai_writer.MODEL_NAME, fallback])

    def test_an_exhausted_quota_falls_back(self):
        replies = {name: _FakeApiError(429, "RESOURCE_EXHAUSTED") for name in ai_writer.model_candidates()}
        replies[ai_writer.model_candidates()[-1]] = json.dumps(VALID_SCRIPT)
        _, calls, _ = self._run(replies)
        self.assertEqual(len(calls), len(ai_writer.model_candidates()))

    def test_every_model_failing_raises_with_the_chain(self):
        replies = {name: _FakeApiError(503, "UNAVAILABLE") for name in ai_writer.model_candidates()}
        with self.assertRaises(RuntimeError) as caught:
            self._run(replies)
        self.assertIn(ai_writer.MODEL_NAME, str(caught.exception))

    def test_an_invalid_request_is_not_retried_on_another_model(self):
        replies = {ai_writer.MODEL_NAME: _FakeApiError(400, "INVALID_ARGUMENT")}
        with self.assertRaises(_FakeApiError):
            self._run(replies)

    def test_a_missing_key_raises_before_any_call(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(ai_writer, "create_client") as factory:
            with self.assertRaises(RuntimeError):
                ai_writer.generate_making_script([], "style_A_material", CONFIG)
        factory.assert_not_called()

    def test_existing_keyframes_are_sent_with_the_prompt(self):
        from PIL import Image

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        frame = os.path.join(tmp.name, "frame.jpg")
        Image.new("RGB", (8, 8), (250, 200, 210)).save(frame)
        replies = {ai_writer.MODEL_NAME: json.dumps(VALID_SCRIPT)}
        calls = []
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                mock.patch.object(ai_writer, "create_client",
                                  return_value=_FakeClient(replies, calls)):
            ai_writer.generate_making_script([frame], "style_A_material", CONFIG)
        contents = calls[0][1]
        self.assertEqual(len(contents), 2)
        self.assertIsInstance(contents[1], Image.Image)


class ClientApiVersionTests(unittest.TestCase):
    def test_the_client_is_pinned_to_v1beta(self):
        try:
            from google.genai import types  # noqa: F401
        except ImportError:  # pragma: no cover - SDK missing locally
            self.skipTest("google-genai is not installed")
        client = ai_writer.create_client("test-key")
        self.assertEqual(client._api_client._http_options.api_version, "v1beta")


if __name__ == "__main__":
    unittest.main()


class ParameterisedPromptTests(unittest.TestCase):
    """Requirement 2 and 4: the voice is budgeted and the language is chosen."""

    def params(self, **overrides):
        import improvement

        base = improvement.validate_params()
        base.update(overrides)
        return base

    def test_the_short_length_and_the_word_budget_travel_with_the_prompt(self):
        prompt = ai_writer.build_script_prompt(
            "style_B_story", CONFIG, params=self.params(target_duration_sec=24, narration_ratio=0.6)
        )
        self.assertIn("about 24 seconds long", prompt)
        # 24 s * 0.6 = 14.4 s of voice: a 34-44 word range, stated in the prompt
        self.assertIn("34-44 words", prompt)

    def test_the_short_voice_asks_for_a_brief_timeline(self):
        prompt = ai_writer.build_script_prompt(
            "style_B_story", CONFIG, params=self.params(target_duration_sec=28, narration_ratio=0.6)
        )
        self.assertIn("stop talking", prompt)
        self.assertNotIn("3-35 s", prompt)

    def test_a_japanese_short_asks_for_the_japanese_hook_first(self):
        prompt = ai_writer.build_script_prompt(
            "style_B_story", CONFIG, params=self.params(language_mode="ja")
        )
        self.assertIn("Japanese only", prompt)
        self.assertIn("hook_ja", prompt)

    def test_a_bilingual_short_explains_the_two_lines(self):
        prompt = ai_writer.build_script_prompt(
            "style_B_story", CONFIG, params=self.params(language_mode="bilingual")
        )
        self.assertIn("Japanese with the English line under it", prompt)

    def test_an_english_short_keeps_the_english_promise(self):
        prompt = ai_writer.build_script_prompt(
            "style_B_story", CONFIG, params=self.params(language_mode="en")
        )
        self.assertIn("English only", prompt)

    def test_the_voice_can_be_switched_off(self):
        prompt = ai_writer.build_script_prompt(
            "style_B_story", CONFIG, params=self.params(tts_mode="none")
        )
        self.assertIn("voice is off", prompt)

    def test_tags_are_english_only(self):
        prompt = ai_writer.build_script_prompt(
            "style_B_story", CONFIG, params=self.params()
        )
        self.assertIn("English only, no Japanese tags", prompt)
        self.assertNotIn("Japanese ones", prompt)

    def test_without_parameters_the_old_prompt_is_unchanged(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG)
        self.assertIn("100-120 words", prompt)
        self.assertIn("3-35 s", prompt)
        self.assertNotIn("Japanese only", prompt)


class TitleNormalisationTests(unittest.TestCase):
    """A Gemini reply is cleaned on the way in: no #Shorts, under 70 chars."""

    def reply(self, **overrides):
        payload = {
            "title": "Drawing Sushi in Oil Pastel #Shorts",
            "hook_text": "Watch this sushi appear",
            "narration": "A short narration.",
            "description": "A story.",
            "tags": ["oil pastel"],
        }
        payload.update(overrides)
        return ai_writer.parse_script_json(json.dumps(payload))

    def test_shorts_is_stripped_from_the_title(self):
        self.assertEqual(self.reply()["title"], "Drawing Sushi in Oil Pastel")

    def test_the_tag_is_stripped_wherever_it_sits(self):
        data = self.reply(title="Drawing #Shorts Sushi in Oil Pastel")
        self.assertEqual(data["title"], "Drawing Sushi in Oil Pastel")
        self.assertNotIn("#", data["title"])

    def test_a_long_title_is_cut_on_a_word_boundary(self):
        long_title = (
            "Drawing a very detailed sushi platter in oil pastel layer by layer "
            "until it glows #Shorts"
        )
        title = self.reply(title=long_title)["title"]
        self.assertLessEqual(len(title), ai_writer.TITLE_MAX_CHARS)
        self.assertNotIn("#Shorts", title)
        self.assertTrue(long_title.startswith(title))

    def test_a_short_title_is_untouched(self):
        self.assertEqual(
            ai_writer.normalise_title("Drawing Sushi in Oil Pastel"),
            "Drawing Sushi in Oil Pastel",
        )

    def test_a_long_hook_is_cut_to_seven_words(self):
        data = self.reply(hook_text="Watch this little sushi roll appear right before your eyes today")
        self.assertEqual(data["hook_text"], "Watch this little sushi roll appear right")
        self.assertLessEqual(len(data["hook_text"].split()), 7)


class MotifEnglishNameTests(unittest.TestCase):
    """The motif in English: a pinned name wins, kana is romanised, kanji is not."""

    def test_an_ascii_motif_is_returned_unchanged(self):
        self.assertEqual(ai_writer.motif_english_name("sushi"), "sushi")

    def test_a_pinned_name_beats_the_plain_romaji(self):
        self.assertEqual(
            ai_writer.motif_english_name("ポッキー", {"ポッキー": "Pocky"}), "Pocky"
        )

    def test_kana_is_romanised(self):
        self.assertEqual(ai_writer.motif_english_name("ポッキー"), "pokkii")
        self.assertEqual(ai_writer.motif_english_name("ねこ"), "neko")
        self.assertEqual(ai_writer.motif_english_name("ラーメン"), "raamen")
        self.assertEqual(ai_writer.motif_english_name("しゃしん"), "shashin")
        self.assertEqual(ai_writer.motif_english_name("きゃらめる"), "kyarameru")
        self.assertEqual(ai_writer.motif_english_name("ファミマ"), "famima")
        self.assertEqual(ai_writer.motif_english_name("さんぽ"), "sampo")
        self.assertEqual(ai_writer.motif_english_name("がっこう"), "gakkou")

    def test_kanji_gives_no_english_name(self):
        self.assertEqual(ai_writer.motif_english_name("桜"), "")
        self.assertEqual(ai_writer.motif_english_name("桜もち"), "")

    def test_an_empty_motif_gives_an_empty_name(self):
        self.assertEqual(ai_writer.motif_english_name(""), "")

    def test_motif_from_name_still_keeps_the_japanese(self):
        # The raw name is the Japanese record; only the English display name
        # is romanised, so the *_ja fields keep the original.
        self.assertEqual(ai_writer.motif_from_name("GO_20261201_ねこ"), "ねこ")


class PromptDepthTests(unittest.TestCase):
    """Depth is one vivid, specific detail per Short - never a longer script."""

    def test_the_prompt_asks_for_one_concrete_detail(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG)
        self.assertIn("One true detail", prompt)
        self.assertIn("why this motif was chosen", prompt)
        self.assertIn("one memory attached to it", prompt)
        self.assertIn("never invent a memory", prompt)
        self.assertIn("never as a generic line", prompt)

    def test_depth_does_not_grow_the_word_budget(self):
        import improvement

        params = improvement.validate_params()
        prompt = " ".join(
            ai_writer.build_script_prompt("style_B_story", CONFIG, params=params).split()
        )
        self.assertIn("Depth is one vivid detail, not a longer narration", prompt)
        low, high = improvement.narration_word_budget(params)
        self.assertIn(f"{low}-{high} words", prompt)

    def test_the_prompt_says_one_idea_per_short(self):
        prompt = ai_writer.build_script_prompt("style_A_material", CONFIG)
        self.assertIn("One idea per Short", prompt)
        self.assertIn("never a second subject", prompt)

    def test_the_prompt_holds_the_reveal_until_the_end(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG)
        self.assertIn("Hold the reveal", prompt)
        self.assertIn("without giving away the finished look", prompt)


class PromptMotifTests(unittest.TestCase):
    """The English motif name reaches the prompt, so the first line can say it."""

    def test_a_given_motif_is_named_in_the_prompt(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG, motif="Pocky")
        self.assertIn("- Motif: Pocky", prompt)
        self.assertIn("use exactly this name", prompt)

    def test_without_a_motif_there_is_no_motif_line(self):
        prompt = ai_writer.build_script_prompt("style_B_story", CONFIG)
        self.assertNotIn("- Motif:", prompt)

    def test_the_generator_passes_the_english_motif_to_the_prompt(self):
        import copy

        config = copy.deepcopy(CONFIG)
        config["brand"]["motif_names"] = {"ポッキー": "Pocky"}
        calls = []
        replies = {ai_writer.MODEL_NAME: json.dumps(VALID_SCRIPT)}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                mock.patch.object(ai_writer, "create_client",
                                  return_value=_FakeClient(replies, calls)):
            ai_writer.generate_making_script(
                [], "style_B_story", config, motif="GO_ポッキー"
            )
        prompt = calls[0][1][0]
        self.assertIn("- Motif: Pocky", prompt)
        self.assertNotIn("ポッキー", prompt)


class FallbackEnglishMotifTests(unittest.TestCase):
    """The offline template speaks English: romanised motif, no #Shorts."""

    PARAMS = {
        "target_duration_sec": 28,
        "narration_ratio": 0.62,
        "language_mode": "en",
        "tts_mode": "short",
    }

    def config(self, motif_names=None):
        import copy

        config = copy.deepcopy(CONFIG)
        if motif_names is not None:
            config["brand"]["motif_names"] = motif_names
        return config

    def script(self, motif, motif_names=None, style="style_A_material"):
        return ai_writer.fallback_script(
            style, self.config(motif_names), motif=motif, params=self.PARAMS
        )

    def test_a_pinned_name_is_used_everywhere_in_english(self):
        script = self.script("GO_ポッキー", {"ポッキー": "Pocky"})
        self.assertEqual(script["title"], "Drawing Pocky in Oil Pastel")
        self.assertEqual(script["hook_text"], "Watch this Pocky appear")
        self.assertIn("Pocky", script["narration"])
        self.assertIn("ポッキー", script["title_ja"])
        for field in ("title", "hook_text", "narration", "description", "overseas_caption"):
            self.assertNotIn("#Shorts", script[field], field)
            self.assertNotIn("ポッキー", script[field], field)
        self.assertLessEqual(len(script["title"]), 65)

    def test_an_unmapped_kana_name_is_romanised(self):
        # ポッキー is pinned in config.yaml's brand.motif_names, so the
        # unmapped case uses a different kana name here.
        script = self.script("GO_ラーメン")
        self.assertEqual(script["title"], "Drawing Raamen in Oil Pastel")
        self.assertNotIn("ラーメン", script["narration"])

    def test_a_hiragana_name_is_romanised(self):
        script = self.script("GO_ねこ")
        self.assertEqual(script["title"], "Drawing Neko in Oil Pastel")

    def test_a_kanji_name_falls_back_to_generic_english(self):
        script = self.script("GO_桜")
        self.assertEqual(script["title"], "Drawing a tiny postcard in oil pastel")
        for field in ("title", "hook_text", "narration", "description", "overseas_caption"):
            self.assertNotIn("桜", script[field], field)
        # The Japanese check copy still names the motif.
        self.assertIn("桜", script["title_ja"])

    def test_the_description_is_two_sentences_and_five_hashtags(self):
        script = self.script("GO_ポッキー", {"ポッキー": "Pocky"})
        body, hashtags = script["description"].rsplit("\n", 1)
        self.assertEqual(len(hashtags.split()), 5)
        self.assertNotIn("#Shorts", script["description"])
        self.assertIn("#CrayonJapan", hashtags)
        self.assertEqual(body.count("."), 2)

    def test_the_tags_are_all_english(self):
        script = self.script("GO_ポッキー", {"ポッキー": "Pocky"})
        self.assertEqual(len(script["tags"]), 10)
        for tag in script["tags"]:
            self.assertTrue(all(ord(ch) < 128 for ch in tag), tag)

    def test_the_ending_echoes_the_opening_strokes(self):
        script = self.script("GO_ポッキー", {"ポッキー": "Pocky"})
        self.assertTrue(
            script["narration"].endswith(
                "And it all starts again with a few oil pastel strokes."
            )
        )
        self.assertIn("comes out of a few oil pastel strokes", script["narration"])
