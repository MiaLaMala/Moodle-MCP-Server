"""Tests for the Obsidian-friendly course markdown renderer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from moodle_mcp.markdown_renderer import render_course_markdown


FIXED_TIME = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)


def _render(**overrides):
    course = overrides.get("course", {
        "id": 224100,
        "shortname": "ITS-G",
        "fullname": "IT Sicherheit Grundlagen",
        "summary": "<p>Einführung.</p>",
    })
    sections = overrides.get("sections", [])
    return render_course_markdown(
        course=course,
        category_name=overrides.get("category_name", "Lernfeld 3"),
        sections=sections,
        assignments_by_cmid=overrides.get("assignments_by_cmid", {}),
        attachments_by_module=overrides.get("attachments_by_module", {}),
        course_root=overrides.get("course_root", Path("/tmp/course")),
        retrieved_at=FIXED_TIME,
    )


def test_renders_yaml_frontmatter_for_obsidian() -> None:
    md = _render()
    assert md.startswith("---\n")
    assert "type: moodle-course" in md
    assert "course_id: 224100" in md
    assert "category: Lernfeld 3" in md
    assert "tags: [moodle]" in md


def test_renders_title_and_metadata() -> None:
    md = _render()
    assert "# IT Sicherheit Grundlagen" in md
    assert "**Kurs-ID:** 224100" in md
    assert "**Kategorie:** Lernfeld 3" in md
    assert "**Shortname:** ITS-G" in md


def test_section_and_module_headers() -> None:
    sections = [{
        "name": "Allgemein",
        "summary": "<p>Hallo</p>",
        "modules": [
            {"id": 1, "name": "Aufgabe 1", "modname": "assign", "visible": 1,
             "description": "<p>Bearbeite …</p>"},
            {"id": 2, "name": "Info-Seite", "modname": "page", "visible": 1,
             "description": "<p>Text.</p>"},
        ],
    }]
    md = _render(sections=sections)
    assert "## Allgemein" in md
    assert "### Aufgabe 1 `[assign]`" in md
    assert "### Info-Seite `[page]`" in md


def test_assignment_due_date_rendered() -> None:
    sections = [{
        "name": "S1",
        "modules": [{"id": 99, "name": "A", "modname": "assign", "visible": 1}],
    }]
    assignments = {99: {"cmid": 99, "duedate": 1740000000, "intro": "<p>X</p>"}}
    md = _render(sections=sections, assignments_by_cmid=assignments)
    assert "**Fällig:**" in md
    assert "2025" in md  # 1740000000 is in 2025


def test_attachment_links_are_relative_and_url_safe() -> None:
    course_root = Path("/tmp/course")
    sections = [{
        "name": "S1",
        "modules": [{"id": 7, "name": "Modul", "modname": "resource", "visible": 1}],
    }]
    attachments = {7: [course_root / "Anhänge" / "01 - S1" / "datei mit space.pdf"]}
    md = _render(sections=sections, attachments_by_module=attachments, course_root=course_root)

    # Link is relative (no leading /) and uses forward slashes.
    assert "Anh%C3%A4nge/01%20-%20S1/datei%20mit%20space.pdf" in md
    assert "[datei mit space.pdf](" in md
    # No absolute paths leaked.
    assert "/tmp/course" not in md


def test_invisible_modules_are_skipped() -> None:
    sections = [{
        "name": "S",
        "modules": [
            {"id": 1, "name": "sichtbar", "modname": "page", "visible": 1},
            {"id": 2, "name": "versteckt", "modname": "page", "visible": 0},
        ],
    }]
    md = _render(sections=sections)
    assert "sichtbar" in md
    assert "versteckt" not in md
