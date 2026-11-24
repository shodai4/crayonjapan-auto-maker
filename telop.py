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
import unicodedata
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

# Japanese on-screen text (the 89 %-Japanese audience) needs a face with kana
# and kanji, which DejaVu has not. The runner cannot be told to install one (the
# workflow file is out of bounds), so the bold Noto Sans CJK JP face travels with
# the repository and is handed to libass through `fontsdir=`. It is subset to the
# JIS X 0208 character set to keep the checkout small (see assets/fonts/).
JP_FONT_FAMILY = "Noto Sans CJK JP"
JP_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
JP_FONT_FILE = os.path.join(JP_FONT_DIR, "NotoSansJP-Bold.otf")


def japanese_font_available() -> bool:
    """True when the bundled kana/kanji face is on disk."""
    return os.path.isfile(JP_FONT_FILE)


def japanese_fonts_dir() -> str:
    """The `fontsdir=` argument for the ASS filter ('' when not bundled)."""
    return JP_FONT_DIR if japanese_font_available() else ""

# Caption geometry. Lines are measured with the drawn font and also capped at
# 16 characters, which keeps a 72 px DejaVu Sans Bold line inside the 740 px
# left between the side margins.
SUB_FONT_SIZE = 72
SUB_MAX_CHARS = 16
SUB_MAX_LINES = 2
SUB_OUTLINE = 6
SUB_SHADOW = 3

# Hook: bigger, two lines of its own, directly under the top bar. The size is
# the *first* rung of a ladder, not a fixed one: the hook is the promise of the
# Short, so its sentence is never cut with an ellipsis. When the whole line does
# not fit at 96 px the size steps down (by 2 px a rung) until it does, and only
# a hook that still needs a fourth line goes below HOOK_MIN_FONT_SIZE.
HOOK_FONT_SIZE = 96
HOOK_MIN_FONT_SIZE = 58
HOOK_FONT_STEP = 2
HOOK_HARD_MIN_FONT_SIZE = 40
HOOK_MAX_LINES = 3
HOOK_OUTLINE = 9
HOOK_SHADOW = 5
HOOK_MARGIN_V = TOP_SAFE + 140
HOOK_SIDE_MARGIN = 90
HOOK_SECONDS = 3.0
# The pop-in overshoots to 112 %, so a hook line is laid out at that width and
# the burst still stays inside the side margins.
HOOK_POP_SCALE = 1.12
# A bilingual hook shows the translation under the promise in a smaller size:
# readable on a phone, but never competing with the line that stops the swipe.
HOOK_SECONDARY_SCALE = 0.62
HOOK_SECONDARY_MIN_SIZE = 40
# A hook that carries a translation starts one rung lower: two Japanese lines at
# 96 px plus the English line would reach down over the artwork.
HOOK_BILINGUAL_FONT_SIZE = 80

# The closing question ("次は何を描いてほしい？ / What should I draw next?").
# It sits above the follow line in the outro, where the comment button and the
# replay are both one thumb away.
ASK_FONT_SIZE = 56
ASK_SECONDARY_SCALE = 0.72
ASK_MAX_CHARS = 34
ASK_LIFT = 300
ASK_OUTLINE = 6
ASK_SHADOW = 3
DEFAULT_ASK_EN = "What should I draw next?"
DEFAULT_ASK_JA = "次は何を描いてほしい？"

# Fixed title: deliberately small and slightly transparent - it is a label, not
# a message. 40 px keeps a 34-character title inside the frame.
TITLE_FONT_SIZE = 40
TITLE_MAX_CHARS = 34
TITLE_MARGIN_V = TOP_SAFE + 20
TITLE_OUTLINE = 4
TITLE_SHADOW = 2

# Closing follow line: it takes the caption band once the captions have left,
# which keeps it off the top edge of the drawing the zoom outro is showing. When
# the narration runs so late that the captions are still on screen, the line
# stacks just above them instead of drawing gold over white.
CTA_FONT_SIZE = 64
CTA_MAX_CHARS = 16
CTA_MARGIN_V = BOTTOM_SAFE
# Two caption lines plus a little air above them: the stacked line never draws
# into the caption that is still on screen.
CTA_STACKED_MARGIN_V = BOTTOM_SAFE + 220
CTA_OUTLINE = 7
CTA_SHADOW = 4
CTA_SECONDS = 3.6
# Readable even when the voice runs right up to the end (the picture only gets
# NARRATION_TAIL_SECONDS after the last word).
CTA_MIN_SECONDS = 2.4
CTA_TAIL_SECONDS = 0.15
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


# Typographic marks a Latin sans draws that are not letters, digits or ASCII.
_DRAWABLE_MARKS = frozenset("—–‐‑…‘’‚“”„·•™©®°±×÷")
# Scripts the drawn face (DejaVu Sans Bold) carries. Everything outside them
# would land as a tofu box, so it is treated like the emoji it usually is.
_DRAWABLE_SCRIPTS = ("LATIN", "GREEK", "CYRILLIC")


def _is_cjk(character: str) -> bool:
    """True for kana, kanji and the punctuation/full-width forms they use."""
    code = ord(character)
    return (
        0x3000 <= code <= 0x30FF      # CJK punctuation, hiragana, katakana
        or 0x31F0 <= code <= 0x31FF   # katakana phonetic extensions
        or 0x3400 <= code <= 0x4DBF   # CJK extension A
        or 0x4E00 <= code <= 0x9FFF   # CJK unified ideographs
        or 0xFF00 <= code <= 0xFFEF   # full-width / half-width forms
        or code in (0x2015, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x301C)
    )


def contains_cjk(text: str) -> bool:
    """True when a line needs the bundled Japanese face."""
    return any(_is_cjk(ch) for ch in (text or ""))


def _is_drawable(character: str) -> bool:
    """True when the drawn font can show this character.

    Emoji and decorative symbols are dropped, but accented Latin letters
    (``é`` in ``Pokémon``) and the combining accent of a decomposed ``e`` +
    ``́`` are letters like any other and stay: a title must never come out as
    ``Pokmon``. Kana and kanji stay too, as long as the bundled Japanese face
    is there to draw them.
    """
    code = ord(character)
    if code < 32 or code == 127:
        return False
    if 32 <= code < 127:
        return True
    if character in _DRAWABLE_MARKS:
        return True
    if _is_cjk(character):
        return japanese_font_available()
    category = unicodedata.category(character)
    if category == "Zs":
        return True
    name = unicodedata.name(character, "")
    if not name:
        return False
    if category[0] in "LMN":
        if any(script in name for script in _DRAWABLE_SCRIPTS):
            return True
        # A mark that puts the accent on a letter we just kept.
        return category == "Mn" and "ACCENT" in name
    return False


def _strip_emoji(text: str) -> str:
    """Keep what the font can draw, drop emoji and decorative symbols."""
    kept = "".join(ch for ch in text if _is_drawable(ch))
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
        # Only a word the cut ran through is dropped: when the cut lands on a
        # word boundary the word itself is kept ("Pokémon" stays "Pokémon").
        if " " in cut and not cut.endswith(" ") and text[len(cut)] != " ":
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
    """The bold file fontconfig resolves for ``family`` ('' when unknown).

    The bundled Japanese face is measured from its own file: it is not installed
    in the system fontconfig, so ``fc-match`` would answer with a fallback and
    every Japanese line would be measured at the wrong width.
    """
    if family in _FONT_FILES:
        return _FONT_FILES[family]
    if family == JP_FONT_FAMILY and japanese_font_available():
        _FONT_FILES[family] = JP_FONT_FILE
        return JP_FONT_FILE
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

    def __init__(
        self,
        size: int,
        max_px: int,
        max_chars: int,
        family: str = DEFAULT_FONT,
        gap_px: float = 0.0,
    ):
        self.size = int(size)
        self.max_px = int(max_px)
        self.max_chars = int(max_chars)
        self.family = family
        # Extra room the render adds at every word gap (the hard space the
        # karaoke grows into), so a line is measured the way it is drawn.
        self.gap_px = float(gap_px)

    def width(self, text: str) -> float:
        return text_width(text, self.size, self.family)

    def rendered_width(self, text: str) -> float:
        """The drawn width, including the extra room at each word gap."""
        gaps = max(len((text or "").split()) - 1, 0)
        return self.width(text) + self.gap_px * gaps

    def fits(self, text: str) -> bool:
        return len(text) <= self.max_chars and self.rendered_width(text) <= self.max_px

    def wrap_cjk(self, text: str, max_lines: Optional[int] = None) -> List[str]:
        """Character wrap for Japanese, which has no spaces to break on.

        A Latin line only breaks between words (technique 21), but a Japanese
        sentence is one unbroken word to a whitespace wrap: it would overflow
        the frame. Kana and kanji allow a break almost anywhere, so the line is
        filled character by character instead.
        """
        lines: List[str] = []
        current = ""
        # Closing brackets and the marks that may not open a line are carried
        # with the character before them, so a line never starts with 。 or 、.
        no_leading = "、。，．・：；？！ー）」』】〕｝”,.!?:;"
        for character in (text or ""):
            candidate = current + character
            if current and not self.fits(candidate) and character not in no_leading:
                lines.append(current)
                current = character
            else:
                current = candidate
        if current:
            lines.append(current)
        if max_lines is not None and len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit_line(lines[-1]) or lines[-1]
        return lines

    def wrap_auto(self, text: str, max_lines: Optional[int] = None) -> List[str]:
        """The wrap a text needs: character-wise for Japanese, words otherwise."""
        if contains_cjk(text):
            return self.wrap_cjk(text, max_lines)
        return self.wrap(text, max_lines)

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

    def wrap_balanced(self, text: str) -> List[str]:
        """The fewest lines that hold ``text``, keeping the widest one narrow.

        A greedy wrap can need one line more than necessary - "Thick textures /
        make childhood / memories pop" - which would push the hook one size
        down the ladder for nothing. This tries every break instead, first
        minimising the number of lines and then how wide the widest one is.
        """
        words = (text or "").split()
        if not words:
            return []
        count = len(words)
        # best[i] = (lines from word i to the end, widest line among them).
        best: List[Tuple[int, float]] = [(0, 0.0)] * (count + 1)
        next_break = [count] * (count + 1)
        for index in range(count - 1, -1, -1):
            chosen = (len(words) + 1, float("inf"))
            for stop in range(index + 1, count + 1):
                line = " ".join(words[index:stop])
                # A single word is never split, so it is allowed to overflow.
                if stop > index + 1 and not self.fits(line):
                    break
                following = best[stop]
                candidate = (following[0] + 1, max(following[1], self.width(line)))
                if candidate[0] < chosen[0] or (
                    candidate[0] == chosen[0] and candidate[1] < chosen[1]
                ):
                    chosen = candidate
                    next_break[index] = stop
            best[index] = chosen
        lines: List[str] = []
        index = 0
        while index < count:
            stop = max(next_break[index], index + 1)
            lines.append(" ".join(words[index:stop]))
            index = stop
        return lines


def layout_for_text(
    layout: LineLayout,
    text: str,
    jp_family: str = JP_FONT_FAMILY,
) -> LineLayout:
    """The same layout, measured in the face that will actually draw ``text``."""
    if contains_cjk(text) and jp_family and japanese_font_available():
        if layout.family != jp_family:
            return LineLayout(
                layout.size, layout.max_px, layout.max_chars, jp_family, layout.gap_px
            )
    return layout


def subtitle_layout(family: str = DEFAULT_FONT) -> LineLayout:
    return LineLayout(
        SUB_FONT_SIZE,
        FRAME_WIDTH - 2 * SIDE_MARGIN,
        SUB_MAX_CHARS,
        family,
        gap_px=text_width(" ", WORD_GAP_FONT_SIZE, family),
    )


def hook_layout(family: str = DEFAULT_FONT, size: int = HOOK_FONT_SIZE) -> LineLayout:
    """One rung of the hook ladder: no character cap, the width is the limit.

    The line may use the full width between the side margins (900 px); its
    112 % pop-in leans into the margin, never past the frame edge.
    """
    return LineLayout(
        size,
        int((FRAME_WIDTH - 2 * HOOK_SIDE_MARGIN)),
        FRAME_WIDTH,
        family,
    )


def fit_hook(
    hook_text: str,
    family: str = DEFAULT_FONT,
    max_size: int = HOOK_FONT_SIZE,
) -> Tuple[List[str], int]:
    """The hook's lines and font size: the whole sentence, never an ellipsis.

    The ladder starts at 96 px and steps down two pixels at a time. Two lines
    are preferred; a hook that needs three takes three rather than lose its
    ending, and only a hook that still needs a fourth at 58 px goes smaller.
    ``max_size`` starts the ladder lower for a bilingual hook.
    """
    text = re.sub(r"\s+", " ", (hook_text or "")).strip()
    if not text:
        return [], HOOK_FONT_SIZE
    try:
        start = int(max_size)
    except (TypeError, ValueError):
        start = HOOK_FONT_SIZE
    start = min(max(start, HOOK_MIN_FONT_SIZE), HOOK_FONT_SIZE)

    three_lines: Optional[Tuple[List[str], int]] = None
    for size in range(start, HOOK_MIN_FONT_SIZE - 1, -HOOK_FONT_STEP):
        lines = _hook_lines(text, family, size)
        if len(lines) <= 2:
            return lines, size
        if three_lines is None and len(lines) <= HOOK_MAX_LINES:
            three_lines = (lines, size)
    if three_lines is not None:
        return three_lines

    size = HOOK_MIN_FONT_SIZE
    while size > HOOK_HARD_MIN_FONT_SIZE:
        size -= HOOK_FONT_STEP
        lines = _hook_lines(text, family, size)
        if len(lines) <= HOOK_MAX_LINES:
            return lines, size
    return _hook_lines(text, family, size), size


def _hook_lines(text: str, family: str, size: int) -> List[str]:
    """Hook lines at one rung: balanced for a sentence, character-split for JP."""
    layout = hook_layout(family, size)
    measured = layout_for_text(layout, text)
    if contains_cjk(text):
        return measured.wrap_cjk(text)
    return measured.wrap_balanced(text)


def fit_hook_pair(
    primary: str,
    secondary: str,
    family: str = DEFAULT_FONT,
) -> Tuple[List[Tuple[str, int, str]], int]:
    """A two-language hook: the primary line(s) big, the other one under them.

    Returns ``[(line, font_size, font_family), ...]`` and the primary size, so
    the caller can draw the Japanese line at the size the English one was
    checked against and the translation small beneath it.
    """
    text = re.sub(r"\s+", " ", (primary or "")).strip()
    extra = re.sub(r"\s+", " ", (secondary or "")).strip()
    if not text:
        return ([], HOOK_FONT_SIZE) if not extra else fit_hook_pair(extra, "", family)

    lines, size = fit_hook(text, family, max_size=HOOK_BILINGUAL_FONT_SIZE if extra else HOOK_FONT_SIZE)
    primary_family = JP_FONT_FAMILY if contains_cjk(text) else family
    block: List[Tuple[str, int, str]] = [(line, size, primary_family) for line in lines]
    if not extra:
        return block, size

    small = max(int(round(size * HOOK_SECONDARY_SCALE)), HOOK_SECONDARY_MIN_SIZE)
    small_family = JP_FONT_FAMILY if contains_cjk(extra) else family
    small_lines = []
    # The translation steps down in size before it is ever cut: it is the same
    # promise, and an ellipsis in it would read as the short version of a hook.
    while small >= HOOK_SECONDARY_MIN_SIZE - 8:
        small_layout = hook_layout(small_family, small)
        small_lines = layout_for_text(small_layout, extra).wrap_auto(extra, max_lines=2)
        if len(small_lines) <= 2 and all(
            layout_for_text(small_layout, line).fits(line) for line in small_lines
        ):
            break
        small -= 4
    # Never let the translation push the promise off the frame: three primary
    # lines already use the hook's budget, so the small block gets one line.
    if len(block) >= 3:
        small_lines = small_lines[:1]
    block += [(line, small, small_family) for line in small_lines]
    return block, size


def title_layout(family: str = DEFAULT_FONT) -> LineLayout:
    return LineLayout(TITLE_FONT_SIZE, FRAME_WIDTH - 2 * 70, TITLE_MAX_CHARS, family)


def cta_layout(family: str = DEFAULT_FONT) -> LineLayout:
    return LineLayout(CTA_FONT_SIZE, FRAME_WIDTH - 2 * 90, CTA_MAX_CHARS, family)


# ---------------------------------------------------------------------------
# Cards: at most two lines, broken at word boundaries
# ---------------------------------------------------------------------------

def _card_lines(texts: Sequence[str], layout: LineLayout) -> List[str]:
    text = " ".join(texts)
    return layout_for_text(layout, text).wrap_auto(text)


def _card_fits(texts: Sequence[str], layout: LineLayout, max_lines: int) -> bool:
    lines = _card_lines(texts, layout)
    return len(lines) <= max_lines and all(layout.fits(line) for line in lines)


def _split_oversized_word(word: dict, layout: LineLayout) -> List[dict]:
    """A word that cannot fit on one line, cut into chunks that can.

    Japanese narration can arrive as one long chunk (a sentence boundary read as
    a word); without this the card would be a single line running off the frame.
    The word's own spoken span is shared between the chunks, so the karaoke still
    follows the voice.
    """
    text = clean_word(word.get("text"))
    if not text or layout.fits(text):
        return [dict(word)]
    measured = layout_for_text(layout, text)
    chunks = measured.wrap_cjk(text) if contains_cjk(text) else measured.wrap(text)
    if len(chunks) <= 1:
        return [dict(word)]
    start = float(word.get("start", 0.0))
    end = max(float(word.get("end", start)), start + 0.05)
    span = (end - start) / len(chunks)
    return [
        {
            "text": chunk,
            "start": round(start + index * span, 3),
            "end": round(start + (index + 1) * span, 3),
        }
        for index, chunk in enumerate(chunks)
    ]


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
        for piece in _split_oversized_word(word, layout):
            text = clean_word(piece["text"])
            if not text:
                continue
            candidate = current + [{"text": text, "start": piece["start"], "end": piece["end"]}]
            if _card_fits([item["text"] for item in candidate], layout, max_lines):
                current = candidate
                if _ends_sentence(text) and len(current) >= 3:
                    cards.append(current)
                    current = []
            else:
                if current:
                    cards.append(current)
                current = [{"text": text, "start": piece["start"], "end": piece["end"]}]
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


def _font_tag(text: str, family: str = DEFAULT_FONT) -> str:
    """Switch to the Japanese face for a run that holds kana or kanji.

    The style's font is the Latin one; a Japanese word inside the same card
    would otherwise fall back to a face that may not carry it. ``\fn`` is
    per-run, so both scripts share one line of geometry.
    """
    if family == JP_FONT_FAMILY:
        return f"\\fn{JP_FONT_FAMILY}"
    return f"\\fn{JP_FONT_FAMILY}" if contains_cjk(text) else ""


def _run(text: str, tags: str = "") -> str:
    """One ASS text run; ``\r`` resets the tags of the run before it."""
    font = _font_tag(text)
    combined = f"{font}{tags}"
    return f"{{\\r{combined}}}{text}" if combined else f"{{\\r}}{text}"


def _plain_run(text: str) -> str:
    return _run(text)


def _emphasis_run(text: str) -> str:
    # Scale, not font size: a bigger font would re-flow the line we just broke.
    return _run(text, f"\\c{ACCENT_COLOUR}\\fscx106\\fscy106")


def _hidden_run(text: str) -> str:
    return _run(text, "\\alpha&HFF&")


# The word being spoken grows only 12 %, because the gap between two words is
# one space (~25 px at 72 px) and a bigger blow-up runs its outline into the
# neighbour ("creamyblue"). Every gap is therefore widened by a hard space on
# top of the space itself - in the card and in the karaoke overlay alike, so the
# two layers keep identical geometry and the grown word still has room.
KARAOKE_SCALE = 112
KARAOKE_SETTLE_SCALE = 106
WORD_GAP_FONT_SIZE = 48


def _karaoke_run(text: str) -> str:
    """The word being spoken: brighter, a little larger, and marked underneath.

    The emphasis colour is already gold, so the current word is told apart by
    size and the underline that follows it, not by a second colour.
    """
    return _run(
        text,
        f"\\c{ACCENT_COLOUR}\\fscx{KARAOKE_SCALE}\\fscy{KARAOKE_SCALE}\\u1"
        f"\\t(0,120,\\fscx{KARAOKE_SETTLE_SCALE}\\fscy{KARAOKE_SETTLE_SCALE})",
    )


def _word_gap() -> str:
    """A space plus a hard space: the room the highlighted word grows into."""
    return " " + _run("\\h", f"\\fs{WORD_GAP_FONT_SIZE}")


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
        lines.append(_word_gap().join(runs))
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
            CTA_OUTLINE, CTA_SHADOW, 2, CTA_MARGIN_V, 90, 90,
        ),
        _style_line(
            "Ask", font, ASK_FONT_SIZE, WHITE_COLOUR,
            ASK_OUTLINE, ASK_SHADOW, 2, CTA_MARGIN_V + ASK_LIFT, 90, 90,
        ),
        "",
    ]


def _dialogue(layer: int, start: float, end: float, style: str, text: str) -> str:
    return (
        f"Dialogue: {layer},{format_timestamp(start)},{format_timestamp(end)},"
        f"{style},,0,0,0,,{text}"
    )


def _hook_event(
    hook_text: str,
    seconds: float,
    family: str = DEFAULT_FONT,
    hook_secondary: str = "",
) -> List[str]:
    """The hook pops in and settles, then leaves before the body starts.

    The size comes from ``fit_hook``: the whole sentence is shown, at the largest
    rung of the ladder that holds it, so it is never cut off with an ellipsis.
    With a translation (`hook_secondary`) both languages share the one pop, the
    primary line big and the other small underneath it.
    """
    block, size = fit_hook_pair(hook_text, hook_secondary, family)
    if not block:
        return []
    text = _bilingual_block(block, family)
    spin = "\\fscx45\\fscy45\\t(0,150,\\fscx112\\fscy112)\\t(150,280,\\fscx100\\fscy100)"
    # A two-language hook is anchored explicitly, a little higher than the
    # single-line style margin, so its extra line stays off the artwork.
    anchor = ""
    if hook_secondary:
        anchor = f"\\an8\\pos({FRAME_WIDTH // 2},{TOP_SAFE + 45})"
    return [
        _dialogue(
            2, 0.25, max(seconds, 0.8), "Hook",
            "{" + anchor + spin + "\\fad(40,240)}" + text,
        )
    ]


def _title_event(
    title: str,
    end: Optional[float],
    layout: LineLayout,
    title_secondary: str = "",
    family: str = DEFAULT_FONT,
    start: float = 0.6,
) -> List[str]:
    """The quiet label line. It waits for the hook when the hook is two lines."""
    if not title and not title_secondary:
        return []
    lines: List[str] = []
    fitted = layout_for_text(layout, title).fit_line(title) if title else ""
    if fitted:
        face = JP_FONT_FAMILY if contains_cjk(fitted) else family
        lines.append(f"{{\\fn{face}}}{fitted}")
    extra = layout_for_text(layout, title_secondary).fit_line(title_secondary) if title_secondary else ""
    if extra:
        face = JP_FONT_FAMILY if contains_cjk(extra) else family
        lines.append(f"{{\\fn{face}\\fs{int(layout.size * 0.85)}}}{extra}")
    if not lines:
        return []
    return [
        _dialogue(
            2, max(float(start), 0.0), end or 30.0, "Title",
            "{\\fad(450,400)}" + "\\N".join(lines),
        )
    ]


def _cta_event(
    cta_text: str,
    start: float,
    end: float,
    layout: LineLayout,
    margin_v: int = CTA_MARGIN_V,
    cta_secondary: str = "",
) -> List[str]:
    """The closing line; ``margin_v`` lifts it above captions still on screen."""
    if not cta_text or end <= start:
        return []
    lines = layout_for_text(layout, cta_text).wrap_auto(cta_text, max_lines=2)
    if not lines:
        return []
    text = "\\N".join(lines)
    extra = str(cta_secondary or "").strip()
    if extra:
        small = LineLayout(
            max(int(layout.size * 0.72), 24), layout.max_px, 40,
            JP_FONT_FAMILY if contains_cjk(extra) else DEFAULT_FONT,
        )
        small_lines = small.wrap_auto(extra, max_lines=1)
        if small_lines:
            face = JP_FONT_FAMILY if contains_cjk(extra) else DEFAULT_FONT
            text += f"\\N{{\\fn{face}\\fs{small.size}}}" + small_lines[0]
    lift = ""
    if int(margin_v) != CTA_MARGIN_V:
        lift = f"\\an2\\pos({FRAME_WIDTH // 2},{int(FRAME_HEIGHT - margin_v)})"
    return [
        _dialogue(
            3, start, end, "Cta",
            "{" + lift + "\\fscx88\\fscy88\\t(0,180,\\fscx105\\fscy105)"
            "\\t(180,300,\\fscx100\\fscy100)\\fad(150,250)}" + text,
        )
    ]


def _ask_event(
    ask_primary: str,
    ask_secondary: str,
    start: float,
    end: float,
    margin_v: int = CTA_MARGIN_V + ASK_LIFT,
) -> List[str]:
    """The comment question above the follow line, in one or two languages."""
    if end <= start:
        return []
    lines: List[str] = []
    for text, size in ((ask_primary, ASK_FONT_SIZE), (ask_secondary, int(round(ASK_FONT_SIZE * ASK_SECONDARY_SCALE)))):
        cleaned = clean_caption_text(text or "")
        if not cleaned:
            continue
        family = JP_FONT_FAMILY if contains_cjk(cleaned) else DEFAULT_FONT
        layout = LineLayout(size, FRAME_WIDTH - 2 * 110, ASK_MAX_CHARS, family)
        wrapped = layout.wrap_auto(cleaned, max_lines=2)
        if wrapped:
            lines.append(
                f"{{\\fn{family}\\fs{size}}}" + "\\N".join(wrapped)
            )
    if not lines:
        return []
    pos = f"\\an2\\pos({FRAME_WIDTH // 2},{int(FRAME_HEIGHT - margin_v)})"
    text = "\\N".join(lines)
    return [
        _dialogue(
            3, start, end, "Ask",
            "{" + pos + "\\fad(150,250)}" + text,
        )
    ]


def _bilingual_block(
    block: Sequence[Tuple[str, int, str]],
    base_family: str = DEFAULT_FONT,
) -> str:
    """Lines as ASS text, each at its own size and face (no `\\r` reset)."""
    parts = []
    japanese_face = False
    for line, size, family in block:
        if family == JP_FONT_FAMILY:
            tags = f"\\fn{JP_FONT_FAMILY}\\fs{int(size)}"
            japanese_face = True
        elif japanese_face:
            # Switch back for the English line under a Japanese one.
            tags = f"\\fn{base_family}\\fs{int(size)}"
        else:
            tags = f"\\fs{int(size)}"
        parts.append(f"{{{tags}}}{line}")
    return "\\N".join(parts)


def build_telop_ass(
    words: Sequence[dict] = (),
    hook_text: str = "",
    hook_text_secondary: str = "",
    title: str = "",
    title_secondary: str = "",
    cta_text: str = DEFAULT_CTA_TEXT,
    cta_secondary: str = "",
    ask_text: str = "",
    ask_text_secondary: str = "",
    video_seconds: Optional[float] = None,
    font_name: Optional[str] = None,
    hook_duration: float = HOOK_SECONDS,
    cta_duration: float = CTA_SECONDS,
    layout: Optional[LineLayout] = None,
    max_lines: int = SUB_MAX_LINES,
    marked: Iterable[str] = (),
    width: int = FRAME_WIDTH,
    height: int = FRAME_HEIGHT,
    karaoke: bool = True,
) -> str:
    """The whole ASS document: hook, cards, karaoke words, title and CTA.

    ``karaoke=False`` leaves the word-by-word highlight out: a Japanese
    narration is captioned one sentence cue at a time, and highlighting the
    single cue would turn the whole line gold for its whole duration.
    """
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
    for card in (cards if karaoke else []):
        for index, word in enumerate(card["words"]):
            start = word["start"]
            end = min(word["end"], card["end"])
            if end <= start:
                continue
            events.append(
                _dialogue(1, start, end, "Telop", _card_text(card, highlight=index))
            )

    hook_end = min(hook_duration, max(total - 0.5, 0.8))
    events += _hook_event(
        hook_text, hook_end, family,
        hook_secondary=hook_text_secondary,
    )
    # A two-line title under a two-line hook would sit on it; the label can wait
    # the two seconds the hook owns.
    title_start = hook_end + 0.2 if hook_text_secondary else 0.6
    events += _title_event(
        title, max(total - 0.3, 2.0), title_layout(family),
        title_secondary=title_secondary, family=family, start=title_start,
    )

    # The closing line lands in the outro: it takes the caption band once the
    # captions have left, and always gets CTA_MIN_SECONDS of screen time - when
    # the voice runs right up to the end it starts earlier and stacks just above
    # the caption still on screen instead of drawing gold over white.
    total = max(total, 1.0)
    cta_end = max(total - CTA_TAIL_SECONDS, 1.0)
    cta_start = max(total - cta_duration, 0.6)
    cta_start = min(cta_start, cta_end - CTA_MIN_SECONDS)
    cta_start = max(min(cta_start, cta_end - 0.2), 0.2)
    captions_clear = max((card["end"] for card in cards), default=0.0)
    cta_margin = CTA_MARGIN_V if cta_start >= captions_clear - 0.05 else CTA_STACKED_MARGIN_V
    events += _cta_event(
        cta_text, cta_start, cta_end, cta_layout(family), cta_margin,
        cta_secondary=cta_secondary,
    )
    # The comment question rides above the follow line for the same stretch, so
    # the last thing on screen asks for the next idea (the comment column is
    # what turns a view into a loop).
    events += _ask_event(
        ask_text, ask_text_secondary, cta_start, cta_end, cta_margin + ASK_LIFT
    )

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


# ---------------------------------------------------------------------------
# Japanese narration without word boundaries
# ---------------------------------------------------------------------------
# edge-tts reports WordBoundary events for the Japanese voices far less
# reliably than for the English ones (a Japanese voice often answers with one
# boundary per sentence, or none at all). The captions still have to follow the
# voice, so a Japanese narration is split into short sentence cues and spread
# over the measured length of the audio track.
_JA_SENTENCE_END = "。！？!?…"
_JA_CLAUSE_END = "、，,"


def split_japanese_sentences(text: str, max_chars: int = SUB_MAX_CHARS) -> List[str]:
    """A Japanese narration as camera-ready short lines.

    Sentences end at 。！？; a sentence longer than ``max_chars`` is cut at a
    、 first and then by length, so no cue ever needs more than two lines.
    """
    body = re.sub(r"\s+", "", str(text or ""))
    if not body:
        return []
    sentences: List[str] = []
    current = ""
    for character in body:
        current += character
        if character in _JA_SENTENCE_END:
            sentences.append(current)
            current = ""
    if current:
        sentences.append(current)

    cues: List[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        while len(sentence) > max_chars:
            cut = max_chars
            for index in range(max_chars - 1, max(max_chars // 2, 4), -1):
                if sentence[index - 1] in _JA_CLAUSE_END:
                    cut = index
                    break
            cues.append(sentence[:cut])
            sentence = sentence[cut:]
        if sentence:
            cues.append(sentence)
    # A break that fell right before a full stop left a cue holding only "。":
    # it belongs to the line above, not on screen by itself.
    merged: List[str] = []
    for cue in cues:
        if merged and all(ch in _JA_SENTENCE_END + _JA_CLAUSE_END for ch in cue):
            merged[-1] = merged[-1] + cue
        else:
            merged.append(cue)
    return [cue for cue in merged if cue]


def spread_cues(
    cues: Sequence[str],
    duration: Optional[float],
    start: float = 0.35,
    tail: float = 0.25,
) -> List[dict]:
    """Timed words for cards when the voice gave no word boundaries.

    Every cue gets a share of the spoken length proportional to its character
    count, so the captions stay in step with the voice without claiming a
    precision the source does not have.
    """
    items = [clean_word(cue) for cue in cues if clean_word(cue)]
    if not items:
        return []
    try:
        total = float(duration or 0.0)
    except (TypeError, ValueError):
        total = 0.0
    if not math.isfinite(total) or total <= 0:
        total = 0.9 * len(items) + start + tail
    usable = max(total - start - tail, 0.6)
    weights = [len(item) + 2 for item in items]
    unit = usable / float(sum(weights) or 1)
    words: List[dict] = []
    cursor = start
    for item, weight in zip(items, weights):
        span = max(unit * weight, 0.45)
        words.append({"text": item, "start": round(cursor, 3), "end": round(cursor + span, 3)})
        cursor += span
    return words


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
