import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import ai_writer
from main import load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = load_config(os.path.join(ROOT, "config.yaml"))


def write_csv(path, rows):
    pd.DataFrame(rows, columns=["video_id", "style_used", "avg_view_percentage"]).to_csv(
        path, index=False
    )


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
        self.assertEqual(data["title"], self.VALID["title"])
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

    def test_asmr_prompt_asks_for_minimal_narration(self):
        prompt = ai_writer.build_script_prompt("style_C_asmr", CONFIG)
        self.assertIn("the drawing sound carries the video", prompt)
        self.assertIn("25 words", prompt)


if __name__ == "__main__":
    unittest.main()
