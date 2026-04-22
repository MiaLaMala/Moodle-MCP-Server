"""Tests for v2.1 path sanitization + per-module layout builders."""

from __future__ import annotations

from pathlib import Path

import pytest

from moodle_mcp.paths import (
    ASSIGNMENTS_GROUP_DIR,
    ATTACHMENTS_DIR,
    COURSES_DIR,
    INFOTEXTS_GROUP_DIR,
    SUBMISSION_DIR,
    build_course_dir,
    build_module_dir,
    build_section_dir,
    classify_module_group,
    get_host_from_url,
    module_attachments_dir,
    module_submission_dir,
    sanitize_path_component,
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
    assert expected_in in sanitize_path_component(raw)


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


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("modname", ["assign"])
def test_assign_goes_to_aufgaben(modname: str) -> None:
    assert classify_module_group(modname) == ASSIGNMENTS_GROUP_DIR == "Aufgaben"


@pytest.mark.parametrize("modname", ["page", "label", "book", "resource", "url", "folder", None])
def test_non_assign_goes_to_infotexte(modname: str | None) -> None:
    assert classify_module_group(modname) == INFOTEXTS_GROUP_DIR == "Infotexte"


# ---------------------------------------------------------------- dir composition
def test_build_course_dir_uses_fullname_and_category(tmp_path: Path) -> None:
    course = {
        "id": 42,
        "shortname": "ITS-G",
        "fullname": "IT Sicherheit Grundlagen",
        "category": 7,
    }
    path = build_course_dir(
        download_root=tmp_path,
        moodle_url="https://lms.lernen.hamburg",
        category_name="Fachinformatik",
        course=course,
    )
    assert path == tmp_path / "lms.lernen.hamburg" / "Fachinformatik" / "IT Sicherheit Grundlagen"


def test_build_course_dir_falls_back_on_missing_category(tmp_path: Path) -> None:
    course = {"fullname": "X"}
    path = build_course_dir(tmp_path, "https://m.example.com", None, course)
    assert "Unkategorisiert" in path.parts


def test_build_section_dir_has_kurse_group(tmp_path: Path) -> None:
    course_dir = tmp_path / "course"
    section = build_section_dir(course_dir, "Fachenglisch", 3)
    assert section == course_dir / COURSES_DIR / "Fachenglisch"


def test_build_module_dir_routes_assign_to_aufgaben(tmp_path: Path) -> None:
    section = tmp_path / "section"
    mod = build_module_dir(section, "LoA", "assign")
    assert mod == section / "Aufgaben" / "LoA"


def test_build_module_dir_routes_page_to_infotexte(tmp_path: Path) -> None:
    section = tmp_path / "section"
    mod = build_module_dir(section, "Vokabelliste", "page")
    assert mod == section / "Infotexte" / "Vokabelliste"


def test_module_attachments_dir_is_anhaenge(tmp_path: Path) -> None:
    assert module_attachments_dir(tmp_path).name == ATTACHMENTS_DIR == "Anhänge"


def test_module_submission_dir_is_abgabe_singular(tmp_path: Path) -> None:
    assert module_submission_dir(tmp_path).name == SUBMISSION_DIR == "Abgabe"


def test_full_layout_composes_as_expected(tmp_path: Path) -> None:
    course = {"fullname": "IT25- Klassenseite", "category": 1}
    course_dir = build_course_dir(
        tmp_path, "https://lms.lernen.hamburg", "Fachinformatik", course
    )
    section_dir = build_section_dir(course_dir, "Fachenglisch", 0)
    mod_dir = build_module_dir(section_dir, "Aufgabe 1", "assign")
    expected = (
        tmp_path
        / "lms.lernen.hamburg"
        / "Fachinformatik"
        / "IT25- Klassenseite"
        / "Kurse"
        / "Fachenglisch"
        / "Aufgaben"
        / "Aufgabe 1"
    )
    assert mod_dir == expected
    assert module_submission_dir(mod_dir) == expected / "Abgabe"
    assert module_attachments_dir(mod_dir) == expected / "Anhänge"
