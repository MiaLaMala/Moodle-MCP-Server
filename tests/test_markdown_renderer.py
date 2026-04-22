"""Tests for the 3-level markdown renderers (v2.1 layout)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from moodle_mcp.markdown_renderer import (
    render_course_overview,
    render_module,
    render_section_overview,
)


FIXED_TIME = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- course overview
def test_course_overview_has_frontmatter_and_section_links(tmp_path: Path) -> None:
    course = {
        "id": 224100,
        "shortname": "ITS-G",
        "fullname": "IT Sicherheit Grundlagen",
        "summary": "<p>Intro</p>",
    }
    kurs_md = tmp_path / "Kurs.md"
    section_md = tmp_path / "Kurse" / "Fachenglisch" / "Section.md"
    out = render_course_overview(
        course=course,
        category_name="Fachinformatik",
        sections_with_paths=[({"name": "Fachenglisch"}, section_md)],
        kurs_md_path=kurs_md,
        retrieved_at=FIXED_TIME,
    )
    assert out.startswith("---\n")
    assert "type: moodle-course" in out
    assert "category: Fachinformatik" in out
    assert "# IT Sicherheit Grundlagen" in out
    # Relative link from Kurs.md down into Kurse/Fachenglisch/Section.md
    assert "[Fachenglisch](Kurse/Fachenglisch/Section.md)" in out


# ---------------------------------------------------------------- section overview
def test_section_overview_groups_aufgaben_and_infotexte(tmp_path: Path) -> None:
    course = {"id": 1}
    section = {"name": "Fachenglisch", "summary": "<p>Willkommen</p>"}
    section_md = tmp_path / "Kurse" / "Fachenglisch" / "Section.md"

    assign_module = {"id": 10, "modname": "assign", "name": "Letter of Application"}
    assign_meta = {"cmid": 10, "duedate": 1740000000}
    assign_md = section_md.parent / "Aufgaben" / "Letter of Application" / "Letter of Application.md"

    info_module = {"id": 11, "modname": "page", "name": "Vokabelliste"}
    info_md = section_md.parent / "Infotexte" / "Vokabelliste" / "Vokabelliste.md"

    out = render_section_overview(
        course=course,
        section=section,
        section_index=0,
        modules_with_paths=[
            (assign_module, assign_meta, assign_md),
            (info_module, None, info_md),
        ],
        section_md_path=section_md,
    )

    assert "## Aufgaben" in out
    assert "## Infotexte" in out
    assert "[Letter of Application](Aufgaben/" in out
    assert "fällig" in out  # duedate formatted
    assert "[Vokabelliste](Infotexte/" in out
    assert "`[page]`" in out


def test_section_overview_skips_empty_groups(tmp_path: Path) -> None:
    section_md = tmp_path / "Section.md"
    info = {"id": 1, "modname": "page", "name": "I"}
    info_md = tmp_path / "Infotexte" / "I" / "I.md"
    out = render_section_overview(
        course={},
        section={"name": "S"},
        section_index=0,
        modules_with_paths=[(info, None, info_md)],
        section_md_path=section_md,
    )
    assert "## Infotexte" in out
    assert "## Aufgaben" not in out


# ---------------------------------------------------------------- module
def test_module_assign_has_duedate_and_abgabe_section(tmp_path: Path) -> None:
    module = {"id": 10, "modname": "assign", "name": "Aufgabe 1", "description": "<p>Do it.</p>"}
    assign_meta = {"id": 555, "cmid": 10, "duedate": 1740000000, "intro": "<p>Aufgabentext</p>"}
    md_path = tmp_path / "Aufgabe 1.md"
    out = render_module(
        course={"id": 1},
        section={"name": "Fachenglisch"},
        module=module,
        assign_meta=assign_meta,
        module_md_path=md_path,
        attachment_paths=[],
    )
    assert "type: moodle-module" in out
    assert "assign_id: 555" in out
    assert "**Fällig:**" in out
    assert "Aufgabentext" in out
    assert "## Abgabe" in out


def test_module_info_has_no_abgabe_section(tmp_path: Path) -> None:
    module = {"id": 11, "modname": "page", "name": "Vokabel", "description": "<p>List</p>"}
    md_path = tmp_path / "Vokabel.md"
    out = render_module(
        course={"id": 1},
        section={"name": "S"},
        module=module,
        assign_meta=None,
        module_md_path=md_path,
        attachment_paths=[],
    )
    assert "## Abgabe" not in out
    assert "List" in out


def test_module_attachment_links_are_relative_to_module_md(tmp_path: Path) -> None:
    module = {"id": 1, "modname": "resource", "name": "Skript"}
    md_path = tmp_path / "Skript.md"
    attachment = tmp_path / "Anhänge" / "skript.pdf"
    out = render_module(
        course={}, section={}, module=module, assign_meta=None,
        module_md_path=md_path, attachment_paths=[attachment],
    )
    assert "## Anhänge" in out
    # `Anhänge` gets URL-encoded in links
    assert "Anh%C3%A4nge/skript.pdf" in out
    assert "[skript.pdf](" in out
