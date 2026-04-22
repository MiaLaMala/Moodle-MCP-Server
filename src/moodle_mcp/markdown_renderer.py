"""Render a Moodle course as Obsidian-friendly Markdown.

Links to downloaded attachments are relative to the course root so the
generated ``.md`` file works immediately when opened in the same vault.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from .html_utils import html_to_plaintext


def _format_duedate(timestamp: Any) -> Optional[str]:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _rel_link(rel_path: Path) -> str:
    """Return an Obsidian-compatible relative link (forward slashes, url-quoted)."""
    as_posix = rel_path.as_posix()
    # Quote but keep '/' separators and a small safe set of chars.
    return quote(as_posix, safe="/-._~()")


def render_course_markdown(
    course: dict[str, Any],
    category_name: Optional[str],
    sections: list[dict[str, Any]],
    assignments_by_cmid: dict[int, dict[str, Any]],
    attachments_by_module: dict[int, list[Path]],
    course_root: Path,
    retrieved_at: Optional[datetime] = None,
) -> str:
    retrieved = retrieved_at or datetime.now(timezone.utc)
    lines: list[str] = []

    lines.append("---")
    lines.append("type: moodle-course")
    lines.append(f"course_id: {course.get('id')}")
    if course.get("shortname"):
        lines.append(f"shortname: {course.get('shortname')}")
    if category_name:
        lines.append(f"category: {category_name}")
    lines.append(f"retrieved: {retrieved.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("tags: [moodle]")
    lines.append("---")
    lines.append("")

    fullname = course.get("fullname") or course.get("shortname") or "Kurs"
    lines.append(f"# {fullname}")
    lines.append("")

    meta_bits = []
    if course.get("id") is not None:
        meta_bits.append(f"**Kurs-ID:** {course['id']}")
    if course.get("shortname"):
        meta_bits.append(f"**Shortname:** {course['shortname']}")
    if category_name:
        meta_bits.append(f"**Kategorie:** {category_name}")
    meta_bits.append(f"**Abgerufen:** {retrieved.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("  \n".join(meta_bits))
    lines.append("")

    summary = html_to_plaintext(course.get("summary") or "")
    if summary:
        lines.append(summary)
        lines.append("")

    for section in sections:
        section_name = section.get("name") or "(ohne Titel)"
        lines.append(f"## {section_name}")
        lines.append("")

        section_summary = html_to_plaintext(section.get("summary") or "")
        if section_summary:
            lines.append(section_summary)
            lines.append("")

        modules = section.get("modules") or []
        for module in modules:
            if not module.get("visible", 1):
                continue
            lines.extend(_render_module(module, assignments_by_cmid, attachments_by_module, course_root))

    return "\n".join(lines).rstrip() + "\n"


def _render_module(
    module: dict[str, Any],
    assignments_by_cmid: dict[int, dict[str, Any]],
    attachments_by_module: dict[int, list[Path]],
    course_root: Path,
) -> Iterable[str]:
    modname = module.get("modname") or "unknown"
    name = module.get("name") or "(unbenannt)"
    yield f"### {name} `[{modname}]`"
    yield ""

    cmid = module.get("id")
    description_html: Optional[str] = module.get("description")
    assign_meta: Optional[dict[str, Any]] = None
    if modname == "assign" and cmid is not None:
        assign_meta = assignments_by_cmid.get(int(cmid))
        if assign_meta:
            due = _format_duedate(assign_meta.get("duedate"))
            if due:
                yield f"**Fällig:** {due}"
                yield ""
            # Prefer the assignment intro over the course-module description
            # (it is the richer version — includes embedded images etc.)
            description_html = assign_meta.get("intro") or description_html

    description = html_to_plaintext(description_html or "")
    if description:
        yield description
        yield ""

    # URL-type modules
    if modname == "url":
        external = module.get("contents") or []
        for item in external:
            if not isinstance(item, dict):
                continue
            url = item.get("fileurl") or item.get("url")
            if url:
                yield f"**Externer Link:** {url}"
                yield ""

    # Downloaded attachments (from both core_course_get_contents and assign introfiles)
    files = attachments_by_module.get(int(cmid)) if cmid is not None else None
    if files:
        yield "**Anhänge:**"
        for path in files:
            try:
                rel = path.relative_to(course_root)
            except ValueError:
                rel = path
            yield f"- [{path.name}]({_rel_link(rel)})"
        yield ""
