"""Gemini-driven English script writing and narrative-style selection.

The style with the best average view percentage wins most of the time, with a
configurable exploration rate so new styles keep getting a chance.
"""

from __future__ import annotations

import json
import os
import random
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


def build_script_prompt(style_key: str, config: dict, reference_scan: bool = False) -> str:
    """The Gemini prompt for one Short; kept pure so it can be unit tested."""
    styles_cfg = config["evolution"]["styles"]
    style_desc = styles_cfg[style_key]["description"]
    shop_url = config["brand"]["shop_url"]
    material = config["brand"]["material_name"]
    theme = config["brand"].get("theme", "Happy everyday moments")
    asmr_note = ""
    if style_key == ASMR_STYLE_KEY:
        asmr_note = (
            f"\nThis is the ASMR style: the drawing sound stays in front, so speak "
            f"softly and calmly ({ASMR_NARRATION_MIN_WORDS}-{ASMR_NARRATION_MAX_WORDS} words). "
            f"Leave at most a one-second beat between sentences - never a long silence - "
            f"and keep every sentence short enough to say in one breath."
        )
    else:
        asmr_note = (
            f"\nKeep the voice moving for the whole Short: {NARRATION_MIN_WORDS}-"
            f"{NARRATION_MAX_WORDS} words, one sentence flowing into the next, with no "
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
    scan_note = ""
    if reference_scan:
        scan_note = (
            "\nThe first attached image is a high-resolution scan of the finished artwork; "
            "treat it as the definitive reference for the subject, colours and details, "
            "and let the time-lapse keyframes show how it was drawn."
        )

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
The finished Short is about {total_seconds} seconds long and the voiceover runs almost
the whole way through it. Plan the narration on this timeline:
- 0-3 s: the hook (point 1 below). No greeting, no warm-up.
- 3-35 s: detailed description of the process, colour by colour, what the hand is
doing right now, in words a viewer can almost feel. Keep talking: a new sentence
starts before the previous one has faded, with no long pauses and no
"and then... and then".
- 35 s onward: one short, satisfying punchline about the finished piece, written so
the ending naturally leads back to the opening hook (point 3 below), then the short
call to action (this 1-of-1 original postcard is available at CrayonJapan, link in
the description and bio). The call to action may carry the loop, but the punchline
has to land first.

Three things every Short has to do, woven into the narration itself (never read
out as a list, never a checklist):
1. Hook (0-3s): Start with a curiosity-gap sentence. (e.g., "The secret to realistic texture is..." or "You won't believe how this layer transforms...")
2. Sensory Language: Use words like 'creamy', 'buttery', 'vibrant', 'crisp' to emphasize the material's quality.
3. Loop-friendly Ending: The last sentence should naturally lead back to the beginning or end with a strong, satisfying punchline.

Generate a JSON response with the following keys:
1. "title": Catchy English YouTube Shorts title (under 65 chars, include #Shorts).
2. "hook_text": Short punchy on-screen hook text for the first 2.5 seconds (max 7 words).
3. "narration": Natural, engaging English voiceover script of {NARRATION_MIN_WORDS}-{NARRATION_MAX_WORDS} words (~40-45 seconds, this Short is about {total_seconds} s), written to be spoken aloud: mostly sentences under 14 words. Open on the curiosity-gap hook, keep the sensory words coming, mention {material} naturally, and close so the ending leads back to the opening or lands as a punchline, with the warm call-to-action in the final line. Do not leave a silent stretch longer than a breath anywhere in the Short.
4. "description": YouTube description with the artwork story, the material ({material}) and the hashtags (#SakuraCrayPas #CrayPasSpecialist #OilPastelArt #JapaneseArtSupplies #OriginalPostcard #CrayonJapan). The pipeline adds the shop call to action ({shop_url}) to this field itself, so write the story and the hashtags - do not write the shop link or a "shop this piece" line.
5. "tags": List of 10 relevant English tags.
6. "title_ja": The same title in natural Japanese, keeping "#Shorts" and any emoji.
7. "hook_ja": The same on-screen hook in natural Japanese, short enough to read in 2.5 seconds.
8. "narration_ja": The whole narration translated into natural, warm Japanese so the artist can check it before posting - the same meaning and order as "narration", one Japanese sentence per English sentence, no emoji and no stage directions. It is a translation only: never add or drop a fact, and never write a different script.
9. "overseas_caption": One English sentence (max 18 words) about this artwork for a TikTok / Instagram Reels / Pinterest caption: name the subject and what the oil pastel looks like. No emoji, no hashtags, no shop link, no "#Shorts". Do not open with "Oil pastel" (the caption already does) and do not describe a person or a camera.
10. "overseas_caption_ja": The same sentence in natural Japanese, for the artist's check. A translation only: no emoji, no hashtags, no shop link.

Every Japanese field must translate the English field beside it, not re-imagine it.

Return strictly valid JSON without markdown fences.{asmr_note}
""".strip()


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


def generate_making_script(
    keyframe_paths: Sequence[str],
    style_key: str,
    config: dict,
    model_name: str = MODEL_NAME,
    reference_scan: bool = False,
) -> dict:
    """Send the keyframes to Gemini and return the parsed script dict."""
    from PIL import Image

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (add it to .env)")

    client = create_client(api_key)
    prompt = build_script_prompt(style_key, config, reference_scan=reference_scan)

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
