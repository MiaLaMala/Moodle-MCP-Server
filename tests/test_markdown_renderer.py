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


def test_module_assign_pluginfile_tokens_rewritten_to_local_links(tmp_path: Path) -> None:
    intro = (
        '<p>Lies <a href="@@PLUGINFILE@@/Arbeitsprozessmatrix.pdf">Matrix</a> '
        'und beantworte.</p>'
    )
    module = {"id": 10, "modname": "assign", "name": "Aufgabe"}
    assign_meta = {"id": 555, "intro": intro}
    md_path = tmp_path / "Aufgabe.md"
    attachment = tmp_path / "Anhänge" / "Arbeitsprozessmatrix.pdf"
    out = render_module(
        course={"id": 1}, section={"name": "S"}, module=module,
        assign_meta=assign_meta, module_md_path=md_path,
        attachment_paths=[attachment],
    )
    assert "Matrix" in out
    assert "@@PLUGINFILE@@" not in out
    assert "Anh%C3%A4nge/Arbeitsprozessmatrix.pdf" in out


def test_module_assign_inline_base64_images_become_relative_embeds(tmp_path: Path) -> None:
    """When the intro contains ``<img src="data:image/png;base64,...">``
    payloads (Hamburg's lesson-text-as-image pattern), the renderer must
    output ``![](Anhänge/...)`` markdown embeds, not the raw base64."""
    import base64
    import hashlib

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
        "426082"
    )
    digest = hashlib.sha256(png).hexdigest()[:12]
    b64 = base64.b64encode(png).decode("ascii")
    intro = f'<h3>Aufgabe 1: Lesen</h3><img src="data:image/png;base64,{b64}"/>'

    module = {"id": 10, "modname": "assign", "name": "A"}
    assign_meta = {"id": 1, "intro": intro}
    md_path = tmp_path / "A.md"
    image_path = tmp_path / "Anhänge" / f"bild-{digest}.png"

    out = render_module(
        course={"id": 1}, section={"name": "S"}, module=module,
        assign_meta=assign_meta, module_md_path=md_path,
        attachment_paths=[image_path],
        inline_image_map={digest: image_path},
    )
    assert "Aufgabe 1: Lesen" in out
    assert "data:image/png;base64" not in out
    assert f"Anh%C3%A4nge/bild-{digest}.png" in out


def test_module_page_renders_inline_content_with_pluginfile_rewrite(tmp_path: Path) -> None:
    content_html = '<p>Body <a href="@@PLUGINFILE@@/handout.pdf">PDF</a></p>'
    module = {
        "id": 20,
        "modname": "page",
        "name": "Skript",
        "contents": [{"type": "content", "content": content_html}],
    }
    md_path = tmp_path / "Skript.md"
    attachment = tmp_path / "Anhänge" / "handout.pdf"
    out = render_module(
        course={}, section={}, module=module, assign_meta=None,
        module_md_path=md_path, attachment_paths=[attachment],
    )
    assert "Body" in out
    assert "Anh%C3%A4nge/handout.pdf" in out
    assert "@@PLUGINFILE@@" not in out


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
