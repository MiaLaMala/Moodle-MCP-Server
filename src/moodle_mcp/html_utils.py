"""Convert Moodle HTML snippets to readable plaintext for LLM consumption.

Also provides helpers for two file-reference patterns Moodle uses in
HTML bodies:

- ``@@PLUGINFILE@@/<path>`` tokens that resolve to a file in a Moodle
  filearea (intro, page content, label intro, …).
- ``<img src="data:image/...;base64,...">`` inline images. Hamburg's
  Moodle frequently stores entire lesson texts as base64-encoded PNGs
  embedded directly in the assignment intro, so we need to extract
  those and treat them as attachments.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote

import html2text


logger = logging.getLogger("moodle_mcp.html_utils")


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

# Matches @@PLUGINFILE@@/<path> tokens. The path may contain percent-encoded
# UTF-8, sub-folders, and is terminated by a quote, whitespace, or closing
# bracket — anything that would end an HTML attribute value.
_PLUGINFILE_REF_RE = re.compile(r"@@PLUGINFILE@@(/[^\"'\s<>)]+)")

# Matches data:image/<mime>;base64,<DATA> payloads. The data field is
# captured loosely (base64 plus whitespace, terminated by a quote, paren,
# or angle bracket) so we can validate it via base64.b64decode below.
_DATA_URI_RE = re.compile(
    r"data:image/(?P<mime>[A-Za-z0-9+.\-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+?)(?=[\"'<>)\s])"
)

_MIME_TO_EXT = {
    "png": "png",
    "jpeg": "jpg",
    "jpg": "jpg",
    "gif": "gif",
    "webp": "webp",
    "bmp": "bmp",
    "svg+xml": "svg",
}


@dataclass(frozen=True)
class InlineImage:
    """A decoded ``<img src="data:image/...;base64,...">`` payload.

    ``digest`` is a short SHA-256 prefix of ``data`` — stable across calls
    so callers can dedup or build name → URL maps for HTML rewriting.
    """

    digest: str
    extension: str
    data: bytes

    @property
    def filename(self) -> str:
        return f"bild-{self.digest}.{self.extension}"


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


def extract_pluginfile_filenames(html: str | None) -> list[str]:
    """Return the basenames referenced by ``@@PLUGINFILE@@/...`` tokens.

    URL-decodes percent-encoded segments so the returned names can be
    matched against Moodle's ``filename`` fields. Preserves order and
    de-duplicates.
    """
    if not html:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for path in _PLUGINFILE_REF_RE.findall(html):
        basename = unquote(path.lstrip("/").rsplit("/", 1)[-1])
        if basename and basename not in seen:
            seen.add(basename)
            names.append(basename)
    return names


def rewrite_pluginfile_refs(
    html: str | None,
    replacements: dict[str, str],
) -> str:
    """Replace ``@@PLUGINFILE@@/.../<filename>`` tokens with mapped URLs.

    ``replacements`` maps a URL-decoded basename to the URL that should
    appear in the rendered output (typically a relative path to the
    locally-downloaded file). Tokens with no matching entry are left
    untouched so callers can spot unresolved references.
    """
    if not html:
        return html or ""
    if not replacements:
        return html

    def _repl(match: re.Match[str]) -> str:
        basename = unquote(match.group(1).lstrip("/").rsplit("/", 1)[-1])
        return replacements.get(basename, match.group(0))

    return _PLUGINFILE_REF_RE.sub(_repl, html)


def _decode_data_uri(mime: str, raw_b64: str) -> tuple[bytes, str] | None:
    """Decode a ``data:image/<mime>;base64,<raw>`` payload.

    Returns ``(bytes, extension)`` or ``None`` if base64 decoding fails
    (truncated/corrupt embed). Extension defaults to ``"bin"`` for unknown
    mime types so the file is still saved with *some* extension.
    """
    cleaned = re.sub(r"\s+", "", raw_b64)
    try:
        data = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        return None
    extension = _MIME_TO_EXT.get(mime.lower(), "bin")
    return data, extension


def extract_inline_base64_images(html: str | None) -> list[InlineImage]:
    """Decode every ``data:image/...;base64,...`` payload in ``html``.

    Returns one :class:`InlineImage` per occurrence in document order.
    Duplicate content yields equal ``digest`` values so callers can dedup.
    Undecodable payloads are skipped and a warning is logged.
    """
    if not html:
        return []
    results: list[InlineImage] = []
    for match in _DATA_URI_RE.finditer(html):
        decoded = _decode_data_uri(match.group("mime"), match.group("data"))
        if decoded is None:
            logger.warning(
                "Konnte data-URI Bild (mime=%s) nicht dekodieren",
                match.group("mime"),
            )
            continue
        data, ext = decoded
        digest = hashlib.sha256(data).hexdigest()[:12]
        results.append(InlineImage(digest=digest, extension=ext, data=data))
    return results


def rewrite_inline_image_srcs(
    html: str | None,
    replacements: dict[str, str],
) -> str:
    """Replace ``data:image/...;base64,...`` srcs with mapped URLs.

    ``replacements`` maps an :class:`InlineImage` ``digest`` to its target
    URL. Tokens with no matching entry (or undecodable payloads) are left
    untouched.
    """
    if not html or not replacements:
        return html or ""

    def _repl(match: re.Match[str]) -> str:
        decoded = _decode_data_uri(match.group("mime"), match.group("data"))
        if decoded is None:
            return match.group(0)
        data, _ = decoded
        digest = hashlib.sha256(data).hexdigest()[:12]
        return replacements.get(digest, match.group(0))

    return _DATA_URI_RE.sub(_repl, html)


def html_to_markdown_with_images(html: str | None) -> str:
    """Same as :func:`html_to_plaintext` but keeps ``<img>`` tags as
    ``![](src)`` embeds.

    Use after :func:`rewrite_inline_image_srcs` so the rendered markdown
    references local files rather than gigantic base64 payloads.
    """
    if not html or not html.strip():
        return ""
    converter = html2text.HTML2Text()
    converter.ignore_images = False
    converter.ignore_emphasis = False
    converter.body_width = 0
    converter.single_line_break = True
    converter.ul_item_mark = "-"
    converter.emphasis_mark = "*"
    converter.strong_mark = "**"
    converter.unicode_snob = True
    text = converter.handle(html)
    text = _TRAILING_WS_RE.sub("", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()
