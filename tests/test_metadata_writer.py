import json
import os
import tempfile
import unittest

import metadata_writer as mw

SCRIPT = {
    "title": "Pastel Sakura Postcard #Shorts",
    "hook_text": "One tiny postcard",
    "narration": "This pastel postcard began as an empty sheet.",
    "description": "A one-of-a-kind original postcard. Shop: https://crayonjapan.base.shop/",
    "tags": ["oil pastel", " crayon ", "", "postcard art"],
    "title_ja": "パステルの桜ポストカード #Shorts",
    "hook_ja": "ちいさなポストカード1枚",
    "narration_ja": "このパステルのポストカードは、まっ白な紙から始まりました。",
}

GENERATED_AT = "2026-11-21T03:30:00+09:00"


def build(**overrides):
    kwargs = dict(
        base_name="clip",
        artwork_path="output/clip_artwork.jpg",
        video_path="output/clip_final.mp4",
        style_key="style_B_story",
        material_name="Sakura Cray-Pas Specialist",
        shop_url="https://crayonjapan.base.shop/",
        generated_at=GENERATED_AT,
    )
    kwargs.update(overrides)
    return mw.build_art_metadata(SCRIPT, **kwargs)


class BuildArtMetadataTests(unittest.TestCase):
    def test_maps_the_gemini_script_into_art_metadata(self):
        metadata = build()
        self.assertEqual(metadata["artwork_id"], "clip")
        self.assertEqual(metadata["artwork_title"], "Pastel Sakura Postcard #Shorts")
        self.assertEqual(metadata["hook_text"], "One tiny postcard")
        self.assertEqual(metadata["style"], "style_B_story")
        self.assertEqual(metadata["material"], "Sakura Cray-Pas Specialist")
        self.assertEqual(metadata["shop_url"], "https://crayonjapan.base.shop/")
        self.assertEqual(metadata["artwork_image"], "clip_artwork.jpg")
        self.assertEqual(metadata["short_video"], "clip_final.mp4")
        self.assertEqual(metadata["generated_at"], GENERATED_AT)

    def test_tags_are_cleaned_and_trimmed(self):
        self.assertEqual(build()["tags"], ["oil pastel", "crayon", "postcard art"])

    def test_comma_separated_tags_still_work(self):
        script = dict(SCRIPT, tags="oil pastel, sakura , ")
        metadata = mw.build_art_metadata(script, base_name="clip", generated_at=GENERATED_AT)
        self.assertEqual(metadata["tags"], ["oil pastel", "sakura"])

    def test_missing_fields_do_not_crash(self):
        metadata = mw.build_art_metadata({}, base_name="clip", generated_at=GENERATED_AT)
        self.assertEqual(metadata["artwork_title"], "")
        self.assertEqual(metadata["tags"], [])
        self.assertEqual(metadata["artwork_image"], "")
        self.assertTrue(metadata["generated_at"])

    def test_generated_at_defaults_to_japan_time(self):
        metadata = mw.build_art_metadata(SCRIPT, base_name="clip")
        self.assertTrue(metadata["generated_at"].endswith("+09:00"))


class FormatMetadataTextTests(unittest.TestCase):
    def test_contains_every_block_needed_to_post(self):
        text = mw.format_metadata_text(build())
        for heading in ("=== TITLE ===", "=== DESCRIPTION ===", "=== TAGS ===", "=== SHOP ==="):
            self.assertIn(heading, text)
        self.assertIn("Pastel Sakura Postcard #Shorts", text)
        self.assertIn("oil pastel, crayon, postcard art", text)
        self.assertIn("clip_artwork.jpg", text)
        self.assertIn("https://crayonjapan.base.shop/", text)

    def test_empty_metadata_still_renders(self):
        text = mw.format_metadata_text({})
        self.assertIn("=== TITLE ===", text)


class JapaneseTranslationTests(unittest.TestCase):
    """The pack carries the English narration and the Japanese the artist checks."""

    def test_json_keeps_the_japanese_translations(self):
        metadata = build()
        self.assertEqual(metadata["artwork_title_ja"], SCRIPT["title_ja"])
        self.assertEqual(metadata["hook_text_ja"], SCRIPT["hook_ja"])
        self.assertEqual(metadata["narration_ja"], SCRIPT["narration_ja"])

    def test_text_lists_each_english_block_before_its_japanese(self):
        text = mw.format_metadata_text(build())
        headings = [
            "=== TITLE ===",
            "=== TITLE (JA) ===",
            "=== HOOK TEXT (first 2.5s) ===",
            "=== HOOK TEXT (JA) ===",
            "=== NARRATION (EN) ===",
            "=== NARRATION (JA) ===",
            "=== DESCRIPTION ===",
            "=== TAGS ===",
            "=== SHOP ===",
        ]
        positions = [text.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))

    def test_text_carries_the_full_english_narration_and_its_japanese(self):
        text = mw.format_metadata_text(build())
        self.assertIn(SCRIPT["narration"], text)
        self.assertIn(SCRIPT["narration_ja"], text)
        self.assertIn(SCRIPT["title_ja"], text)
        self.assertIn(SCRIPT["hook_ja"], text)

    def test_missing_translations_are_marked_not_left_blank(self):
        script = dict(SCRIPT, title_ja="", hook_ja="", narration_ja="")
        metadata = mw.build_art_metadata(script, base_name="clip", generated_at=GENERATED_AT)
        text = mw.format_metadata_text(metadata)
        self.assertEqual(text.count(mw.TRANSLATION_MISSING), 3)

    def test_an_older_script_without_japanese_still_writes_the_pack(self):
        script = {key: SCRIPT[key] for key in ("title", "hook_text", "narration", "description", "tags")}
        metadata = mw.build_art_metadata(script, base_name="clip", generated_at=GENERATED_AT)
        text = mw.format_metadata_text(metadata)
        self.assertIn(SCRIPT["narration"], text)
        self.assertNotIn("None", text)


class WriteMetadataFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_writes_text_and_json_with_matching_names(self):
        paths = mw.write_metadata_files(
            SCRIPT,
            output_dir=self.tmp.name,
            base_name="clip",
            artwork_path="output/clip_artwork.jpg",
            video_path="output/clip_final.mp4",
            style_key="style_A_material",
            material_name="Sakura Cray-Pas Specialist",
            shop_url="https://crayonjapan.base.shop/",
            generated_at=GENERATED_AT,
        )
        self.assertEqual(paths["text"], os.path.join(self.tmp.name, "clip_metadata.txt"))
        self.assertEqual(paths["json"], os.path.join(self.tmp.name, "clip_art_metadata.json"))
        self.assertTrue(os.path.exists(paths["text"]))
        self.assertTrue(os.path.exists(paths["json"]))

        with open(paths["json"], "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["artwork_id"], "clip")
        self.assertEqual(saved["style"], "style_A_material")
        self.assertEqual(saved["narration_ja"], SCRIPT["narration_ja"])

        with open(paths["text"], "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("=== TITLE ===", text)
        self.assertIn("=== NARRATION (JA) ===", text)
        self.assertIn(SCRIPT["narration_ja"], text)

    def test_creates_a_missing_output_directory(self):
        nested = os.path.join(self.tmp.name, "output", "day1")
        paths = mw.write_metadata_files(SCRIPT, output_dir=nested, base_name="clip")
        self.assertTrue(os.path.exists(paths["json"]))


class ShopCallToActionTests(unittest.TestCase):
    """The BASE shop link and the original-for-sale line are guaranteed."""

    URL = "https://crayonjapan.base.shop/"

    def test_the_description_always_ends_with_the_shop_call_to_action(self):
        text = mw.ensure_shop_cta("Just the story of one warm layer.", self.URL)
        self.assertIn("Original artwork available at:", text)
        self.assertIn(self.URL, text)
        self.assertIn("1点ものの原画はこちらで販売中です", text)
        self.assertTrue(text.startswith("Just the story"))

    def test_a_shop_line_from_the_model_is_replaced_not_doubled(self):
        gemini = (
            "A one-of-a-kind original postcard made of creamy pastel.\n\n"
            "Shop this original 1-of-1 postcard: https://crayonjapan.base.shop/\n\n"
            "#SakuraCrayPas #OilPastelArt"
        )
        text = mw.ensure_shop_cta(gemini, self.URL)
        self.assertNotIn("Shop this original 1-of-1 postcard", text)
        # the English block and the Japanese block: one link each, no third one
        self.assertEqual(text.count(self.URL), 2)
        self.assertIn("Original artwork available at:", text)

    def test_the_hashtags_stay_at_the_end(self):
        gemini = "A warm story.\n\n#SakuraCrayPas #CrayPasSpecialist #OilPastelArt"
        lines = mw.ensure_shop_cta(gemini, self.URL).splitlines()
        self.assertTrue(lines[-1].startswith("#SakuraCrayPas"))
        self.assertIn("1点ものの原画はこちらで販売中です", "\n".join(lines))

    def test_a_story_sentence_that_mentions_the_shop_is_kept(self):
        gemini = (
            "Read the story at https://crayonjapan.base.shop/ where this original "
            "postcard is waiting for its next home."
        )
        text = mw.ensure_shop_cta(gemini, self.URL)
        self.assertIn("where this original postcard is waiting", text)
        self.assertIn("Original artwork available at:", text)

    def test_the_call_to_action_is_not_added_twice(self):
        once = mw.ensure_shop_cta("A warm story.", self.URL)
        self.assertEqual(mw.ensure_shop_cta(once, self.URL), once)

    def test_a_url_without_scheme_or_slash_is_recognised(self):
        gemini = "A warm story.\n\nShop: crayonjapan.base.shop\n\n#Shorts"
        text = mw.ensure_shop_cta(gemini, self.URL)
        self.assertNotIn("Shop: crayonjapan.base.shop", text)
        self.assertEqual(text.count(self.URL), 2)

    def test_without_a_shop_url_the_description_is_untouched(self):
        self.assertEqual(mw.ensure_shop_cta("A warm story.", ""), "A warm story.")
        self.assertEqual(mw.ensure_shop_cta("", self.URL), "\n\n".join(
            [mw.SHOP_CTA_EN.format(url=self.URL), mw.SHOP_CTA_JA.format(url=self.URL)]
        ))

    def test_the_pack_and_the_json_both_carry_the_call_to_action(self):
        metadata = build()
        self.assertIn(self.URL, metadata["description_en"])
        self.assertIn("Original artwork available at:", metadata["description_en"])
        pack = mw.format_metadata_text(metadata)
        self.assertIn("Original artwork available at:", pack)
        self.assertIn("1点ものの原画はこちらで販売中です", pack)

    def test_the_description_reaches_the_upload_body_unchanged(self):
        import youtube_uploader

        metadata = build()
        body = youtube_uploader.build_upload_body(
            {"title": "t", "description": metadata["description_en"], "tags": []}
        )
        self.assertIn(self.URL, body["snippet"]["description"])
        self.assertIn("Original artwork available at:", body["snippet"]["description"])


if __name__ == "__main__":
    unittest.main()