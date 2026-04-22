"""Tests for path sanitization and course dir composition."""

from __future__ import annotations

from pathlib import Path

import pytest

from moodle_mcp.paths import (
    ATTACHMENTS_DIR,
    SUBMISSIONS_DIR,
    attachments_subdir,
    build_course_dir,
    get_host_from_url,
    sanitize_path_component,
    submissions_dir,
)


@pytest.mark.parametrize(
    "raw,expected_in",
    [
        ("Lernfeld 3", "Lernfeld 3"),
        ("Datei mit ä/ö/ü", "Datei mit ä"),  # umlauts preserved, slash replaced
        ("con:tains<illegal>?chars", "con_tains_illegal__chars"),
        ("  with   spaces  ", "with spaces"),
        ("trailing dot.", "trailing dot"),
        ("...nothing....", "nothing"),
    ],
)
def test_sanitize_keeps_meaningful_content(raw: str, expected_in: str) -> None:
    cleaned = sanitize_path_component(raw)
    assert expected_in in cleaned


def test_sanitize_empty_gives_fallback() -> None:
    assert sanitize_path_component("") == "unbenannt"
    assert sanitize_path_component(None) == "unbenannt"
    assert sanitize_path_component("    ") == "unbenannt"


def test_sanitize_truncates_long_names() -> None:
    result = sanitize_path_component("x" * 500)
    assert 0 < len(result) <= 120


def test_sanitize_strips_control_chars() -> None:
    result = sanitize_path_component("name\x00with\x1fnulls")
    assert "\x00" not in result and "\x1f" not in result


def test_get_host_from_url_strips_scheme_and_path() -> None:
    assert get_host_from_url("https://lms.lernen.hamburg/some/path") == "lms.lernen.hamburg"
    assert get_host_from_url("http://moodle.example.com:8080") == "moodle.example.com"


def test_build_course_dir_composes_full_path(tmp_path: Path) -> None:
    course = {"id": 42, "shortname": "ITS-Grundlagen", "fullname": "IT Sicherheit"}
    path = build_course_dir(
        download_root=tmp_path,
        moodle_url="https://lms.lernen.hamburg",
        category_name="Lernfeld 3",
        course=course,
    )
    assert path == tmp_path / "lms.lernen.hamburg" / "Lernfeld 3" / "ITS-Grundlagen"


def test_build_course_dir_falls_back_on_missing_category(tmp_path: Path) -> None:
    course = {"shortname": "X"}
    path = build_course_dir(tmp_path, "https://m.example.com", None, course)
    assert "Unkategorisiert" in path.parts


def test_attachments_subdir_prefixes_with_index(tmp_path: Path) -> None:
    course_dir = tmp_path / "course"
    result = attachments_subdir(course_dir, "Allgemein", 0)
    assert result.name == "00 - Allgemein"
    assert result.parent.name == ATTACHMENTS_DIR


def test_submissions_dir_is_abgaben(tmp_path: Path) -> None:
    course_dir = tmp_path / "course"
    assert submissions_dir(course_dir).name == SUBMISSIONS_DIR == "Abgaben"
