"""Convert Moodle HTML snippets to readable plaintext for LLM consumption."""

from __future__ import annotations

import re

import html2text


_CONVERTER = html2text.HTML2Text()
_CONVERTER.ignore_images = True
_CONVERTER.ignore_emphasis = False
_CONVERTER.body_width = 0  # do not hard-wrap
_CONVERTER.single_line_break = True
_CONVERTER.ul_item_mark = "-"
_CONVERTER.emphasis_mark = "*"
_CONVERTER.strong_mark = "**"
_CONVERTER.unicode_snob = True  # keep é, ä, ß instead of ASCII-folding


_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def html_to_plaintext(html: str | None) -> str:
    """Convert a Moodle-style HTML fragment to a clean markdown-ish plaintext.

    Returns an empty string for ``None`` or whitespace-only input.
    """
    if not html or not html.strip():
        return ""

    text = _CONVERTER.handle(html)
    text = _TRAILING_WS_RE.sub("", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()
