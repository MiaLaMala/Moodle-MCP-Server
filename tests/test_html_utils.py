"""Tests for HTML → plaintext conversion."""

from __future__ import annotations

import pytest

import base64
import hashlib

from moodle_mcp.html_utils import (
    extract_inline_base64_images,
    extract_pluginfile_filenames,
    html_to_markdown_with_images,
    html_to_plaintext,
    rewrite_inline_image_srcs,
    rewrite_pluginfile_refs,
)


_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")
_PNG_DIGEST = hashlib.sha256(_PNG_BYTES).hexdigest()[:12]


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\n\t"])
def test_empty_input_returns_empty_string(empty: str | None) -> None:
    assert html_to_plaintext(empty) == ""


def test_paragraph_conversion() -> None:
    result = html_to_plaintext("<p>Hallo Welt</p>")
    assert result == "Hallo Welt"


def test_headings_are_preserved_as_markdown() -> None:
    result = html_to_plaintext("<h2>Lernfeld 3</h2>")
    assert "Lernfeld 3" in result
    assert "##" in result


def test_links_are_kept() -> None:
    result = html_to_plaintext('<p>siehe <a href="https://example.com/x">hier</a></p>')
    assert "hier" in result
    assert "https://example.com/x" in result


def test_unordered_list_converts_to_dash_bullets() -> None:
    html = "<ul><li>Eins</li><li>Zwei</li></ul>"
    result = html_to_plaintext(html)
    lines = [ln.strip() for ln in result.splitlines() if ln.strip()]
    assert lines == ["- Eins", "- Zwei"]


def test_html_entities_are_decoded() -> None:
    result = html_to_plaintext("<p>Caf&eacute; &amp; Bar</p>")
    assert "Café" in result
    assert "&amp;" not in result


def test_nbsp_collapses_to_whitespace() -> None:
    result = html_to_plaintext("<p>Aufgabe&nbsp;1</p>")
    assert "Aufgabe" in result
    assert "1" in result
    assert "&nbsp;" not in result


def test_script_and_style_are_dropped() -> None:
    html = "<p>Text</p><script>alert('x')</script><style>p{color:red}</style>"
    result = html_to_plaintext(html)
    assert "Text" in result
    assert "alert" not in result
    assert "color:red" not in result


def test_excessive_blank_lines_are_collapsed() -> None:
    html = "<p>Eins</p><br/><br/><br/><br/><p>Zwei</p>"
    result = html_to_plaintext(html)
    # Not more than one fully-blank line in a row.
    assert "\n\n\n" not in result
    assert "Eins" in result
    assert "Zwei" in result


# ---------------------------------------------------------------- pluginfile helpers
def test_extract_pluginfile_filenames_returns_decoded_basenames() -> None:
    html = (
        '<p>Siehe <a href="@@PLUGINFILE@@/Arbeitsprozessmatrix.pdf">PDF</a> '
        'und <a href="@@PLUGINFILE@@/sub/Lasten%20%26%20Pflichten.pdf">L&amp;P</a>.</p>'
    )
    assert extract_pluginfile_filenames(html) == [
        "Arbeitsprozessmatrix.pdf",
        "Lasten & Pflichten.pdf",
    ]


def test_extract_pluginfile_filenames_deduplicates() -> None:
    html = (
        '<a href="@@PLUGINFILE@@/foo.pdf">a</a>'
        '<a href="@@PLUGINFILE@@/foo.pdf">b</a>'
    )
    assert extract_pluginfile_filenames(html) == ["foo.pdf"]


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_extract_pluginfile_filenames_handles_empty(empty: str | None) -> None:
    assert extract_pluginfile_filenames(empty) == []


def test_rewrite_pluginfile_refs_replaces_known_basenames() -> None:
    html = '<a href="@@PLUGINFILE@@/foo.pdf">F</a><a href="@@PLUGINFILE@@/bar.pdf">B</a>'
    out = rewrite_pluginfile_refs(html, {"foo.pdf": "Anh%C3%A4nge/foo.pdf"})
    assert 'href="Anh%C3%A4nge/foo.pdf"' in out
    # Unknown ref preserved verbatim so the gap stays visible.
    assert "@@PLUGINFILE@@/bar.pdf" in out


def test_rewrite_pluginfile_refs_handles_encoded_path() -> None:
    html = '<a href="@@PLUGINFILE@@/sub/Lasten%20%26%20Pflichten.pdf">x</a>'
    out = rewrite_pluginfile_refs(html, {"Lasten & Pflichten.pdf": "local/file.pdf"})
    assert 'href="local/file.pdf"' in out


def test_rewrite_pluginfile_refs_passthrough_when_empty_map() -> None:
    html = '<a href="@@PLUGINFILE@@/foo.pdf">x</a>'
    assert rewrite_pluginfile_refs(html, {}) == html


def test_html_to_plaintext_after_rewrite_produces_working_link() -> None:
    html = '<p><a href="@@PLUGINFILE@@/Arbeitsprozessmatrix.pdf">Matrix</a></p>'
    rewritten = rewrite_pluginfile_refs(
        html, {"Arbeitsprozessmatrix.pdf": "Anh%C3%A4nge/Arbeitsprozessmatrix.pdf"}
    )
    text = html_to_plaintext(rewritten)
    assert "Matrix" in text
    assert "Anh%C3%A4nge/Arbeitsprozessmatrix.pdf" in text
    assert "@@PLUGINFILE@@" not in text


# ---------------------------------------------------------------- inline base64 images
def test_extract_inline_base64_images_decodes_payload() -> None:
    html = f'<p><img src="data:image/png;base64,{_PNG_B64}" alt="x"/></p>'
    images = extract_inline_base64_images(html)
    assert len(images) == 1
    img = images[0]
    assert img.extension == "png"
    assert img.data == _PNG_BYTES
    assert img.digest == _PNG_DIGEST
    assert img.filename == f"bild-{_PNG_DIGEST}.png"


def test_extract_inline_base64_images_preserves_order_and_duplicates() -> None:
    html = (
        f'<img src="data:image/png;base64,{_PNG_B64}"/>'
        f'<img src="data:image/png;base64,{_PNG_B64}"/>'
    )
    images = extract_inline_base64_images(html)
    # Same content → same digest; both kept (dedup is the caller's job).
    assert len(images) == 2
    assert images[0].digest == images[1].digest == _PNG_DIGEST


def test_extract_inline_base64_images_handles_jpeg_mime() -> None:
    jpg_b64 = base64.b64encode(b"\xff\xd8\xff\xe0fake").decode("ascii")
    images = extract_inline_base64_images(f'<img src="data:image/jpeg;base64,{jpg_b64}"/>')
    assert images[0].extension == "jpg"


def test_extract_inline_base64_images_skips_corrupt(caplog) -> None:
    # "abc" is base64-shaped (passes the regex character class) but has a
    # length that isn't a multiple of 4, so b64decode(validate=True) fails.
    html = '<img src="data:image/png;base64,abc"/>'
    with caplog.at_level("WARNING", logger="moodle_mcp.html_utils"):
        assert extract_inline_base64_images(html) == []
    assert any("data-URI" in rec.message for rec in caplog.records)


@pytest.mark.parametrize("empty", [None, "", "<p>no images</p>"])
def test_extract_inline_base64_images_handles_empty(empty: str | None) -> None:
    assert extract_inline_base64_images(empty) == []


def test_rewrite_inline_image_srcs_replaces_known_digest() -> None:
    html = f'<img src="data:image/png;base64,{_PNG_B64}" alt="t"/>'
    out = rewrite_inline_image_srcs(html, {_PNG_DIGEST: "Anh%C3%A4nge/bild.png"})
    assert "data:image/png;base64" not in out
    assert 'src="Anh%C3%A4nge/bild.png"' in out
    # Surrounding markup preserved.
    assert 'alt="t"' in out


def test_rewrite_inline_image_srcs_leaves_unknown_digest() -> None:
    html = f'<img src="data:image/png;base64,{_PNG_B64}"/>'
    out = rewrite_inline_image_srcs(html, {"otherdigest12": "x"})
    assert out == html


def test_html_to_markdown_with_images_emits_image_embeds() -> None:
    html = '<p>Vor</p><img src="Anhänge/bild.png" alt="A"/><p>Nach</p>'
    out = html_to_markdown_with_images(html)
    assert "Vor" in out and "Nach" in out
    # html2text emits ![alt](src)
    assert "![A](Anhänge/bild.png)" in out


def test_full_pipeline_inline_image_becomes_local_reference() -> None:
    """End-to-end: data-URI in → local relative path in rendered markdown."""
    html = (
        f'<h3>Aufgabe 1</h3>'
        f'<img src="data:image/png;base64,{_PNG_B64}" alt=""/>'
    )
    rewritten = rewrite_inline_image_srcs(
        html, {_PNG_DIGEST: f"Anh%C3%A4nge/bild-{_PNG_DIGEST}.png"}
    )
    out = html_to_markdown_with_images(rewritten)
    assert "Aufgabe 1" in out
    assert f"Anh%C3%A4nge/bild-{_PNG_DIGEST}.png" in out
    assert "data:image/png" not in out
