"""Tests for HTML → plaintext conversion."""

from __future__ import annotations

import pytest

from moodle_mcp.html_utils import html_to_plaintext


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
