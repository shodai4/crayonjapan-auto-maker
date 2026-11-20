"""Gemini-driven English script writing and narrative-style selection.

The style with the best average view percentage wins most of the time, with a
configurable exploration rate so new styles keep getting a chance.
"""

from __future__ import annotations

import json
import os
import random
from typing import Dict, List, Optional, Sequence

import pandas as pd

MODEL_NAME = "gemini-3-flash-preview"
REQUIRED_SCRIPT_KEYS = ("title", "hook_text", "narration", "description", "tags")
ASMR_STYLE_KEY = "style_C_asmr"
ASMR_NARRATION_MAX_WORDS = 25


def load_performance(csv_path: str) -> Optional[pd.DataFrame]:
    """Read the performance log, or None when it is missing/unreadable."""
    if not os.path.exists(csv_path):
        return None
    try:
        return pd.read_csv(csv_path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError, OSError):
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
            f"\nThis is the ASMR style: the drawing sound carries the video, so keep the "
            f"narration ultra-minimal (max {ASMR_NARRATION_MAX_WORDS} words) and let the "
            f"pastel strokes breathe."
        )
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

Generate a JSON response with the following keys:
1. "title": Catchy English YouTube Shorts title (under 65 chars, include #Shorts).
2. "hook_text": Short punchy on-screen hook text for the first 2.5 seconds (max 7 words).
3. "narration": Natural, engaging English voiceover script (~65-85 words for a 35-40s video). Mention {material} naturally and end with a warm call-to-action that this 1-of-1 original postcard is available at CrayonJapan (link in description/bio). If style is style_C_asmr, keep narration ultra-minimal (max 25 words).
4. "description": YouTube description including artwork story, material ({material}), shop link ({shop_url}), and hashtags (#SakuraCrayPas #CrayPasSpecialist #OilPastelArt #JapaneseArtSupplies #OriginalPostcard #CrayonJapan).
5. "tags": List of 10 relevant English tags.

Return strictly valid JSON without markdown fences.{asmr_note}
""".strip()


def parse_script_json(raw_text: str) -> dict:
    """Parse Gemini's JSON reply even when it arrives inside markdown fences."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Gemini reply is not a JSON object")
    missing = [key for key in REQUIRED_SCRIPT_KEYS if key not in data]
    if missing:
        raise ValueError(f"Gemini reply is missing required keys: {', '.join(missing)}")

    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",")]
    data["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()][:10]
    for key in ("title", "hook_text", "narration", "description"):
        data[key] = str(data[key]).strip()
    return data


def generate_making_script(
    keyframe_paths: Sequence[str],
    style_key: str,
    config: dict,
    model_name: str = MODEL_NAME,
    reference_scan: bool = False,
) -> dict:
    """Send the keyframes to Gemini and return the parsed script dict."""
    from google import genai
    from PIL import Image

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (add it to .env)")

    client = genai.Client(api_key=api_key)
    prompt = build_script_prompt(style_key, config, reference_scan=reference_scan)

    contents: List[object] = [prompt]
    for path in keyframe_paths:
        if os.path.exists(path):
            contents.append(Image.open(path))

    response = client.models.generate_content(model=model_name, contents=contents)
    return parse_script_json(getattr(response, "text", "") or "")
