"""English metadata bundle for one finished artwork.

Two files land next to the rendered Short so the same package can be dropped
into ``02_READY_TO_POST`` and reused for the BASE shop listing:

* ``{stem}_metadata.txt``       - paste-ready title / description / tags
* ``{stem}_art_metadata.json``  - the same data as structured art metadata

Keeping this separate from ``ai_writer.py`` means the Gemini reply stays the
only AI-dependent step; formatting the bundle is pure and unit tested.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

JST = timezone(timedelta(hours=9))

METADATA_TEXT_SUFFIX = "_metadata.txt"
ART_METADATA_SUFFIX = "_art_metadata.json"


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
        "description_en": _clean(script_data.get("description")),
        "narration_en": _clean(script_data.get("narration")),
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
        "=== HOOK TEXT (first 2.5s) ===",
        _clean(metadata.get("hook_text")),
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
