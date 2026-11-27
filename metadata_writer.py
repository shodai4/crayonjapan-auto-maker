"""English metadata bundle for one finished artwork.

Two files land next to the rendered Short so the same package can be dropped
into ``02_READY_TO_POST`` and reused for the BASE shop listing:

* ``{stem}_metadata.txt``       - paste-ready title / description / tags, plus
  the full English narration with its Japanese translation for the check, and
  a paste-ready TikTok / Instagram Reels / Pinterest caption block
* ``{stem}_art_metadata.json``  - the same data as structured art metadata

Keeping this separate from ``ai_writer.py`` means the Gemini reply stays the
only AI-dependent step; formatting the bundle is pure and unit tested. The
overseas caption block is built here, without an API call, so the keywords and
hashtags the artist asked for survive a Gemini outage or an offline run.
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
# The description carries 3-5 hashtags (the brief); a longer trailing block
# is trimmed to this many.
DESCRIPTION_MAX_HASHTAGS = 5
# Tags are English-only metadata for an overseas Short: a tag carrying CJK
# script (or an emoji) never reaches the upload.
_NON_LATIN_TAG = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u2600-\u27bf]")
_URL = re.compile(r"https?://\S+")


# ---------------------------------------------------------------------------
# Overseas SNS captions (TikTok / Instagram Reels / Pinterest)
# ---------------------------------------------------------------------------
# The overseas viewer searches in English for the material, not for the channel,
# so every caption opens with "Oil Pastel" and names "Sakura Cray-Pas" as the
# Japanese maker beside it. These four hashtags are the ones the artist asked to
# carry on every post; a platform's own extras are appended after them, never
# instead of them. The block is assembled purely from the script and the config,
# so a Gemini outage or an offline run still produces it.
OVERSEAS_MAIN_KEYWORD = "Oil Pastel"
OVERSEAS_MATERIAL_KEYWORD = "Sakura Cray-Pas"
OVERSEAS_REQUIRED_HASHTAGS = ("#oilpastel", "#sakuracraypas", "#crayonart", "#artprocess")
OVERSEAS_PLATFORMS = ("tiktok", "instagram", "pinterest")
OVERSEAS_PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
    "pinterest": "Pinterest",
}
OVERSEAS_PLATFORM_HASHTAGS = {
    "tiktok": ("#oilpastelart", "#arttok"),
    "instagram": ("#oilpastelart", "#crayonartwork", "#artreels", "#reelsart"),
    "pinterest": ("#oilpastelart", "#arttimelapse"),
}
# The subject is one short sentence; a long reply is cut on a word boundary so
# the caption stays readable on a phone.
OVERSEAS_SUBJECT_MAX_CHARS = 90
OVERSEAS_SUBJECT_FALLBACK = "A small original postcard, drawn by hand"

# A caption line is English: Japanese, emoji and decoration are dropped, while
# accented Latin letters (é, ü) stay like any other letter.
_NON_LATIN = re.compile(r"[^\x20-\x7E\u00C0-\u024F\u2010-\u2027\u2030-\u205E\u20AC]")
_SERIES_PREFIX = re.compile(r"^\s*CrayonJapan\s*[/|:\-]\s*", re.IGNORECASE)


def _material_keyword(material_name: object) -> str:
    """The material as the overseas captions name it (always Sakura Cray-Pas)."""
    text = _clean(material_name)
    if text and OVERSEAS_MATERIAL_KEYWORD.lower() in text.lower():
        return text
    return OVERSEAS_MATERIAL_KEYWORD


def _english_subject(text: object, max_chars: int = OVERSEAS_SUBJECT_MAX_CHARS) -> str:
    """One English caption line: no hashtags, links, emoji or series label."""
    line = _NON_LATIN.sub(" ", str(text or ""))
    line = re.sub(r"https?://\S+", " ", line)
    line = re.sub(r"#\S+", " ", line)
    line = line.replace("**", "")
    line = _SERIES_PREFIX.sub("", line)
    line = re.sub(r"\s+", " ", line).strip(" \"'“”‘’-–—|/,;:.")
    if len(line) > max_chars:
        cut = line[:max_chars]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        line = f"{cut.rstrip(' ,-|/')}..."
    return line


def _sentence(text: str) -> str:
    """The subject with one full stop, never a doubled one after ``!``."""
    stripped = (text or "").strip().rstrip(".,;:")
    if not stripped:
        return stripped
    return stripped if stripped[-1] in "!?…" else f"{stripped}."


def _hashtags_for(platform: str) -> List[str]:
    """The four fixed hashtags first, then the ones this platform uses."""
    return list(OVERSEAS_REQUIRED_HASHTAGS) + list(OVERSEAS_PLATFORM_HASHTAGS.get(platform, ()))


def _english_caption(platform: str, subject: str, shop_url: str, material: str) -> str:
    """One paste-ready English caption: body, then its hashtags."""
    opening = f"{OVERSEAS_MAIN_KEYWORD} on paper with {material}: {_sentence(subject)}"
    if platform == "tiktok":
        body = f"{opening}\nLayered until the colour glows."
    elif platform == "instagram":
        body = (
            f"{opening}\nOne creamy stroke at a time, layered until the colour glows.\n"
            "1-of-1 original postcard - link in bio."
        )
    else:
        body = (
            f"{opening}\nA making-of time-lapse of a 1-of-1 original postcard, "
            "drawn with Japanese oil pastels."
        )
        if shop_url:
            body = f"{body}\nOriginal artwork: {shop_url}"
    return f"{body}\n\n{' '.join(_hashtags_for(platform))}"


def _japanese_caption(platform: str, subject_ja: str, shop_url: str) -> str:
    """The same caption in Japanese, for the artist's check before posting."""
    lines = [
        f"紙にオイルパステル（{OVERSEAS_MAIN_KEYWORD}）。"
        f"日本製の{OVERSEAS_MATERIAL_KEYWORD}で色を重ねました。"
    ]
    # A reply without Japanese has no subject to translate, so the block is left
    # with the fixed Japanese lines instead of a marker mid-sentence.
    if subject_ja:
        lines.append(f"作品：{subject_ja}")
    if platform == "instagram":
        lines.append("一層ずつ重ねて、つやが出るまで。")
        lines.append("1点ものの原画はプロフィールのリンクから。")
    elif platform == "pinterest":
        lines.append("1点ものの原画の制作過程です。")
        if shop_url:
            lines.append(f"原画：{shop_url}")
    return "\n".join(lines)


def build_overseas_captions(
    script_data: dict,
    material_name: object = "",
    shop_url: object = "",
) -> Dict[str, object]:
    """The overseas SNS block: keywords, hashtags and one caption per platform.

    Gemini's own sentence (``overseas_caption``) is used when it sent one; the
    artist's hook, then the title, stands in when it did not, so the required
    keywords and hashtags are never missing from the pack. The Japanese subject
    comes from ``overseas_caption_ja`` / ``hook_ja`` / ``title_ja`` and is marked
    as missing rather than left blank when the reply carried no Japanese.
    """
    script = script_data or {}
    model_line = _clean(script.get("overseas_caption"))
    subject = _english_subject(
        model_line
        or _clean(script.get("hook_text"))
        or _clean(script.get("title"))
    ) or OVERSEAS_SUBJECT_FALLBACK
    subject_ja = (
        _clean(script.get("overseas_caption_ja"))
        or _clean(script.get("hook_ja"))
        or _clean(script.get("title_ja"))
    )
    material = _material_keyword(material_name)
    url = str(shop_url or "").strip()

    captions: Dict[str, object] = {
        "source": "gemini" if model_line else "fallback",
        "keywords": [OVERSEAS_MAIN_KEYWORD, OVERSEAS_MATERIAL_KEYWORD],
        "hashtags": [
            tag
            for tag in dict.fromkeys(
                tag for platform in OVERSEAS_PLATFORMS for tag in _hashtags_for(platform)
            )
        ],
        "subject": subject,
        "subject_ja": subject_ja,
    }
    for platform in OVERSEAS_PLATFORMS:
        captions[platform] = _english_caption(platform, subject, url, material)
        captions[f"{platform}_ja"] = _japanese_caption(platform, subject_ja, url)
    return captions


def _overseas_text(overseas: Dict[str, object]) -> List[str]:
    """The overseas block as pack lines: English first, then the check copy."""
    if not overseas:
        return []
    lines = [
        "=== OVERSEAS SNS CAPTIONS (TikTok / Instagram Reels / Pinterest) ===",
        f"KEYWORDS : {' / '.join(overseas.get('keywords') or [])}",
        f"HASHTAGS : {' '.join(overseas.get('hashtags') or [])}",
        "",
    ]
    for platform in OVERSEAS_PLATFORMS:
        lines += [
            f"--- {OVERSEAS_PLATFORM_LABELS[platform]} ---",
            _clean(overseas.get(platform)),
            "",
        ]
    lines.append("=== OVERSEAS SNS CAPTIONS (JA / 日本語訳) ===")
    for platform in OVERSEAS_PLATFORMS:
        lines += [
            f"--- {OVERSEAS_PLATFORM_LABELS[platform]}（日本語訳）---",
            _block(overseas.get(f"{platform}_ja")),
            "",
        ]
    lines.append("（投稿するハッシュタグと固有名詞は英語のままです）")
    lines.append("")
    return lines


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


def _normalise_hashtags(hashtags: str) -> str:
    """The trailing hashtag block: no ``#Shorts``, at most five, no repeats.

    The description asks for 3-5 hashtags; a reply that still carries
    ``#Shorts`` (it adds nothing to reach) or six tags gets trimmed here, so
    the upload description always matches the brief.
    """
    kept: List[str] = []
    for tag in (hashtags or "").split():
        if tag.lower() == "#shorts":
            continue
        if tag.startswith("#") and tag not in kept:
            kept.append(tag)
    return " ".join(kept[:DESCRIPTION_MAX_HASHTAGS])


def ensure_shop_cta(description: object, shop_url: object) -> str:
    """The description with the BASE shop link and the original-sale line.

    Applied to the Gemini reply before the pack and the upload are built, so
    both carry the same call to action. Idempotent: a description that already
    holds the block is returned untouched.
    """
    text = str(description or "").strip()
    url = str(shop_url or "").strip()
    # #Shorts adds nothing to reach, wherever it sits in the description.
    text = re.sub(r"#shorts\b", "", text, flags=re.IGNORECASE)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not url:
        return text
    english = SHOP_CTA_EN.format(url=url)
    japanese = SHOP_CTA_JA.format(url=url)
    if english in text and japanese in text:
        return text

    body, hashtags = _split_hashtags(text)
    body = _drop_shop_only_lines(body, url)
    block = f"{english}\n\n{japanese}"
    return "\n\n".join(
        part for part in (body, block, _normalise_hashtags(hashtags)) if part
    )


def _clean(value: object) -> str:
    return str(value or "").strip()


def _clean_tags(tags: object) -> List[str]:
    if isinstance(tags, str):
        tags = tags.split(",")
    cleaned: List[str] = []
    for tag in tags or []:
        text = _clean(tag)
        if text and not _NON_LATIN_TAG.search(text):
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
        # Paste-ready captions for TikTok / Instagram Reels / Pinterest, with the
        # keywords and hashtags the artist asked for guaranteed by the builder.
        "overseas_captions": build_overseas_captions(
            script_data, material_name=material_name, shop_url=shop_url
        ),
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
    lines += _overseas_text(metadata.get("overseas_captions") or {})
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
