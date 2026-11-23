"""On-screen telop (caption) design for the CrayonJapan Shorts.

The pipeline used to burn word-level SRT cues with one fixed style. This module
turns the same word timings into an ASS script built around what the artist
asked for, using the caption techniques he picked out of the 100-technique
sheet:

* reading comfort - at most two lines per card, one message per card, breaks on
  word boundaries, no trailing full stop or comma (1/4 of the sheet);
* placement - a band that stays clear of the Shorts UI (right-hand buttons,
  bottom title/description, top bar) and off the middle of the drawing, a heavy
  sans font with a black outline and a drop shadow, and gold on the words that
  carry the meaning (2/4);
* motion and role - a word-by-word karaoke highlight, a hook that pops in, a
  quiet fixed title at the top, and a closing follow line over the outro (3/4
  and 4/4).

Everything here is pure: the functions take word timings and text, and return
an ASS document (or plain data). Only ``write_telop_ass`` touches the disk, so
the layout rules can be unit tested without FFmpeg.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Canvas of every Short the pipeline renders (config.yaml video.width/height).
FRAME_WIDTH = 1080
FRAME_HEIGHT = 1920

# Shorts UI overlays, measured on a 1080x1920 Short. They are the reason the
# captions are not simply centred: the like/comment/share column owns the right
# edge, the title and description own the bottom, and the top bar (search,
# "Shorts") owns the very top.
TOP_SAFE = 160
BOTTOM_SAFE = 380
RIGHT_SAFE = 140

# Captions keep this much room on both sides, so even the right-hand side stays
# clear of the action buttons and the text never touches the frame edge.
SIDE_MARGIN = 170

# A heavy, always-present sans: fonts-dejavu-core is installed by the workflow
# (DejaVu Sans Bold), Liberation Sans Bold ships with the Ubuntu runner image,
# and both render the same Latin text without falling back to a thin face.
FONT_CANDIDATES = ("DejaVu Sans", "Liberation Sans", "Noto Sans", "FreeSans")
DEFAULT_FONT = "DejaVu Sans"

# Caption geometry. Lines are measured with the drawn font and also capped at
# 16 characters, which keeps a 72 px DejaVu Sans Bold line inside the 740 px
# left between the side margins.
SUB_FONT_SIZE = 72
SUB_MAX_CHARS = 16
SUB_MAX_LINES = 2
SUB_OUTLINE = 6
SUB_SHADOW = 3

# Hook: bigger, two lines of its own, directly under the top bar.
HOOK_FONT_SIZE = 96
HOOK_MAX_CHARS = 16
HOOK_OUTLINE = 9
HOOK_SHADOW = 5
HOOK_MARGIN_V = TOP_SAFE + 140
HOOK_SECONDS = 3.0

# Fixed title: deliberately small and slightly transparent - it is a label, not
# a message. 40 px keeps a 34-character title inside the frame.
TITLE_FONT_SIZE = 40
TITLE_MAX_CHARS = 34
TITLE_MARGIN_V = TOP_SAFE + 20
TITLE_OUTLINE = 4
TITLE_SHADOW = 2

# Closing follow line: shown over the silent zoom outro, in the same upper band
# as the hook (the two never share a moment), so the drawing stays visible.
CTA_FONT_SIZE = 64
CTA_MAX_CHARS = 16
CTA_MARGIN_V = TOP_SAFE + 240
CTA_OUTLINE = 7
CTA_SHADOW = 4
CTA_SECONDS = 3.6
DEFAULT_CTA_TEXT = "Follow for more oil pastel art!"

# Colours are ASS &HBBGGRR; gold RGB(255, 200, 20).
ACCENT_COLOUR = "&H14C8FF&"
WHITE_COLOUR = "&H00FFFFFF&"

# A card shorter than this is merged into a neighbour when it still fits in two
# lines, so a single fast word never flashes by (technique 22: 1 s minimum).
MIN_CARD_SECONDS = 0.85
# A card full of gold stops emphasising anything, so only the first two
# meaningful words of a card get the accent (techniques 29 and 36).
MAX_EMPHASIS_WORDS = 2
# A card ends here after its last word when the next one starts after a pause.
CARD_TAIL_SECONDS = 0.28
# ...and holds on until the next card starts when the narration runs on.
CARD_GAP_SECONDS = 0.35

# Words whose last character ends a sentence: the card is closed there, which is
# the "one message per card" rule (technique 12) without hard-cutting a phrase.
SENTENCE_END = ".!?…"
# Punctuation that carries no meaning once spoken: dropped at the end of a card
# (techniques 13 and 14). "!" and "?" stay - they carry the tone.
SILENT_END_PUNCTUATION = ".,;:、。・"

# Automatic emphasis (technique 29). Gemini may also mark words itself with
# **word**, which is honoured first; these cover the rest.
KEYWORDS = frozenset(
    """
    pastel pastels crayon crayons cray-pas pigment pigments wax oil paper layer
    layers layering blend blends blended blending texture textures stroke strokes
    edge edges line lines sketch sketches canvas grain tooth highlight highlights
    shadow shadows palette colour colors colouring red blue green yellow gold
    golden orange pink violet purple brown black white cream ochre scarlet emerald
    turquoise amber ruby ivory silver copper peach coral mint beige
    secret wow whoa amazing incredible beautiful perfect vibrant buttery creamy
    crisp smooth rich bright soft bold instant satisfying magic
    first finally
    """.split()
)

_BRACES = str.maketrans({"\\": "/", "{": "(", "}": ")"})
_EMPHASIS_MARKER = re.compile(r"\*\*(.+?)\*\*")


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def clean_word(text: object) -> str:
    """One word as ASS-safe display text (no braces, no backslashes)."""
    if not isinstance(text, str):
        return ""
    return text.translate(_BRACES).strip()


def strip_emphasis_markers(text: str) -> Tuple[str, List[str]]:
    """Remove ``**word**`` markers from the narration, returning the words.

    The narration is spoken by edge-tts, which would otherwise read the
    asterisks, so the markers are taken out before synthesis; the words they
    marked stay emphasised on screen.
    """
    marked = [clean_word(match.group(1)) for match in _EMPHASIS_MARKER.finditer(text or "")]
    return _EMPHASIS_MARKER.sub(r"\1", text or ""), [word for word in marked if word]


def is_emphasis_word(word: str, marked: Iterable[str] = ()) -> bool:
    """True when a word deserves the accent colour.

    Marked words win; otherwise the automatic rules apply: anything with a
    digit (technique 16) and the material, colour and texture vocabulary above.
    A keyword also matches its own stems, so "layering" and "golden" count.
    """
    text = clean_word(word).strip("\"'()[]!?.,;:")
    if not text:
        return False
    lowered = text.lower()
    if lowered in {clean_word(item).lower() for item in marked if item}:
        return True
    if any(character.isdigit() for character in text):
        return True
    if lowered in KEYWORDS:
        return True
    # "layering", "blended", "golden" - the stem carries the keyword.
    for keyword in KEYWORDS:
        if len(keyword) >= 5 and lowered.startswith(keyword):
            return True
    return False


def trim_card_end(text: str) -> str:
    """Drop the spoken-away punctuation at the end of a card."""
    trimmed = text.rstrip()
    while trimmed and trimmed[-1] in SILENT_END_PUNCTUATION:
        trimmed = trimmed[:-1].rstrip()
    return trimmed or text.strip()


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip().rstrip("\"'”’)]")
    return bool(stripped) and stripped[-1] in SENTENCE_END


def _strip_emoji(text: str) -> str:
    """Keep only characters a Latin sans font can draw (no tofu boxes)."""
    kept = "".join(ch for ch in text if 32 <= ord(ch) < 127)
    return re.sub(r"\s+", " ", kept).strip()


def clean_title(title: str, series_label: str = "", max_chars: int = TITLE_MAX_CHARS) -> str:
    """The fixed title line: series label plus title, minus hashtags and emoji.

    A title that does not fit is cut on a word boundary, never mid-word.
    """
    text = _strip_emoji(title or "")
    text = re.sub(r"#\S+", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -|:,")
    series = _strip_emoji(series_label or "")
    if series and text and not text.lower().startswith(series.lower()):
        text = f"{series} / {text}"
    elif series and not text:
        text = series
    if len(text) > max_chars:
        cut = text[: max(max_chars - 3, 1)]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = f"{cut.rstrip(' ,-|/')}..."
    return text


def clean_hook(hook_text: str) -> str:
    """A short caption line, minus emoji, quotes and a trailing full stop."""
    text = _strip_emoji(hook_text or "").strip("\"' ")
    return trim_card_end(text)


# The hook and the closing line are cleaned the same way.
clean_caption_text = clean_hook


# ---------------------------------------------------------------------------
# Word timings out of edge-tts boundaries
# ---------------------------------------------------------------------------

def _ticks_to_seconds(value: object) -> float:
    """edge-tts reports 100-nanosecond ticks."""
    try:
        return max(float(value), 0.0) / 10_000_000.0
    except (TypeError, ValueError):
        return 0.0


def word_timings_from_boundaries(entries: Iterable[dict]) -> List[dict]:
    """Timed words from the WordBoundary/SentenceBoundary chunks edge-tts sent.

    A WordBoundary is one word. A SentenceBoundary (what older servers send for
    some voices) is split into its words and the sentence's span is shared out
    by length, so the karaoke still lines up roughly with the voice instead of
    being dropped.
    """
    words: List[dict] = []
    for entry in entries or []:
        kind = str(entry.get("type", ""))
        if "Boundary" not in kind:
            continue
        start = _ticks_to_seconds(entry.get("offset"))
        duration = _ticks_to_seconds(entry.get("duration"))
        text = clean_word(entry.get("text"))
        if not text:
            continue
        if kind.endswith("WordBoundary"):
            words.append({"text": text, "start": start, "end": start + max(duration, 0.06)})
            continue

        pieces = [piece for piece in text.split() if piece]
        if not pieces:
            continue
        weights = [len(piece) + 1 for piece in pieces]
        total = float(sum(weights))
        cursor = start
        for piece, weight in zip(pieces, weights):
            span = duration * (weight / total)
            words.append({"text": piece, "start": cursor, "end": cursor + span})
            cursor += span

    words.sort(key=lambda word: word["start"])
    return _tidy_timings(words)


def _tidy_timings(words: Sequence[dict]) -> List[dict]:
    """Drop overlaps and zero-length words so no cue is emitted backwards."""
    tidied: List[dict] = []
    for word in words:
        start = max(float(word["start"]), 0.0)
        end = max(float(word["end"]), start + 0.05)
        if tidied and start < tidied[-1]["start"]:
            start = tidied[-1]["start"]
        if tidied and end <= start:
            end = start + 0.05
        tidied.append({"text": clean_word(word["text"]), "start": start, "end": end})
    return [word for word in tidied if word["text"]]


def load_word_timings(path: str) -> List[dict]:
    """Read the timings synthesize_voice wrote, or [] when there are none."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("words") or []
    if not isinstance(data, list):
        return []
    return _tidy_timings([item for item in data if isinstance(item, dict) and item.get("text")])


_TOKEN = re.compile(r"\S+")
_EDGE_PUNCTUATION = "\"'()[]!?.,;:…—–-"


def _same_word(token: str, word: str) -> bool:
    """True when a narration token is the same word as a boundary word."""
    left = token.strip(_EDGE_PUNCTUATION).lower()
    right = word.strip(_EDGE_PUNCTUATION).lower()
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def attach_punctuation(words: Sequence[dict], narration: str) -> List[dict]:
    """Put the narration's punctuation back onto edge-tts' word timings.

    edge-tts reports the words without their punctuation, so a card could not
    tell where a sentence ends (nothing to close a card on) and had nothing to
    tidy at its end. The words are walked in order against the narration, so
    the commas and full stops land on the right words even when the two lists
    do not line up perfectly.
    """
    tokens = _TOKEN.findall(narration or "")
    if not tokens:
        return list(words)

    attached: List[dict] = []
    index = 0
    for word in words:
        item = dict(word)
        # A boundary that already holds several words came from a sentence
        # boundary: there is nothing to align it to, so it is kept as it is.
        if len(item["text"].split()) != 1:
            attached.append(item)
            continue
        for look in range(index, min(index + 4, len(tokens))):
            if _same_word(tokens[look], item["text"]):
                item["text"] = clean_word(tokens[look])
                index = look + 1
                break
        attached.append(item)
    return attached


def write_word_timings(path: str, words: Sequence[dict]) -> str:
    """Save the timings next to the voice file (also handy for debugging)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"words": list(words)}, handle, ensure_ascii=False, indent=1)
    return path


# ---------------------------------------------------------------------------
# Line layout: what actually fits inside the frame
# ---------------------------------------------------------------------------
# A character count alone is not enough - "W" is twice as wide as "i" - so the
# wrap measures the text with the very font file libass will use (fontconfig
# names it) and keeps a character cap as a second, cheap limit.
AVG_CHAR_EM = 0.60
MAX_FONT_CACHE = 8
_FONT_FILES: Dict[str, str] = {}
_FONTS: Dict[Tuple[str, int], object] = {}


def font_path_for(family: str) -> str:
    """The bold file fontconfig resolves for ``family`` ('' when unknown)."""
    if family in _FONT_FILES:
        return _FONT_FILES[family]
    path = ""
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", f"{family}:bold"],
            capture_output=True, text=True, timeout=10,
        )
        candidate = (result.stdout or "").strip()
        if candidate and os.path.isfile(candidate):
            path = candidate
    except Exception:
        path = ""
    _FONT_FILES[family] = path
    return path


def text_width(text: str, size: int, family: str = DEFAULT_FONT) -> float:
    """Width in frame pixels, measured with the drawn face when possible."""
    key = (family, int(size))
    if key not in _FONTS:
        font = None
        path = font_path_for(family)
        if path:
            try:
                from PIL import ImageFont

                font = ImageFont.truetype(path, int(size))
            except Exception:
                font = None
        if len(_FONTS) >= MAX_FONT_CACHE:
            _FONTS.clear()
        _FONTS[key] = font
    font = _FONTS[key]
    if font is None:
        return len(text) * float(size) * AVG_CHAR_EM
    return float(font.getlength(text))


class LineLayout:
    """How much text one style may put on one line."""

    def __init__(self, size: int, max_px: int, max_chars: int, family: str = DEFAULT_FONT):
        self.size = int(size)
        self.max_px = int(max_px)
        self.max_chars = int(max_chars)
        self.family = family

    def width(self, text: str) -> float:
        return text_width(text, self.size, self.family)

    def fits(self, text: str) -> bool:
        return len(text) <= self.max_chars and self.width(text) <= self.max_px

    def wrap(self, text: str, max_lines: Optional[int] = None) -> List[str]:
        """Greedy word wrap; a word is never split (technique 21)."""
        lines: List[str] = []
        current = ""
        for word in (text or "").split():
            candidate = f"{current} {word}".strip()
            if current and not self.fits(candidate):
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        if max_lines is not None and len(lines) > max_lines:
            lines = lines[:max_lines]
            last = lines[-1]
            while last and not self.fits(f"{last}..."):
                last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
            lines[-1] = f"{last}..." if last else "..."
        return lines

    def fit_line(self, text: str) -> str:
        """One line, cut on a word boundary with an ellipsis when too long."""
        if self.fits(text):
            return text
        trimmed = text
        while trimmed and not self.fits(f"{trimmed}..."):
            trimmed = trimmed.rsplit(" ", 1)[0] if " " in trimmed else trimmed[:-1]
        return f"{trimmed.rstrip(' ,-|/')}..." if trimmed else ""


def subtitle_layout(family: str = DEFAULT_FONT) -> LineLayout:
    return LineLayout(SUB_FONT_SIZE, FRAME_WIDTH - 2 * SIDE_MARGIN, SUB_MAX_CHARS, family)


def hook_layout(family: str = DEFAULT_FONT) -> LineLayout:
    return LineLayout(HOOK_FONT_SIZE, FRAME_WIDTH - 2 * 90, HOOK_MAX_CHARS, family)


def title_layout(family: str = DEFAULT_FONT) -> LineLayout:
    return LineLayout(TITLE_FONT_SIZE, FRAME_WIDTH - 2 * 70, TITLE_MAX_CHARS, family)


def cta_layout(family: str = DEFAULT_FONT) -> LineLayout:
    return LineLayout(CTA_FONT_SIZE, FRAME_WIDTH - 2 * 90, CTA_MAX_CHARS, family)


# ---------------------------------------------------------------------------
# Cards: at most two lines, broken at word boundaries
# ---------------------------------------------------------------------------

def _card_lines(texts: Sequence[str], layout: LineLayout) -> List[str]:
    return layout.wrap(" ".join(texts))


def _card_fits(texts: Sequence[str], layout: LineLayout, max_lines: int) -> bool:
    lines = _card_lines(texts, layout)
    return len(lines) <= max_lines and all(layout.fits(line) for line in lines)


def build_cards(
    words: Sequence[dict],
    layout: Optional[LineLayout] = None,
    max_lines: int = SUB_MAX_LINES,
    marked: Iterable[str] = (),
    min_seconds: float = MIN_CARD_SECONDS,
) -> List[dict]:
    """Group timed words into caption cards of at most ``max_lines`` lines.

    A card is closed when the next word would need a third line, and also after
    a sentence end once the card already says something - one message per card.
    A card that would flash by in less than ``min_seconds`` is merged into a
    neighbour when it still fits.
    """
    layout = layout or subtitle_layout()
    marked = tuple(word for word in marked if word)
    cards: List[List[dict]] = []
    current: List[dict] = []

    for word in words:
        text = clean_word(word["text"])
        if not text:
            continue
        candidate = current + [{"text": text, "start": word["start"], "end": word["end"]}]
        if _card_fits([item["text"] for item in candidate], layout, max_lines):
            current = candidate
            if _ends_sentence(text) and len(current) >= 3:
                cards.append(current)
                current = []
        else:
            if current:
                cards.append(current)
            current = [{"text": text, "start": word["start"], "end": word["end"]}]
            if _ends_sentence(text) and len(current) >= 3:
                cards.append(current)
                current = []
    if current:
        cards.append(current)

    cards = _merge_short_cards(cards, layout, max_lines, min_seconds)
    return [_finish_card(card, marked, layout, max_lines) for card in cards]


def _merge_short_cards(
    cards: Sequence[List[dict]],
    layout: LineLayout,
    max_lines: int,
    min_seconds: float,
) -> List[List[dict]]:
    """Join a flash-quick card into a neighbour that can still hold it."""
    merged: List[List[dict]] = [list(card) for card in cards]
    index = 0
    while index < len(merged) and min_seconds > 0:
        card = merged[index]
        spoken = card[-1]["end"] - card[0]["start"]
        following = merged[index + 1] if index + 1 < len(merged) else None
        until_next = (following[0]["start"] - card[0]["start"]) if following else spoken
        if max(spoken, until_next) >= min_seconds or len(merged) == 1:
            index += 1
            continue
        joined_with_next = following is not None and _card_fits(
            [item["text"] for item in card + following], layout, max_lines
        )
        if joined_with_next:
            merged[index : index + 2] = [card + following]
            continue
        if index > 0 and _card_fits(
            [item["text"] for item in merged[index - 1] + card], layout, max_lines
        ):
            merged[index - 1 : index + 1] = [merged[index - 1] + card]
            continue
        index += 1
    return merged


def _finish_card(
    card: Sequence[dict],
    marked: Iterable[str],
    layout: LineLayout,
    max_lines: int,
) -> dict:
    """A card ready for rendering: display text, lines and per-word flags."""
    words = [dict(item) for item in card]
    # Punctuation is carried by the voice, not the caption: a full stop or a
    # comma in the middle of a card reads as noise (techniques 13 and 14).
    for word in words:
        word["text"] = trim_card_end(word["text"])
    for word in words:
        word["emphasis"] = is_emphasis_word(word["text"], marked)
    emphasized = [word for word in words if word["emphasis"]]
    for word in emphasized[MAX_EMPHASIS_WORDS:]:
        word["emphasis"] = False

    lines: List[List[int]] = []
    current: List[int] = []
    current_text = ""
    for index, word in enumerate(words):
        candidate = f"{current_text} {word['text']}".strip()
        if current and not layout.fits(candidate):
            lines.append(current)
            current = [index]
            current_text = word["text"]
        else:
            current.append(index)
            current_text = candidate
    if current:
        lines.append(current)
    while len(lines) > max_lines:
        # A measured line can still be too long for the cap; keep the card valid.
        last = lines.pop()
        lines[-1] = lines[-1] + last

    return {
        "words": words,
        "lines": lines,
        "text": " ".join(word["text"] for word in words),
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "spoken_end": words[-1]["end"],
    }


def time_cards(cards: Sequence[dict], min_seconds: float = 0.2) -> List[dict]:
    """Give every card the moment it stays on screen (technique 10)."""
    timed = [dict(card) for card in cards]
    for index, card in enumerate(timed):
        spoken_end = float(card["spoken_end"])
        following = timed[index + 1] if index + 1 < len(timed) else None
        if following is None:
            end = spoken_end + CARD_TAIL_SECONDS
        elif following["start"] - spoken_end > CARD_GAP_SECONDS:
            end = spoken_end + CARD_TAIL_SECONDS
        else:
            end = following["start"]
        card["end"] = max(end, card["start"] + min_seconds)
    return timed


def plan_cards(
    words: Sequence[dict],
    layout: Optional[LineLayout] = None,
    max_lines: int = SUB_MAX_LINES,
    marked: Iterable[str] = (),
) -> List[dict]:
    """The finished caption plan: cards with lines, flags and their moments."""
    return time_cards(build_cards(words, layout, max_lines, marked))


# ---------------------------------------------------------------------------
# ASS document
# ---------------------------------------------------------------------------

def format_timestamp(seconds: float) -> str:
    """ASS time: H:MM:SS.cc."""
    value = max(float(seconds or 0.0), 0.0)
    total_centiseconds = int(round(value * 100))
    hours, remainder = divmod(total_centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def parse_timestamp(text: str) -> float:
    """Seconds from an ASS timestamp, so a test can read an event back."""
    hours, minutes, seconds = str(text).strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _run(text: str, tags: str = "") -> str:
    """One ASS text run; ``\r`` resets the tags of the run before it."""
    return f"{{\\r{tags}}}{text}" if tags else f"{{\\r}}{text}"


def _plain_run(text: str) -> str:
    return _run(text)


def _emphasis_run(text: str) -> str:
    # Scale, not font size: a bigger font would re-flow the line we just broke.
    return _run(text, f"\\c{ACCENT_COLOUR}\\fscx106\\fscy106")


def _hidden_run(text: str) -> str:
    return _run(text, "\\alpha&HFF&")


def _karaoke_run(text: str) -> str:
    """The word being spoken: brighter, larger, and marked underneath.

    The emphasis colour is already gold, so the current word is told apart by
    size and the underline that follows it, not by a second colour.
    """
    return _run(
        text,
        f"\\c{ACCENT_COLOUR}\\fscx124\\fscy124\\u1\\t(0,120,\\fscx110\\fscy110)",
    )


def _card_text(card: dict, highlight: Optional[int] = None) -> str:
    """The card's lines as ASS text, optionally highlighting one word."""
    lines = []
    for line in card["lines"]:
        runs = []
        for word_index in line:
            word = card["words"][word_index]
            if highlight is None:
                runs.append(_emphasis_run(word["text"]) if word["emphasis"] else _plain_run(word["text"]))
            elif word_index == highlight:
                runs.append(_karaoke_run(word["text"]))
            else:
                runs.append(_hidden_run(word["text"]))
        lines.append(" ".join(runs))
    return "\\N".join(lines)


def _style_line(
    name: str,
    font: str,
    size: int,
    primary: str,
    outline: int,
    shadow: int,
    alignment: int,
    margin_v: int,
    margin_l: int = SIDE_MARGIN,
    margin_r: int = SIDE_MARGIN,
) -> str:
    return (
        f"Style: {name},{font},{size},{primary},{primary},&H00000000&,"
        f"&H80000000&,-1,0,0,0,100,100,0,0,1,{outline},{shadow},"
        f"{alignment},{margin_l},{margin_r},{margin_v},1"
    )


def build_style_block(font: str = DEFAULT_FONT) -> List[str]:
    """The [V4+ Styles] section of the telop document."""
    return [
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        _style_line(
            "Telop", font, SUB_FONT_SIZE, WHITE_COLOUR,
            SUB_OUTLINE, SUB_SHADOW, 2, BOTTOM_SAFE,
        ),
        _style_line(
            "Hook", font, HOOK_FONT_SIZE, WHITE_COLOUR,
            HOOK_OUTLINE, HOOK_SHADOW, 8, HOOK_MARGIN_V, 90, 90,
        ),
        # A 60 % transparent white: the label is there to be found, not read.
        _style_line(
            "Title", font, TITLE_FONT_SIZE, "&H60FFFFFF&",
            TITLE_OUTLINE, TITLE_SHADOW, 8, TITLE_MARGIN_V, 70, 70,
        ),
        _style_line(
            "Cta", font, CTA_FONT_SIZE, ACCENT_COLOUR,
            CTA_OUTLINE, CTA_SHADOW, 8, CTA_MARGIN_V, 90, 90,
        ),
        "",
    ]


def _dialogue(layer: int, start: float, end: float, style: str, text: str) -> str:
    return (
        f"Dialogue: {layer},{format_timestamp(start)},{format_timestamp(end)},"
        f"{style},,0,0,0,,{text}"
    )


def _hook_event(hook_text: str, seconds: float, layout: LineLayout) -> List[str]:
    """The hook pops in and settles, then leaves before the body starts."""
    if not hook_text:
        return []
    lines = layout.wrap(hook_text, max_lines=2)
    if not lines:
        return []
    text = "\\N".join(lines)
    return [
        _dialogue(
            2, 0.25, max(seconds, 0.8), "Hook",
            "{\\fscx45\\fscy45\\t(0,150,\\fscx112\\fscy112)\\t(150,280,\\fscx100\\fscy100)"
            "\\fad(40,240)}" + text,
        )
    ]


def _title_event(title: str, end: Optional[float], layout: LineLayout) -> List[str]:
    if not title:
        return []
    fitted = layout.fit_line(title)
    if not fitted:
        return []
    return [
        _dialogue(
            2, 0.6, end or 30.0, "Title",
            "{\\fad(450,400)}" + fitted,
        )
    ]


def _cta_event(cta_text: str, start: float, end: float, layout: LineLayout) -> List[str]:
    if not cta_text or end <= start:
        return []
    lines = layout.wrap(cta_text, max_lines=2)
    if not lines:
        return []
    return [
        _dialogue(
            3, start, end, "Cta",
            "{\\fscx88\\fscy88\\t(0,180,\\fscx105\\fscy105)\\t(180,300,\\fscx100\\fscy100)"
            "\\fad(150,250)}" + "\\N".join(lines),
        )
    ]


def build_telop_ass(
    words: Sequence[dict] = (),
    hook_text: str = "",
    title: str = "",
    cta_text: str = DEFAULT_CTA_TEXT,
    video_seconds: Optional[float] = None,
    font_name: Optional[str] = None,
    hook_duration: float = HOOK_SECONDS,
    cta_duration: float = CTA_SECONDS,
    layout: Optional[LineLayout] = None,
    max_lines: int = SUB_MAX_LINES,
    marked: Iterable[str] = (),
    width: int = FRAME_WIDTH,
    height: int = FRAME_HEIGHT,
) -> str:
    """The whole ASS document: hook, cards, karaoke words, title and CTA."""
    family = font_name or DEFAULT_FONT
    layout = layout or subtitle_layout(family)
    cards = plan_cards(words, layout, max_lines, marked)
    last_spoken = cards[-1]["spoken_end"] if cards else 0.0
    total = float(video_seconds or 0.0)
    if not math.isfinite(total) or total <= 0:
        total = max(last_spoken + 1.2, cta_duration + 1.0)

    events: List[str] = []
    for card in cards:
        events.append(
            _dialogue(0, card["start"], card["end"], "Telop", _card_text(card))
        )
    # One overlay per spoken word: the same lines with only that word visible,
    # in gold and a touch larger, so the highlight follows the voice.
    for card in cards:
        for index, word in enumerate(card["words"]):
            start = word["start"]
            end = min(word["end"], card["end"])
            if end <= start:
                continue
            events.append(
                _dialogue(1, start, end, "Telop", _card_text(card, highlight=index))
            )

    events += _hook_event(
        hook_text, min(hook_duration, max(total - 0.5, 0.8)), hook_layout(family)
    )
    events += _title_event(title, max(total - 0.3, 2.0), title_layout(family))

    # The closing line lands in the outro: when the voice runs long it waits for
    # the last words, and it always keeps a moment on screen.
    total = max(total, 1.0)
    cta_end = max(total - 0.15, 1.0)
    cta_start = max(total - cta_duration, last_spoken - 0.4, 0.6)
    if cta_start > cta_end - 1.2:
        cta_start = max(cta_end - 1.2, 0.2)
    events += _cta_event(cta_text, cta_start, cta_end, cta_layout(family))

    header = [
        "[Script Info]",
        "; CrayonJapan telop - generated by telop.py",
        "ScriptType: v4.00+",
        f"PlayResX: {int(width)}",
        f"PlayResY: {int(height)}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
    ]
    body = ["[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            ""]
    return "\n".join(header + build_style_block(family) + body)


def detect_font(candidates: Sequence[str] = FONT_CANDIDATES) -> str:
    """The first installed family of ``candidates``, else the workflow's font.

    ``fc-list`` is how fontconfig names the faces libass will actually use; on
    the Ubuntu runner ``fonts-dejavu-core`` is installed by the workflow, so
    DejaVu Sans Bold is always there even if the probe fails.
    """
    wanted = [name.lower() for name in candidates]
    try:
        result = subprocess.run(
            ["fc-list", ":", "family"],
            capture_output=True, text=True, timeout=10,
        )
        families = {
            part.strip().lower()
            for line in (result.stdout or "").splitlines()
            for part in line.split(",")
            if part.strip()
        }
    except Exception:
        return DEFAULT_FONT
    for name in wanted:
        if name in families:
            return candidates[wanted.index(name)]
    return DEFAULT_FONT


def write_telop_ass(path: str, **options) -> str:
    """Build the telop document and save it as UTF-8 ASS."""
    text = build_telop_ass(**options)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def summarise(cards: Sequence[dict]) -> Dict[str, float]:
    """Numbers for the render log: cards, lines and the longest line."""
    if not cards:
        return {"cards": 0.0, "lines": 0.0, "longest_line": 0.0, "longest_card": 0.0}
    words = [word for card in cards for word in card["words"]]
    return {
        "cards": float(len(cards)),
        "lines": float(sum(len(card["lines"]) for card in cards)),
        "longest_line": float(max((len(word["text"]) for word in words), default=0)),
        "longest_card": float(max(len(card["text"]) for card in cards)),
    }
