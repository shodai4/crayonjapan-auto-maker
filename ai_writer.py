"""Gemini-driven English script writing and narrative-style selection.

The style with the best average view percentage wins most of the time, with a
configurable exploration rate so new styles keep getting a chance.
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Dict, List, Mapping, Optional, Sequence

from analytics_updater import read_performance_csv

MODEL_NAME = "gemini-3-flash-preview"
# The gemini-3 family is served on v1beta only. google-genai 3.x defaults to
# v1, where a v1beta-only model answers 404 "not found for API version v1".
API_VERSION = "v1beta"
# Tried in order when a model is retired, out of free-tier quota or busy.
# Retired/quota/overload replies cost no quota, so a fallback is cheap.
# The GAS board relay uses gemini-3.8-flash, so the pipeline keeps its daily
# free-tier quota on a different model when it can.
FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.8-flash")
# 404 retired/unknown model, 429 free-tier quota, 5xx overload.
RETRYABLE_STATUS_CODES = frozenset({404, 429, 500, 502, 503, 504})
# The artist checks every script in Japanese before it is posted, so the reply
# carries the three Japanese fields next to their English originals. They are
# optional (an older or partial reply must still render); the metadata pack
# simply says the translation is missing.
TRANSLATION_KEYS = ("title_ja", "hook_ja", "narration_ja")
# The overseas caption sentence is optional in the same way: metadata_writer
# builds the TikTok / Instagram / Pinterest block without it, using the hook or
# the title as the subject, so an older or partial reply never costs the pack.
OVERSEAS_KEYS = ("overseas_caption", "overseas_caption_ja")
REQUIRED_SCRIPT_KEYS = ("title", "hook_text", "narration", "description", "tags")
# Gemini occasionally answers with the key names of its own proposal ("hook",
# "script") or a different spelling of a Japanese field, so one naming slip does
# not throw away a whole render. The first accepted name is the canonical one.
SCRIPT_KEY_ALIASES = {
    "hook_text": ("hook_text", "hook"),
    "narration": ("narration", "script"),
}
TRANSLATION_KEY_ALIASES = {
    "title_ja": ("title_ja", "title_jp", "title_japanese"),
    "hook_ja": ("hook_ja", "hook_text_ja", "hook_jp", "hook_japanese"),
    "narration_ja": ("narration_ja", "script_ja", "narration_jp", "script_japanese"),
}
OVERSEAS_KEY_ALIASES = {
    "overseas_caption": (
        "overseas_caption",
        "overseas_caption_en",
        "overseas_text",
        "caption_overseas",
    ),
    "overseas_caption_ja": (
        "overseas_caption_ja",
        "overseas_caption_jp",
        "overseas_text_ja",
        "caption_overseas_ja",
    ),
}
ASMR_STYLE_KEY = "style_C_asmr"
# The voice now has to carry the whole Short. The earlier 65-85 word script (and
# the 25-word ASMR cap) left the second half of the video silent, which the
# artist asked to remove: 100-120 words at the edge-tts pace is 40-45 seconds of
# nearly continuous speech, matching a ~45 s cut.
NARRATION_MIN_WORDS = 100
NARRATION_MAX_WORDS = 120
# ASMR still keeps the drawing sound in front, so it talks a little less and
# leans on short beats - but never returns to long silences.
ASMR_NARRATION_MIN_WORDS = 85
ASMR_NARRATION_MAX_WORDS = 105
# Setting this environment variable (or the workflow_dispatch input that feeds
# it) forces one style for a single run, e.g. while a fix is being verified.
# Unset/empty keeps the usual learned pick, so normal runs stay automatic.
STYLE_OVERRIDE_ENV = "STYLE_OVERRIDE"


def load_performance(csv_path: str) -> Optional[List[dict]]:
    """Read the performance log as rows, or None when it is missing/unreadable.

    A file with only a header reads as an empty list.
    """
    if not os.path.exists(csv_path):
        return None
    try:
        return read_performance_csv(csv_path)
    except Exception:  # noqa: BLE001 - a broken log is the same as no log
        return None


def requested_style_override(
    config: dict,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The style forced through ``STYLE_OVERRIDE``, if it names a known one."""
    env = os.environ if environ is None else environ
    requested = str(env.get(STYLE_OVERRIDE_ENV, "") or "").strip()
    if not requested:
        return None
    styles = config["evolution"]["styles"]
    if requested in styles:
        return requested
    print(
        f"[style] ignoring {STYLE_OVERRIDE_ENV}={requested!r}: "
        f"not one of {', '.join(styles)}"
    )
    return None


def choose_best_style(
    csv_path: str,
    config: dict,
    rng: Optional[random.Random] = None,
) -> str:
    """Pick the narrative style for the next video."""
    rng = rng or random
    styles_cfg = config["evolution"]["styles"]
    style_names: List[str] = list(styles_cfg.keys())

    override = requested_style_override(config)
    if override:
        print(f"[style] {STYLE_OVERRIDE_ENV}={override} in use; the learned pick is skipped")
        return override

    explore_rate = float(config["evolution"].get("exploration_rate", 0.25))

    rows = load_performance(csv_path)
    if not rows or len(rows) < 3:
        return rng.choice(style_names)
    if rng.random() < explore_rate:
        return rng.choice(style_names)

    if "style_used" not in rows[0] or "avg_view_percentage" not in rows[0]:
        return rng.choice(style_names)

    # Mean retention per style, ignoring rows whose number does not parse
    # (the dropna() the groupby used to do). The first style wins a tie,
    # the same as idxmax() on the grouped frame.
    totals: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for row in rows:
        style = str(row.get("style_used") or "")
        if style not in style_names:
            continue
        try:
            score = float(row.get("avg_view_percentage"))
        except (TypeError, ValueError):
            continue
        totals[style] = totals.get(style, 0.0) + score
        counts[style] = counts.get(style, 0) + 1
    if not totals:
        return rng.choice(style_names)
    best = max(totals, key=lambda style: totals[style] / counts[style])
    return str(best)


def _narration_range(style_key: str, params=None) -> tuple:
    """Words the voice should aim for: the legacy counts, or this run's budget."""
    if params is None:
        if style_key == ASMR_STYLE_KEY:
            return ASMR_NARRATION_MIN_WORDS, ASMR_NARRATION_MAX_WORDS
        return NARRATION_MIN_WORDS, NARRATION_MAX_WORDS
    from improvement import narration_word_budget

    low, high = narration_word_budget(params)
    if style_key == ASMR_STYLE_KEY:
        # The ASMR style keeps the drawing sound in front, so it talks less.
        return max(int(round(low * 0.9)), 12), max(int(round(high * 0.9)), 16)
    return low, high


def build_script_prompt(style_key: str, config: dict, reference_scan: bool = False, params=None, motif: str = "") -> str:
    """The Gemini prompt for one Short; kept pure so it can be unit tested.

    ``motif`` is the English name of what is being drawn (see
    :func:`motif_english_name`); when it is given, the prompt tells Gemini to
    use exactly that name in the title, the first spoken line and the hook, so
    the viewer hears the topic in the first seconds instead of guessing it
    from the keyframes.
    """
    styles_cfg = config["evolution"]["styles"]
    style_desc = styles_cfg[style_key]["description"]
    shop_url = config["brand"]["shop_url"]
    material = config["brand"]["material_name"]
    theme = config["brand"].get("theme", "Happy everyday moments")
    word_min, word_max = _narration_range(style_key, params)
    asmr_note = ""
    if style_key == ASMR_STYLE_KEY:
        asmr_note = (
            f"\nThis is the ASMR style: the drawing sound stays in front, so speak "
            f"softly and calmly ({word_min}-{word_max} words). "
            f"Leave at most a one-second beat between sentences - never a long silence - "
            f"and keep every sentence short enough to say in one breath."
        )
    else:
        asmr_note = (
            f"\nKeep the voice moving for the whole stretch it is on: {word_min}-"
            f"{word_max} words, one sentence flowing into the next, with no "
            f"long pauses and no filler."
        )
    artist_notes = [
        str(note).strip()
        for note in (config["brand"].get("artist_notes") or [])
        if str(note).strip()
    ]
    artist_block = ""
    if artist_notes:
        lines = "\n".join(f"- {note}" for note in artist_notes)
        artist_block = (
            "\nArtist context (background for the voice; use only what fits the footage, "
            "and never invent a fact that is not here or visible in the frames):\n" + lines + "\n"
        )
    try:
        total_seconds = int(round(float(config["video"].get("target_duration_sec", 45))))
    except (TypeError, ValueError):
        total_seconds = 45
    if params is not None:
        try:
            total_seconds = int(round(float(params.get("target_duration_sec", total_seconds))))
        except (TypeError, ValueError):
            pass
    scan_note = ""
    if reference_scan:
        scan_note = (
            "\nThe first attached image is a high-resolution scan of the finished artwork; "
            "treat it as the definitive reference for the subject, colours and details, "
            "and let the time-lapse keyframes show how it was drawn."
        )

    timeline, language_block = _run_shape(total_seconds, params)
    motif_line = ""
    if str(motif or "").strip():
        motif_line = (
            f"\n- Motif: {str(motif).strip()} (the English name of what is being drawn - "
            f"use exactly this name in the title, the first spoken line and the hook text)."
        )
    return f"""
You are a creative director for the global art channel "CrayonJapan".
Analyze the attached making-of keyframes of a hand-drawn postcard artwork.{scan_note}

Context:
- Art Material: {material} (Japanese professional oil pastels)
- Artwork Type: One-of-a-kind original postcard painting
- Theme: {theme}
- Shop URL: {shop_url}{motif_line}
- Selected Narrative Style ({style_key}): {style_desc}
{artist_block}
The finished Short is about {total_seconds} seconds long. Plan the narration on this
timeline:
{timeline}

One idea per Short: this motif and nothing else - never a second subject, never a
digression.

Four things every Short has to do, woven into the narration itself (never read
out as a list, never a checklist):
1. Hook (0-3s): The first spoken line names the motif and the payoff - what is being
drawn and what the finished piece will look like - in the first seconds, in the same
words as the on-screen hook text. The first frame, the first line and the hook text
all point at the same payoff. No greeting, no warm-up, no logo.
2. Sensory Language: Use words like 'creamy', 'buttery', 'vibrant', 'crisp' to emphasize the material's quality.
3. Loop-friendly Ending: The last sentence echoes the first line's words, so the ending
naturally leads back to the beginning, or ends with a strong, satisfying punchline.
4. One true detail: Exactly one concrete, specific detail from the artist context
above - why this motif was chosen, or one memory attached to it (a place, a time, a
person, a taste). Say it once, in concrete words, never as a generic line ('happy
memories', 'a small everyday thing'). If no detail in the context fits this footage,
say nothing personal at all - never invent a memory, a place or a person. Depth is
one vivid detail, not a longer narration: the word budget below does not grow for it.
{language_block}
Generate a JSON response with the following keys:
1. "title": Search-phrase-first YouTube Shorts title: the motif and "oil pastel" up
front, the point inside the first 40 characters, under 65 characters in total. No
"#Shorts", no hashtags, no Japanese, no emoji.
2. "hook_text": The same payoff as the first spoken line, as on-screen hook text for
the first 2.5 seconds (max 7 words) - the first frame, the first line and this text
all point at the same payoff.
3. "narration": Natural, engaging voiceover script of {word_min}-{word_max} words (this Short is about {total_seconds} s), written to be spoken aloud: mostly sentences under 14 words. Open on the hook: name the motif and the payoff in the first sentence, so the first spoken words say the topic aloud. One idea only. Hold the reveal: describe the process without giving away the finished look until the end. Keep the sensory words coming, mention {material} naturally, say the one true detail (point 4) once in concrete words, and close so the ending leads back to the opening. Do not leave a silent stretch longer than a breath anywhere the voice is used.
4. "description": 1-2 plain sentences about the artwork that restate the topic in
different words from the title, then 3-5 hashtags (#SakuraCrayPas #OilPastelArt
#CrayPasSpecialist #OriginalPostcard #CrayonJapan). No "#Shorts". The pipeline adds the shop call
to action ({shop_url}) to this field itself, so write the story and the hashtags - do
not write the shop link or a "shop this piece" line.
5. "tags": List of 10 relevant tags, English only, no Japanese tags.
6. "title_ja": The same title in natural Japanese, keeping "#Shorts" and any emoji.
7. "hook_ja": The same on-screen hook in natural Japanese, short enough to read in 2.5 seconds.
8. "narration_ja": The whole narration translated into natural, warm Japanese so the artist can check it before posting - the same meaning and order as "narration", one Japanese sentence per English sentence, no emoji and no stage directions. It is a translation only: never add or drop a fact, and never write a different script.
9. "overseas_caption": One English sentence (max 18 words) about this artwork for a TikTok / Instagram Reels / Pinterest caption: name the subject and what the oil pastel looks like. No emoji, no hashtags, no shop link, no "#Shorts". Do not open with "Oil pastel" (the caption already does) and do not describe a person or a camera.
10. "overseas_caption_ja": The same sentence in natural Japanese, for the artist's check. A translation only: no emoji, no hashtags, no shop link.

Every Japanese field must translate the English field beside it, not re-imagine it.

Return strictly valid JSON without markdown fences.{asmr_note}
""".strip()


def _run_shape(total_seconds: int, params=None) -> tuple:
    """The timeline paragraph and the language instruction for this run.

    Without parameters the original 45-second shape is kept verbatim, so an
    older config renders exactly what it used to. With parameters the voice is
    given only the share the run planned for it (the rest belongs to the
    drawing sound), and the on-screen language follows ``language_mode``.
    """
    if params is None:
        timeline = (
            "- 0-3 s: the hook (point 1 below). No greeting, no warm-up.\n"
            "- 3-35 s: detailed description of the process, colour by colour, what the hand is\n"
            "doing right now, in words a viewer can almost feel. Keep talking: a new sentence\n"
            "starts before the previous one has faded, with no long pauses and no\n"
            "\"and then... and then\".\n"
            "- 35 s onward: one short, satisfying punchline about the finished piece, written so\n"
            "the ending naturally leads back to the opening hook (point 3 below), then the short\n"
            "call to action (this 1-of-1 original postcard is available at CrayonJapan, link in\n"
            "the description and bio). The call to action may carry the loop, but the punchline\n"
            "has to land first."
        )
        return timeline, ""

    from improvement import narrated_seconds

    spoken = narrated_seconds(params)
    speech_end = max(int(min(spoken, total_seconds - 2)), 6)
    punch_end = max(min(speech_end + 4, total_seconds - 1), speech_end + 1)
    tts_mode = str(params.get("tts_mode", "short"))
    voice_note = (
        "- The voice is off for this Short (drawing sound only). Write the narration as the\n"
        "  written story of the artwork for the description, short and vivid.\n"
        if tts_mode == "none"
        else (
            f"- 0-3 s: the hook (point 1 below): name the motif and the payoff in the first\n"
            f"sentence, so the topic is said aloud at once. No greeting, no warm-up.\n"
            f"- 3-{speech_end} s: the process, colour by colour, in words a viewer can almost\n"
            "feel. Nothing about the tools or the channel; only what is happening on the paper.\n"
            "Hold the reveal: describe the work without giving away the finished look, and\n"
            "say the one true detail (point 4) once, in concrete words, where it fits.\n"
            f"- {speech_end}-{punch_end} s: one satisfying punchline about the finished piece that\n"
            "echoes the opening line's words and leads back to it, then the short call to action\n"
            "(1-of-1 original, link in the description). After that the drawing sound and the\n"
            "closing question carry the ending, so stop talking: no long trailing sentence.\n"
        )
    )
    mode = str(params.get("language_mode", "bilingual"))
    if mode == "ja":
        language = (
            "\nOn-screen language: Japanese only. This Short is for a Japanese audience, so\n"
            "\"hook_ja\" is the line that actually appears on screen - write it as the real\n"
            "hook, not a translation afterthought - and \"title_ja\" is the title shown. Keep\n"
            "the motif name in the words a Japanese viewer would use. The English \"hook_text\"\n"
            "and \"title\" are still filled in for the upload metadata.\n"
        )
    elif mode == "bilingual":
        language = (
            "\nOn-screen language: Japanese with the English line under it. \"hook_ja\" is the\n"
            "main promise on screen (write it to stop a Japanese viewer's thumb, not as a\n"
            "literal translation), \"hook_text\" is the smaller English line beneath, and\n"
            "\"title_ja\" follows the title. Keep both short.\n"
        )
    else:
        language = "\nOn-screen language: English only. The Japanese fields are for the artist's check.\n"
    return "\n".join([voice_note]) if tts_mode == "none" else voice_note, language


def _text(value: object) -> str:
    """A stripped string, or '' for anything that is not one."""
    return value.strip() if isinstance(value, str) else ""


def adopt_script_key_aliases(data: dict) -> None:
    """Copy an accepted alias onto the canonical key when that key is empty."""
    for canonical, aliases in SCRIPT_KEY_ALIASES.items():
        if _text(data.get(canonical)):
            continue
        for alias in aliases:
            if _text(data.get(alias)):
                data[canonical] = data[alias]
                break


def translation_text(data: dict, key: str) -> str:
    """The Japanese field under any accepted spelling, or '' when absent."""
    for alias in TRANSLATION_KEY_ALIASES[key]:
        text = _text(data.get(alias))
        if text:
            return text
    return ""


def overseas_text(data: dict, key: str) -> str:
    """The overseas caption field under any accepted spelling, or ''."""
    for alias in OVERSEAS_KEY_ALIASES[key]:
        text = _text(data.get(alias))
        if text:
            return text
    return ""


TITLE_MAX_CHARS = 70
HOOK_MAX_WORDS = 7
_SHORTS_TOKEN = re.compile(r"#shorts\b", re.IGNORECASE)


def normalise_title(title: object) -> str:
    """The upload title: no ``#Shorts``, under 70 characters.

    Gemini is told not to write ``#Shorts`` (it adds nothing to reach), but an
    older or careless reply still gets cleaned here instead of on upload: the
    tag is dropped and a title past the 60-70 character sweet spot is cut on a
    word boundary, so the search phrase up front always survives.
    """
    text = _SHORTS_TOKEN.sub("", _text(title))
    text = re.sub(r"\s+", " ", text).strip(" -–—|")
    if len(text) > TITLE_MAX_CHARS:
        cut = text[:TITLE_MAX_CHARS]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" -–—|,;:")
    return text


def normalise_hook(hook_text: object) -> str:
    """The on-screen hook: at most seven words, so it fits the first 2.5 s."""
    words = _text(hook_text).split()
    return " ".join(words[:HOOK_MAX_WORDS])


def parse_script_json(raw_text: str) -> dict:
    """Parse Gemini's JSON reply even when it arrives inside markdown fences."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Gemini reply is not a JSON object")
    adopt_script_key_aliases(data)
    missing = [key for key in REQUIRED_SCRIPT_KEYS if key not in data]
    if missing:
        raise ValueError(f"Gemini reply is missing required keys: {', '.join(missing)}")

    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",")]
    data["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()][:10]
    for key in ("title", "hook_text", "narration", "description"):
        data[key] = str(data[key]).strip()
    # A reply that still carries #Shorts, or a title past the sweet spot, is
    # cleaned here: the pack and the upload only ever see the short form.
    data["title"] = normalise_title(data["title"])
    data["hook_text"] = normalise_hook(data["hook_text"])
    # A missing translation becomes an empty string rather than an error, so the
    # render and the English upload pack are never lost over it.
    for key in TRANSLATION_KEYS:
        data[key] = translation_text(data, key)
    # Same for the overseas caption: metadata_writer fills the block from the
    # hook or the title when the reply has no sentence of its own.
    for key in OVERSEAS_KEYS:
        data[key] = overseas_text(data, key)
    return data


def model_candidates(model_name: str = MODEL_NAME) -> List[str]:
    """The primary model first, then the fallbacks, without duplicates."""
    names: List[str] = []
    for name in (model_name,) + tuple(FALLBACK_MODELS):
        if name and name not in names:
            names.append(name)
    return names


def is_retryable_model_error(exc: BaseException) -> bool:
    """True when another model could succeed (retired, quota, overload)."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in RETRYABLE_STATUS_CODES:
        return True
    status = str(getattr(exc, "status", "") or "").upper()
    return status in {
        "NOT_FOUND",
        "RESOURCE_EXHAUSTED",
        "UNAVAILABLE",
        "INTERNAL",
        "DEADLINE_EXCEEDED",
    }


def create_client(api_key: str):
    """A Gemini client pinned to the API version the gemini-3 models live on."""
    from google import genai
    from google.genai import types

    http_options = types.HttpOptions(api_version=API_VERSION)
    return genai.Client(api_key=api_key, http_options=http_options)


# ---------------------------------------------------------------------------
# The offline script: a render that never depends on Gemini
# ---------------------------------------------------------------------------
# The artist stopped paying for Gemini, so a run may have no key at all, or the
# key may answer 403 / 429 / 5xx for the whole day. A Short still has to be made
# and published, so the script falls back to templates built from the config and
# the artwork's own name: honest sentences about the material and the process,
# nothing invented about a drawing nobody looked at.
SCRIPT_SOURCE_GEMINI = "gemini"
SCRIPT_SOURCE_TEMPLATE = "template"
# An artwork name that is only a date or a number says nothing about the motif.
_DATE_LIKE = re.compile(r"^(?:\d{6,8}|\d{4}[-_.]?\d{2}[-_.]?\d{2}|\d+)$")
_NAME_PREFIX = re.compile(r"^(?:GO_|go_)", re.S)


def motif_from_name(name: str) -> str:
    """A readable motif from an artwork name (``20261121_sushi`` -> ``Sushi``).

    The folder name is the only material the offline script has, so it is used
    as the subject when it carries one: the ``GO_`` signal and a date prefix are
    dropped, separators become spaces and a Japanese name is kept as it is.
    """
    text = os.path.splitext(os.path.basename(str(name or "").strip()))[0]
    text = _NAME_PREFIX.sub("", text)
    tokens = [token for token in re.split(r"[\s_.\-]+", text) if token]
    tokens = [token for token in tokens if not _DATE_LIKE.match(token)]
    motif = " ".join(tokens).strip(" .,")
    if len(motif) > 40:
        motif = motif[:40].rsplit(" ", 1)[0] or motif[:40]
    return motif


def _motif_title(motif: str) -> str:
    """The motif in the form a sentence uses (``sushi`` -> ``Sushi``)."""
    if not motif:
        return ""
    if all(ord(ch) < 128 for ch in motif):
        return motif[0].upper() + motif[1:]
    return motif


# ---------------------------------------------------------------------------
# The English name of the motif: the English voice and the English captions
# must never carry untranslated Japanese (GO_ポッキー -> "ポッキー" in the
# narration). The artist can pin an exact English name per motif in
# config.yaml under ``brand.motif_names`` (``ポッキー`` -> ``Pocky``); anything
# else in kana is romanised with this small table, and kanji - which no table
# can read - falls back to no English name, so the generic English lines are
# used and the Japanese name survives only in the *_ja check fields.
# ---------------------------------------------------------------------------
_KANA_ROMAJI: Dict[str, str] = {}
for _hiragana, _katakana, _sounds in (
    ("あいうえお", "アイウエオ", ("a", "i", "u", "e", "o")),
    ("かきくけこ", "カキクケコ", ("ka", "ki", "ku", "ke", "ko")),
    ("がぎぐげご", "ガギグゲゴ", ("ga", "gi", "gu", "ge", "go")),
    ("さしすせそ", "サシスセソ", ("sa", "shi", "su", "se", "so")),
    ("ざじずぜぞ", "ザジズゼゾ", ("za", "ji", "zu", "ze", "zo")),
    ("たちつてと", "タチツテト", ("ta", "chi", "tsu", "te", "to")),
    ("だぢづでど", "ダヂヅデド", ("da", "ji", "zu", "de", "do")),
    ("なにぬねの", "ナニヌネノ", ("na", "ni", "nu", "ne", "no")),
    ("はひふへほ", "ハヒフヘホ", ("ha", "hi", "fu", "he", "ho")),
    ("ばびぶべぼ", "バビブベボ", ("ba", "bi", "bu", "be", "bo")),
    ("ぱぴぷぺぽ", "パピプペポ", ("pa", "pi", "pu", "pe", "po")),
    ("まみむめも", "マミムメモ", ("ma", "mi", "mu", "me", "mo")),
    ("やゆよ", "ヤユヨ", ("ya", "yu", "yo")),
    ("らりるれろ", "ラリルレロ", ("ra", "ri", "ru", "re", "ro")),
    ("わを", "ワヲ", ("wa", "wo")),
):
    for _char, _sound in zip(_hiragana + _katakana, _sounds * 2):
        _KANA_ROMAJI[_char] = _sound
_KANA_ROMAJI["ん"] = "n"
_KANA_ROMAJI["ン"] = "n"
# Small vowels standing on their own (ファ -> "fa" comes through the yoon
# path below only for ゃゅょ; a lone small vowel just says its vowel).
for _char, _sound in zip("ぁぃぅぇぉァィゥェォ", ("a", "i", "u", "e", "o") * 2):
    _KANA_ROMAJI[_char] = _sound
_YOON_VOWEL = {"ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ャ": "ya", "ュ": "yu", "ョ": "yo"}
# Small vowels combine the same way (ファ -> "fa", シェ -> "she").
_SMALL_COMBINE = dict(
    _YOON_VOWEL,
    **{c: v for c, v in zip("ぁぃぅぇぉァィゥェォ", ("a", "i", "u", "e", "o") * 2)},
)
_SOKUON = {"っ", "ッ"}
_CHOONPU = "ー"
_VOWELS = "aiueo"
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _romanise_kana(text: str) -> str:
    """Hiragana/katakana as romaji; any other character is left as it is."""
    out: List[str] = []
    double_next = False
    index = 0
    while index < len(text):
        char = text[index]
        if char in _SOKUON:
            double_next = True
            index += 1
            continue
        if char == _CHOONPU:
            for piece in reversed(out):
                for letter in reversed(piece):
                    if letter in _VOWELS:
                        out.append(letter)
                        break
                else:
                    continue
                break
            index += 1
            continue
        sound = _KANA_ROMAJI.get(char)
        if sound is None:
            out.append(char)
            double_next = False
            index += 1
            continue
        # A small ゃ/ゅ/ょ (or ァ/ィ/ゥ/ェ/ォ) after a kana combines with it:
        # きゃ -> "kya", しゃ -> "sha" (the "y" gives way after sh/ch/j),
        # ファ -> "fa", シェ -> "she".
        if index + 1 < len(text) and text[index + 1] in _SMALL_COMBINE:
            base = sound.rstrip(_VOWELS)
            tail = _SMALL_COMBINE[text[index + 1]]
            if tail.startswith("y") and base in ("sh", "ch", "j"):
                sound = base + tail[1:]
            else:
                sound = base + tail
            index += 1
        if double_next:
            # Double the opening consonant: っき -> "kki", っち -> "cchi".
            head = sound[:2] if sound[:2] in ("ch", "sh", "ts") else sound[:1]
            if head not in _VOWELS:
                sound = head + sound
            double_next = False
        elif char in ("ん", "ン"):
            # ん before b/m/p is "m" (さんぽ -> "sampo"); otherwise "n".
            following = _KANA_ROMAJI.get(text[index + 1]) if index + 1 < len(text) else None
            sound = "m" if following and following[0] in "bmp" else "n"
        out.append(sound)
        index += 1
    return "".join(out)


def motif_english_name(motif: str, names: Optional[Mapping[str, str]] = None) -> str:
    """The motif in English, for the voice, the hook and the metadata.

    An exact entry in ``names`` (config ``brand.motif_names``) wins, so a brand
    name stays its real English name (``ポッキー`` -> ``Pocky``, not the plain
    romaji ``pokkii``). Otherwise kana is romanised; a name that still holds
    CJK after that (kanji, which no table can read) gives "" - the caller then
    uses the generic English lines, and the Japanese name is kept only in the
    ``*_ja`` fields. An all-ASCII motif is returned unchanged.
    """
    raw = str(motif or "").strip()
    if not raw:
        return ""
    if names:
        for key, value in dict(names).items():
            if str(key).strip() == raw and str(value or "").strip():
                return str(value).strip()
    if all(ord(ch) < 128 for ch in raw):
        return raw
    romanised = _romanise_kana(raw)
    if _CJK.search(romanised):
        return ""
    return romanised


# Sentence pairs for the offline narration: English and its Japanese
# translation, so the pack the artist checks keeps the same order as the voice.
# Every sentence is about the material or the process (or the motif the name
# gives) - nothing that would have needed a look at the drawing.
MATERIAL_SENTENCES = (
    (
        "The first layer of colour goes down like a breath on the paper.",
        "最初の色は、紙の上に息をのせるように置かれます。",
    ),
    (
        "{material} is soft enough to blend with a fingertip and firm enough to keep a line.",
        "{material}は指先で混ざるほど柔らかく、線を保てるほど芯があります。",
    ),
    (
        "The pigment settles creamy at first, then holds on to the grain of the sheet.",
        "絵の具はまずクリーミーにのり、それから紙の目にしっかり留まります。",
    ),
    (
        "A paler pastel lifted over the top lets the light back into the colour.",
        "上から淡い色を重ねると、色の中に光が戻ってきます。",
    ),
    (
        "The corner of the stick draws the edge, the flat side lays the colour down.",
        "パステルの角で輪郭を描き、腹で色を置いていきます。",
    ),
    (
        "Little by little the drawing stops being lines and becomes colour.",
        "少しずつ、線だったものが色になっていきます。",
    ),
    (
        "The time-lapse hides the minutes, but every layer is still there on the paper.",
        "早送りでは時間は見えませんが、重ねた層はすべて紙の上に残っています。",
    ),
)
STYLE_SENTENCES = {
    "style_A_material": (
        (
            "Buttery layers stack without turning muddy, and that is the whole point of this material.",
            "バターのような層は濁らずに重なり、それがこの画材の持ち味です。",
        ),
        (
            "The richer the layer, the more the finished surface catches the light.",
            "層が濃いほど、完成した面は光をよく受けとめます。",
        ),
    ),
    "style_B_story": (
        (
            "It started with a small, ordinary thing that most people walk past.",
            "はじまりは、多くの人が通り過ぎてしまう、小さな日常のかけらでした。",
        ),
        (
            "Colour is the way that small thing gets to be looked at properly.",
            "色を重ねることで、その小さなものが、ちゃんと見てもらえます。",
        ),
    ),
    "style_C_asmr": (
        (
            "You can hear the pastel catching on the paper, a dry and soft sound.",
            "紙の上をパステルがすべる、乾いた柔らかい音が聞こえます。",
        ),
        (
            "No music, no hurry, just the sound of a drawing being made.",
            "音楽もなく、急ぐこともなく、ただ描かれる音だけがあります。",
        ),
    ),
}
CLOSING_SENTENCES = (
    (
        "And it all starts again with a few oil pastel strokes.",
        "そして、また数本のオイルパステルの線から始まります。",
    ),
    (
        "And it begins again with a single quiet line.",
        "そしてまた、静かな一本の線から始まります。",
    ),
)
FALLBACK_TAGS = (
    "oil pastel",
    "sakura cray-pas",
    "crayon art",
    "oil pastel art",
    "art process",
    "timelapse art",
    "postcard art",
    "japanese art supplies",
    "satisfying art",
    "hand drawn",
)
# The description carries 3-5 hashtags, no #Shorts: the same five the Gemini
# prompt asks for, so the template and the model agree. #CrayonJapan is the
# channel tag, and the pack's "CrayonJapan" needle comes from it.
FALLBACK_HASHTAGS = (
    "#SakuraCrayPas",
    "#OilPastelArt",
    "#CrayPasSpecialist",
    "#OriginalPostcard",
    "#CrayonJapan",
)


def _format_pair(pair: tuple, motif: str, material: str) -> tuple:
    english, japanese = pair
    return (
        english.format(motif=motif, material=material),
        japanese.format(motif=motif, material=material),
    )


def _budgeted_sentences(pool: tuple, low: int, high: int, motif: str, material: str) -> List[tuple]:
    """Fill the narration to the planned word budget from the sentence pool."""
    chosen: List[tuple] = []
    words = 0
    index = 0
    while words < max(low, 1) and index < len(pool) * 4:
        english, japanese = _format_pair(pool[index % len(pool)], motif, material)
        index += 1
        count = len(english.split())
        if words + count > max(high, low) and words >= low:
            break
        chosen.append((english, japanese))
        words += count
    return chosen


def _word_count(sentences: Sequence[tuple]) -> int:
    return sum(len(english.split()) for english, _ in sentences)


def _compose_narration(
    pool: tuple,
    closing_pool: tuple,
    low: int,
    high: int,
    motif: str,
    material: str,
) -> List[tuple]:
    """Body sentences sized to the voice budget, with the closing line last.

    The closing line is booked first so the ending is always there inside the
    budget, then the body fills what is left; a sentence that would run past the
    planned share of the Short is left out instead of stretching the cut.
    """
    low = max(int(low), 8)
    high = max(int(high), low + 4)
    closing = [_format_pair(closing_pool[0], motif, material)]
    closing_words = _word_count(closing)
    body_low = max(low - closing_words, 6)
    body_high = max(high - closing_words, body_low + 4)
    body = _budgeted_sentences(pool, body_low, body_high, motif, material)
    sentences = body + closing
    # A last body sentence that overshot the budget is dropped again.
    while len(sentences) > 1 and _word_count(sentences) > high:
        sentences.pop(-2)
    return sentences


def fallback_script(
    style_key: str,
    config: dict,
    motif: str = "",
    params=None,
    reason: str = "",
) -> dict:
    """A complete script written without Gemini, from the config and the name.

    It fills every key the pipeline and the metadata pack read (title, hook,
    narration, description, tags and the four Japanese / overseas fields), so a
    run with no API key - or one whose key is out of quota - still compiles a
    telop, voices the narration, writes the pack and uploads the Short.
    """
    from improvement import narration_word_budget

    brand = (config or {}).get("brand") or {}
    material = str(brand.get("material_name") or "oil pastel").strip()
    series = str(brand.get("name") or "CrayonJapan").strip()
    motif = motif_from_name(motif)
    # The English voice, hook and metadata say the motif in English (a pinned
    # name from brand.motif_names, else romaji); the Japanese name lives only
    # in the *_ja fields. A name no table can read (kanji) gives no English
    # name, so the generic English lines are used instead of untranslated
    # Japanese in the voice.
    motif_en = motif_english_name(motif, names=brand.get("motif_names"))
    shown = _motif_title(motif_en)
    shown_ja = _motif_title(motif)
    low, high = _narration_range(style_key, params)

    if shown:
        # Search phrase first: the motif and "oil pastel" up front, no #Shorts.
        title = f"Drawing {shown} in Oil Pastel"
        title_ja = f"{shown_ja}をオイルパステルで描く #Shorts"
        hook_text = f"Watch this {motif_en} appear"
        hook_ja = f"{shown_ja}が色になっていく"
        opening = (
            f"This is how {shown} comes out of a few oil pastel strokes.",
            f"{shown_ja}が、いくつかの線から生まれます。",
        )
        overseas = f"{shown} drawn in creamy oil pastel, layer by layer"
        overseas_ja = f"クレヨンで描く{shown_ja}。色を重ねて、少しずつ形になっていきます。"
    else:
        title = "Drawing a tiny postcard in oil pastel"
        # The Japanese check copy can still name the motif even when the
        # English voice cannot say it (a kanji name has no romaji to use).
        title_ja = f"{shown_ja}をオイルパステルで描く #Shorts" if shown_ja else "小さなオイルパステル画 #Shorts"
        hook_text = "Watch it appear stroke by stroke"
        hook_ja = f"{shown_ja}が色になっていく" if shown_ja else "一筆ずつ、色が生まれる"
        opening = (
            "This is how a small oil pastel drawing is built, stroke by stroke.",
            "小さなオイルパステル画が、一筆ずつ出来ていきます。",
        )
        overseas = "A small original postcard, drawn in creamy oil pastel"
        overseas_ja = (
            f"クレヨンで描く{shown_ja}。色を重ねて、少しずつ形になっていきます。"
            if shown_ja
            else "小さなポストカードを、クレヨンで一筆ずつ描いています。"
        )

    # The opening, then the sentences of this run's style, then the material
    # ones: what the style is about has to survive the word budget.
    pool = (opening,) + STYLE_SENTENCES.get(style_key, ()) + MATERIAL_SENTENCES
    sentences = _compose_narration(
        pool, CLOSING_SENTENCES, low, high, motif_en or shown, material
    )
    narration = " ".join(english for english, _ in sentences)
    narration_ja = "".join(japanese for _, japanese in sentences)

    description_lines = [
        f"{shown or 'A small original'} - drawn by hand with {material}, built up layer by layer.",
        "Every artwork is a one-of-a-kind original postcard, drawn once and never repeated.",
        "",
        " ".join(FALLBACK_HASHTAGS),
    ]
    tags = list(FALLBACK_TAGS)
    if motif_en and motif_en.lower() not in {tag.lower() for tag in tags}:
        tags = [motif_en] + tags

    script = {
        "title": title[:100],
        "hook_text": hook_text,
        "narration": narration,
        "description": "\n".join(line for line in description_lines if line is not None).strip(),
        "tags": tags[:10],
        "title_ja": title_ja,
        "hook_ja": hook_ja,
        "narration_ja": narration_ja,
        "overseas_caption": overseas,
        "overseas_caption_ja": overseas_ja,
        "script_source": SCRIPT_SOURCE_TEMPLATE,
        "script_note": (reason or "written without Gemini")[:300],
    }
    return script


def write_script(
    keyframe_paths: Sequence[str],
    style_key: str,
    config: dict,
    reference_scan: bool = False,
    params=None,
    motif: str = "",
    log=print,
    generator=None,
) -> dict:
    """The script for this Short: Gemini's when it answers, the template otherwise.

    ``generator`` is the Gemini call (``generate_making_script`` by default), so
    the render keeps working when the key is missing, out of quota or rejected;
    the returned dict always carries every key the rest of the pipeline reads.
    """
    call = generator or generate_making_script
    try:
        script = call(
            keyframe_paths,
            style_key,
            config,
            reference_scan=reference_scan,
            params=params,
            motif=motif,
        )
    except Exception as exc:  # noqa: BLE001 - a Short must still be made
        log(
            f"[gemini] no script from Gemini ({exc}); "
            "using the offline template script from config.yaml and the artwork name"
        )
        return fallback_script(style_key, config, motif=motif, params=params, reason=str(exc))

    script = dict(script or {})
    script.setdefault("script_source", SCRIPT_SOURCE_GEMINI)
    return script


def generate_making_script(
    keyframe_paths: Sequence[str],
    style_key: str,
    config: dict,
    model_name: str = MODEL_NAME,
    reference_scan: bool = False,
    params=None,
    motif: str = "",
) -> dict:
    """Send the keyframes to Gemini and return the parsed script dict.

    ``motif`` is the artwork/folder name (``GO_ポッキー``); its English name
    goes into the prompt so Gemini can name the topic in the first line
    instead of guessing it from the frames.
    """
    from PIL import Image

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (add it to .env)")

    client = create_client(api_key)
    motif_en = motif_english_name(
        motif_from_name(motif),
        names=((config or {}).get("brand") or {}).get("motif_names"),
    )
    prompt = build_script_prompt(
        style_key, config, reference_scan=reference_scan, params=params, motif=motif_en
    )

    contents: List[object] = [prompt]
    for path in keyframe_paths:
        if os.path.exists(path):
            contents.append(Image.open(path))

    candidates = model_candidates(model_name)
    last_error: Optional[BaseException] = None
    for name in candidates:
        try:
            response = client.models.generate_content(model=name, contents=contents)
        except Exception as exc:  # noqa: BLE001 - classified below
            if not is_retryable_model_error(exc):
                raise
            print(f"[gemini] {name} unavailable ({exc}); trying the next model")
            last_error = exc
            continue
        print(f"[gemini] script written by {name}")
        return parse_script_json(getattr(response, "text", "") or "")

    raise RuntimeError(
        f"Gemini rejected every model in {', '.join(candidates)}: {last_error}"
    )
