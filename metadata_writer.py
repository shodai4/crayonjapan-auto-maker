"""English metadata bundle for one finished artwork.

Two files land next to the rendered Short so the same package can be dropped
into ``02_READY_TO_POST`` and reused for the BASE shop listing:

* ``{stem}_metadata.txt``       - paste-ready title / description / tags, plus
  the full English narration with its Japanese translation for the check
* ``{stem}_art_metadata.json``  - the same data as structured art metadata

Keeping this separate from ``ai_writer.py`` means the Gemini reply stays the
only AI-dependent step; formatting the bundle is pure and unit tested.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

JST = timezone(timedelta(hours=9))

METADATA_TEXT_SUFFIX = "_metadata.txt"
ART_METADATA_SUFFIX = "_art_metadata.json"
# Shown inside the pack when Gemini sent no Japanese for a field, so a missing
# translation is visible instead of silently blank.
TRANSLATION_MISSING = "（日本語訳なし）"

# The shop line is not left to the model. Every description ends with the same
# two-language call to action, before the hashtags, so the link to the BASE
# shop - where the one-of-a-kind original is sold - cannot go missing from a
# Short and reads the same on every upload.
SHOP_CTA_EN = "Original artwork available at:\n{url}"
SHOP_CTA_JA = "1点ものの原画はこちらで販売中です：\n{url}"
# A line holding the shop link is a shop line (and is replaced by the block
# above) when what is left of it after the URL is this short.
SHOP_LINE_MAX_REMAINDER = 45
_URL = re.compile(r"https?://\S+")


def _url_needle(url: str) -> str:
    """The URL without its scheme or trailing slash, for a forgiving lookup."""
    return re.sub(r"^https?://", "", str(url or "").strip()).rstrip("/")


def _line_has_url(line: str, url: str) -> bool:
    needle = _url_needle(url)
    return bool(needle) and needle.lower() in re.sub(r"https?://", "", line).lower()


def _split_hashtags(text: str):
    """The description body and the trailing hashtag block."""
    lines = text.splitlines()
    index = len(lines)
    while index > 0 and lines[index - 1].lstrip().startswith("#"):
        index -= 1
    return "\n".join(lines[:index]).strip(), "\n".join(lines[index:]).strip()


def _drop_shop_only_lines(text: str, url: str) -> str:
    """Remove a line that only said "shop: <url>" - the block says it better."""
    kept: List[str] = []
    for line in text.splitlines():
        if _line_has_url(line, url):
            remainder = _URL.sub("", line).strip(" :：-–—|•·\t")
            if len(remainder) <= SHOP_LINE_MAX_REMAINDER:
                continue
        kept.append(line)
    return "\n".join(kept).strip()


def ensure_shop_cta(description: object, shop_url: object) -> str:
    """The description with the BASE shop link and the original-sale line.

    Applied to the Gemini reply before the pack and the upload are built, so
    both carry the same call to action. Idempotent: a description that already
    holds the block is returned untouched.
    """
    text = str(description or "").strip()
    url = str(shop_url or "").strip()
    if not url:
        return text
    english = SHOP_CTA_EN.format(url=url)
    japanese = SHOP_CTA_JA.format(url=url)
    if english in text and japanese in text:
        return text

    body, hashtags = _split_hashtags(text)
    body = _drop_shop_only_lines(body, url)
    block = f"{english}\n\n{japanese}"
    return "\n\n".join(part for part in (body, block, hashtags) if part)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _clean_tags(tags: object) -> List[str]:
    if isinstance(tags, str):
        tags = tags.split(",")
    cleaned: List[str] = []
    for tag in tags or []:
        text = _clean(tag)
        if text:
            cleaned.append(text)
    return cleaned


def _block(value: object) -> str:
    """A translated block, or a clear note that the reply had no Japanese."""
    return _clean(value) or TRANSLATION_MISSING


def build_art_metadata(
    script_data: dict,
    base_name: str,
    artwork_path: str = "",
    video_path: str = "",
    style_key: str = "",
    material_name: str = "",
    shop_url: str = "",
    generated_at: Optional[str] = None,
) -> Dict[str, object]:
    """Structure the Gemini script as art metadata for the upload + shop."""
    return {
        "artwork_id": _clean(base_name),
        "artwork_title": _clean(script_data.get("title")),
        "hook_text": _clean(script_data.get("hook_text")),
        "description_en": ensure_shop_cta(script_data.get("description"), shop_url),
        "narration_en": _clean(script_data.get("narration")),
        # Japanese translations, for the artist's check before posting.
        "artwork_title_ja": _clean(script_data.get("title_ja")),
        "hook_text_ja": _clean(script_data.get("hook_ja")),
        "narration_ja": _clean(script_data.get("narration_ja")),
        "tags": _clean_tags(script_data.get("tags")),
        "material": _clean(material_name),
        "style": _clean(style_key),
        "shop_url": _clean(shop_url),
        "artwork_image": os.path.basename(artwork_path) if artwork_path else "",
        "short_video": os.path.basename(video_path) if video_path else "",
        "generated_at": generated_at or datetime.now(JST).replace(microsecond=0).isoformat(),
    }


def format_metadata_text(metadata: Dict[str, object]) -> str:
    """Plain-text upload pack: everything needed to post, in one screen."""
    tags = ", ".join(metadata.get("tags") or [])
    lines = [
        "CRAYONJAPAN - UPLOAD PACK",
        "",
        f"ARTWORK ID   : {_clean(metadata.get('artwork_id'))}",
        f"ARTWORK IMAGE: {_clean(metadata.get('artwork_image'))}",
        f"SHORT VIDEO  : {_clean(metadata.get('short_video'))}",
        f"MATERIAL     : {_clean(metadata.get('material'))}",
        f"STYLE        : {_clean(metadata.get('style'))}",
        f"GENERATED    : {_clean(metadata.get('generated_at'))}",
        "",
        "=== TITLE ===",
        _clean(metadata.get("artwork_title")),
        "",
        "=== TITLE (JA) ===",
        _block(metadata.get("artwork_title_ja")),
        "",
        "=== HOOK TEXT (first 2.5s) ===",
        _clean(metadata.get("hook_text")),
        "",
        "=== HOOK TEXT (JA) ===",
        _block(metadata.get("hook_text_ja")),
        "",
        "=== NARRATION (EN) ===",
        _clean(metadata.get("narration_en")),
        "",
        "=== NARRATION (JA) ===",
        _block(metadata.get("narration_ja")),
        "",
        "=== DESCRIPTION ===",
        _clean(metadata.get("description_en")),
        "",
        "=== TAGS ===",
        tags,
        "",
        "=== SHOP ===",
        _clean(metadata.get("shop_url")),
        "",
    ]
    return "\n".join(lines)


def write_metadata_files(
    script_data: dict,
    output_dir: str,
    base_name: str,
    artwork_path: str = "",
    video_path: str = "",
    style_key: str = "",
    material_name: str = "",
    shop_url: str = "",
    generated_at: Optional[str] = None,
) -> Dict[str, str]:
    """Write the .txt and .json bundle and return their paths."""
    metadata = build_art_metadata(
        script_data,
        base_name=base_name,
        artwork_path=artwork_path,
        video_path=video_path,
        style_key=style_key,
        material_name=material_name,
        shop_url=shop_url,
        generated_at=generated_at,
    )
    os.makedirs(output_dir, exist_ok=True)
    text_path = os.path.join(output_dir, f"{base_name}{METADATA_TEXT_SUFFIX}")
    json_path = os.path.join(output_dir, f"{base_name}{ART_METADATA_SUFFIX}")

    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(format_metadata_text(metadata))
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    return {"text": text_path, "json": json_path}
