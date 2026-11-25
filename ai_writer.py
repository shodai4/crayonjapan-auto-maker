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
import pandas as pd

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


def load_performance(csv_path: str) -> Optional[pd.DataFrame]:
    """Read the performance log, or None when it is missing/unreadable."""
    if not os.path.exists(csv_path):
        return None
    try:
        return pd.read_csv(csv_path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError, OSError):
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

    df = load_performance(csv_path)
    if df is None or df.empty or len(df) < 3:
        return rng.choice(style_names)
    if rng.random() < explore_rate:
        return rng.choice(style_names)

    if "style_used" not in df.columns or "avg_view_percentage" not in df.columns:
        return rng.choice(style_names)

    df = df[df["style_used"].isin(style_names)]
    scores = pd.to_numeric(df["avg_view_percentage"], errors="coerce")
    grouped = scores.groupby(df["style_used"]).mean().dropna()
    if grouped.empty:
        return rng.choice(style_names)
    return str(grouped.idxmax())


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


def build_script_prompt(style_key: str, config: dict, reference_scan: bool = False, params=None) -> str:
    """The Gemini prompt for one Short; kept pure so it can be unit tested."""
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
    return f"""
You are a creative director for the global art channel "CrayonJapan".
Analyze the attached making-of keyframes of a hand-drawn postcard artwork.{scan_note}

Context:
- Art Material: {material} (Japanese professional oil pastels)
- Artwork Type: One-of-a-kind original postcard painting
- Theme: {theme}
- Shop URL: {shop_url}
- Selected Narrative Style ({style_key}): {style_desc}
{artist_block}
The finished Short is about {total_seconds} seconds long. Plan the narration on this
timeline:
{timeline}

Three things every Short has to do, woven into the narration itself (never read
out as a list, never a checklist):
1. Hook (0-3s): Start with a curiosity-gap sentence. (e.g., "The secret to realistic texture is..." or "You won't believe how this layer transforms...")
2. Sensory Language: Use words like 'creamy', 'buttery', 'vibrant', 'crisp' to emphasize the material's quality.
3. Loop-friendly Ending: The last sentence should naturally lead back to the beginning or end with a strong, satisfying punchline.
{language_block}
Generate a JSON response with the following keys:
1. "title": Catchy YouTube Shorts title (under 65 chars, include #Shorts).
2. "hook_text": Short punchy on-screen hook text for the first 2.5 seconds (max 7 words).
3. "narration": Natural, engaging voiceover script of {word_min}-{word_max} words (this Short is about {total_seconds} s), written to be spoken aloud: mostly sentences under 14 words. Open on the curiosity-gap hook, keep the sensory words coming, mention {material} naturally, and close so the ending leads back to the opening or lands as a punchline. Do not leave a silent stretch longer than a breath anywhere the voice is used.
4. "description": YouTube description with the artwork story, the material ({material}) and the hashtags (#SakuraCrayPas #CrayPasSpecialist #OilPastelArt #JapaneseArtSupplies #OriginalPostcard #CrayonJapan). The pipeline adds the shop call to action ({shop_url}) to this field itself, so write the story and the hashtags - do not write the shop link or a "shop this piece" line.
5. "tags": List of 10 relevant tags (English names are fine; add 1-2 Japanese ones).
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
            f"- 0-3 s: the hook (point 1 below). No greeting, no warm-up.\n"
            f"- 3-{speech_end} s: the process, colour by colour, in words a viewer can almost\n"
            "feel. Nothing about the tools or the channel; only what is happening on the paper.\n"
            f"- {speech_end}-{punch_end} s: one satisfying punchline about the finished piece that\n"
            "leads back to the opening, then the short call to action (1-of-1 original, link in\n"
            "the description). After that the drawing sound and the closing question carry the\n"
            "ending, so stop talking: no long trailing sentence.\n"
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
        "One original postcard, drawn by hand and never repeated.",
        "世界に1枚だけのポストカードを、手描きで。",
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
    "手描き",
)
FALLBACK_HASHTAGS = (
    "#SakuraCrayPas",
    "#CrayPasSpecialist",
    "#OilPastelArt",
    "#JapaneseArtSupplies",
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
    shown = _motif_title(motif)
    low, high = _narration_range(style_key, params)

    if shown:
        title = f"{shown} in oil pastel #Shorts"
        title_ja = f"{shown}をオイルパステルで描く #Shorts"
        hook_text = f"Watch this {motif} appear"
        hook_ja = f"{shown}が色になっていく"
        opening = (
            f"This is how {shown} comes out of a few oil pastel strokes.",
            f"{shown}が、いくつかの線から生まれます。",
        )
        overseas = f"{shown} drawn in creamy oil pastel, layer by layer"
        overseas_ja = f"クレヨンで描く{shown}。色を重ねて、少しずつ形になっていきます。"
    else:
        title = "A tiny oil pastel drawing #Shorts"
        title_ja = "小さなオイルパステル画 #Shorts"
        hook_text = "Watch it appear stroke by stroke"
        hook_ja = "一筆ずつ、色が生まれる"
        opening = (
            "This is how a small oil pastel drawing is built, stroke by stroke.",
            "小さなオイルパステル画が、一筆ずつ出来ていきます。",
        )
        overseas = "A small original postcard, drawn in creamy oil pastel"
        overseas_ja = "小さなポストカードを、クレヨンで一筆ずつ描いています。"

    # The opening, then the sentences of this run's style, then the material
    # ones: what the style is about has to survive the word budget.
    pool = (opening,) + STYLE_SENTENCES.get(style_key, ()) + MATERIAL_SENTENCES
    sentences = _compose_narration(
        pool, CLOSING_SENTENCES, low, high, motif or shown, material
    )
    narration = " ".join(english for english, _ in sentences)
    narration_ja = "".join(japanese for _, japanese in sentences)

    description_lines = [
        f"{shown or 'A small original'} - drawn by hand with {material}.",
        "" if not shown else f"The {motif} is built up layer by layer: soft pigment first, then the details.",
        "Every artwork is a one-of-a-kind original postcard, drawn once and never repeated.",
        "",
        " ".join(FALLBACK_HASHTAGS),
    ]
    tags = list(FALLBACK_TAGS)
    if motif and motif.lower() not in {tag.lower() for tag in tags}:
        tags = [motif] + tags

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
) -> dict:
    """Send the keyframes to Gemini and return the parsed script dict."""
    from PIL import Image

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (add it to .env)")

    client = create_client(api_key)
    prompt = build_script_prompt(style_key, config, reference_scan=reference_scan, params=params)

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
