"""The script without Gemini: no key, exhausted quota, or a refused request.

The artist stopped paying for Gemini, so these tests are the promise that a run
still produces a complete script - every key the render, the telop and the
metadata pack read - from config.yaml and the artwork's own name.
"""

import json
import os
import unittest
from unittest import mock

import ai_writer

CONFIG = {
    "brand": {
        "name": "CrayonJapan",
        "shop_url": "https://example.test/shop",
        "material_name": "Sakura Cray-Pas Specialist",
        "theme": "Happy everyday moments",
    },
    "video": {"target_duration_sec": 28},
    "evolution": {
        "styles": {
            "style_A_material": {"weight": 0.34, "description": "texture"},
            "style_B_story": {"weight": 0.33, "description": "story"},
            "style_C_asmr": {"weight": 0.33, "description": "asmr"},
        }
    },
    "improvement": {"defaults": {"target_duration_sec": 28, "narration_ratio": 0.62}},
}

PARAMS = {
    "target_duration_sec": 28,
    "narration_ratio": 0.62,
    "language_mode": "bilingual",
    "tts_mode": "short",
}


class MotifTests(unittest.TestCase):
    def test_a_date_and_the_go_signal_are_dropped(self):
        self.assertEqual(ai_writer.motif_from_name("GO_20261121_sushi"), "sushi")

    def test_a_plain_file_name_keeps_its_word(self):
        self.assertEqual(ai_writer.motif_from_name("drawing.mp4"), "drawing")

    def test_a_japanese_name_survives(self):
        self.assertEqual(ai_writer.motif_from_name("GO_20261201_ねこ"), "ねこ")

    def test_a_name_that_is_only_a_date_gives_no_motif(self):
        self.assertEqual(ai_writer.motif_from_name("20261121"), "")
        self.assertEqual(ai_writer.motif_from_name("GO_2026-11-21"), "")

    def test_a_long_name_is_cut_on_a_word_boundary(self):
        motif = ai_writer.motif_from_name("a" * 30 + "_" + "b" * 30)
        self.assertLessEqual(len(motif), 40)


class FallbackScriptTests(unittest.TestCase):
    def _script(self, style="style_A_material", motif="GO_20261121_sushi", params=None):
        return ai_writer.fallback_script(style, CONFIG, motif=motif, params=params or PARAMS)

    def test_every_key_the_pipeline_reads_is_present(self):
        script = self._script()
        for key in ai_writer.REQUIRED_SCRIPT_KEYS + ai_writer.TRANSLATION_KEYS + ai_writer.OVERSEAS_KEYS:
            self.assertTrue(script.get(key), key)

    def test_the_pack_can_be_built_from_it(self):
        import metadata_writer

        script = self._script()
        metadata = metadata_writer.build_art_metadata(
            script,
            base_name="20261121_sushi",
            artwork_path="",
            video_path="",
            style_key="style_A_material",
            material_name=CONFIG["brand"]["material_name"],
            shop_url=CONFIG["brand"]["shop_url"],
        )
        text = metadata_writer.format_metadata_text(metadata)
        for needle in ("CrayonJapan", "Sakura Cray-Pas", "#oilpastel", "TikTok"):
            self.assertIn(needle, text)

    def test_the_motif_reaches_the_title_and_the_hook(self):
        script = self._script()
        self.assertIn("Sushi", script["title"])
        self.assertIn("sushi", script["hook_text"])
        self.assertLessEqual(len(script["hook_text"].split()), 7)
        self.assertLessEqual(len(script["title"]), 65)

    def test_the_narration_fits_the_planned_voice_budget(self):
        low, high = ai_writer._narration_range("style_A_material", PARAMS)
        words = len(self._script()["narration"].split())
        self.assertGreaterEqual(words, low)
        self.assertLessEqual(words, high)

    def test_a_longer_target_gets_more_words(self):
        params = dict(PARAMS, target_duration_sec=32, narration_ratio=0.95)
        long_words = len(self._script(params=params)["narration"].split())
        short_words = len(self._script()["narration"].split())
        self.assertGreater(long_words, short_words)

    def test_the_asmr_style_speaks_less(self):
        asmr, _ = ai_writer._narration_range("style_C_asmr", PARAMS)
        material, _ = ai_writer._narration_range("style_A_material", PARAMS)
        self.assertLess(asmr, material)

    def test_each_style_says_something_of_its_own(self):
        texts = {
            style: self._script(style=style)["narration"]
            for style in CONFIG["evolution"]["styles"]
        }
        self.assertNotEqual(texts["style_A_material"], texts["style_B_story"])
        self.assertNotEqual(texts["style_B_story"], texts["style_C_asmr"])

    def test_the_japanese_narration_is_a_parallel_text(self):
        script = self._script()
        self.assertTrue(any("\u3040" <= ch <= "\u30ff" for ch in script["narration_ja"]))
        # one Japanese sentence per English one, so the check reads line by line
        english = script["narration"].count(".")
        japanese = script["narration_ja"].count("。")
        self.assertEqual(english, japanese)

    def test_no_shop_link_is_written_by_hand(self):
        # ensure_shop_cta adds it; a second copy would double the call to action
        self.assertNotIn(CONFIG["brand"]["shop_url"], self._script()["description"])

    def test_without_a_motif_the_title_stays_generic(self):
        script = self._script(motif="")
        self.assertIn("oil pastel", script["title"])
        self.assertNotIn("None", script["title"])
        self.assertNotIn("{motif}", script["narration"])

    def test_the_ten_tags_are_filled(self):
        tags = self._script()["tags"]
        self.assertEqual(len(tags), 10)
        self.assertIn("sushi", tags)

    def test_the_script_says_where_it_came_from(self):
        script = self._script()
        self.assertEqual(script["script_source"], ai_writer.SCRIPT_SOURCE_TEMPLATE)

    def test_a_missing_brand_block_still_works(self):
        script = ai_writer.fallback_script("style_B_story", {}, motif="cat")
        self.assertTrue(script["title"])
        self.assertTrue(script["narration"])


class WriteScriptTests(unittest.TestCase):
    def test_gemini_is_used_when_it_answers(self):
        answer = {
            "title": "Tiny pastel cat #Shorts",
            "hook_text": "Three strokes only",
            "narration": "A one-of-a-kind postcard.",
            "description": "desc",
            "tags": ["oil pastel"],
        }

        def generator(*args, **kwargs):
            return dict(answer)

        script = ai_writer.write_script(
            [], "style_A_material", CONFIG, generator=generator, log=lambda *_: None
        )
        self.assertEqual(script["title"], answer["title"])
        self.assertEqual(script["script_source"], ai_writer.SCRIPT_SOURCE_GEMINI)

    def test_a_missing_key_falls_back_without_raising(self):
        logs = []

        def generator(*args, **kwargs):
            raise RuntimeError("GEMINI_API_KEY is not set (add it to .env)")

        script = ai_writer.write_script(
            [], "style_A_material", CONFIG, params=PARAMS, motif="GO_cat",
            generator=generator, log=logs.append,
        )
        self.assertEqual(script["script_source"], ai_writer.SCRIPT_SOURCE_TEMPLATE)
        self.assertIn("Cat", script["title"])
        self.assertTrue(any("no script from Gemini" in line for line in logs))
        self.assertIn("GEMINI_API_KEY", script["script_note"])

    def test_a_quota_error_falls_back_too(self):
        class Refused(Exception):
            code = 429
            status = "RESOURCE_EXHAUSTED"

        def generator(*args, **kwargs):
            raise Refused("quota")

        script = ai_writer.write_script(
            [], "style_C_asmr", CONFIG, params=PARAMS, generator=generator,
            log=lambda *_: None,
        )
        self.assertTrue(script["narration"])

    def test_the_real_generator_is_the_default(self):
        calls = []

        def generator(*args, **kwargs):
            calls.append(kwargs)
            return {
                "title": "t", "hook_text": "h", "narration": "n", "description": "d",
                "tags": [],
            }

        original = ai_writer.generate_making_script
        ai_writer.generate_making_script = generator
        try:
            ai_writer.write_script([], "style_A_material", CONFIG, params=PARAMS,
                                   reference_scan=True, log=lambda *_: None)
        finally:
            ai_writer.generate_making_script = original
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["reference_scan"])

    def test_a_missing_key_is_really_absent(self):
        """The generator must fail the same way the pipeline would see it."""
        previous = os.environ.pop("GEMINI_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                ai_writer.generate_making_script([], "style_A_material", CONFIG)
        finally:
            if previous is not None:
                os.environ["GEMINI_API_KEY"] = previous


class NoGeminiPipelineTests(unittest.TestCase):
    """main.py hands every Gemini failure to the template, not to the traceback."""

    def test_main_uses_the_template_when_the_generator_raises(self):
        import main

        logs = []
        with mock.patch.object(
            main, "generate_making_script", side_effect=RuntimeError("403 PERMISSION_DENIED")
        ), unittest.mock.patch("builtins.print", logs.append):
            script = main._script_for(
                ["frame.jpg"], "style_A_material", CONFIG, False, PARAMS, motif="GO_cat"
            )
        self.assertEqual(script["script_source"], ai_writer.SCRIPT_SOURCE_TEMPLATE)
        self.assertTrue(script["narration"])
        self.assertTrue(any("PERMISSION_DENIED" in line for line in logs))

    def test_main_keeps_geminis_script_when_it_works(self):
        import main

        answer = {
            "title": "t", "hook_text": "h", "narration": "n", "description": "d",
            "tags": ["oil pastel"],
        }
        with mock.patch.object(main, "generate_making_script", return_value=answer):
            script = main._script_for(
                [], "style_A_material", CONFIG, False, PARAMS, motif="GO_cat"
            )
        self.assertEqual(script["title"], "t")

    def test_the_json_reply_of_a_failed_run_is_never_needed(self):
        script = ai_writer.fallback_script("style_B_story", CONFIG, motif="cat", params=PARAMS)
        json.dumps(script)  # must be serialisable for the state/history
        self.assertIn("script_source", script)


if __name__ == "__main__":
    unittest.main()
