import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

import telop

CARD = "The secret to a glowing sakura petal"


def timed(text, start=0.30, duration=0.30, gap=0.06):
    """A word list with the steady pace edge-tts produces."""
    words = []
    moment = start
    for piece in text.split():
        words.append({"text": piece, "start": round(moment, 3), "end": round(moment + duration, 3)})
        moment += duration + gap
    return words


def dialogues(ass_text):
    rows = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:"):].split(",", 9)
        rows.append({
            "layer": int(fields[0]),
            "start": telop.parse_timestamp(fields[1]),
            "end": telop.parse_timestamp(fields[2]),
            "style": fields[3],
            "text": fields[9],
        })
    return rows


def style_rows(ass_text):
    rows = {}
    for line in ass_text.splitlines():
        if not line.startswith("Style:"):
            continue
        fields = line[len("Style:"):].strip().split(",")
        rows[fields[0]] = {
            "font": fields[1],
            "size": int(fields[2]),
            "bold": int(fields[7]),
            "outline": int(fields[16]),
            "shadow": int(fields[17]),
            "alignment": int(fields[18]),
            "margin_l": int(fields[19]),
            "margin_r": int(fields[20]),
            "margin_v": int(fields[21]),
        }
    return rows


def visible_text(ass_line):
    """The words a viewer sees on one dialogue line (tags removed)."""
    return re.sub(r"\{[^}]*\}", "", ass_line.replace("\\N", " ")).strip()


class CardTests(unittest.TestCase):
    """1/4 of the sheet: reading comfort."""

    def test_a_card_never_needs_three_lines(self):
        cards = telop.plan_cards(timed(CARD * 3))
        self.assertTrue(cards)
        for card in cards:
            self.assertLessEqual(len(card["lines"]), 2)

    def test_a_line_stays_inside_the_frame(self):
        words = timed("This one-of-a-kind postcard is wonderfully smooth " * 2)
        cards = telop.plan_cards(words)
        for card in cards:
            for line in card["lines"]:
                rendered = " ".join(card["words"][index]["text"] for index in line)
                self.assertLessEqual(len(rendered), telop.SUB_MAX_CHARS)

    def test_words_are_never_cut_in_half(self):
        narration = "Creamy buttery pigment, layered warm, keeps every petal alive"
        cards = telop.plan_cards(timed(narration))
        seen = [word["text"] for card in cards for word in card["words"]]
        expected = [word.strip(".,") for word in narration.split()]
        self.assertEqual(seen, expected)

    def test_a_card_ends_without_a_full_stop_or_comma(self):
        cards = telop.plan_cards(timed("I start here. Then the next layer, warm."))
        for card in cards:
            self.assertNotIn(card["text"][-1], ".,;:")

    def test_punctuation_inside_a_card_is_dropped_too(self):
        cards = telop.plan_cards(timed("The paper grabs the wax. Then the colour sings."))
        for card in cards:
            for word in card["words"]:
                self.assertFalse(word["text"].endswith((".", ",")), word["text"])

    def test_an_exclamation_keeps_its_voice(self):
        cards = telop.plan_cards(timed("Watch this!"))
        self.assertEqual(cards[0]["text"], "Watch this!")

    def test_a_flash_quick_card_is_merged_into_a_neighbour(self):
        words = timed("so good it glows")
        # "it" is spoken in 60 ms: too quick to read on its own.
        words[2]["end"] = words[2]["start"] + 0.06
        words[3]["start"] = words[2]["end"]
        words[3]["end"] = words[3]["start"] + 0.30
        cards = telop.plan_cards(words)
        shortest = min(card["end"] - card["start"] for card in cards)
        self.assertGreaterEqual(shortest, telop.MIN_CARD_SECONDS - 0.001)
        self.assertEqual(
            " ".join(card["text"] for card in cards).split(),
            [word["text"] for word in words],
        )

    def test_one_message_per_card_closes_on_a_sentence(self):
        cards = telop.plan_cards(timed("The paper grabs the wax. Then the colour sings."))
        self.assertGreaterEqual(len(cards), 2)
        self.assertIn("wax", cards[0]["text"])

    def test_cards_follow_each_other_without_a_gap(self):
        cards = telop.plan_cards(timed(CARD * 2))
        for earlier, later in zip(cards, cards[1:]):
            self.assertLessEqual(earlier["end"], later["start"] + 0.001)
            self.assertGreaterEqual(later["start"], earlier["spoken_end"] - 0.001)

    def test_a_pause_lets_the_card_leave_the_screen(self):
        words = timed("first thought", start=0.4)
        words += timed("second thought", start=6.0)
        cards = telop.plan_cards(words)
        self.assertAlmostEqual(
            cards[0]["end"], cards[0]["spoken_end"] + telop.CARD_TAIL_SECONDS, places=3
        )


class PlacementTests(unittest.TestCase):
    """2/4 of the sheet: where the text goes and how it is drawn."""

    def setUp(self):
        self.ass = telop.build_telop_ass(
            timed(CARD * 2),
            hook_text="One warm layer",
            title="CrayonJapan / Sakura",
            cta_text="Follow for more!",
            video_seconds=12.0,
        )
        self.styles = style_rows(self.ass)

    def test_the_subtitle_band_clears_the_shorts_ui(self):
        telop_style = self.styles["Telop"]
        self.assertEqual(telop_style["alignment"], 2)
        self.assertEqual(telop_style["margin_v"], telop.BOTTOM_SAFE)
        self.assertGreaterEqual(telop_style["margin_r"], telop.RIGHT_SAFE)
        self.assertGreaterEqual(telop_style["margin_l"], telop.RIGHT_SAFE)

    def test_the_hook_and_title_sit_below_the_top_bar(self):
        for name in ("Hook", "Title"):
            self.assertGreater(self.styles[name]["margin_v"], telop.TOP_SAFE)
            self.assertEqual(self.styles[name]["alignment"], 8)

    def test_the_hook_stays_above_the_middle_of_the_drawing(self):
        self.assertLess(
            self.styles["Hook"]["margin_v"] + 2 * telop.HOOK_FONT_SIZE, telop.FRAME_HEIGHT // 2
        )
        self.assertLess(
            self.styles["Cta"]["margin_v"] + 2 * telop.CTA_FONT_SIZE, telop.FRAME_HEIGHT // 2
        )

    def test_the_type_is_heavy_with_an_outline_and_a_shadow(self):
        for name in ("Telop", "Hook", "Cta"):
            style = self.styles[name]
            self.assertEqual(style["bold"], -1)
            self.assertGreaterEqual(style["outline"], 4)
            self.assertGreaterEqual(style["shadow"], 2)
            self.assertIn(style["font"], telop.FONT_CANDIDATES)

    def test_the_font_probe_only_names_a_font_we_can_count_on(self):
        self.assertIn(telop.detect_font(), telop.FONT_CANDIDATES)

    def test_a_missing_font_probe_still_returns_the_runner_font(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("no fc-list")):
            self.assertEqual(telop.detect_font(), telop.DEFAULT_FONT)

    def test_the_font_probe_reads_the_families_fontconfig_lists(self):
        done = mock.Mock(stdout="Liberation Sans\nLiberation Serif, Liberation Mono\n")
        with mock.patch.object(subprocess, "run", return_value=done):
            self.assertEqual(telop.detect_font(), "Liberation Sans")

    def test_a_keyword_is_gold_and_slightly_larger(self):
        ass = telop.build_telop_ass(timed("the secret layer"), video_seconds=4.0)
        card = next(row for row in dialogues(ass) if row["layer"] == 0)
        accent = "\\c" + telop.ACCENT_COLOUR
        self.assertIn(accent + "\\fscx106\\fscy106}secret", card["text"])
        self.assertIn("{\\r}the", card["text"])

    def test_the_scripts_own_emphasis_is_honoured(self):
        text, marked = telop.strip_emphasis_markers("This **canvas** is warm")
        self.assertEqual(text, "This canvas is warm")
        self.assertEqual(marked, ["canvas"])
        ass = telop.build_telop_ass(timed(text), marked=marked, video_seconds=4.0)
        card = next(row for row in dialogues(ass) if row["layer"] == 0)
        self.assertIn("\\c" + telop.ACCENT_COLOUR + "\\fscx106\\fscy106}canvas", card["text"])

    def test_a_brace_in_a_word_cannot_break_the_document(self):
        words = [{"text": "a{b}c", "start": 0.4, "end": 0.9}]
        ass = telop.build_telop_ass(words, video_seconds=3.0)
        self.assertNotIn("{b}", ass)
        self.assertIn("a(b)c", ass)


class MotionTests(unittest.TestCase):
    """3/4 and 4/4: how the text behaves and what each line is for."""

    def setUp(self):
        self.words = timed("The secret to a glowing sakura petal", start=0.4)
        self.ass = telop.build_telop_ass(
            self.words,
            hook_text="One warm layer",
            title="CrayonJapan / Sakura",
            cta_text="Follow for more!",
            video_seconds=12.0,
        )
        self.rows = dialogues(self.ass)

    def test_the_hook_pops_in_and_leaves_before_the_body(self):
        hook = next(row for row in self.rows if row["style"] == "Hook")
        self.assertIn("\\fscx45", hook["text"])
        self.assertIn("\\t(0,150", hook["text"])
        self.assertEqual(hook["start"], 0.25)
        self.assertLessEqual(hook["end"], telop.HOOK_SECONDS + 0.01)

    def test_the_title_stays_for_the_whole_short(self):
        title = next(row for row in self.rows if row["style"] == "Title")
        self.assertEqual(title["start"], 0.6)
        self.assertGreaterEqual(title["end"], 11.5)

    def test_the_follow_line_closes_the_short(self):
        cta = next(row for row in self.rows if row["style"] == "Cta")
        self.assertGreater(cta["start"], 6.0)
        self.assertLessEqual(cta["end"], 12.0)
        self.assertIn("Follow", visible_text(cta["text"]))

    def test_every_spoken_word_gets_its_own_highlight_overlay(self):
        overlays = [row for row in self.rows if row["layer"] == 1]
        cards = telop.plan_cards(self.words)
        self.assertEqual(len(overlays), sum(len(card["words"]) for card in cards))
        self.assertGreaterEqual(len([row for row in self.rows if row["layer"] == 0]), 1)

    def test_an_overlay_shows_one_word_and_hides_the_rest(self):
        overlays = [row for row in self.rows if row["layer"] == 1]
        first = overlays[0]
        self.assertIn("\\fscx124", first["text"])
        self.assertEqual(first["text"].count("\\alpha&HFF&"), len(visible_text(first["text"]).split()) - 1)
        self.assertEqual(visible_text(first["text"]).split()[0], "The")

    def test_the_highlight_moves_with_the_voice(self):
        cards = telop.plan_cards(self.words)
        overlays = [row for row in self.rows if row["layer"] == 1]
        first_card = cards[0]
        for index in range(len(first_card["words"])):
            self.assertAlmostEqual(
                overlays[index]["start"], round(first_card["words"][index]["start"], 2),
                places=2,
            )

    def test_the_overlay_never_runs_past_its_card(self):
        cards = telop.plan_cards(self.words)
        overlays = [row for row in self.rows if row["layer"] == 1]
        index = 0
        for card in cards:
            for _ in card["words"]:
                self.assertLessEqual(overlays[index]["end"], card["end"] + 0.01)
                index += 1

    def test_a_short_without_word_timings_still_gets_hook_title_and_cta(self):
        ass = telop.build_telop_ass(
            [], hook_text="One warm layer", title="CrayonJapan",
            cta_text="Follow for more!", video_seconds=8.0,
        )
        self.assertEqual({row["style"] for row in dialogues(ass)}, {"Hook", "Title", "Cta"})

    def test_without_anything_to_say_the_document_is_still_valid(self):
        ass = telop.build_telop_ass([], hook_text="", title="", cta_text="", video_seconds=5.0)
        self.assertIn("[Events]", ass)
        self.assertNotIn("Dialogue:", ass)

    def test_the_cta_never_falls_before_the_last_words(self):
        words = timed("a long closing line that ends late", start=0.4)
        last_end = words[-1]["end"]
        ass = telop.build_telop_ass(words, cta_text="Follow for more!", video_seconds=30.0)
        cta = next(row for row in dialogues(ass) if row["style"] == "Cta")
        self.assertGreater(cta["start"], last_end - 1.5)


class DocumentTests(unittest.TestCase):
    def test_the_header_is_a_1080x1920_ass_script(self):
        ass = telop.build_telop_ass(timed(CARD), video_seconds=10.0)
        self.assertIn("ScriptType: v4.00+", ass)
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn("ScaledBorderAndShadow: yes", ass)

    def test_every_event_has_the_ten_fields_ass_expects(self):
        ass = telop.build_telop_ass(
            timed(CARD * 2), hook_text="One warm layer", title="CrayonJapan",
            cta_text="Follow for more!", video_seconds=14.0,
        )
        for line in ass.splitlines():
            if line.startswith("Dialogue:"):
                self.assertEqual(len(line[len("Dialogue:"):].split(",", 9)), 10, line)

    def test_timestamps_use_centiseconds(self):
        self.assertEqual(telop.format_timestamp(0), "0:00:00.00")
        self.assertEqual(telop.format_timestamp(61.239), "0:01:01.24")
        self.assertEqual(telop.format_timestamp(-3), "0:00:00.00")
        self.assertAlmostEqual(telop.parse_timestamp("0:01:01.24"), 61.24, places=2)

    def test_the_title_loses_hashtags_and_emoji(self):
        title = telop.clean_title("Glowing sakura 🌸 #Shorts #OilPastel", "CrayonJapan")
        self.assertNotIn("#", title)
        self.assertNotIn("🌸", title)
        self.assertTrue(title.startswith("CrayonJapan / Glowing sakura"))

    def test_a_long_title_is_cut_on_a_word_boundary(self):
        title = telop.clean_title(
            "A wonderfully long title about creamy butter-yellow sakura petals",
            "CrayonJapan",
            max_chars=30,
        )
        self.assertLessEqual(len(title), 33)
        self.assertTrue(title.endswith("..."))
        self.assertNotIn("  ", title)
        # the cut is a whole word plus the ellipsis, never a chopped stem
        self.assertTrue(title[:-3].endswith(title[:-3].split(" ")[-1]))

    def test_cards_never_exceed_two_lines_over_a_long_narration(self):
        narration = (
            "The secret to a glowing sakura petal is not more pink. It is one warm layer "
            "underneath, pressed flat with my thumb. The grain grabs the wax, and the "
            "colour already looks alive. Creamy, buttery pigment makes every stroke sing."
        )
        cards = telop.plan_cards(timed(narration))
        self.assertGreaterEqual(len(cards), 6)
        for card in cards:
            self.assertLessEqual(len(card["lines"]), 2)


class TimingTests(unittest.TestCase):
    def test_the_narration_punctuation_lands_on_its_word(self):
        words = telop.word_timings_from_boundaries([
            {"type": "WordBoundary", "offset": i * 3000000, "duration": 2500000, "text": word}
            for i, word in enumerate(["The", "paper", "grabs", "the", "wax"])
        ])
        attached = telop.attach_punctuation(words, "The paper grabs the wax. Then it sings.")
        self.assertEqual([word["text"] for word in attached],
                         ["The", "paper", "grabs", "the", "wax."])

    def test_a_sentence_end_from_the_narration_closes_a_card(self):
        text = "The paper grabs the wax. Then the colour sings."
        words = telop.attach_punctuation([
            {"text": word, "start": round(i * 0.4, 3), "end": round(i * 0.4 + 0.32, 3)}
            for i, word in enumerate(text.replace(".", "").split())
        ], text)
        cards = telop.plan_cards(words)
        self.assertIn("wax", cards[0]["text"])
        self.assertNotIn("Then", cards[0]["text"])
        for card in cards:
            self.assertNotIn(card["text"][-1], ".")

    def test_an_unmatched_word_keeps_its_own_text(self):
        words = [{"text": "Sakura", "start": 0.0, "end": 0.4}]
        attached = telop.attach_punctuation(words, "Nothing in common here")
        self.assertEqual(attached[0]["text"], "Sakura")

    def test_word_boundaries_are_converted_from_ticks(self):
        words = telop.word_timings_from_boundaries([
            {"type": "WordBoundary", "offset": 1000000, "duration": 5000000, "text": "Wait"},
            {"type": "WordBoundary", "offset": 7000000, "duration": 4000000, "text": " for it "},
        ])
        self.assertEqual([word["text"] for word in words], ["Wait", "for it"])
        self.assertAlmostEqual(words[0]["start"], 0.1, places=3)
        self.assertAlmostEqual(words[0]["end"], 0.6, places=3)

    def test_a_sentence_boundary_is_shared_out_over_its_words(self):
        words = telop.word_timings_from_boundaries([
            {"type": "SentenceBoundary", "offset": 0, "duration": 20000000,
             "text": "Two words now"},
        ])
        self.assertEqual([word["text"] for word in words], ["Two", "words", "now"])
        self.assertAlmostEqual(words[0]["start"], 0.0, places=3)
        self.assertAlmostEqual(words[-1]["end"], 2.0, places=3)
        self.assertLess(words[0]["end"], words[1]["end"])

    def test_audio_chunks_are_ignored(self):
        self.assertEqual(telop.word_timings_from_boundaries([{"type": "audio", "data": b"x"}]), [])

    def test_timings_round_trip_through_the_voice_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clip_words.json")
            telop.write_word_timings(path, telop.word_timings_from_boundaries([
                {"type": "WordBoundary", "offset": 0, "duration": 3000000, "text": "Hello"},
            ]))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["words"][0]["text"], "Hello")
            self.assertEqual(telop.load_word_timings(path)[0]["text"], "Hello")

    def test_an_unreadable_timings_file_is_simply_empty(self):
        self.assertEqual(telop.load_word_timings("/nope/missing.json"), [])


if __name__ == "__main__":
    unittest.main()
